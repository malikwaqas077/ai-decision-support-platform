"""Layer 4: Business workflow automation.

Evaluates the declarative rules in config/automation_rules.yaml against the
unified dataset and raises actions for the right owner. Actions are
idempotent: re-running the pipeline does not duplicate an open action. Each
evaluation is written to the audit log.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from .governance import AuditLog

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = ROOT / "config" / "automation_rules.yaml"


def load_rules(path: Path = DEFAULT_RULES) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]


def run_rules(con: duckdb.DuckDBPyConnection, rules: list[dict] | None = None,
              audit: AuditLog | None = None) -> pd.DataFrame:
    rules = rules if rules is not None else load_rules()
    con.execute("""
        CREATE TABLE IF NOT EXISTS workflow_actions (
            action_key VARCHAR PRIMARY KEY, rule_id VARCHAR, rule_name VARCHAR, priority VARCHAR,
            owner VARCHAR, customer_id VARCHAR, company_name VARCHAR, account_manager VARCHAR,
            reason VARCHAR, action VARCHAR, status VARCHAR, raised_at TIMESTAMP)
    """)
    now = datetime.now().replace(microsecond=0)
    for rule in rules:
        hits = con.execute(rule["sql"]).df()
        new = 0
        for _, h in hits.iterrows():
            key = f"{rule['id']}:{h['customer_id']}"
            exists = con.execute("SELECT 1 FROM workflow_actions WHERE action_key = ? AND status = 'open'",
                                 [key]).fetchone()
            if exists:
                continue
            owner = h["account_manager"] if rule["owner"] == "account_manager" else rule["owner"]
            con.execute("INSERT OR REPLACE INTO workflow_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                key, rule["id"], rule["name"], rule["priority"], owner, h["customer_id"],
                h["company_name"], h["account_manager"], h["reason"], rule["action"], "open", now])
            new += 1
        if audit:
            audit.record("automation", rule["id"], {"matches": len(hits), "new_actions": new})
    return con.execute("""
        SELECT rule_name, priority, owner, customer_id, company_name, reason, action, status, raised_at
        FROM workflow_actions WHERE status = 'open'
        ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, rule_name, company_name
    """).df()
