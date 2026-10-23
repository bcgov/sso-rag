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


def test_conversational_skips_search(search):
    result, _ = _call(SearchTurn(prompt="hi", rewrite=_done(RewrittenQuery.conversational())))
    assert result == NO_SEARCH_NEEDED and search.calls == []


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


def test_result_is_cached_per_turn(search):
    turn = SearchTurn(prompt="raw", rewrite=_done(RewrittenQuery(intent="sso_question", rewritten_query="q")))
    citations = []
    first, _ = _call(turn, citations)
    second, _ = _call(turn, citations)
    assert first == second and len(search.calls) == 1 and len(citations) == 1


def test_rewrite_failure_uses_raw_prompt(search):
    failed: Future = Future()
    failed.set_exception(RuntimeError("boom"))
    _call(SearchTurn(prompt="raw prompt", rewrite=failed))
    assert search.calls[0] == ("raw prompt", [("raw prompt", "rewrite")])


def test_rewrite_timeout_uses_raw_prompt(search, monkeypatch):
    monkeypatch.setattr(kbt.settings, "query_rewriter_timeout_seconds", 0.05)
    _call(SearchTurn(prompt="raw prompt", rewrite=Future()))
    assert search.calls[0][0] == "raw prompt"


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
