import json
import time

import pytest

from app.services import query_rewriter as qr
from app.services.query_rewriter import (
    GENERAL_AUTH,
    SSO_QUESTION,
    QueryRewriter,
    RewrittenQuery,
    fallback_query,
    is_greeting,
    parse_retry,
    parse_rewrite,
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
    "hi how do I add a redirect URI",
    "hello\nhow do I add a redirect URI?",
    "Thanks!\n\nHow do I get BCeID?",
    "thanks. one more: what about prod?",
    "hey! what is CSS?",
    "who are you? what is IDIR",
    "good morning team, my login is broken",
    "ok so how do I rotate the secret",
    "thanks, and what about prod?",
    "how do I get BCeID in prod",
    "what can you tell me about IDIR?",
    "",
])
def test_questions_are_not_greetings(text):
    assert not is_greeting(text)


# --- parsing ------------------------------------------------------------------

def _reply(**fields) -> str:
    return json.dumps({"intent": "sso_question", "query": "main query", "bcgov_query": "",
                       "step_back": "", "sub_queries": [], **fields})


def test_parse_rewrite_plain_and_fenced():
    reply = _reply(query="  How do I  add a redirect URI? ")
    expected = RewrittenQuery(intent=SSO_QUESTION, rewritten_query="How do I add a redirect URI?")
    assert parse_rewrite(reply) == expected
    fenced = "Here you go:\n```json\n" + reply + "\n```"
    assert parse_rewrite(fenced) == expected


def test_parse_rewrite_all_fields():
    reply = _reply(query="Business BCeID PROD approval", step_back="IdP production approval process",
                   sub_queries=["a b", "c d", "A B"])
    assert parse_rewrite(reply) == RewrittenQuery(
        intent=SSO_QUESTION,
        rewritten_query="Business BCeID PROD approval",
        step_back_query="IdP production approval process",
        sub_queries=["a b", "c d"],
    )


@pytest.mark.parametrize("subs, expected", [
    (["only one"], []),
    ([], []),
    (["q1", "q2", "q3", "q4", "q5"], ["q1", "q2", "q3", "q4"]),
    ("not a list", []),
])
def test_parse_rewrite_sub_queries(subs, expected):
    assert parse_rewrite(_reply(sub_queries=subs)).sub_queries == expected


def test_parse_rewrite_general_auth_keeps_bcgov_query():
    reply = _reply(intent="general_auth", query="refresh token rotation for a SPA",
                   bcgov_query="refresh token lifespan for a public client in CSS")
    result = parse_rewrite(reply)
    assert result.intent == GENERAL_AUTH
    assert result.rewritten_query == "refresh token rotation for a SPA"
    assert result.bcgov_query == "refresh token lifespan for a public client in CSS"


def test_parse_rewrite_bcgov_query_only_for_general_auth():
    assert parse_rewrite(_reply(bcgov_query="something")).bcgov_query is None
    # A BC Gov query identical to the query adds nothing.
    assert parse_rewrite(_reply(intent="general_auth", query="x y", bcgov_query="X Y")).bcgov_query is None


def test_parse_rewrite_step_back_equal_to_query_is_dropped():
    assert parse_rewrite(_reply(query="redirect URIs", step_back="Redirect URIs")).step_back_query is None


def test_parse_rewrite_conversational_and_off_topic():
    assert parse_rewrite(_reply(intent="conversational", query="")) == RewrittenQuery.conversational()
    assert parse_rewrite(_reply(intent="off_topic", query="")) == RewrittenQuery.off_topic()


def test_parse_rewrite_unknown_intent_is_question():
    assert parse_rewrite(_reply(intent="maybe", query="IDIR login")).intent == SSO_QUESTION


def test_parse_rewrite_accepts_the_old_field_name():
    assert parse_rewrite('{"intent": "sso_question", "rewritten_query": "IDIR login"}').rewritten_query == "IDIR login"


