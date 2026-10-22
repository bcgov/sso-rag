"""Compare retrieval strategies on the golden set: Recall@K, MRR, latency and
LLM tokens spent on planning.

    uv run python evals/run_retrieval.py
    uv run python evals/run_retrieval.py --strategies baseline planner_iterative --tags multi_facet

The baseline searches the question verbatim (there is no agent here to write
queries), so it is a lower bound for the production baseline. Needs AWS
credentials and the usual .env (KNOWLEDGE_BASE_ID, MODEL_ARN, ...).
"""

import argparse
import json
import time
from collections import defaultdict

from common import (
    RESULTS,
    STRATEGIES,
    load_golden,
    markdown_table,
    mean,
    messages_for,
    ranked_pages,
    recall_at_k,
    reciprocal_rank,
)

from app.services.retrieval_pipeline import search

KS = (1, 5, 10)


def evaluate(row: dict, strategy: str, hyde: bool) -> dict:
    started = time.monotonic()
    result = search(row["question"], [row["question"]], messages_for(row), strategy=strategy, hyde=hyde)
    ms = int((time.monotonic() - started) * 1000)
    ranked = ranked_pages([h.metadata.get("source_url") or h.source for h in result.hits])
    return {
        "id": row["id"],
        "tags": row.get("tags", []),
        **{f"recall@{k}": recall_at_k(ranked, row["expected_urls"], k) for k in KS},
        "mrr": reciprocal_rank(ranked, row["expected_urls"]),
        "ms": ms,
        "tokens": result.stats.get("tokens", 0),
        "rounds": len(result.stats.get("rounds", [])) or 1,
        "planner": result.stats.get("planner", "-"),
        "stop": result.stats.get("stop", "-"),
        "ranked": ranked,
        "expected": row["expected_urls"],
        "queries": result.stats.get("queries", []),
    }


def summarize(name: str, rows: list[dict]) -> list:
    ms = sorted(r["ms"] for r in rows)
    return [
        name, len(rows),
        *(mean([r[f"recall@{k}"] for r in rows]) for k in KS),
        mean([r["mrr"] for r in rows]),
        ms[len(ms) // 2] if ms else 0,
        ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else 0,
        round(mean([r["tokens"] for r in rows])),
        mean([r["rounds"] for r in rows]),
        sum(r["planner"] == "skipped" for r in rows),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    ap.add_argument("--tags", nargs="*")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    rows = load_golden(tags=args.tags, limit=args.limit)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    headers = ["strategy", "n", *(f"R@{k}" for k in KS), "MRR", "p50 ms", "p95 ms",
               "tokens", "rounds", "fast path"]
    overall, by_tag = [], defaultdict(list)

    for name in args.strategies:
        strategy, hyde = STRATEGIES[name]
        results = []
        for row in rows:
            try:
                results.append(evaluate(row, strategy, hyde))
            except Exception as e:
                print(f"{name} {row['id']}: {type(e).__name__}: {e}")
        (RESULTS / f"retrieval-{stamp}-{name}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in results) + "\n", encoding="utf-8")
        overall.append(summarize(name, results))
        for tag in sorted({t for r in results for t in r["tags"]}):
            by_tag[tag].append(summarize(name, [r for r in results if tag in r["tags"]]))

    report = ["# Retrieval evaluation", "", markdown_table(headers, overall), ""]
    for tag, table in by_tag.items():
        report += [f"## {tag}", "", markdown_table(headers, table), ""]
    text = "\n".join(report)
    (RESULTS / f"retrieval-{stamp}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
