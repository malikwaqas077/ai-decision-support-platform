"""Layer 1: Data ingestion and semantic integration.

Reads each source system's extract, maps its fields onto the canonical
vocabulary in config/source_mappings.yaml, normalises keys and types, and
records data-quality issues instead of silently dropping rows.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING = ROOT / "config" / "source_mappings.yaml"
DEFAULT_RAW = ROOT / "data" / "raw"


@dataclass
class QualityIssue:
    source: str
    row: int
    field: str
    problem: str


@dataclass
class IngestResult:
    frames: dict[str, pd.DataFrame]
    issues: list[QualityIssue] = field(default_factory=list)
    lineage: dict[str, dict] = field(default_factory=dict)

    def quality_report(self) -> pd.DataFrame:
        return pd.DataFrame([i.__dict__ for i in self.issues],
                            columns=["source", "row", "field", "problem"])


def canonical_customer_id(raw, pattern: str) -> str | None:
    """Turn 'CUST0007', 'NB/0007', 7 or 'C-0007' into the canonical 'C-0007'."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    m = re.match(pattern, str(raw).strip())
    return f"C-{int(m.group(1)):04d}" if m else None


def _convert(series: pd.Series, spec: dict) -> pd.Series:
    kind = spec["type"]
    s = series.replace("", pd.NA)
    if kind == "date":
        return pd.to_datetime(s, format=spec["format"], errors="coerce").dt.date
    if kind == "datetime":
        return pd.to_datetime(s, format=spec["format"], errors="coerce")
    if kind == "currency":
        return pd.to_numeric(s.astype("string").str.replace(r"[£,\s]", "", regex=True), errors="coerce")
    if kind == "int":
        return pd.to_numeric(s, errors="coerce").astype("Int64")
    if kind == "float":
        return pd.to_numeric(s, errors="coerce")
    raise ValueError(f"Unknown type {kind}")


TRANSFORMS = {"title_case": lambda s: s.astype("string").str.strip().str.title()}


def _read(path: Path, fmt: str) -> pd.DataFrame:
    if fmt == "csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if fmt == "json":
        return pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
    raise ValueError(f"Unsupported format {fmt}")


def ingest(raw_dir: Path = DEFAULT_RAW, mapping_path: Path = DEFAULT_MAPPING) -> IngestResult:
    mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
    result = IngestResult(frames={})

    for name, src in mapping["sources"].items():
        path = raw_dir / src["file"]
        df = _read(path, src["format"])
        n_in = len(df)
        key_field, pattern = src["key"]["field"], src["key"]["pattern"]

        ids = df[key_field].map(lambda v: canonical_customer_id(v, pattern))
        for idx in ids[ids.isna()].index:
            result.issues.append(QualityIssue(name, int(idx), key_field, "unrecognised customer key"))

        out = df[list(src["fields"])].rename(columns=src["fields"])
        out["customer_id"] = ids

        for col, spec in src.get("types", {}).items():
            converted = _convert(out[col], spec)
            bad = converted.isna() & out[col].notna() & (out[col].astype("string") != "")
            for idx in out.index[bad]:
                result.issues.append(QualityIssue(name, int(idx), col, f"could not parse as {spec['type']}"))
            out[col] = converted

        for col, t in src.get("transforms", {}).items():
            out[col] = TRANSFORMS[t](out[col])

        out = out[out["customer_id"].notna()].reset_index(drop=True)
        result.frames[src["entity"]] = out
        result.lineage[src["entity"]] = {
            "source_system": name, "file": src["file"], "rows_in": n_in,
            "rows_loaded": len(out), "pii_fields": src.get("pii", []),
        }

    # Referential check: every fact row must belong to a known customer
    known = set(result.frames["customer"]["customer_id"])
    for entity, df in result.frames.items():
        if entity == "customer":
            continue
        orphans = df[~df["customer_id"].isin(known)]
        for idx in orphans.index:
            result.issues.append(QualityIssue(entity, int(idx), "customer_id", "no matching CRM account (quarantined)"))
        result.frames[entity] = df[df["customer_id"].isin(known)].reset_index(drop=True)
        result.lineage[entity]["rows_loaded"] = len(result.frames[entity])
    return result
