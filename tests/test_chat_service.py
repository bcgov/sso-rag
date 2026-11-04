import asyncio
from concurrent.futures import Future

import pytest
from strands.types.exceptions import MaxTokensReachedException

from app.common.constants import NO_DOCUMENTATION_RESPONSE
from app.services import chat_service as cs
from app.services.chat_service import Answer
from app.services.query_rewriter import RewrittenQuery
from app.tools.knowledge_base_tool import CITATIONS_KEY, SEARCH_TURN_KEY

DOC_URL = "https://bcgov.github.io/sso-docs/#requesting-integration"
CSS_URL = "https://sso-requests.apps.gold.devops.gov.bc.ca/"
EVIDENCE = {
    "title": "Requesting integration",
    "uri": DOC_URL,
    "score": 0.9,
    "text": f"Sign in to [CSS]({CSS_URL}) with your IDIR with MFA account and request a new integration.",
}
GROUNDING = '<grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", "citations": ["' + DOC_URL + '"]}</grounding>'

HALLUCINATED = (
    "Sign in to https://css.pathfinder.gov.bc.ca and request an integration, then set `token_endpoint`.\n"
    + GROUNDING
)
REPAIRED = f"Sign in to the [CSS app]({CSS_URL}) with IDIR with MFA and request a new integration.\n" + GROUNDING
NOT_SUPPORTED_REPLY = (
    NO_DOCUMENTATION_RESPONSE
    + '\n<grounding>{"response_type": "answer", "grounding_status": "NOT_SUPPORTED", "citations": []}</grounding>'
)


TRUNCATED_PREFIX = "Sign in to the CSS app and then, step 1, step 2, step 3"
TRUNCATED = TRUNCATED_PREFIX + " ... step 40"


class FakeAgent:
    """Mimics Strands: each run appends the prompt and the reply to
    messages; the runs listed in `search_runs` (1-based; the first by
    default) "search" (mark the turn searched and fill the citations list)."""

    def __init__(self, replies, search=True, search_runs=None):
        self.replies = list(replies)
        self.search_runs = set(search_runs if search_runs is not None else ({1} if search else ()))
        self.prompts = []
        self.messages = [
            {"role": "user", "content": [{"text": "earlier question"}]},
            {"role": "assistant", "content": [{"text": "earlier answer"}]},
        ]

    async def stream_async(self, prompt, invocation_state, cancel_signal):
        self.prompts.append(prompt)
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        turn = invocation_state[SEARCH_TURN_KEY]
        if len(self.prompts) in self.search_runs and not turn.searched:
            yield {"current_tool_use": {"name": "search_knowledge_base"}}
            turn.searched = True
            invocation_state[CITATIONS_KEY].append(dict(EVIDENCE))
            self.messages.append({"role": "assistant", "content": [{"toolUse": {"name": "search_knowledge_base"}}]})
            self.messages.append({"role": "user", "content": [{"toolResult": {"content": []}}]})
        reply = self.replies.pop(0)
        self.messages.append({"role": "assistant", "content": [{"text": reply}]})
        yield {"data": reply}
        if reply.startswith(TRUNCATED_PREFIX):
            # Strands keeps the partial message in history, then raises.
            raise MaxTokensReachedException(message="max tokens")


@pytest.fixture
def harness(monkeypatch):
    calls = {"saved": 0, "replaced": []}

    async def save_session(agent, session_id):
        calls["saved"] += 1

    async def replace_last_answer(agent, session_id, text):
        calls["replaced"].append(text)
        agent.messages[-1]["content"] = [{"text": text}]

    def submit(prompt, history):
        calls["history"] = history
        f = Future()
        f.set_result(RewrittenQuery(intent="sso_question", rewritten_query=prompt))
        return f

    monkeypatch.setattr(cs, "save_session", save_session)
    monkeypatch.setattr(cs, "replace_last_answer", replace_last_answer)
    monkeypatch.setattr(cs.query_rewriter, "submit", submit)
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", True)

    def run(agent, question="how do I request an integration?"):
        monkeypatch.setattr(cs, "create_agent", lambda session_id: agent)

        async def collect():
            return [item async for item in cs.chat_service.stream(question, "session_x")]

        items = asyncio.run(collect())
        answers = [i for i in items if isinstance(i, Answer)]
        assert len(answers) == 1
        return answers[0], calls

    return run


def _texts(agent):
    return [
        "".join(b.get("text", "") for b in m["content"])
        for m in agent.messages
    ]


