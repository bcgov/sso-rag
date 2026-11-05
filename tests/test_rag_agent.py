"""The real create_agent() with a fake model and local storage: what the model
is actually sent, across turns restored from snapshots like production."""

import asyncio

import pytest
from strands import tool
from strands.models.model import Model
from strands.storage import LocalFileStorage

from app.agents import rag_agent

# Six search results of ~1,200 characters each: a typical search result
# (median chunk ~1,130 characters) and well over 1,500 estimated tokens.
SEARCH_RESULT = "\n\n---\n\n".join(
    f"[{i}] Section {i}\nsource: https://bcgov.github.io/sso-docs/{i}\nscore: 0.900\n" + f"fact{i} " * 190
    for i in range(1, 7)
)


@tool(name="search_knowledge_base")
def fake_search() -> str:
    """Search."""
    return SEARCH_RESULT


class FakeModel(Model):
    """Calls search_knowledge_base once per user question, then answers;
    records what every call was sent."""

    def __init__(self):
        self.config = {}
        self.calls: list[dict] = []

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        if False:
            yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({
            "messages": len(messages),
            "tools": [t["name"] for t in tool_specs or []],
            "tool_results": [
                "".join(c.get("text", "") for c in block["toolResult"]["content"])
                for m in messages for block in m["content"] if "toolResult" in block
            ],
        })
        last = messages[-1]["content"]
        yield {"messageStart": {"role": "assistant"}}
        if not any("toolResult" in b for b in last):
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"t{len(self.calls)}", "name": "search_knowledge_base"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": "An answer of a few sentences. " * 10}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    model = FakeModel()
    monkeypatch.setattr(rag_agent, "model", model)
    monkeypatch.setattr(rag_agent, "storage", LocalFileStorage(str(tmp_path)))
    monkeypatch.setattr(rag_agent, "search_knowledge_base", fake_search)
    return model


def _turn(question: str):
    # One request: a fresh agent restored from the stored snapshot, saved
    # explicitly at the end as ChatService does.
    agent = rag_agent.create_agent("session_test")
    agent(question)
    asyncio.run(rag_agent.save_session(agent, "session_test"))


def test_model_sees_the_whole_search_result_this_turn(fake):
    _turn("how do I request an integration?")
    answer_call = fake.calls[1]
    assert answer_call["tool_results"] == [SEARCH_RESULT]


def test_model_is_offered_only_the_search_tool(fake):
    _turn("how do I request an integration?")
    assert all(call["tools"] == ["search_knowledge_base"] for call in fake.calls)


def test_history_sent_to_the_model_is_bounded(fake):
    for i in range(30):
        _turn(f"question {i}")
    # 30 turns x 4 messages = 120 without a cap.
    assert fake.calls[-1]["messages"] <= 40


def test_earlier_turns_search_results_are_not_resent(fake):
    for i in range(3):
        _turn(f"question {i}")
    # Only this turn's results count as evidence; older ones are dead weight.
    assert len(fake.calls[-1]["tool_results"]) == 1


def test_window_setting_caps_history(fake, monkeypatch):
    monkeypatch.setattr(rag_agent.settings, "conversation_window_messages", 6)
    for i in range(10):
        _turn(f"question {i}")
    # Restored history (at most 6 messages) + this turn's question, call and result.
    assert fake.calls[-1]["messages"] <= 6 + 3


def test_restored_history_alternates_and_keeps_answers(fake):
    for i in range(3):
        _turn(f"question {i}")
    agent = rag_agent.create_agent("session_test")
    assert [m["role"] for m in agent.messages] == ["user", "assistant"] * 3
    assert agent.messages[0]["content"][0]["text"] == "question 0"


# --- drop_tool_exchanges ------------------------------------------------------

def _msg(role, *blocks):
    return {"role": role, "content": list(blocks)}


Q = {"text": "question"}
ANSWER = {"text": "answer"}
TOOL_USE = {"toolUse": {"toolUseId": "t", "name": "search_knowledge_base", "input": {}}}
TOOL_RESULT = {"toolResult": {"toolUseId": "t", "content": [{"text": "results"}], "status": "success"}}


def test_drops_tool_calls_and_results():
    messages = [
        _msg("user", Q),
        _msg("assistant", {"text": "<thinking>search</thinking>"}, TOOL_USE),
        _msg("user", TOOL_RESULT),
        _msg("assistant", ANSWER),
    ]
    rag_agent.drop_tool_exchanges(messages)
    assert messages == [_msg("user", Q), _msg("assistant", ANSWER)]


def test_interrupted_turn_is_merged_to_keep_roles_alternating():
    messages = [
        _msg("user", {"text": "Hello"}),
        _msg("assistant", TOOL_USE),
        _msg("user", TOOL_RESULT),
        _msg("user", {"text": "Hello"}),
        _msg("assistant", ANSWER),
    ]
    rag_agent.drop_tool_exchanges(messages)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == [{"text": "Hello"}, {"text": "Hello"}]


def test_history_starts_with_a_user_message():
    messages = [_msg("assistant", TOOL_USE), _msg("user", TOOL_RESULT), _msg("assistant", ANSWER),
                _msg("user", Q), _msg("assistant", ANSWER)]
    rag_agent.drop_tool_exchanges(messages)
    assert messages == [_msg("user", Q), _msg("assistant", ANSWER)]


def test_no_tool_messages_is_unchanged():
    messages = [_msg("user", Q), _msg("assistant", ANSWER)]
    rag_agent.drop_tool_exchanges(messages)
    assert messages == [_msg("user", Q), _msg("assistant", ANSWER)]