@pytest.mark.parametrize("reply", [
    "not json", "[]", '{"intent": "sso_question"}', '{"intent": "sso_question", "query": 5}', "",
    '{"intent": "general_auth", "query": ""}',
])
def test_parse_rewrite_unusable(reply):
    assert parse_rewrite(reply) is None


def test_parse_rewrite_truncates_long_query():
    assert len(parse_rewrite(_reply(query="x" * 1000)).rewritten_query) == qr.MAX_QUERY_CHARS


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


# --- fallback query -----------------------------------------------------------

def test_fallback_query_without_history_is_the_prompt():
    assert fallback_query("  what   about prod? ", []) == "what about prod?"
    assert fallback_query("what about prod?") == "what about prod?"


def test_fallback_query_prepends_recent_user_messages():
    history = [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "How do I get BCeID in TEST?"),
        ("assistant", "Request it in the CSS app."),
        ("user", "and for IDIR?"),
        ("assistant", "Same steps."),
    ]
    assert fallback_query("what about prod?", history) == \
        "How do I get BCeID in TEST? and for IDIR? what about prod?"


def test_fallback_query_keeps_the_most_recent_context_within_the_limit():
    history = [("user", "older " * 100), ("user", "recent topic " + "x " * 200)]
    query = fallback_query("what about prod?", history)
    assert len(query) <= qr.MAX_QUERY_CHARS
    assert query.startswith("recent topic") and query.endswith("what about prod?")
    assert "older" not in query


def test_fallback_query_keeps_a_long_prompt_whole():
    prompt = "word " * 80
    assert fallback_query(prompt, [("user", "earlier")]) == prompt.strip()


# --- transform ----------------------------------------------------------------

def _fake_converse(replies: dict, delays: dict | None = None, errors: set | None = None, calls: list | None = None):
    systems = {qr.REWRITE_SYSTEM: "rewrite", qr.RETRY_SYSTEM: "retry"}

    def converse(system, user, max_tokens):
        name = systems[system]
        if calls is not None:
            calls.append((name, user))
        if errors and name in errors:
            raise RuntimeError(f"{name} failed")
        if delays and name in delays:
            time.sleep(delays[name])
        return replies[name], {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 8}

    return converse


GOOD = {
    "rewrite": json.dumps({
        "intent": "sso_question",
        "query": "Business BCeID PROD approval",
        "bcgov_query": "",
        "step_back": "CSS app integration approval process",
        "sub_queries": ["BCeID PROD approval", "service account client roles"],
    }),
    "retry": '{"retry_queries": ["BCeID production request"]}',
}


def test_transform_makes_one_call_for_all_techniques(monkeypatch):
    calls = []
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, calls=calls))
    result = QueryRewriter().transform("bceid prod + roles?", [])
    assert result == RewrittenQuery(
        intent=SSO_QUESTION,
        rewritten_query="Business BCeID PROD approval",
        step_back_query="CSS app integration approval process",
        sub_queries=["BCeID PROD approval", "service account client roles"],
    )
    assert [name for name, _ in calls] == ["rewrite"]


def test_transform_sends_matching_vocabulary(monkeypatch):
    calls = []
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, calls=calls))
    QueryRewriter().transform("where is my client secret?", [])
    (_, user), = calls
    assert "-> client secret (confidential client)" in user
    assert "-> CSS team" not in user


def test_transform_conversational_drops_queries(monkeypatch):
    replies = {**GOOD, "rewrite": '{"intent": "conversational", "query": ""}'}
    monkeypatch.setattr(qr, "_converse", _fake_converse(replies))
    assert QueryRewriter().transform("nice weather", []) == RewrittenQuery.conversational()


def test_transform_off_topic(monkeypatch):
    replies = {**GOOD, "rewrite": '{"intent": "off_topic", "query": ""}'}
    monkeypatch.setattr(qr, "_converse", _fake_converse(replies))
    assert QueryRewriter().transform("write a poem", []) == RewrittenQuery.off_topic()