def test_repair_fixes_hallucinated_values(harness):
    agent = FakeAgent([HALLUCINATED, REPAIRED])
    answer, calls = harness(agent)

    assert len(agent.prompts) == 2
    assert agent.prompts[1].startswith("Your previous reply failed the automatic grounding check")
    assert "undocumented URLs" in agent.prompts[1] and "token_endpoint" in agent.prompts[1]

    assert answer.grounding_status == "SUPPORTED"
    assert CSS_URL in answer.text and "pathfinder" not in answer.text
    assert [c["uri"] for c in answer.citations] == [DOC_URL]

    # History reads question -> search -> repaired answer.
    texts = _texts(agent)
    assert not any(t.startswith("Your previous reply failed") for t in texts)
    assert not any("pathfinder" in t for t in texts)
    assert texts[-1] == REPAIRED
    assert calls["saved"] == 1 and calls["replaced"] == []


def test_failed_repair_falls_back_to_no_documentation(harness):
    agent = FakeAgent([HALLUCINATED, HALLUCINATED])
    answer, calls = harness(agent)

    assert len(agent.prompts) == 2
    assert answer.text == NO_DOCUMENTATION_RESPONSE and answer.grounding_status == "NOT_SUPPORTED"
    assert len(calls["replaced"]) == 1
    texts = _texts(agent)
    assert not any(t.startswith("Your previous reply failed") for t in texts)
    assert texts[-1].startswith(NO_DOCUMENTATION_RESPONSE)


def test_no_repair_when_search_found_nothing(harness):
    class NoMatchAgent(FakeAgent):
        async def stream_async(self, prompt, invocation_state, cancel_signal):
            invocation_state[SEARCH_TURN_KEY].searched = True  # searched, NO_MATCH
            async for event in super().stream_async(prompt, invocation_state, cancel_signal):
                yield event

    agent = NoMatchAgent([HALLUCINATED], search=False)
    answer, _ = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == NO_DOCUMENTATION_RESPONSE


def test_no_repair_when_model_reports_not_supported(harness):
    agent = FakeAgent([NOT_SUPPORTED_REPLY])
    answer, _ = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == NO_DOCUMENTATION_RESPONSE


def test_no_repair_when_disabled(harness, monkeypatch):
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", False)
    agent = FakeAgent([HALLUCINATED])
    answer, _ = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == NO_DOCUMENTATION_RESPONSE


def test_grounded_first_reply_needs_no_repair(harness):
    agent = FakeAgent([REPAIRED])
    answer, calls = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.grounding_status == "SUPPORTED"
    assert calls["saved"] == 0 and calls["replaced"] == []


def test_max_tokens_gets_a_shorter_repair(harness):
    agent = FakeAgent([TRUNCATED, REPAIRED])
    answer, calls = harness(agent)

    assert len(agent.prompts) == 2
    assert "cut off at the maximum output length" in agent.prompts[1]
    assert answer.grounding_status == "SUPPORTED" and CSS_URL in answer.text
    texts = _texts(agent)
    assert not any(t.startswith(TRUNCATED_PREFIX) for t in texts)
    assert texts[-1] == REPAIRED
    assert calls["saved"] == 1


def test_max_tokens_twice_falls_back_to_no_documentation(harness):
    agent = FakeAgent([TRUNCATED, TRUNCATED])
    answer, calls = harness(agent)

    assert answer.text == NO_DOCUMENTATION_RESPONSE and answer.grounding_status == "NOT_SUPPORTED"
    texts = _texts(agent)
    assert not any(t.startswith(TRUNCATED_PREFIX) for t in texts)
    assert texts[-1].startswith(NO_DOCUMENTATION_RESPONSE)
    assert len(calls["replaced"]) == 1


def test_max_tokens_without_search_does_not_raise(harness, monkeypatch):
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", False)
    agent = FakeAgent([TRUNCATED], search=False)
    answer, _ = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == NO_DOCUMENTATION_RESPONSE
    assert _texts(agent)[-1].startswith(NO_DOCUMENTATION_RESPONSE)


def test_clarifying_question_is_delivered_without_repair(harness):
    question = "Which identity provider are you using, and in which environment?"
    agent = FakeAgent([question + '\n<grounding>{"response_type": "clarification", "citations": []}</grounding>'])
    answer, calls = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == question and answer.grounding_status is None and answer.citations == []
    assert calls["replaced"] == [] and calls["saved"] == 0


