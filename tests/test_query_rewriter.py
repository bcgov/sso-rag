import json
import time

import pytest

from app.services import query_rewriter as qr
from app.services.query_rewriter import (
    CONVERSATIONAL,
    SSO_QUESTION,
    QueryRewriter,
    RewrittenQuery,
    is_greeting,
    parse_retry,
    parse_rewrite,
    parse_step_back,
    parse_sub_queries,
    recent_history,
)

# --- greeting short-circuit ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "hi", "Hello!", "hey there", "Good morning", "thanks", "Thank you so much!",
    "ok thanks", "what can you do?", "What can you help me with", "who are you",
])
def test_greetings_are_detected(text):
    assert is_greeting(text)


@pytest.mark.parametrize("text", [
    "hi, how do I add a redirect URI?",
    "thanks, and what about prod?",
    "how do I get BCeID in prod",
    "what can you tell me about IDIR?",
    "",
])
def test_questions_are_not_greetings(text):
    assert not is_greeting(text)


# --- parsing ------------------------------------------------------------------

def test_parse_rewrite_plain_and_fenced():
    reply = '{"intent": "sso_question", "rewritten_query": "  How do I  add a redirect URI? "}'
    assert parse_rewrite(reply) == (SSO_QUESTION, "How do I add a redirect URI?")
    fenced = "Here you go:\n```json\n" + reply + "\n```"
    assert parse_rewrite(fenced) == (SSO_QUESTION, "How do I add a redirect URI?")


def test_parse_rewrite_conversational():
    assert parse_rewrite('{"intent": "conversational", "rewritten_query": ""}') == (CONVERSATIONAL, "")


def test_parse_rewrite_unknown_intent_is_question():
    assert parse_rewrite('{"intent": "maybe", "rewritten_query": "IDIR login"}') == (SSO_QUESTION, "IDIR login")


@pytest.mark.parametrize("reply", [
    "not json", "[]", '{"intent": "sso_question"}', '{"intent": "sso_question", "rewritten_query": 5}', "",
])
def test_parse_rewrite_unusable(reply):
    assert parse_rewrite(reply) is None


def test_parse_rewrite_truncates_long_query():
    reply = json.dumps({"intent": "sso_question", "rewritten_query": "x" * 1000})
    assert len(parse_rewrite(reply)[1]) == qr.MAX_QUERY_CHARS


def test_parse_step_back():
    assert parse_step_back('{"step_back_query": "redirect URI configuration"}') == "redirect URI configuration"
    assert parse_step_back('{"step_back_query": ""}') is None
    assert parse_step_back("nope") is None


def test_parse_sub_queries():
    assert parse_sub_queries('{"sub_queries": ["a b", "c d", "A B"]}') == ["a b", "c d"]
    assert parse_sub_queries('{"sub_queries": ["only one"]}') == []
    assert parse_sub_queries('{"sub_queries": []}') == []
    assert parse_sub_queries('{"sub_queries": ["q1", "q2", "q3", "q4", "q5"]}') == ["q1", "q2", "q3", "q4"]
    assert parse_sub_queries('{"other": 1}') is None
    assert parse_sub_queries("garbage") is None


def test_parse_retry_limits_to_failed_count():
    assert parse_retry('{"retry_queries": ["a1", "b2", "c3"]}', 2) == ["a1", "b2"]
    assert parse_retry("bad", 2) == []


# --- history ------------------------------------------------------------------

def test_recent_history_skips_tools_strips_grounding_and_limits():
    messages = [
        {"role": "user", "content": [{"text": "old question"}]},
        {"role": "assistant", "content": [{"text": "old answer"}]},
        {"role": "user", "content": [{"text": "How do I get BCeID?"}]},
        {"role": "assistant", "content": [{"toolUse": {"name": "search_knowledge_base"}}]},
        {"role": "user", "content": [{"toolResult": {"content": []}}]},
        {"role": "assistant", "content": [{"text": 'Request it in the CSS app.\n<grounding>{"a": 1}</grounding>'}]},
    ]
    assert recent_history(messages, 1) == [
        ("user", "How do I get BCeID?"),
        ("assistant", "Request it in the CSS app."),
    ]
    assert recent_history(messages, 0) == []
    assert len(recent_history(messages, 5)) == 4


