"""Retrieval over the company knowledge base (policies, playbooks, product
information) for retrieval-augmented generation.

The documents are split into chunks by heading and ranked with BM25. That
needs no external service or vector database, and the ranking is easy to
inspect. The interface (`search(query, k)`) means an embedding index can be
swapped in later without changing the LLM layer.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB = ROOT / "knowledge"
TOKEN = re.compile(r"[a-z0-9£]+")
STOP = set("a an and are as at be by for from has have how i in is it of on or our that the this to we what when "
           "which who will with you your do does should".split())


@dataclass
class Chunk:
    doc: str
    heading: str
    text: str


def _stem(t: str) -> str:
    """Light suffix stripping so 'breaches'/'breach' and 'credits'/'credit' match."""
    for suffix in ("ing", "es", "s"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 4 and not t.endswith("ss"):
            return t[: -len(suffix)]
    return t


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in TOKEN.findall(text.lower()) if t not in STOP]


def load_chunks(kb_dir: Path = DEFAULT_KB) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(kb_dir.glob("*.md")):
        title, heading, buf = path.stem, path.stem, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = heading = line[2:].strip()
            elif line.startswith("## "):
                if any(s.strip() for s in buf):
                    chunks.append(Chunk(title, heading, "\n".join(buf).strip()))
                heading, buf = line[3:].strip(), []
            else:
                buf.append(line)
        if any(s.strip() for s in buf):
            chunks.append(Chunk(title, heading, "\n".join(buf).strip()))
    return chunks


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.docs = [_tokens(f"{c.heading} {c.heading} {c.text}") for c in chunks]
        self.avgdl = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def search(self, query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        q = _tokens(query)
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = sum(self.idf.get(t, 0) * tf[t] * (self.k1 + 1)
                    / (tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl)) for t in q if t in tf)
            if s > 0:
                scored.append((self.chunks[i], s))
        return sorted(scored, key=lambda x: -x[1])[:k]
