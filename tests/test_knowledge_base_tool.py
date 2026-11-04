from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from app.retrieval.models import RetrievedChunk
from app.services.query_rewriter import RewrittenQuery
from app.tools import knowledge_base_tool as kbt
from app.tools.knowledge_base_tool import (
    CITATIONS_KEY,
    NO_MATCH,
    NO_SEARCH_NEEDED,
    SEARCH_TURN_KEY,
    SearchTurn,
    search_knowledge_base,
)


def _done(value) -> Future:
    f: Future = Future()
    f.set_result(value)
    return f


def _hit(text, queries, score=0.8):
    return RetrievedChunk(text=text, score=score, source="s3://x",
                          metadata={"section": f"Sec {text}", "source_url": f"https://docs/{text}",
                                    "_queries": queries})


def _call(turn, citations=None):
    state = {SEARCH_TURN_KEY: turn, CITATIONS_KEY: citations if citations is not None else []}
    return search_knowledge_base(tool_context=SimpleNamespace(invocation_state=state)), state


class FakeSearch:
    """multi_search stand-in: every query finds a chunk named after it,
    except those listed in `empty`."""

    def __init__(self, empty=()):
        self.empty = set(empty)
        self.calls = []

    def __call__(self, question, queries):
        self.calls.append((question, [(q.text, q.origin) for q in queries]))
        hits = [_hit(q.text, [q.text]) for q in queries if q.text not in self.empty]
        stats = {"queries": [q.text for q in queries], "origins": [q.origin for q in queries],
                 "missing": [q.text for q in queries if q.text in self.empty]}
        return hits, stats


@pytest.fixture
def search(monkeypatch):
    fake = FakeSearch()
    monkeypatch.setattr(kbt, "multi_search", fake)
    return fake


def test_tool_takes_no_arguments():
    schema = search_knowledge_base.tool_spec["inputSchema"]["json"]
    assert schema.get("properties", {}) == {} and not schema.get("required")


def test_greeting_skips_search(search):
    turn = SearchTurn(prompt="hi", rewrite=_done(RewrittenQuery.greeting()))
    result, _ = _call(turn)
    assert result == NO_SEARCH_NEEDED and search.calls == [] and not turn.searched
    # Not cached as the turn's search: a second call isn't told ALREADY_SEARCHED.
    assert _call(turn)[0] == NO_SEARCH_NEEDED and turn.result is None


def test_agent_call_overrides_conversational_classification(search):
    history = [("user", "How do I get BCeID approved in TEST?"), ("assistant", "Request it in CSS.")]
    turn = SearchTurn(prompt="what's CSS?", rewrite=_done(RewrittenQuery.conversational()), history=history)
    citations = []
    result, _ = _call(turn, citations)
    query = "How do I get BCeID approved in TEST? what's CSS?"
    assert search.calls[0] == (query, [(query, "rewrite")])
    assert result.startswith("[1]") and citations and turn.searched


def test_searches_rewriter_queries_in_order(search):
    rewrite = RewrittenQuery(intent="sso_question", rewritten_query="main q",
                             step_back_query="background q", sub_queries=["part a", "part b"])
    citations = []
    result, _ = _call(SearchTurn(prompt="raw", rewrite=_done(rewrite)), citations)

    question, queries = search.calls[0]
    assert question == "main q"
    assert queries == [("main q", "rewrite"), ("part a", "sub"), ("part b", "sub"), ("background q", "step_back")]
    assert "[1] Sec main q" in result and "source: https://docs/main q" in result
    assert [c["uri"] for c in citations] == [f"https://docs/{q}" for q, _ in queries]
    assert set(citations[0]) == {"title", "uri", "score", "text"}


def test_searches_once_per_turn(search):
    turn = SearchTurn(prompt="raw", rewrite=_done(RewrittenQuery(intent="sso_question", rewritten_query="q")))
    citations = []
    first, _ = _call(turn, citations)
    second, _ = _call(turn, citations)
    assert first.startswith("[1]")
    assert second == kbt.ALREADY_SEARCHED
    assert len(search.calls) == 1 and len(citations) == 1


def test_rewrite_failure_uses_raw_prompt(search):
    failed: Future = Future()
    failed.set_exception(RuntimeError("boom"))
    turn = SearchTurn(prompt="raw prompt", rewrite=failed)
    _call(turn)
    assert search.calls[0] == ("raw prompt", [("raw prompt", "rewrite")])
    assert turn.searched


def test_rewrite_failure_on_follow_up_uses_history(search):
    failed: Future = Future()
    failed.set_exception(RuntimeError("boom"))
    history = [("user", "How do I get BCeID approved in TEST?"), ("assistant", "Request it in CSS.")]
    _call(SearchTurn(prompt="what about prod?", rewrite=failed, history=history))
    assert search.calls[0][0] == "How do I get BCeID approved in TEST? what about prod?"


def test_rewrite_timeout_uses_raw_prompt(search, monkeypatch):
    monkeypatch.setattr(kbt.settings, "query_rewriter_timeout_seconds", 0.05)
    _call(SearchTurn(prompt="raw prompt", rewrite=Future()))
    assert search.calls[0][0] == "raw prompt"


def test_rewrite_timeout_on_follow_up_uses_history(search, monkeypatch):
    monkeypatch.setattr(kbt.settings, "query_rewriter_timeout_seconds", 0.05)
    history = [("user", "IDIR login in TEST"), ("assistant", "Use IDIR - MFA.")]
    _call(SearchTurn(prompt="what about prod?", rewrite=Future(), history=history))
    assert search.calls[0][0] == "IDIR login in TEST what about prod?"


def test_retry_rewords_failed_facets_once(search, monkeypatch):
    search.empty = {"part b", "background q"}
    retries = []
    monkeypatch.setattr(kbt.query_rewriter, "retry", lambda q, failed: retries.append(failed) or ["part b reworded"])
    rewrite = RewrittenQuery(intent="sso_question", rewritten_query="main q",
                             step_back_query="background q", sub_queries=["part a", "part b"])
    result, _ = _call(SearchTurn(prompt="raw", rewrite=_done(rewrite)))

    # Step-back misses don't trigger a retry.
    assert retries == [["part b"]]
    assert search.calls[1] == ("main q", [("part b reworded", "retry")])
    assert "Sec part b reworded" in result and "NOTE" not in result


def test_note_for_uncovered_sub_query(search, monkeypatch):
    search.empty = {"part b", "part b reworded"}
    monkeypatch.setattr(kbt.query_rewriter, "retry", lambda q, failed: ["part b reworded"])
    rewrite = RewrittenQuery(intent="sso_question", rewritten_query="main q", sub_queries=["part a", "part b"])
    result, _ = _call(SearchTurn(prompt="raw", rewrite=_done(rewrite)))
    assert 'NOTE: the documentation returned nothing for: "part b"' in result
    assert "PARTIALLY_SUPPORTED" in result


def test_no_match(search, monkeypatch):
    search.empty = {"main q"}
    monkeypatch.setattr(kbt.query_rewriter, "retry", lambda q, failed: [])
    citations = []
    result, _ = _call(SearchTurn(prompt="raw", rewrite=_done(
        RewrittenQuery(intent="sso_question", rewritten_query="main q"))), citations)
    assert result == NO_MATCH and citations == []


def test_missing_turn_returns_no_match(search):
    result = search_knowledge_base(tool_context=SimpleNamespace(invocation_state={}))
    assert result == NO_MATCH and search.calls == []
