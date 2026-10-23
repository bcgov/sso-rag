import asyncio
from concurrent.futures import Future

import pytest
from strands.types.exceptions import MaxTokensReachedException

from app.common.constants import NO_DOCUMENTATION_RESPONSE
from app.services import chat_service as cs
from app.services.chat_service import Answer
from app.services.query_rewriter import RewrittenQuery
from app.tools.knowledge_base_tool import CITATIONS_KEY

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
    messages; the first run "searches" (fills the citations list)."""

    def __init__(self, replies, search=True):
        self.replies = list(replies)
        self.search = search
        self.prompts = []
        self.messages = [
            {"role": "user", "content": [{"text": "earlier question"}]},
            {"role": "assistant", "content": [{"text": "earlier answer"}]},
        ]

    async def stream_async(self, prompt, invocation_state, cancel_signal):
        self.prompts.append(prompt)
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        if self.search and len(self.prompts) == 1:
            yield {"current_tool_use": {"name": "search_knowledge_base"}}
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
        f = Future()
        f.set_result(RewrittenQuery(intent="sso_question", rewritten_query=prompt))
        return f

    monkeypatch.setattr(cs, "save_session", save_session)
    monkeypatch.setattr(cs, "replace_last_answer", replace_last_answer)
    monkeypatch.setattr(cs.query_rewriter, "submit", submit)
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", True)

    def run(agent):
        monkeypatch.setattr(cs, "create_agent", lambda session_id: agent)

        async def collect():
            return [item async for item in cs.chat_service.stream("how do I request an integration?", "session_x")]

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


def test_no_repair_without_retrieval(harness):
    agent = FakeAgent([HALLUCINATED], search=False)
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


def test_max_tokens_without_retrieval_does_not_raise(harness):
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
