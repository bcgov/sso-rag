import asyncio

from app.common import turn_report
from app.common.turn_report import TurnReport, capture, record
from app.retrieval.models import RetrievedChunk
from app.services import chat_service, query_rewriter
from app.services.grounding import Verdict
from app.services.query_rewriter import QueryRewriter, RewrittenQuery
from app.tools import knowledge_base_tool
from app.tools.knowledge_base_tool import SearchTurn


def test_record_is_a_no_op_without_a_report():
    assert turn_report._current.get() is None
    record("anything", value=1)  # must not raise


def test_capture_collects_events_and_detaches_afterwards():
    with capture() as report:
        record("a", value=1)
        record("b", value=2)
        record("a", value=3)
    record("a", value=4)

    assert [e["event"] for e in report.events] == ["a", "b", "a"]
    assert report.of("a") == [{"event": "a", "value": 1}, {"event": "a", "value": 3}]
    assert report.last("b") == {"event": "b", "value": 2}
    assert report.last("missing") is None
    assert turn_report._current.get() is None


def test_concurrent_turns_keep_separate_reports():
    async def turn(name: str) -> TurnReport:
        with capture() as report:
            for i in range(3):
                record("step", turn=name, i=i)
                await asyncio.sleep(0)
        return report

    async def both():
        return await asyncio.gather(turn("one"), turn("two"))

    one, two = asyncio.run(both())
    assert {e["turn"] for e in one.events} == {"one"}
    assert {e["turn"] for e in two.events} == {"two"}


def test_rewriter_job_thread_records_into_the_callers_report(monkeypatch):
    def transform(self, prompt, history):
        record("rewrite", intent="sso_question", query=prompt)
        return RewrittenQuery(intent="sso_question", rewritten_query=prompt)

    monkeypatch.setattr(QueryRewriter, "transform", transform)
    with capture() as report:
        QueryRewriter().submit("how do I rotate my secret", []).result(timeout=5)

    assert report.last("rewrite")["query"] == "how do I rotate my secret"


def test_greeting_short_circuit_is_recorded():
    with capture() as report:
        QueryRewriter().submit("hi", []).result(timeout=5)

    event = report.last("rewrite")
    assert event["intent"] == query_rewriter.CONVERSATIONAL
    assert event["short_circuit"] is True


def test_model_call_and_attempt_are_recorded():
    run = chat_service._Run(parts=["The answer."])
    retrieved = [{"uri": "https://docs/x", "title": "X", "score": 0.9, "text": "chunk text", "extra": 1}]
    verdict = Verdict("The answer.", "SUPPORTED", [{"uri": "https://docs/x"}], response_type="answer")

    with capture() as report:
        chat_service._log_model_call("first", "end_turn", {"inputTokens": 10, "outputTokens": 5})
        chat_service._trace_attempt("first", "q", run, retrieved, verdict)

    call = report.last("model_call")
    assert call["usage"] == {"inputTokens": 10, "outputTokens": 5}
    attempt = report.last("attempt")
    assert attempt["raw_reply"] == "The answer."
    assert attempt["response_type"] == "answer"
    assert attempt["grounding_status"] == "SUPPORTED"
    assert attempt["reasons"] == []
    assert attempt["replaced"] is False
    assert attempt["citations"] == ["https://docs/x"]
    assert attempt["retrieved"] == [{"uri": "https://docs/x", "title": "X", "score": 0.9, "text": "chunk text"}]


def test_search_is_recorded_with_ranked_hits():
    turn = SearchTurn(prompt="q", rewrite=None, history=[])
    turn.intent = "sso_question"
    hit = RetrievedChunk(text="t", score=0.8, metadata={
        "source_url": "https://docs/page#a", "section": "Page > A", "_queries": ["q1"]})
    stats = {"queries": ["q1", "q2"], "origins": ["rewrite", "sub"], "missing": ["q2"],
             "candidates": 5, "reranked": True}

    with capture() as report:
        knowledge_base_tool._record_search(turn, "question", stats, None, [hit],
                                           waited_ms=3, fallback=False, intent_override=False)

    event = report.last("search")
    assert event["queries"] == [{"text": "q1", "origin": "rewrite"}, {"text": "q2", "origin": "sub"}]
    assert event["missing"] == ["q2"]
    assert event["hits"] == [{"uri": "https://docs/page#a", "title": "Page > A", "score": 0.8, "found_by": ["q1"]}]
    assert event["waited_ms"] == 3
