"""Layer 5: AI-integrated large language model capability.

An operational-intelligence assistant that answers business questions in
plain English. Claude has two tools:

  run_sql                 Read-only SQL over the unified reporting dataset (Layer 2).
                          Every query goes through governance.guard_sql first.
  search_knowledge_base   Retrieval over company policies and playbooks (RAG).

Claude decides which tools to call, combines the data with policy context and
answers with the figures and the policy it relied on. Every tool call is
audited.

Without an ANTHROPIC_API_KEY the assistant runs in an offline demo mode. A
small set of intent templates stands in for the model, so the platform can
still be evaluated end to end.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import duckdb

from .governance import AuditLog, UnsafeQuery, guard_sql, redact
from .retrieval import BM25Index, load_chunks
from .warehouse import SCHEMA_DOC

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = f"""You are the operational-intelligence assistant for Northbridge Comms Ltd, an SME IT services and
telecoms provider (connectivity, hosted VoIP, cloud hosting, managed IT support, data centre colocation).
Your users are managers and account managers who are not SQL specialists.

You can query the unified reporting dataset (DuckDB SQL, read-only) with run_sql:
{SCHEMA_DOC}

You can search company policies, SLAs and playbooks with search_knowledge_base.

How to work:
- Base every figure on a run_sql result. Never estimate numbers.
- When a question touches what the company should do (escalation, credit control, service credits, reviews),
  search the knowledge base and follow the policy, naming the document you used.
- Keep answers short and decision-focused: the answer first, then the key figures (use £ and round sensibly),
  then a recommended next action where one is appropriate.
