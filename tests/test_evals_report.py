import json

from evals import report


def _result(case_id: str, category: str, outcome: str, status: str | None, latency: int,
            expected=("answer",), reasons=(), draft=False) -> dict:
    turn = {"outcome": outcome, "grounding_status": status, "latency_ms": latency, "rewrite_latency_ms": 400,
            "attempts": [{"reasons": list(reasons)}], "repair_attempted": bool(reasons),
            "tokens": {"agent_input": 10000, "agent_output": 500, "agent_cache_read": 0,
                       "rewriter_input": 2000, "rewriter_output": 60, "rewriter_cache_read": 1800}}
    return {"id": case_id, "case": {"id": case_id, "category": category, "expected_outcome": list(expected),
                                    "draft": draft},
            "turns": [turn], "error": None}


def _score(case_id, category, metric, score, success, kind="check", reason="r", error=None) -> dict:
    return {"id": case_id, "category": category, "metric": metric, "kind": kind, "score": score,
            "success": success, "threshold": 0.5, "reason": reason, "error": error, "seconds": 0.1}


def _run(tmp_path, name, results, scores, **settings):
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": name, "git": {"commit": "abc1234def", "branch": "b", "dirty": True},
        "dataset": {"sha256": "same"}, "settings": {"model_arn": "agent", **settings}}))
    (run_dir / "scoring.json").write_text(json.dumps({"k": 6, "judge": {"model": "kimi", "calls": 3,
                                                                           "input_tokens": 9, "output_tokens": 3,
                                                                           "errors": 0}}))
    (run_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    (run_dir / "scores.jsonl").write_text("".join(json.dumps(s) + "\n" for s in scores))
    return run_dir


RESULTS = [
    _result("a", "sso_question", "answer", "SUPPORTED", 8000, draft=True),
    _result("b", "sso_question", "not_supported", "NOT_SUPPORTED", 12000, reasons=["undocumented URLs"]),
    _result("c", "clarification", "clarification", None, 4000, expected=("clarification",)),
]
SCORES = [
    _score("a", "sso_question", "outcome_match", 1.0, True),
    _score("b", "sso_question", "outcome_match", 0.0, False, reason="outcome 'not_supported'"),
    _score("c", "clarification", "outcome_match", 1.0, True),
    _score("a", "sso_question", "faithfulness", 0.9, True, kind="judge"),
    _score("b", "sso_question", "faithfulness", None, False, kind="judge", error="JudgeError: x"),
]


def test_summarize():
    s = report.summarize(RESULTS, SCORES)
    assert s["cases"] == 3 and s["drafts"] == 1
    assert list(s["metrics"]) == ["outcome_match", "faithfulness"]
    assert s["metrics"]["outcome_match"]["pass_rate"] == 2 / 3
    assert s["metrics"]["faithfulness"] == {"kind": "judge", "n": 1, "mean": 0.9, "pass_rate": 1.0, "errors": 1}
    assert s["categories"]["sso_question"]["outcome_match"] == 0.5
    assert s["outcomes"] == {"answer -> answer": 1, "answer -> not_supported": 1,
                             "clarification -> clarification": 1}
    assert s["grounding"]["first_pass_accepted"] == 2 / 3
    assert s["grounding"]["repairs"] == 1 and s["grounding"]["repaired"] == 0
    assert s["tokens_per_case"]["agent_input"] == 10000
    # inputTokens excludes cache reads: 1800 of 2000 + 1800 prompt tokens.
    assert s["tokens_per_case"]["rewriter_cache_read_share"] == 1800 / 3800
    assert s["tokens_per_case"]["agent_cache_read_share"] == 0
    assert s["latency_ms"]["turn_p50"] == 8000


def test_write_report(tmp_path):
    run_dir = _run(tmp_path, "run1", RESULTS, SCORES)
    text = report.write_report(run_dir).read_text()
    assert "# Evaluation run run1" in text
    assert "(uncommitted changes)" in text
    assert "Draft cases are included" in text
    assert "| outcome_match | check | 3 | 0.67 | 67% | 0 |" in text
    assert "## Failures (1)" in text
    assert "`b` faithfulness: JudgeError: x" in text
    assert json.loads((run_dir / "summary.json").read_text())["cases"] == 3


def test_comparison_lists_regressions_and_setting_changes(tmp_path):
    base = _run(tmp_path, "base", RESULTS, SCORES, model_arn="old-agent")
    after_scores = [dict(s) for s in SCORES]
    after_scores[0].update(score=0.0, success=False, reason="now wrong")
    after_scores[1].update(score=1.0, success=True)
    run = _run(tmp_path, "new", RESULTS, after_scores, model_arn="new-agent")

    text = report.render_comparison(base, run)
    assert "# new compared with base" in text
    assert "`model_arn` old-agent -> new-agent" in text
    assert "## Regressions (1; fixed: 1)" in text
    assert "| a | outcome_match | 1.00 | 0.00 | now wrong |" in text
