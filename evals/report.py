"""Step 3: a markdown report for a run, optionally compared with a baseline.

    uv run --group eval python -m evals.report evals/runs/<run id>
        [--baseline evals/runs/<other run id>]

`python -m evals.score` writes report.md itself; run this to compare runs
(written to the run's directory as compare-<baseline run id>.md).
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from evals.metrics import mean, percentile

# Shown first, in this order; any other metric follows alphabetically.
METRIC_ORDER = [
    "outcome_match", "intent_match", "retrieval_hit@k", "retrieval_recall@k", "retrieval_mrr",
    "citation_precision", "first_pass_accepted", "faithfulness", "answer_relevancy",
    "correctness", "contextual_precision", "contextual_recall", "bcgov_bias", "clarification_quality",
]
CATEGORY_METRICS = ["outcome_match", "retrieval_hit@k", "faithfulness", "correctness"]
WORST_LIMIT = 25


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _ordered(names) -> list[str]:
    names = set(names)
    return [m for m in METRIC_ORDER if m in names] + sorted(names - set(METRIC_ORDER))


def summarize(results: list[dict], scores: list[dict]) -> dict:
    last_turns = [r["turns"][-1] for r in results if r.get("turns")]

    by_metric: dict[str, list[dict]] = defaultdict(list)
    for row in scores:
        by_metric[row["metric"]].append(row)
    metrics = {}
    for name in _ordered(by_metric):
        rows = by_metric[name]
        ok = [r for r in rows if not r["error"]]
        metrics[name] = {
            "kind": rows[0]["kind"],
            "n": len(ok),
            "mean": mean(r["score"] for r in ok),
            "pass_rate": (sum(r["success"] for r in ok) / len(ok)) if ok else None,
            "errors": len(rows) - len(ok),
        }

    categories: dict[str, dict] = {}
    for cat in sorted({r["case"]["category"] for r in results}):
        rows = [s for s in scores if s["category"] == cat and not s["error"]]
        categories[cat] = {
            "cases": sum(1 for r in results if r["case"]["category"] == cat),
            **{m: mean(s["score"] for s in rows if s["metric"] == m) for m in CATEGORY_METRICS},
        }

    confusion = Counter()
    for r in results:
        if r.get("turns"):
            expected = "/".join(r["case"].get("expected_outcome") or []) or "(none)"
            confusion[(expected, r["turns"][-1].get("outcome") or "(error)")] += 1

    with_attempts = [t for t in last_turns if t.get("attempts")]
    repairs = [t for t in last_turns if t.get("repair_attempted")]

    def per_case(key: str) -> float | None:
        return mean(sum(t["tokens"].get(key, 0) for t in r["turns"]) for r in results if r.get("turns"))

    agent_input = per_case("agent_input")
    cache_read = per_case("agent_cache_read")
    return {
        "cases": len(results),
        "drafts": sum(1 for r in results if r["case"].get("draft")),
        "errors": sum(1 for r in results if r.get("error")),
        "metrics": metrics,
        "categories": categories,
        "outcomes": {f"{e} -> {a}": n for (e, a), n in sorted(confusion.items())},
        "grounding": {
            "status": dict(Counter(t.get("grounding_status") or "(none)" for t in last_turns)),
            "first_pass_accepted": (
                sum(not (t["attempts"][0].get("reasons")) for t in with_attempts) / len(with_attempts)
                if with_attempts else None
            ),
            "repairs": len(repairs),
            "repaired": sum(t.get("grounding_status") != "NOT_SUPPORTED" for t in repairs),
        },
        "tokens_per_case": {
            "agent_input": agent_input,
            "agent_output": per_case("agent_output"),
            "agent_cache_read_share": (cache_read / agent_input) if agent_input and cache_read is not None else None,
            "rewriter_input": per_case("rewriter_input"),
            "rewriter_output": per_case("rewriter_output"),
            "rewriter_cache_read": per_case("rewriter_cache_read"),
        },
        "latency_ms": {
            "turn_p50": percentile([t["latency_ms"] for t in last_turns], 50),
            "turn_p95": percentile([t["latency_ms"] for t in last_turns], 95),
            "rewrite_p50": percentile([t.get("rewrite_latency_ms") for t in last_turns], 50),
            "rewrite_p95": percentile([t.get("rewrite_latency_ms") for t in last_turns], 95),
        },
    }


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |"
              for row in rows]
    return lines


def render(meta: dict, scoring: dict, summary: dict, scores: list[dict], results: list[dict]) -> str:
    s = meta.get("settings", {})
    git = meta.get("git", {})
    judge = (scoring or {}).get("judge") or {}
    lines = [
        f"# Evaluation run {meta.get('run_id')}",
        "",
        f"- Commit `{(git.get('commit') or '')[:10]}` on `{git.get('branch')}`"
        + (" (uncommitted changes)" if git.get("dirty") else ""),
        f"- Agent `{s.get('model_arn')}`, rewriter `{s.get('query_rewriter_model_id')}`, "
        f"judge `{judge.get('model', 'none (deterministic only)')}`",
        f"- {summary['cases']} cases ({summary['drafts']} drafts not yet reviewed), "
        f"{summary['errors']} failed to run; retrieval cut-off k={scoring.get('k', '–')}",
    ]
    if summary["drafts"]:
        lines.append("- **Draft cases are included: their expectations haven't been reviewed, "
                     "so treat these scores as provisional.**")

    lines += ["", "## Metrics", ""]
    lines += _table(
        ["Metric", "Kind", "Cases", "Mean", "Pass rate", "Errors"],
        [[m, v["kind"], v["n"], _fmt(v["mean"]), _pct(v["pass_rate"]), v["errors"]]
         for m, v in summary["metrics"].items()],
    )

    lines += ["", "## By category", ""]
    lines += _table(
        ["Category", "Cases", *CATEGORY_METRICS],
        [[c, v["cases"], *(_fmt(v[m]) for m in CATEGORY_METRICS)] for c, v in summary["categories"].items()],
    )

    lines += ["", "## Outcomes (expected -> actual)", ""]
    lines += _table(["Expected -> actual", "Cases"], [[k, v] for k, v in summary["outcomes"].items()])

    g = summary["grounding"]
    t = summary["tokens_per_case"]
    lat = summary["latency_ms"]
    lines += [
        "", "## Grounding, cost and latency", "",
        f"- Grounding status: {', '.join(f'{k} {v}' for k, v in sorted(g['status'].items()))}",
        f"- First reply accepted by the grounding check: {_pct(g['first_pass_accepted'])}; "
        f"repairs {g['repairs']}, of which delivered {g['repaired']}",
        f"- Agent tokens per case: input {_fmt(t['agent_input'], 0)} "
        f"(cache reads {_pct(t['agent_cache_read_share'])}), output {_fmt(t['agent_output'], 0)}",
        f"- Rewriter tokens per case: input {_fmt(t['rewriter_input'], 0)} "
        f"(cache reads {_fmt(t['rewriter_cache_read'], 0)}), output {_fmt(t['rewriter_output'], 0)}",
        f"- Turn latency p50 {_fmt(lat['turn_p50'], 0)} ms, p95 {_fmt(lat['turn_p95'], 0)} ms; "
        f"rewriter p50 {_fmt(lat['rewrite_p50'], 0)} ms, p95 {_fmt(lat['rewrite_p95'], 0)} ms",
    ]
    if judge:
        lines.append(f"- Judge: {judge.get('calls')} calls, {judge.get('input_tokens')} input and "
                     f"{judge.get('output_tokens')} output tokens, {judge.get('errors')} errors")

    failed = [r for r in scores if not r["error"] and not r["success"]]
    failed.sort(key=lambda r: (r["kind"] != "check", r["score"] if r["score"] is not None else 0))
    lines += ["", f"## Failures ({len(failed)})", ""]
    if failed:
        lines += _table(
            ["Case", "Metric", "Score", "Reason"],
            [[r["id"], r["metric"], _fmt(r["score"]), (r["reason"] or "")[:300]] for r in failed[:WORST_LIMIT]],
        )
        if len(failed) > WORST_LIMIT:
            lines.append(f"\n…and {len(failed) - WORST_LIMIT} more in scores.jsonl.")

    errors = [r for r in scores if r["error"]]
    run_errors = [r for r in results if r.get("error")]
    if errors or run_errors:
        lines += ["", "## Errors", ""]
        lines += [f"- `{r['id']}` (run): {r['error'][:300]}" for r in run_errors]
        lines += [f"- `{r['id']}` {r['metric']}: {r['error'][:300]}" for r in errors[:WORST_LIMIT]]
    return "\n".join(lines) + "\n"


def write_report(run_dir: Path) -> Path:
    meta = _read_json(run_dir / "run.json")
    scoring = _read_json(run_dir / "scoring.json")
    results = _read_jsonl(run_dir / "results.jsonl")
    scores = _read_jsonl(run_dir / "scores.jsonl")
    summary = summarize(results, scores)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    out = run_dir / "report.md"
    out.write_text(render(meta, scoring, summary, scores, results), encoding="utf-8")
    return out


def _delta(a, b, pct: bool = False) -> str:
    if a is None or b is None:
        return "–"
    d = b - a
    return f"{d * 100:+.0f} pts" if pct else f"{d:+.2f}"


def render_comparison(base_dir: Path, run_dir: Path) -> str:
    base_meta, run_meta = _read_json(base_dir / "run.json"), _read_json(run_dir / "run.json")
    base_scores, run_scores = _read_jsonl(base_dir / "scores.jsonl"), _read_jsonl(run_dir / "scores.jsonl")
    base = summarize(_read_jsonl(base_dir / "results.jsonl"), base_scores)
    run = summarize(_read_jsonl(run_dir / "results.jsonl"), run_scores)

    lines = [f"# {run_meta.get('run_id')} compared with {base_meta.get('run_id')}", ""]
    changed = {k: (v, run_meta.get("settings", {}).get(k)) for k, v in base_meta.get("settings", {}).items()
               if run_meta.get("settings", {}).get(k) != v}
    if changed:
        lines.append("Settings that differ: " + "; ".join(f"`{k}` {a} -> {b}" for k, (a, b) in changed.items()))
        lines.append("")
    if base_meta.get("dataset", {}).get("sha256") != run_meta.get("dataset", {}).get("sha256"):
        lines += ["**The datasets differ**: compare per-case results, not only the means.", ""]

    names = _ordered(set(base["metrics"]) | set(run["metrics"]))
    rows = []
    for m in names:
        a, b = base["metrics"].get(m, {}), run["metrics"].get(m, {})
        rows.append([m, _fmt(a.get("mean")), _fmt(b.get("mean")), _delta(a.get("mean"), b.get("mean")),
                     _pct(a.get("pass_rate")), _pct(b.get("pass_rate")),
                     _delta(a.get("pass_rate"), b.get("pass_rate"), pct=True)])
    lines += _table(["Metric", "Mean before", "Mean after", "Δ", "Pass before", "Pass after", "Δ"], rows)

    ta, tb = base["tokens_per_case"], run["tokens_per_case"]
    la, lb = base["latency_ms"], run["latency_ms"]
    lines += ["", "## Cost and latency", ""]
    lines += _table(
        ["Per case", "Before", "After", "Δ"],
        [[k, _fmt(ta[k], 0), _fmt(tb[k], 0), _delta(ta[k], tb[k])]
         for k in ("agent_input", "agent_output", "rewriter_input", "rewriter_output")]
        + [[k, _fmt(la[k], 0), _fmt(lb[k], 0), _delta(la[k], lb[k])] for k in la]
        + [["first_pass_accepted", _pct(base["grounding"]["first_pass_accepted"]),
            _pct(run["grounding"]["first_pass_accepted"]),
            _delta(base["grounding"]["first_pass_accepted"], run["grounding"]["first_pass_accepted"], pct=True)]],
    )

    before = {(r["id"], r["metric"]): r for r in base_scores if not r["error"]}
    after = {(r["id"], r["metric"]): r for r in run_scores if not r["error"]}
    regressions = [(key, before[key], after[key]) for key in sorted(before.keys() & after.keys())
                   if before[key]["success"] and not after[key]["success"]]
    fixed = [key for key in before.keys() & after.keys()
             if not before[key]["success"] and after[key]["success"]]
    lines += ["", f"## Regressions ({len(regressions)}; fixed: {len(fixed)})", ""]
    if regressions:
        lines += _table(
            ["Case", "Metric", "Before", "After", "Reason now"],
            [[k[0], k[1], _fmt(a["score"]), _fmt(b["score"]), (b["reason"] or "")[:250]]
             for k, a, b in regressions],
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(prog="python -m evals.report", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--baseline", type=Path, help="an earlier run to compare with")
    args = parser.parse_args(argv)

    if args.baseline is None:
        out = write_report(args.run_dir)
    else:
        out = args.run_dir / f"compare-{args.baseline.name}.md"
        out.write_text(render_comparison(args.baseline, args.run_dir), encoding="utf-8")
    print(out)
    return out


if __name__ == "__main__":
    main()