- If the data cannot answer the question, say so plainly rather than guessing."""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only DuckDB SELECT query against the unified reporting dataset and return up to "
                       "50 rows as JSON. Only the tables in the schema are available.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement."},
                "purpose": {"type": "string", "description": "One line on what this query is for."},
            },
            "required": ["sql", "purpose"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "search_knowledge_base",
        "description": "Search company policies, SLAs, credit-control procedures, playbooks and the product "
                       "catalogue. Returns the most relevant passages with their source document.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class Step:
    tool: str
    input: dict
    output: str


@dataclass
class Answer:
    text: str
    steps: list[Step] = field(default_factory=list)
    mode: str = "claude"


class Assistant:
    def __init__(self, con: duckdb.DuckDBPyConnection, audit: AuditLog | None = None,
                 api_key: str | None = None):
        self.con = con
        self.audit = audit or AuditLog()
        self.index = BM25Index(load_chunks())
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    # ---- tools -------------------------------------------------------------
    def run_sql(self, sql: str, purpose: str = "") -> str:
        try:
            safe = guard_sql(sql)
        except UnsafeQuery as e:
            self.audit.record("llm", "sql_blocked", {"sql": sql, "reason": str(e)})
            return json.dumps({"error": f"Query blocked by governance guard: {e}"})
        try:
            df = self.con.execute(safe).df()
        except duckdb.Error as e:
            return json.dumps({"error": f"SQL error: {e}"})
        self.audit.record("llm", "sql_run", {"purpose": purpose, "sql": sql, "rows": len(df)})
        return df.head(50).to_json(orient="records", date_format="iso", double_precision=2)

    def search_knowledge_base(self, query: str) -> str:
        hits = self.index.search(query, k=3)
        self.audit.record("llm", "kb_search", {"query": query, "hits": [h[0].heading for h in hits]})
        return json.dumps([{"document": c.doc, "section": c.heading, "text": c.text} for c, _ in hits])

    def _call_tool(self, name: str, args: dict) -> str:
        if name == "run_sql":
            return self.run_sql(args.get("sql", ""), args.get("purpose", ""))
        if name == "search_knowledge_base":
            return self.search_knowledge_base(args.get("query", ""))
        return json.dumps({"error": f"Unknown tool {name}"})

    # ---- question answering ------------------------------------------------
    def ask(self, question: str, max_turns: int = 8) -> Answer:
        question = redact(question)
        self.audit.record("llm", "question", {"question": question, "mode": "claude" if self.online else "offline"})
        if not self.online:
            return self._offline(question)

        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        messages: list[dict] = [{"role": "user", "content": question}]
        steps: list[Step] = []
        for _ in range(max_turns):
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=messages,
            )
            if response.stop_reason == "refusal":
                return Answer("The model declined to answer this request.", steps)
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                self.audit.record("llm", "answer", {"chars": len(text), "tool_calls": len(steps)})
                return Answer(text or "(no answer returned)", steps)
            results = []
            for tu in tool_uses:
                out = self._call_tool(tu.name, tu.input)
                steps.append(Step(tu.name, dict(tu.input), out))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out,
                                "is_error": out.startswith('{"error"')})
            messages.append({"role": "user", "content": results})
        return Answer("Stopped after the maximum number of reasoning steps.", steps)

    # ---- offline demo mode -------------------------------------------------
    OFFLINE_INTENTS = [
        (("risk", "churn", "at-risk", "at risk"),
         "Accounts most at risk, by revenue:",
         "SELECT company_name, account_manager, ROUND(health_score) AS health, ROUND(monthly_revenue_gbp) AS mrr_gbp "
         "FROM customer_360 WHERE health_score < 40 ORDER BY monthly_revenue_gbp DESC LIMIT 10",
         "high-risk accounts health score service review call"),
        (("overdue", "debt", "unpaid", "owe"),
         "Customers with invoices more than 45 days overdue:",
         "SELECT company_name, account_manager, ROUND(overdue_gbp) AS overdue_gbp FROM customer_360 "
         "WHERE overdue_gbp > 0 ORDER BY overdue_gbp DESC LIMIT 10",
         "credit control overdue"),
        (("sla", "breach", "ticket", "support"),
         "Ticket categories by SLA breach rate:",
         "SELECT category, COUNT(*) AS tickets, ROUND(100.0*AVG(CASE WHEN sla_breached THEN 1 ELSE 0 END),1) "
         "AS breach_pct FROM fact_ticket GROUP BY 1 ORDER BY breach_pct DESC",
         "SLA service credit"),
        (("revenue", "mrr", "product", "sales"),
         "Monthly recurring revenue by product line (latest month):",
         "SELECT product, ROUND(SUM(amount_gbp)) AS mrr_gbp FROM fact_invoice "
         "WHERE issued_date = (SELECT MAX(issued_date) FROM fact_invoice) GROUP BY 1 ORDER BY 2 DESC",
         "product catalogue"),
        (("manager", "portfolio"),
         "Account manager portfolios:",
         "SELECT account_manager, COUNT(*) AS accounts, ROUND(SUM(monthly_revenue_gbp)) AS mrr_gbp, "
         "SUM(CASE WHEN health_score < 40 THEN 1 ELSE 0 END) AS at_risk FROM customer_360 GROUP BY 1 ORDER BY 3 DESC",
         "service review"),
    ]

    def _offline(self, question: str) -> Answer:
        q = question.lower()
        steps: list[Step] = []
        parts = ["_Offline demo mode: no API key set, so a template matched your question instead of Claude._"]
        intent = next((i for i in self.OFFLINE_INTENTS if any(k in q for k in i[0])), None)
        if intent:
            _, heading, sql, kb_query = intent
            out = self.run_sql(sql, "offline template")
            steps.append(Step("run_sql", {"sql": sql}, out))
            rows = json.loads(out)
            parts.append(f"**{heading}**")
            if rows:
                cols = list(rows[0])
                table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
                fmt = lambda v: f"{v:,.0f}" if isinstance(v, float) and v.is_integer() else str(v)
                table += ["| " + " | ".join(fmt(r[c]) for c in cols) + " |" for r in rows[:10]]
                parts.append("\n".join(table))
        else:
            kb_query = question
        kb = self.search_knowledge_base(kb_query)
        steps.append(Step("search_knowledge_base", {"query": kb_query}, kb))
        top = json.loads(kb)
        if top:
            parts.append(f"**Relevant policy** ({top[0]['document']} → {top[0]['section']}):\n\n"
                         f"> {top[0]['text'][:500]}")
        return Answer("\n\n".join(parts), steps, mode="offline")
