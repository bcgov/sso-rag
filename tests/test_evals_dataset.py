import json

import pytest

from evals.config import DEFAULT_DATASET
from evals.dataset import DatasetError, EvalCase, load_cases, select, write_cases


def _write(tmp_path, *rows) -> str:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows), encoding="utf-8")
    return path


def test_loads_cases_and_normalizes_expected_outcome(tmp_path):
    path = _write(tmp_path,
                  {"id": "a", "category": "sso_question", "turns": ["q?"], "expected_outcome": "answer"},
                  "",
                  "// a comment line",
                  {"id": "b", "category": "clarification", "turns": ["x", "y"],
                   "expected_outcome": ["clarification", "answer"], "draft": True})
    a, b = load_cases(path)
    assert a.expected_outcome == ["answer"]
    assert b.question == "y"
    assert b.draft is True


def test_reports_every_problem_with_its_line_number(tmp_path):
    path = _write(tmp_path,
                  {"id": "ok", "category": "c", "turns": ["q"]},
                  "{not json",
                  {"id": "ok", "category": "c", "turns": ["q"]},
                  {"id": "Bad Id", "category": "c", "turns": []},
                  {"id": "x", "category": "c", "turns": ["q"], "expected_outcome": "maybe"},
                  {"id": "y", "category": "c", "turns": ["q"], "unknown": 1})
    with pytest.raises(DatasetError) as e:
        load_cases(path)
    message = str(e.value)
    assert "cases.jsonl:2: invalid JSON" in message
    assert "cases.jsonl:3: duplicate id 'ok' (first on line 1)" in message
    assert "cases.jsonl:4: id:" in message
    assert "cases.jsonl:4: turns:" in message
    assert "cases.jsonl:5: expected_outcome" in message
    assert "cases.jsonl:6: unknown" in message


def test_blank_turn_is_rejected():
    with pytest.raises(ValueError):
        EvalCase(id="a", category="c", turns=["q", "  "])


def test_select_filters_and_rejects_unknown_ids():
    cases = [EvalCase(id=i, category=c, turns=["q"], draft=d)
             for i, c, d in [("a", "x", False), ("b", "y", True), ("c", "x", True)]]
    assert [c.id for c in select(cases, categories=["x"])] == ["a", "c"]
    assert [c.id for c in select(cases, include_drafts=False)] == ["a"]
    assert [c.id for c in select(cases, ids=["c", "a"])] == ["a", "c"]
    assert [c.id for c in select(cases, limit=1)] == ["a"]
    with pytest.raises(DatasetError):
        select(cases, ids=["nope"])


def test_write_cases_round_trips(tmp_path):
    cases = [EvalCase(id="a", category="c", turns=["q"], expected_outcome="answer", draft=True)]
    path = tmp_path / "out.jsonl"
    write_cases(path, cases)
    assert load_cases(path) == cases


def test_shipped_dataset_is_valid():
    cases = load_cases(DEFAULT_DATASET)
    assert len(cases) >= 30
    categories = {c.category for c in cases}
    assert {"sso_question", "general_auth", "clarification", "not_supported",
            "conversational", "off_topic", "multi_turn", "adversarial", "onboarding"} <= categories
    # Every expected source is a documentation page.
    for case in cases:
        for url in case.expected_sources:
            assert url.startswith("https://bcgov.github.io/sso-docs/"), (case.id, url)
