"""Layer 3: Business analytics and performance visualisation.

Every KPI is defined once, here, as a named query over the unified dataset.
The dashboard, the automation rules and the LLM assistant all reuse these
definitions.
"""
from __future__ import annotations

import duckdb
import pandas as pd

KPIS = {
    "monthly_recurring_revenue": (
        "Monthly recurring revenue (£)",
        "SELECT ROUND(SUM(monthly_revenue_gbp)) FROM customer_360"),
    "overdue_debt": (
        "Overdue debt > 45 days (£)",
        "SELECT ROUND(SUM(overdue_gbp)) FROM customer_360"),
    "sla_compliance": (
        "SLA compliance, last 90 days (%)",
        """SELECT ROUND(100.0 * AVG(CASE WHEN sla_breached THEN 0 ELSE 1 END), 1) FROM fact_ticket
           WHERE opened_at >= (SELECT MAX(opened_at) FROM fact_ticket) - INTERVAL 90 DAY"""),
    "at_risk_accounts": (
        "Accounts at risk (health < 40)",
        "SELECT COUNT(*) FROM customer_360 WHERE health_score < 40"),
    "revenue_at_risk": (
        "Revenue at risk (£ / month)",
        "SELECT ROUND(SUM(monthly_revenue_gbp)) FROM customer_360 WHERE health_score < 40"),
    "avg_csat": (
        "Average CSAT, last 90 days (1–5)",
        "SELECT ROUND(AVG(avg_csat), 2) FROM customer_360"),
}

VIEWS = {
    "revenue_trend": """
        SELECT issued_date AS month, product, ROUND(SUM(amount_gbp)) AS revenue_gbp
        FROM fact_invoice GROUP BY 1, 2 ORDER BY 1, 2""",
    "tickets_by_category": """
        SELECT category, COUNT(*) AS tickets,
               ROUND(100.0 * AVG(CASE WHEN sla_breached THEN 1 ELSE 0 END), 1) AS breach_rate_pct
        FROM fact_ticket GROUP BY 1 ORDER BY tickets DESC""",
    "health_by_sector": """
        SELECT sector, COUNT(*) AS accounts, ROUND(AVG(health_score), 1) AS avg_health,
               ROUND(SUM(monthly_revenue_gbp)) AS mrr_gbp
        FROM customer_360 GROUP BY 1 ORDER BY avg_health""",
    "account_manager_portfolio": """
        SELECT account_manager, COUNT(*) AS accounts, ROUND(SUM(monthly_revenue_gbp)) AS mrr_gbp,
               SUM(CASE WHEN health_score < 40 THEN 1 ELSE 0 END) AS at_risk
        FROM customer_360 GROUP BY 1 ORDER BY mrr_gbp DESC""",
    "at_risk_accounts": """
        SELECT customer_id, company_name, sector, account_manager, ROUND(health_score) AS health,
               ROUND(monthly_revenue_gbp) AS mrr_gbp, sla_breaches_90d, ROUND(overdue_gbp) AS overdue_gbp,
               outage_minutes_3m
        FROM customer_360 WHERE health_score < 40 ORDER BY monthly_revenue_gbp DESC""",
}


def kpis(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, float]]:
    return {k: (label, con.execute(sql).fetchone()[0]) for k, (label, sql) in KPIS.items()}


def view(con: duckdb.DuckDBPyConnection, name: str) -> pd.DataFrame:
    return con.execute(VIEWS[name]).df()
