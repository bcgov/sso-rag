import pytest

from app.retrieval import knowledge_base as kb
from app.retrieval.models import RetrievedChunk, SearchQuery


def _chunk(text, score):
    return RetrievedChunk(text=text, score=score, source=f"s3://{text}", metadata={"source_url": f"https://docs/{text}"})


@pytest.fixture
def no_reranker(monkeypatch):
    monkeypatch.setattr(kb.settings, "reranker_model_arn", None)
    monkeypatch.setattr(kb.settings, "number_of_results", 25)
    monkeypatch.setattr(kb.settings, "number_of_reranked_results", 6)


def test_multi_search_dedupes_caps_and_tags(monkeypatch, no_reranker):
    seen = []

    def retrieve(query, max_results=None):
        seen.append((query, max_results))
        return [_chunk(f"{query[:4]}-1", 0.5), _chunk("shared", 0.4)]

    monkeypatch.setattr(kb, "retrieve", retrieve)
    queries = [SearchQuery(text="aaaa one", origin="rewrite"),
               SearchQuery(text="AAAA  one", origin="sub"),
               *[SearchQuery(text=f"q{i:03} sub", origin="sub") for i in range(4)],
               SearchQuery(text="bbbb back", origin="step_back"),
               SearchQuery(text="cccc extra", origin="sub")]

    hits, stats = kb.multi_search("question", queries)

    assert stats["queries"] == ["aaaa one", "q000 sub", "q001 sub", "q002 sub", "q003 sub", "bbbb back"]
    assert stats["origins"] == ["rewrite", "sub", "sub", "sub", "sub", "step_back"]
    assert all(n == max(5, 25 // 6) for _, n in seen)
    # 5 facets (rewrite + 4 sub; step-back excluded) -> 6 + 2*4 = 14, capped at 12.
    assert len(hits) <= kb.MAX_RERANKED
    shared = next(h for h in hits if h.text == "shared")
    assert len(shared.metadata["_queries"]) == 6


def test_multi_search_step_back_does_not_widen(monkeypatch, no_reranker):
    monkeypatch.setattr(kb, "retrieve", lambda q, n=None: [_chunk(f"{q}-{i}", 1 - i / 100) for i in range(20)])
    hits, _ = kb.multi_search("q", [SearchQuery(text="main", origin="rewrite"),
                                    SearchQuery(text="background", origin="step_back")])
    assert len(hits) == 6


def test_multi_search_reports_missing(monkeypatch):
    monkeypatch.setattr(kb.settings, "reranker_model_arn", "arn:reranker")
    monkeypatch.setattr(kb, "retrieve", lambda q, n=None: [_chunk(q, 0.5)])
    # The reranker drops the "part b" chunk below the minimum score.
    monkeypatch.setattr(kb, "rerank", lambda question, chunks, top_n: [
        c.model_copy(update={"score": 0.9 if c.text != "part b" else 0.0}) for c in chunks
    ])
    monkeypatch.setattr(kb.settings, "min_rerank_score", 0.05)

    hits, stats = kb.multi_search("q", [SearchQuery(text="part a", origin="sub"),
                                        SearchQuery(text="part b", origin="sub")])
    assert [h.text for h in hits] == ["part a"]
    assert stats["missing"] == ["part b"] and stats["reranked"]


def test_multi_search_empty_queries_uses_question(monkeypatch, no_reranker):
    seen = []
    monkeypatch.setattr(kb, "retrieve", lambda q, n=None: seen.append(q) or [])
    hits, stats = kb.multi_search("the question", [SearchQuery(text="  ", origin="sub")])
    assert seen == ["the question"] and hits == [] and stats["missing"] == ["the question"]


def test_merge_hits_dedupes_and_sorts():
    a = _chunk("x", 0.3).model_copy(update={"metadata": {"_queries": ["q1"]}})
    b = _chunk("x", 0.7).model_copy(update={"metadata": {"_queries": ["q2"]}})
    c = _chunk("y", 0.5).model_copy(update={"metadata": {"_queries": ["q2"]}})
    merged = kb.merge_hits([a], [b, c])
    assert [(h.text, h.score) for h in merged] == [("x", 0.7), ("y", 0.5)]
    assert merged[0].metadata["_queries"] == ["q1", "q2"]
    assert a.metadata["_queries"] == ["q1"]  # inputs untouched
    assert len(kb.merge_hits([a, c], [], limit=1)) == 1