def test_transform_rewrite_error_falls_back_to_raw_prompt(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, errors={"rewrite"}))
    result = QueryRewriter().transform("  raw prompt ", [])
    assert result == RewrittenQuery(intent=SSO_QUESTION, rewritten_query="raw prompt", fallback=True)


FOLLOW_UP_HISTORY = [("user", "How do I get BCeID in TEST?"), ("assistant", "Request it in the CSS app.")]


def test_transform_rewrite_error_on_follow_up_uses_history(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, errors={"rewrite"}))
    result = QueryRewriter().transform("what about prod?", FOLLOW_UP_HISTORY)
    assert result == RewrittenQuery(
        intent=SSO_QUESTION,
        rewritten_query="How do I get BCeID in TEST? what about prod?",
        fallback=True,
    )


def test_transform_timeout_uses_history(monkeypatch):
    monkeypatch.setattr(qr.settings, "query_rewriter_timeout_seconds", 0.2)
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD, delays={"rewrite": 1}))
    result = QueryRewriter().transform("what about prod?", FOLLOW_UP_HISTORY)
    assert result.fallback and result.rewritten_query.endswith("what about prod?")


def test_transform_unexpected_error_uses_history(monkeypatch):
    def boom(*_):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(QueryRewriter, "_transform", boom)
    result = QueryRewriter().transform("what about prod?", FOLLOW_UP_HISTORY)
    assert result == RewrittenQuery.raw("what about prod?", FOLLOW_UP_HISTORY)
    assert result.rewritten_query == "How do I get BCeID in TEST? what about prod?"


def test_transform_invalid_json_falls_back(monkeypatch):
    monkeypatch.setattr(qr, "_converse", _fake_converse({**GOOD, "rewrite": "I think..."}))
    assert QueryRewriter().transform("q?", []).fallback


def test_transform_respects_toggles(monkeypatch):
    monkeypatch.setattr(qr.settings, "query_rewriter_step_back_enabled", False)
    monkeypatch.setattr(qr.settings, "query_rewriter_decomposition_enabled", False)
    monkeypatch.setattr(qr, "_converse", _fake_converse(GOOD))
    result = QueryRewriter().transform("q?", [])
    assert result == RewrittenQuery(intent=SSO_QUESTION, rewritten_query="Business BCeID PROD approval")


# --- prompt caching -----------------------------------------------------------

class FakeBedrock:
    def __init__(self):
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        return {"output": {"message": {"content": [{"text": "{}"}]}},
                "usage": {"inputTokens": 3, "outputTokens": 1}}


@pytest.mark.parametrize("model_id, enabled, cached", [
    ("global.amazon.nova-2-lite-v1:0", True, True),
    ("us.anthropic.claude-haiku-4-5", True, True),
    ("global.amazon.nova-2-lite-v1:0", False, False),
    ("meta.llama3-8b-instruct-v1:0", True, False),
])
def test_converse_adds_a_cache_point_only_for_caching_models(monkeypatch, model_id, enabled, cached):
    fake = FakeBedrock()
    monkeypatch.setattr(qr, "get_bedrock_model_client", lambda: fake)
    monkeypatch.setattr(qr.settings, "query_rewriter_model_id", model_id)
    monkeypatch.setattr(qr.settings, "query_rewriter_prompt_caching", enabled)
    text, usage = qr._converse("system text", "user text", 50)
    (request,) = fake.requests
    assert request["system"][0] == {"text": "system text"}
    assert (request["system"][1:] == [{"cachePoint": {"type": "default"}}]) is cached
    assert len(request["system"]) == (2 if cached else 1)
    assert text == "{}" and usage == {"inputTokens": 3, "outputTokens": 1}


def test_submit_short_circuits_greetings(monkeypatch):
    def boom(*_):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(qr, "_converse", boom)
    future = QueryRewriter().submit("Thanks!", [])
    assert future.done() and future.result() == RewrittenQuery.greeting()
    assert future.result().short_circuit


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