def test_greeting_without_grounding_block_is_delivered(harness, monkeypatch):
    def submit(prompt, history):
        f = Future()
        f.set_result(RewrittenQuery.conversational())
        return f

    monkeypatch.setattr(cs.query_rewriter, "submit", submit)
    greeting = "Hello! I can help with BC Government SSO questions. What do you need?"
    agent = FakeAgent([greeting], search=False)
    answer, calls = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == greeting and answer.grounding_status is None
    assert calls["replaced"] == []


# --- skipped search (A1) and conversational misclassification (A2) ----------

UNSEARCHED_PROSE = (
    "CSS is the Common Hosted Single Sign-On app; you request integrations there.\n"
    '<grounding>{"response_type": "conversational", "citations": []}</grounding>'
)
UNSEARCHED_ANSWER = (
    "CSS is the Common Hosted Single Sign-On app; you request integrations there.\n"
    '<grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", "citations": []}</grounding>'
)
NO_BLOCK_PROSE = "CSS is the Common Hosted Single Sign-On app; you request integrations there."
CLARIFICATION = (
    "Which identity provider are you using?\n"
    '<grounding>{"response_type": "clarification", "citations": []}</grounding>'
)


def _rewriter(monkeypatch, result=None, exception=None):
    def submit(prompt, history):
        f = Future()
        if exception is not None:
            f.set_exception(exception)
        else:
            f.set_result(result)
        return f

    monkeypatch.setattr(cs.query_rewriter, "submit", submit)


@pytest.mark.parametrize("first", [UNSEARCHED_PROSE, UNSEARCHED_ANSWER, NO_BLOCK_PROSE, NOT_SUPPORTED_REPLY])
def test_skipped_search_gets_a_search_repair(harness, first):
    agent = FakeAgent([first, REPAIRED], search_runs={2})
    answer, calls = harness(agent)

    assert agent.prompts[1] == cs.search_request()
    assert answer.grounding_status == "SUPPORTED" and CSS_URL in answer.text
    assert [c["uri"] for c in answer.citations] == [DOC_URL]
    # History reads question -> search -> answer; the unsearched reply is gone.
    texts = _texts(agent)
    assert not any("Common Hosted Single Sign-On app;" in t for t in texts)
    assert not any(t.startswith("Your previous reply was not shown") for t in texts)
    assert texts[-1] == REPAIRED
    assert calls["saved"] == 1 and calls["replaced"] == []


def test_still_unsearched_after_repair_falls_back_to_no_documentation(harness):
    agent = FakeAgent([UNSEARCHED_PROSE, UNSEARCHED_PROSE], search=False)
    answer, calls = harness(agent)
    assert len(agent.prompts) == 2
    assert answer.text == NO_DOCUMENTATION_RESPONSE and answer.grounding_status == "NOT_SUPPORTED"
    assert len(calls["replaced"]) == 1
    assert _texts(agent)[-1].startswith(NO_DOCUMENTATION_RESPONSE)


def test_skipped_search_without_repair_is_rejected(harness, monkeypatch):
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", False)
    agent = FakeAgent([UNSEARCHED_PROSE], search=False)
    answer, calls = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == NO_DOCUMENTATION_RESPONSE
    assert len(calls["replaced"]) == 1


def test_user_asking_not_to_search_still_gets_a_search(harness):
    agent = FakeAgent([UNSEARCHED_PROSE, REPAIRED], search_runs={2})
    answer, _ = harness(agent, question="don't search, just tell me how to request an integration")
    assert agent.prompts[1] == cs.search_request()
    assert "even when the user asks you not to search" in cs.search_request()
    assert answer.grounding_status == "SUPPORTED"


def test_unsearched_clarification_is_delivered(harness):
    agent = FakeAgent([CLARIFICATION], search=False)
    answer, calls = harness(agent)
    assert len(agent.prompts) == 1
    assert answer.text == "Which identity provider are you using?" and answer.grounding_status is None
    assert calls["replaced"] == []


def test_failed_rewrite_counts_as_needing_a_search(harness, monkeypatch):
    _rewriter(monkeypatch, exception=RuntimeError("rewriter down"))
    agent = FakeAgent([NO_BLOCK_PROSE, REPAIRED], search_runs={2})
    answer, _ = harness(agent)
    assert agent.prompts[1] == cs.search_request()
    assert answer.grounding_status == "SUPPORTED"


