"""End-to-end pipeline: generate sample data if needed, then run Layers 1 → 4.

    python -m dsp.pipeline
"""
from __future__ import annotations

from pathlib import Path

from .automation import run_rules
from .governance import AuditLog
from .ingest import DEFAULT_RAW, ingest
from .warehouse import DEFAULT_DB, build


def run(raw_dir: Path = DEFAULT_RAW, db_path: Path | str = DEFAULT_DB, audit: AuditLog | None = None):
    if not (raw_dir / "crm_accounts.csv").exists():
        from data.generate_synthetic import generate
        generate(raw_dir)
    audit = audit or AuditLog()
    result = ingest(raw_dir)
    audit.record("ingestion", "load", {"lineage": result.lineage, "quality_issues": len(result.issues)})
    con = build(result, db_path)
    actions = run_rules(con, audit=audit)
    return con, result, actions


if __name__ == "__main__":
    con, result, actions = run()
    for entity, info in result.lineage.items():
        print(f"{entity:9s} {info['rows_loaded']:>6} rows from {info['source_system']} ({info['file']})")
    print(f"Data-quality issues logged: {len(result.issues)}")
    print(f"Open workflow actions: {len(actions)}")
    print(actions.groupby('rule_name').size().to_string())
