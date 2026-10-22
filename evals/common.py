"""Shared helpers for the evaluation scripts."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # so pydantic-settings finds .env

GOLDEN = ROOT / "evals" / "golden.jsonl"
RESULTS = ROOT / "evals" / "results"

# Strategy name -> (QUERY_STRATEGY, HyDE on?)
STRATEGIES = {
    "baseline": ("baseline", False),
    "planner": ("planner", False),
    "planner_iterative": ("planner_iterative", False),
    "planner_iterative+hyde": ("planner_iterative", True),
}


def load_golden(path: Path = GOLDEN, tags: list[str] | None = None, limit: int | None = None) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if tags:
        rows = [r for r in rows if set(tags) & set(r.get("tags", []))]
    return rows[:limit] if limit else rows


def messages_for(row: dict, include_question: bool = True) -> list[dict]:
    msgs = [{"role": t["role"], "content": [{"text": t["text"]}]} for t in row.get("history", [])]
    if include_question:
        msgs.append({"role": "user", "content": [{"text": row["question"]}]})
    return msgs


def page_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.split("#", 1)[0].rstrip("/").lower()


def ranked_pages(urls: list[str | None]) -> list[str]:
    """Unique page URLs in rank order."""
    return list(dict.fromkeys(p for p in (page_url(u) for u in urls) if p))


def recall_at_k(ranked: list[str], expected: list[str], k: int) -> float:
    expected = {page_url(u) for u in expected}
    return len(expected & set(ranked[:k])) / len(expected) if expected else 0.0


def reciprocal_rank(ranked: list[str], expected: list[str]) -> float:
    expected = {page_url(u) for u in expected}
    return next((1 / i for i, p in enumerate(ranked, 1) if p in expected), 0.0)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def markdown_table(headers: list[str], rows: list[list]) -> str:
    def fmt(v):
        return f"{v:.3f}" if isinstance(v, float) else str(v)
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(fmt(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)
