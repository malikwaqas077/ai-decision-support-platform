"""Layer 2: Unified reporting dataset.

Loads the integrated data into a DuckDB star schema (one customer dimension,
one fact table per business process) and builds a customer_360 view that
every report, automation rule and LLM query reads from. One governed source of
truth means a KPI is calculated the same way wherever it appears.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .ingest import IngestResult

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "warehouse.duckdb"

SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}

SCHEMA_DOC = """
dim_customer(customer_id, company_name, sector, town, account_manager, customer_since DATE, seats INT)
fact_invoice(invoice_id, customer_id, product, amount_gbp DOUBLE, issued_date DATE, paid_date DATE NULL,
             days_to_pay INT NULL, days_outstanding INT NULL)
fact_ticket(ticket_id, customer_id, priority 'P1'..'P4', category, opened_at TIMESTAMP, resolved_at TIMESTAMP NULL,
            resolution_hours DOUBLE NULL, sla_hours INT, sla_breached BOOLEAN, csat_score INT NULL 1-5)
fact_usage(customer_id, month DATE, voice_minutes INT, data_gb DOUBLE, outage_minutes INT)
customer_360(customer_id, company_name, sector, town, account_manager, seats, customer_since,
             monthly_revenue_gbp, product_count, overdue_gbp, tickets_90d, sla_breaches_90d, avg_csat,
             outage_minutes_3m, data_growth_pct, health_score 0-100)
""".strip()


def build(result: IngestResult, db_path: Path | str = DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(db_path).unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))

    cust = result.frames["customer"].drop(columns=["contact_email"])  # PII stays out of the reporting layer
    inv = result.frames["invoice"]
    tix = result.frames["ticket"].drop(columns=["summary"])
    use = result.frames["usage"]
    con.register("cust_df", cust)
    con.register("inv_df", inv)
    con.register("tix_df", tix)
    con.register("use_df", use)

    con.execute("""
        CREATE TABLE dim_customer AS
        SELECT customer_id, company_name, sector, town, account_manager,
               CAST(customer_since AS DATE) AS customer_since, CAST(seats AS INT) AS seats
        FROM cust_df;
    """)
    con.execute("""
        CREATE TABLE fact_invoice AS
        WITH as_of_date AS (SELECT MAX(CAST(issued_date AS DATE)) + 30 AS d FROM inv_df)
        SELECT invoice_id, customer_id, product, CAST(amount_gbp AS DOUBLE) AS amount_gbp,
               CAST(issued_date AS DATE) AS issued_date, CAST(paid_date AS DATE) AS paid_date,
               DATE_DIFF('day', CAST(issued_date AS DATE), CAST(paid_date AS DATE)) AS days_to_pay,
               CASE WHEN paid_date IS NULL THEN DATE_DIFF('day', CAST(issued_date AS DATE), (SELECT d FROM as_of_date)) END
                   AS days_outstanding
        FROM inv_df;
    """)
    sla_case = " ".join(f"WHEN '{p}' THEN {h}" for p, h in SLA_HOURS.items())
    con.execute(f"""
        CREATE TABLE fact_ticket AS
        SELECT CAST(ticket_id AS BIGINT) AS ticket_id, customer_id, priority, category,
               CAST(opened_at AS TIMESTAMP) AS opened_at, CAST(resolved_at AS TIMESTAMP) AS resolved_at,
               DATE_DIFF('minute', CAST(opened_at AS TIMESTAMP), CAST(resolved_at AS TIMESTAMP)) / 60.0
                   AS resolution_hours,
               CASE priority {sla_case} END AS sla_hours,
               COALESCE(DATE_DIFF('minute', CAST(opened_at AS TIMESTAMP), CAST(resolved_at AS TIMESTAMP)) / 60.0
                        > CASE priority {sla_case} END, FALSE) AS sla_breached,
               CAST(csat_score AS INT) AS csat_score
        FROM tix_df;
    """)
    con.execute("""
        CREATE TABLE fact_usage AS
        SELECT customer_id, CAST(month AS DATE) AS month, CAST(voice_minutes AS INT) AS voice_minutes,
               CAST(data_gb AS DOUBLE) AS data_gb, CAST(outage_minutes AS INT) AS outage_minutes
        FROM use_df;
    """)
    con.execute(CUSTOMER_360_SQL)
    return con


# Health score: transparent, explainable weighted rules rather than a black box,
# so account managers can see *why* an account is flagged.
CUSTOMER_360_SQL = """
CREATE VIEW customer_360 AS
WITH last_month AS (SELECT MAX(issued_date) AS m FROM fact_invoice),
     t_end AS (SELECT MAX(opened_at) AS t FROM fact_ticket),
     u_end AS (SELECT MAX(month) AS m FROM fact_usage),
rev AS (
    SELECT customer_id, SUM(amount_gbp) AS monthly_revenue_gbp, COUNT(DISTINCT product) AS product_count
    FROM fact_invoice WHERE issued_date = (SELECT m FROM last_month) GROUP BY 1),
debt AS (
    SELECT customer_id, SUM(amount_gbp) AS overdue_gbp
    FROM fact_invoice WHERE paid_date IS NULL AND days_outstanding > 45 GROUP BY 1),
tix AS (
    SELECT customer_id, COUNT(*) AS tickets_90d, SUM(CASE WHEN sla_breached THEN 1 ELSE 0 END) AS sla_breaches_90d,
           AVG(csat_score) AS avg_csat
    FROM fact_ticket WHERE opened_at >= (SELECT t FROM t_end) - INTERVAL 90 DAY GROUP BY 1),
use AS (
    SELECT customer_id,
           SUM(CASE WHEN month > (SELECT m FROM u_end) - INTERVAL 3 MONTH THEN outage_minutes ELSE 0 END)
               AS outage_minutes_3m,
           100.0 * (AVG(CASE WHEN month > (SELECT m FROM u_end) - INTERVAL 3 MONTH THEN data_gb END)
                    / NULLIF(AVG(CASE WHEN month <= (SELECT m FROM u_end) - INTERVAL 9 MONTH THEN data_gb END), 0) - 1)
               AS data_growth_pct
    FROM fact_usage GROUP BY 1)
SELECT c.*,
       COALESCE(rev.monthly_revenue_gbp, 0) AS monthly_revenue_gbp,
       COALESCE(rev.product_count, 0) AS product_count,
       COALESCE(debt.overdue_gbp, 0) AS overdue_gbp,
       COALESCE(tix.tickets_90d, 0) AS tickets_90d,
       COALESCE(tix.sla_breaches_90d, 0) AS sla_breaches_90d,
       tix.avg_csat,
       COALESCE(use.outage_minutes_3m, 0) AS outage_minutes_3m,
       COALESCE(use.data_growth_pct, 0) AS data_growth_pct,
       GREATEST(0, LEAST(100,
           100
           - 6 * COALESCE(tix.sla_breaches_90d, 0)
           - LEAST(20, COALESCE(debt.overdue_gbp, 0) / 250)
           - LEAST(20, COALESCE(use.outage_minutes_3m, 0) / 20)
           - CASE WHEN tix.avg_csat IS NULL THEN 0 ELSE (5 - tix.avg_csat) * 6 END
           + LEAST(10, GREATEST(-15, COALESCE(use.data_growth_pct, 0) / 3))
       )) AS health_score
FROM dim_customer c
LEFT JOIN rev USING (customer_id)
LEFT JOIN debt USING (customer_id)
LEFT JOIN tix USING (customer_id)
LEFT JOIN use USING (customer_id);
"""
