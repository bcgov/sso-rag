import pytest

import app.common.config  # noqa: F401  (the collect guard test needs an app module loaded)
from evals import collect


def _events():
    return [
        {"event": "rewrite", "intent": "sso_question", "query": "rotate secret", "status": "ok",
         "latency_ms": 420, "usage": {"inputTokens": 2000, "outputTokens": 60, "cacheReadInputTokens": 1900}},
        {"event": "model_call", "attempt": "first", "usage": {"inputTokens": 3000, "outputTokens": 100}},
        {"event": "search", "intent": "sso_question", "hits": [{"uri": "https://d/a"}]},
        {"event": "model_call", "attempt": "first",
         "usage": {"inputTokens": 5000, "outputTokens": 400, "cacheReadInputTokens": 2500}},
        {"event": "attempt", "attempt": "first", "raw_reply": "bad", "response_type": "answer",
         "grounding_status": "NOT_SUPPORTED", "reasons": ["undocumented URLs"], "replaced": True,
         "citations": [], "retrieved": [{"uri": "https://d/a", "title": "A", "score": 0.9, "text": "chunk"}]},
        {"event": "rewrite_retry", "usage": {"inputTokens": 400, "outputTokens": 20}},
        {"event": "model_call", "attempt": "repair", "usage": {"inputTokens": 6000, "outputTokens": 300}},
        {"event": "attempt", "attempt": "repair", "raw_reply": "good", "response_type": "answer",
         "grounding_status": "SUPPORTED", "reasons": [], "replaced": False,
         "citations": ["https://d/a"], "retrieved": [{"uri": "https://d/a"}]},
    ]


def test_summarize_turn_after_a_repair():
    answer = {"text": "good", "grounding_status": "SUPPORTED", "citations": ["https://d/a"]}
    turn = collect.summarize_turn("how do I rotate?", answer, _events(), latency_ms=9000)

    assert turn["outcome"] == "answer"
    assert turn["intent"] == "sso_question"
    assert turn["rewrite"]["query"] == "rotate secret"
    assert "usage" not in turn["rewrite"]
    assert turn["searched"] is True
    assert turn["retrieved"] == [{"uri": "https://d/a", "title": "A", "score": 0.9, "text": "chunk"}]
    assert [a["attempt"] for a in turn["attempts"]] == ["first", "repair"]
    assert "retrieved" not in turn["attempts"][0]
    assert turn["repair_attempted"] is True
    assert turn["tokens"] == {
        "agent_calls": 3, "agent_input": 14000, "agent_output": 800,
        "agent_cache_read": 2500, "agent_cache_write": 0,
        "rewriter_calls": 2, "rewriter_input": 2400, "rewriter_output": 80, "rewriter_cache_read": 1900,
    }
    assert turn["rewrite_latency_ms"] == 420
    assert turn["error"] is None


def test_summarize_turn_without_an_answer():
    turn = collect.summarize_turn("q", None, [], latency_ms=5, error="TurnTimeout: too slow")
    assert turn["outcome"] is None
    assert turn["searched"] is False
    assert turn["error"] == "TurnTimeout: too slow"


@pytest.mark.parametrize("status, attempts, link_only, expected", [
    ("NOT_SUPPORTED", [{"response_type": "answer"}], False, "not_supported"),
    ("GENERAL", [{"response_type": "general"}], False, "general"),
    ("SUPPORTED", [{"response_type": "answer"}], False, "answer"),
    (None, [{"response_type": "clarification"}], False, "clarification"),
    (None, [{"response_type": "conversational"}], False, "conversational"),
    (None, [], True, "clarification"),
])
def test_outcome_of(status, attempts, link_only, expected):
    assert collect.outcome_of(status, attempts, link_only) == expected


def test_environment_must_be_prepared_before_app_import():
    # app.common.config is imported above, so the guard refuses (and leaves
    # os.environ alone).
    with pytest.raises(RuntimeError, match="app modules imported"):
        collect._prepare_environment("/tmp/x")


@pytest.mark.parametrize("label, expected", [
    ("feat/deepevals-setup", "feat-deepevals-setup"),
    ("my branch!", "my-branch"),
    ("main", "main"),
    ("", ""),
])
def test_label_is_safe_for_a_directory_name(label, expected):
    assert collect.safe_label(label) == expected
