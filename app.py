"""Streamlit front end for the five-layer decision-support platform.

    streamlit run app.py
"""
from __future__ import annotations

import json
import os

import pandas as pd
import plotly.express as px
import streamlit as st

from dsp import analytics
from dsp.governance import AuditLog
from dsp.llm import Assistant
from dsp.pipeline import run

st.set_page_config(page_title="AI Decision-Support Platform", page_icon="📊", layout="wide")


@st.cache_resource
def platform():
    audit = AuditLog()
    con, result, actions = run(db_path=":memory:", audit=audit)
    return con, result, audit


con, result, audit = platform()

st.title("AI-enabled decision-support platform")
st.caption("Fictional SME IT services & telecoms provider · CRM, billing, service desk and network data "
           "integrated into one governed dataset with automation and an LLM assistant on top.")

tabs = st.tabs(["📈 Performance", "🤖 Ask the data", "⚙️ Workflow actions", "🧩 Data integration", "🛡️ Governance"])

# ---- Layer 3 -----------------------------------------------------------------
with tabs[0]:
    k = analytics.kpis(con)
    cols = st.columns(len(k))
    for col, (label, value) in zip(cols, k.values()):
        shown = f"£{value:,.0f}" if "£" in label else (f"{value:,.1f}" if isinstance(value, float) else f"{value:,}")
        col.metric(label.replace(" (£)", "").replace(" (£ / month)", " / month"), shown)

    left, right = st.columns(2)
    rev = analytics.view(con, "revenue_trend")
    left.plotly_chart(px.area(rev, x="month", y="revenue_gbp", color="product",
                              title="Monthly revenue by product line (£)"), width="stretch")
    tix = analytics.view(con, "tickets_by_category")
    right.plotly_chart(px.bar(tix, x="category", y="tickets", color="breach_rate_pct",
                              color_continuous_scale="Reds", title="Tickets by category (colour = SLA breach %)"),
                       width="stretch")
    left, right = st.columns(2)
    left.subheader("Health by sector")
    left.dataframe(analytics.view(con, "health_by_sector"), hide_index=True, width="stretch")
    right.subheader("Account manager portfolios")
    right.dataframe(analytics.view(con, "account_manager_portfolio"), hide_index=True, width="stretch")
    st.subheader("Accounts at risk (health < 40)")
    st.dataframe(analytics.view(con, "at_risk_accounts"), hide_index=True, width="stretch")

# ---- Layer 5 -----------------------------------------------------------------
with tabs[1]:
    key = st.text_input("Anthropic API key (optional; leave blank for offline demo mode)", type="password",
                        value=os.environ.get("ANTHROPIC_API_KEY", ""))
    assistant = Assistant(con, audit, api_key=key or None)
    st.caption("Mode: **Claude (tool use + RAG)**" if assistant.online
               else "Mode: **offline demo** (intent templates). Add a key to let Claude plan its own queries.")
    examples = ["Which high-value accounts are at risk, and what should we do this week?",
                "Who owes us money and should anyone go on credit hold?",
                "Which customers are owed a service credit under the SLA policy?",
                "How is revenue split across product lines?"]
    q = st.selectbox("Example questions", [""] + examples)
    question = st.text_area("Ask a business question", value=q, height=80)
    if st.button("Ask", type="primary") and question.strip():
        with st.spinner("Thinking…"):
            ans = assistant.ask(question)
        st.markdown(ans.text)
        with st.expander(f"How this answer was produced ({len(ans.steps)} tool calls)"):
            for s in ans.steps:
                st.markdown(f"**{s.tool}**")
                if s.tool == "run_sql":
                    st.code(s.input.get("sql", ""), language="sql")
                    try:
                        st.dataframe(pd.DataFrame(json.loads(s.output)), hide_index=True)
                    except ValueError:
                        st.write(s.output)
                else:
                    st.write(f"Query: {s.input.get('query')}")
                    st.json(json.loads(s.output))

# ---- Layer 4 -----------------------------------------------------------------
with tabs[2]:
    actions = con.execute("""SELECT rule_name, priority, owner, company_name, reason, action, raised_at
                             FROM workflow_actions WHERE status='open'
                             ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END""").df()
    c1, c2, c3 = st.columns(3)
    c1.metric("Open actions", len(actions))
    c2.metric("High priority", int((actions.priority == "high").sum()))
    c3.metric("Owners", actions.owner.nunique())
    owner = st.selectbox("Filter by owner", ["All"] + sorted(actions.owner.unique()))
    st.dataframe(actions if owner == "All" else actions[actions.owner == owner],
                 hide_index=True, width="stretch")
    st.caption("Rules live in config/automation_rules.yaml as plain SQL. Operations staff can add or change one "
               "without a code release, and re-running the pipeline never duplicates an open action.")

# ---- Layers 1 & 2 ------------------------------------------------------------
with tabs[3]:
    st.subheader("Source lineage")
    st.dataframe(pd.DataFrame(result.lineage).T, width="stretch")
    st.subheader("Data-quality issues (quarantined, not silently dropped)")
    st.dataframe(result.quality_report(), hide_index=True, width="stretch")
    st.subheader("Unified customer 360 view")
    st.dataframe(con.execute("SELECT * FROM customer_360 ORDER BY health_score").df(),
                 hide_index=True, width="stretch")

# ---- Governance --------------------------------------------------------------
with tabs[4]:
    st.markdown("- Contact emails and ticket free text **never enter** the reporting layer.\n"
                "- Questions are **redacted** (emails, phone numbers) before reaching the LLM.\n"
                "- LLM-written SQL passes a **read-only guard**: single SELECT, approved tables only, row cap.\n"
                "- Every ingestion run, automation rule and LLM tool call is written to an **audit log**.")
    st.subheader("Audit log (latest)")
    st.dataframe(pd.DataFrame(audit.tail(100)).iloc[::-1], hide_index=True, width="stretch")
