import json
from pathlib import Path

import pytest

from data.generate_synthetic import generate
from dsp import analytics
from dsp.automation import run_rules
from dsp.governance import AuditLog, UnsafeQuery, guard_sql, redact
from dsp.ingest import canonical_customer_id, ingest
from dsp.llm import Assistant
from dsp.retrieval import BM25Index, load_chunks
from dsp.warehouse import build


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dsp")
    raw = tmp / "raw"
    generate(raw)
    result = ingest(raw)
    audit = AuditLog(tmp / "audit.jsonl")
    con = build(result, ":memory:")
    return con, result, audit


# ---- Layer 1 -------------------------------------------------------------------
@pytest.mark.parametrize("raw,pattern", [
    ("C-0007", r"^C-(\d+)$"), ("CUST0007", r"^CUST(\d+)$"), ("NB/0007", r"^NB/(\d+)$"), (7, r"^(\d+)$")])
def test_keys_from_every_system_resolve_to_one_customer(raw, pattern):
    assert canonical_customer_id(raw, pattern) == "C-0007"


def test_dirty_records_are_reported_and_quarantined(platform):
    _, result, _ = platform
    report = result.quality_report()
    assert {"could not parse as currency", "unrecognised customer key"} <= set(report.problem)
    assert report.problem.str.contains("no matching CRM account").any()
    assert "C-0999" not in set(result.frames["invoice"].customer_id)


def test_types_and_casing_are_normalised(platform):
    _, result, _ = platform
    inv = result.frames["invoice"]
    assert inv.amount_gbp.dropna().between(1, 10000).all()
    assert all(s == s.title() for s in result.frames["customer"].sector)


# ---- Layer 2 -------------------------------------------------------------------
def test_pii_never_reaches_reporting_layer(platform):
    con, _, _ = platform
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name IN "
        "('dim_customer','fact_ticket','customer_360')").fetchall()}
    assert "contact_email" not in cols and "summary" not in cols


def test_customer_360_has_one_row_per_customer_and_bounded_health(platform):
    con, result, _ = platform
    n, distinct, lo, hi = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT customer_id), MIN(health_score), MAX(health_score) FROM customer_360"
    ).fetchone()
    assert n == distinct == len(result.frames["customer"])
    assert 0 <= lo <= hi <= 100


# ---- Layer 3 -------------------------------------------------------------------
def test_kpis_compute(platform):
    con, _, _ = platform
    k = analytics.kpis(con)
    assert k["monthly_recurring_revenue"][1] > 0
    assert 0 <= k["sla_compliance"][1] <= 100
    for name in analytics.VIEWS:
        assert len(analytics.view(con, name)) > 0


# ---- Layer 4 -------------------------------------------------------------------
def test_rules_raise_actions_idempotently(platform):
    con, _, audit = platform
    first = run_rules(con, audit=audit)
    second = run_rules(con, audit=audit)
    assert len(first) > 0 and len(first) == len(second)
    assert set(first.rule_name) >= {"Churn-risk account review", "Overdue invoices"}


# ---- Governance ----------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "DROP TABLE dim_customer",
    "SELECT 1; DELETE FROM fact_invoice",
    "SELECT * FROM read_csv('secrets.csv')",
    "SELECT * FROM workflow_actions",
    "COPY dim_customer TO 'out.csv'",
])
def test_guard_blocks_unsafe_sql(sql):
    with pytest.raises(UnsafeQuery):
        guard_sql(sql)


def test_guard_allows_read_only_select_with_cte():
    sql = guard_sql("WITH x AS (SELECT * FROM customer_360) SELECT company_name FROM x")
    assert sql.endswith("LIMIT 500")


def test_redaction():
    assert redact("email jo@acme.co.uk or call 07700 900123") == "email [EMAIL] or call [PHONE]"


# ---- Layer 5 -------------------------------------------------------------------
def test_retrieval_finds_the_right_policy():
    index = BM25Index(load_chunks())
    top = index.search("service credit for repeated SLA breaches", k=1)[0][0]
    assert top.doc == "Service Level Agreement Policy" and top.heading == "Service credits"


def test_assistant_offline_mode_answers_with_data_and_policy(platform):
    con, _, audit = platform
    ans = Assistant(con, audit, api_key="").ask("Which accounts are at risk?")
    assert ans.mode == "offline"
    assert [s.tool for s in ans.steps] == ["run_sql", "search_knowledge_base"]
    assert json.loads(ans.steps[0].output)


def test_assistant_blocks_unsafe_sql_from_model(platform):
    con, _, audit = platform
    out = json.loads(Assistant(con, audit, api_key="").run_sql("DELETE FROM fact_invoice"))
    assert "blocked" in out["error"]
    events = [e["event"] for e in audit.tail(5)]
    assert "sql_blocked" in events


def test_assistant_agent_loop_with_mocked_claude(platform, monkeypatch):
    """Simulates Claude calling run_sql, then answering. Checks the tool loop and request shape."""
    from types import SimpleNamespace as NS

    import anthropic

    con, _, audit = platform
    calls = []
    script = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="tu_1", name="run_sql", input={
            "sql": "SELECT COUNT(*) AS n FROM customer_360 WHERE health_score < 40", "purpose": "count at-risk"})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="There are N at-risk accounts.")]),
    ]

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return script[len(calls) - 1]

    class FakeClient:
        def __init__(self, api_key=None):
            self.beta = NS(messages=FakeMessages())

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    ans = Assistant(con, audit, api_key="test-key").ask("How many accounts are at risk?")
    assert ans.text == "There are N at-risk accounts."
    assert [s.tool for s in ans.steps] == ["run_sql"]
    assert json.loads(ans.steps[0].output)[0]["n"] >= 0
    assert calls[0]["model"] == "claude-opus-5-5"
    tool_result = next(m for m in calls[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list))["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu_1"