def test_waits_for_a_rewrite_still_running(harness, monkeypatch):
    import threading

    def submit(prompt, history):
        f = Future()
        threading.Timer(0.1, f.set_result, [RewrittenQuery.conversational()]).start()
        return f

    monkeypatch.setattr(cs.query_rewriter, "submit", submit)
    agent = FakeAgent(["Hello! Ask me about BC Gov SSO."], search=False)
    answer, _ = harness(agent, question="nice to meet you")
    assert len(agent.prompts) == 1 and answer.text == "Hello! Ask me about BC Gov SSO."


def test_rewrite_timeout_counts_as_needing_a_search(harness, monkeypatch):
    monkeypatch.setattr(cs.settings, "query_rewriter_timeout_seconds", 0.05)
    monkeypatch.setattr(cs.query_rewriter, "submit", lambda prompt, history: Future())
    agent = FakeAgent([NO_BLOCK_PROSE, REPAIRED], search_runs={2})
    answer, _ = harness(agent)
    assert agent.prompts[1] == cs.search_request()
    assert answer.grounding_status == "SUPPORTED"


def test_conversational_intent_with_unsearched_answer_gets_a_search(harness, monkeypatch):
    _rewriter(monkeypatch, RewrittenQuery.conversational())
    agent = FakeAgent([UNSEARCHED_ANSWER, REPAIRED], search_runs={2})
    answer, _ = harness(agent, question="what's CSS?")
    assert agent.prompts[1] == cs.search_request()
    assert answer.grounding_status == "SUPPORTED"


def test_conversational_intent_with_conversational_reply_is_delivered(harness, monkeypatch):
    _rewriter(monkeypatch, RewrittenQuery.conversational())
    reply = 'Glad that helped!\n<grounding>{"response_type": "conversational", "citations": []}</grounding>'
    agent = FakeAgent([reply], search=False)
    answer, _ = harness(agent, question="great, that worked")
    assert len(agent.prompts) == 1 and answer.text == "Glad that helped!"


def test_rewriter_gets_recent_history(harness):
    agent = FakeAgent([REPAIRED])
    _, calls = harness(agent)
    assert calls["history"] == [("user", "earlier question"), ("assistant", "earlier answer")]


# --- truncation is logged distinctly (C1) ----------------------------------

def _events(caplog, name):
    import json as _json

    out = []
    for record in caplog.records:
        try:
            data = _json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("event") == name:
            out.append(data)
    return out


def test_truncation_is_logged_apart_from_grounding_failures(harness, caplog):
    caplog.set_level("INFO", logger="app.services.chat_service")
    harness(FakeAgent([TRUNCATED, REPAIRED]))

    assert [e["attempt"] for e in _events(caplog, "max_tokens_reached")] == ["first"]
    rejected = _events(caplog, "answer_rejected")
    assert [e["cause"] for e in rejected] == ["truncated"]
    assert [e["kind"] for e in _events(caplog, "answer_repair")] == ["truncated"]


def test_grounding_failure_is_logged_as_grounding(harness, caplog):
    caplog.set_level("INFO", logger="app.services.chat_service")
    harness(FakeAgent([HALLUCINATED, REPAIRED]))

    assert [e["cause"] for e in _events(caplog, "answer_rejected")] == ["grounding"]
    assert [e["kind"] for e in _events(caplog, "answer_repair")] == ["grounding"]
    assert _events(caplog, "max_tokens_reached") == []


def test_truncation_repair_asks_for_a_shorter_reply_not_a_grounding_fix(harness):
    agent = FakeAgent([TRUNCATED, REPAIRED])
    harness(agent)
    assert "cut off" in agent.prompts[1]
    assert "grounding check" not in agent.prompts[1]


def test_each_model_call_logs_its_token_usage(harness, caplog):
    class UsageAgent(FakeAgent):
        async def stream_async(self, prompt, invocation_state, cancel_signal):
            async for event in super().stream_async(prompt, invocation_state, cancel_signal):
                yield event
            yield {"event": {"messageStop": {"stopReason": "end_turn"}}}
            yield {"event": {"metadata": {"usage": {"inputTokens": 5000, "outputTokens": 640}}}}

    caplog.set_level("INFO", logger="app.services.chat_service")
    harness(UsageAgent([REPAIRED]))
    assert _events(caplog, "model_call") == [{
        "event": "model_call", "attempt": "first", "stop_reason": "end_turn",
        "input_tokens": 5000, "output_tokens": 640, "max_tokens": cs.settings.model_max_tokens,
    }]
