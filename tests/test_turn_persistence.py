"""What a turn leaves in storage when the client disconnects mid-turn: the
real create_agent() and ChatService, with a scripted fake model, a fake
search tool and local storage (no AWS calls)."""

import asyncio
from concurrent.futures import Future

import pytest
from strands import ToolContext, tool
from strands.models.model import Model
from strands.storage import LocalFileStorage

from app.agents import rag_agent
from app.services import chat_service as cs
from app.services.chat_service import Status
from app.services.query_rewriter import RewrittenQuery
from app.tools.knowledge_base_tool import CITATIONS_KEY, SEARCH_TURN_KEY

DOC_URL = "https://bcgov.github.io/sso-docs/#requesting-integration"
EVIDENCE = {"title": "Requesting integration", "uri": DOC_URL, "score": 0.9,
            "text": "Sign in to the CSS app and request a new integration."}
CLARIFY = ('Which identity provider are you using?\n'
           '<grounding>{"response_type": "clarification", "citations": []}</grounding>')
HALLUCINATED = ('Go to https://css.pathfinder.gov.bc.ca and set `token_endpoint`.\n'
                '<grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", '
                f'"citations": ["{DOC_URL}"]}}</grounding>')

SEARCH_DELAY = {"seconds": 0.0}


@tool(name="search_knowledge_base", context=True)
async def fake_search(tool_context: ToolContext) -> str:
    """Search."""
    await asyncio.sleep(SEARCH_DELAY["seconds"])
    state = tool_context.invocation_state
    state[CITATIONS_KEY].append(dict(EVIDENCE))
    state[SEARCH_TURN_KEY].searched = True
    return f"[1] Requesting integration\nsource: {DOC_URL}\nscore: 0.900\n{EVIDENCE['text']}"


class ScriptedModel(Model):
    """Each call takes the next step: "tool" (call the search tool),
    "sleep" (hang until cancelled) or a reply text."""

    def __init__(self):
        self.config = {}
        self.steps: list[str] = []

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        if False:
            yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        step = self.steps.pop(0)
        if step == "sleep":
            await asyncio.sleep(30)
        yield {"messageStart": {"role": "assistant"}}
        if step == "tool":
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{len(self.steps)}",
                                                               "name": "search_knowledge_base"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": step}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.fixture
def model(monkeypatch, tmp_path):
    scripted = ScriptedModel()
    monkeypatch.setattr(rag_agent, "model", scripted)
    monkeypatch.setattr(rag_agent, "storage", LocalFileStorage(str(tmp_path)))
    monkeypatch.setattr(rag_agent, "search_knowledge_base", fake_search)
    monkeypatch.setattr(cs, "create_agent", rag_agent.create_agent)
    monkeypatch.setattr(cs.settings, "grounding_repair_enabled", True)
    SEARCH_DELAY["seconds"] = 0.0

    def submit(prompt, history):
        f = Future()
        f.set_result(RewrittenQuery(intent="sso_question", rewritten_query=prompt))
        return f

    monkeypatch.setattr(cs.query_rewriter, "submit", submit)
    return scripted


SESSION = "session_disconnect"


async def _complete_turn(question: str):
    return [item async for item in cs.chat_service.stream(question, SESSION)]


async def _disconnect_turn(question: str, after_status: str, statuses_seen: int = 1):
    """Consumes the turn until `after_status` has been seen `statuses_seen`
    times, then cancels it, as Starlette does when the client goes away."""

    seen = asyncio.Event()

    async def consume():
        count = 0
        async for item in cs.chat_service.stream(question, SESSION):
            if isinstance(item, Status) and item.value == after_status:
                count += 1
                if count >= statuses_seen:
                    seen.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(seen.wait(), 5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _stored_texts() -> list[tuple[str, str]]:
    agent = rag_agent.create_agent(SESSION)
    return [
        (m["role"], " ".join(b["text"] for b in m["content"] if "text" in b))
        for m in agent.messages
    ]


def test_disconnect_during_search_leaves_history_unchanged(model):
    model.steps = ["tool", CLARIFY]
    asyncio.run(_complete_turn("first question"))
    before = _stored_texts()
    assert [role for role, _ in before] == ["user", "assistant"]

    model.steps = ["tool", CLARIFY]
    SEARCH_DELAY["seconds"] = 10
    asyncio.run(_disconnect_turn("second question", "searching"))

    assert _stored_texts() == before


def test_disconnect_during_repair_does_not_keep_the_rejected_answer(model):
    # The first reply fails the grounding check; the client goes away while
    # the repair runs.
    model.steps = ["tool", HALLUCINATED, "sleep"]
    asyncio.run(_disconnect_turn("how do I request an integration?", "writing"))

    texts = " ".join(text for _, text in _stored_texts())
    assert "pathfinder" not in texts
    assert "failed the automatic grounding check" not in texts
