"""Security and governance controls shared by every layer.

- PII never enters the reporting layer (see warehouse.build), and any text
  sent to an LLM is redacted first.
- LLM-generated SQL goes through a read-only guard before it runs.
- Every automated decision and LLM call is written to an append-only audit
  log (JSON Lines).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT = ROOT / "data" / "audit_log.jsonl"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UK_PHONE = re.compile(r"(?:\+44\s?|0)(?:\d\s?){9,10}")


def redact(text: str) -> str:
    """Mask emails and UK phone numbers before text leaves the platform."""
    return UK_PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", text))


ALLOWED_TABLES = {"dim_customer", "fact_invoice", "fact_ticket", "fact_usage", "customer_360"}
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command,
             exp.Copy, exp.Attach, exp.Detach, exp.Pragma, exp.Set, exp.Merge)


class UnsafeQuery(ValueError):
    pass


def guard_sql(sql: str, max_rows: int = 500) -> str:
    """Allow only a single read-only SELECT over approved tables, and cap the row count."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeQuery(f"Could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeQuery("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With)) and not tree.find(exp.Select):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise UnsafeQuery(f"{type(node).__name__} statements are not allowed.")
        if isinstance(node, exp.Anonymous) and node.name.lower().startswith(("read_", "write_")):
            raise UnsafeQuery("File access functions are not allowed.")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name not in ALLOWED_TABLES and name not in ctes:
            raise UnsafeQuery(f"Table '{table.name}' is not in the approved reporting dataset.")
    return f"SELECT * FROM ({tree.sql(dialect='duckdb')}) AS q LIMIT {max_rows}"


class AuditLog:
    def __init__(self, path: Path | str = DEFAULT_AUDIT):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, layer: str, event: str, detail: dict) -> None:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "layer": layer, "event": event, **detail}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines]