def test_recent_history_truncates():
    messages = [{"role": "user", "content": [{"text": "word " * 400}]}]
    (_, text), = recent_history(messages, 1)
    assert len(text) <= qr.HISTORY_MESSAGE_CHARS + 1 and text.endswith("…")


# --- transform ----------------------------------------------------------------

def _fake_converse(replies: dict, delays: dict | None = None, errors: set | None = None):
    systems = {
        qr.REWRITE_SYSTEM: "rewrite",
        qr.STEP_BACK_SYSTEM: "step_back",
        qr.DECOMPOSE_SYSTEM: "decomposition",
        qr.RETRY_SYSTEM: "retry",
    }

    def converse(system, user, max_tokens):
        name = systems[system]
        if errors and name in errors:
            raise RuntimeError(f"{name} failed")
        if delays and name in delays:
            time.sleep(delays[name])
        return replies[name]

    return converse


GOOD = {
    "rewrite": '{"intent": "sso_question", "rewritten_query": "Business BCeID PROD approval"}',
    "step_back": '{"step_back_query": "CSS app integration approval process"}',
    "decomposition": '{"sub_queries": ["BCeID PROD approval", "service account client roles"]}',
    "retry": '{"retry_queries": ["BCeID production request"]}',
}


def test_transform_runs_all_techniques(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD))
    result = QueryRewriter().transform("bceid prod + roles?", [])
    assert result == RewrittenQuery(
        intent=SSO_QUESTION,
        rewritten_query="Business BCeID PROD approval",
        step_back_query="CSS app integration approval process",
        sub_queries=["BCeID PROD approval", "service account client roles"],
    )


def test_transform_conversational_drops_queries(monkeypatch):
    replies = {**GOOD, "rewrite": '{"intent": "conversational", "rewritten_query": ""}'}
    monkeypatch.setattr(qr, "_converse", _fake_converse(replies))
    assert QueryRewriter().transform("nice weather", []) == RewrittenQuery.conversational()


def test_transform_rewrite_error_falls_back_to_raw_prompt(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, errors={"rewrite"}))
    result = QueryRewriter().transform("  raw prompt ", [])
    assert result.fallback and result.intent == SSO_QUESTION
    assert result.rewritten_query == "raw prompt"
    # Other techniques still contribute.
    assert result.sub_queries == ["BCeID PROD approval", "service account client roles"]


def test_transform_invalid_json_falls_back(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse({**GOOD, "rewrite": "I think..."}))
    assert QueryRewriter().transform("q?", []).fallback


def test_transform_timeouts_are_dropped(monkeypatch):
    monkeypatch.setattr(qr.settings, "query_rewriter_timeout_seconds", 0.2)
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, delays={"step_back": 1, "decomposition": 1}))
    result = QueryRewriter().transform("q?", [])
    assert not result.fallback
    assert result.step_back_query is None and result.sub_queries == []


def test_transform_respects_toggles(monkeypatch):
    calls = []

    def converse(system, user, max_tokens):
        calls.append(system)
        return GOOD["rewrite"]

    monkeypatch.setattr(qr.settings, "query_rewriter_step_back_enabled", False)
    monkeypatch.setattr(qr.settings, "query_rewriter_decomposition_enabled", False)
    monkeypatch.setattr(qr, "_converse", converse)
    QueryRewriter().transform("q?", [])
    assert calls == [qr.REWRITE_SYSTEM]


def test_submit_short_circuits_greetings(monkeypatch):
    def boom(*_):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(qr, "_converse", boom)
    future = QueryRewriter().submit("Thanks!", [])
    assert future.done() and future.result() == RewrittenQuery.conversational()


def test_submit_runs_transform(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD))
    assert QueryRewriter().submit("bceid prod?", []).result(timeout=5).rewritten_query == "Business BCeID PROD approval"


# --- retry --------------------------------------------------------------------

def test_retry(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD))
    assert QueryRewriter().retry("q", ["failed one"]) == ["BCeID production request"]


def test_retry_failure_and_disabled(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, errors={"retry"}))
    assert QueryRewriter().retry("q", ["failed"]) == []
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD))
    monkeypatch.setattr(qr.settings, "query_rewriter_retry_enabled", False)
    assert QueryRewriter().retry("q", ["failed"]) == []
    assert QueryRewriter().retry("q", []) == []
