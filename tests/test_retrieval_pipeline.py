from types import SimpleNamespace

import pytest

from app.retrieval import iterative, knowledge_base
from app.retrieval.models import RetrievedChunk
from app.schemas.query_plan import GapAssessment, QueryPlan, SubQuestion
from app.services import query_planner, retrieval_pipeline


def chunk(text: str, score: float = 0.5, url: str = "https://docs/x") -> RetrievedChunk:
    return RetrievedChunk(text=text, score=score, source=url, metadata={"source_url": url})


class FakeKB:
    """Stands in for Bedrock retrieve: maps query substrings to chunks."""

    def __init__(self, corpus: dict[str, list[RetrievedChunk]]):
        self.corpus = corpus
        self.calls: list[tuple[str, int, dict | None]] = []

    def retrieve(self, query, max_results=None, retrieval_filter=None):
        self.calls.append((query, max_results, retrieval_filter))
        if retrieval_filter and retrieval_filter.get("equals", {}).get("value") == "empty":
            return []
        return [c for key, cs in self.corpus.items() if key in query.lower() for c in cs]


@pytest.fixture
def kb(monkeypatch):
    fake = FakeKB({
        "session": [chunk("SSO session idle timeout is 30 minutes", 0.9, "https://docs/sessions"),
                    chunk("Session max is 10 hours", 0.8, "https://docs/sessions")],
        "second application": [chunk("Add another integration to the same project", 0.7, "https://docs/teams")],
    })
    monkeypatch.setattr(knowledge_base, "retrieve", fake.retrieve)
    monkeypatch.setattr(knowledge_base.settings, "reranker_model_arn", None)
    return fake


def budget(tokens=10_000, ms=60_000):
    return iterative.Budget(tokens, ms)


# -- search_round / merge ---------------------------------------------------

def test_search_round_dedupes_and_tags_queries(kb):
    pool, new = knowledge_base.search_round(["sso session", "session timeout"], 15)
    assert len(pool) == 2 and new == set(pool)
    first = next(c for c in pool.values() if "idle" in c.text)
    assert first.metadata["_queries"] == ["sso session", "session timeout"]
    assert all(call[1] == 15 for call in kb.calls)


def test_search_round_retries_unfiltered_when_filter_finds_nothing(kb):
    pool, _ = knowledge_base.search_round(["session"], 10, {"equals": {"key": "k", "value": "empty"}})
    assert len(pool) == 2
    assert [c[2] for c in kb.calls] == [{"equals": {"key": "k", "value": "empty"}}, None]


# -- iterative loop ---------------------------------------------------------

def multi_plan() -> QueryPlan:
    return QueryPlan(
        original_question="How do sessions work with a second app?",
        canonical_query="sso session lifetime",
        query_variants=["session timeout"],
        sub_questions=[SubQuestion(facet="sessions", query="sso session"),
                       SubQuestion(facet="2nd app", query="register app two")],
        complexity="multi_facet",
    )


def test_follow_up_round_targets_gap_and_reranks_union(kb):
    seen = {}

    def gap_check(plan, hits, searched, uncovered):
        seen["uncovered"] = uncovered
        return GapAssessment(missing_facets=["second app"], follow_up_queries=["second application integration"],
                             sufficient=False), 100

    b = budget()
    hits, stats = iterative.run(multi_plan(), per_query=15, max_rounds=2, min_new_evidence=1,
                                budget=b, gap_check=gap_check)
    assert seen["uncovered"] == ["register app two"]
    assert any("another integration" in h.text for h in hits)
    assert stats["rounds"][1]["queries"] == ["second application integration"]
    assert stats["rounds"][1]["new_in_top"] == 1
    assert stats["tokens"] == 100
    assert stats["stop"] == "max_rounds"


def test_stops_when_new_round_adds_no_evidence(kb):
    gap = lambda *a: (GapAssessment(follow_up_queries=["session idle"], sufficient=False), 10)  # noqa: E731
    _, stats = iterative.run(multi_plan(), per_query=15, max_rounds=3, min_new_evidence=1,
                             budget=budget(), gap_check=gap)
    assert stats["stop"] == "no_new_evidence"
    assert len(stats["rounds"]) == 2


def test_stops_when_sufficient(kb):
    gap = lambda *a: (GapAssessment(sufficient=True), 10)  # noqa: E731
    _, stats = iterative.run(multi_plan(), per_query=15, max_rounds=2, min_new_evidence=1,
                             budget=budget(), gap_check=gap)
    assert stats["stop"] == "sufficient"


def test_stops_on_budget(kb):
    called = []
    _, stats = iterative.run(multi_plan(), per_query=15, max_rounds=2, min_new_evidence=1,
                             budget=budget(tokens=0), gap_check=lambda *a: called.append(1))
    assert stats["stop"] == "budget" and not called


def test_gap_check_failure_keeps_round_one(kb):
    def boom(*a):
        raise RuntimeError("model down")
    hits, stats = iterative.run(multi_plan(), per_query=15, max_rounds=2, min_new_evidence=1,
                                budget=budget(), gap_check=boom)
    assert hits and stats["stop"] == "gap_check_failed"


def test_simple_covered_plan_skips_gap_check(kb):
    plan = QueryPlan(canonical_query="sso session", complexity="simple")
    _, stats = iterative.run(plan, per_query=15, max_rounds=2, min_new_evidence=1, budget=budget(),
                             gap_check=lambda *a: pytest.fail("should not be called"))
    assert stats["stop"] == "simple_and_covered"


def test_round_one_queries_prioritise_facets_and_reserve_agent_slots():
    plan = QueryPlan(canonical_query="a", query_variants=["v1", "v2", "v3", "v4"],
                     sub_questions=[SubQuestion(facet="f", query="s1"), SubQuestion(facet="g", query="s2")])
    assert iterative.round_one_queries(plan, ["B", "extra"]) == ["a", "s1", "s2", "B", "extra", "v1"]
    full = plan.model_copy(update={"sub_questions": [SubQuestion(facet=str(i), query=f"s{i}") for i in range(4)]})
    assert iterative.round_one_queries(full, ["retry1", "retry2"])[-2:] == ["retry1", "retry2"]
    assert iterative.round_one_queries(plan, []) == ["a", "s1", "s2", "v1", "v2", "v3"]


def test_fast_path_keeps_agent_facets(kb, monkeypatch):
    monkeypatch.setattr(query_planner, "assess_gaps", lambda *a, **k: (GapAssessment(sufficient=True), 1))
    q = "BCeID PROD approval and service account client roles"
    result = retrieval_pipeline.search(q, ["sso session", "nothing here"], strategy="planner_iterative")
    assert result.stats["planner"] == "skipped"
    assert [s.query for s in result.plan.sub_questions] == ["sso session", "nothing here"]
    assert result.missing == ["nothing here"]


# -- pipeline ---------------------------------------------------------------

def messages(*turns):
    return [{"role": r, "content": [{"text": t}]} for r, t in turns]


def test_conversation_extracts_latest_user_message_and_cleans_history():
    msgs = messages(
        ("user", "How do I get BCeID approval?"),
        ("assistant", "<thinking>x</thinking>Submit a request.\n<grounding>{}</grounding>"),
        ("user", "what about prod?"),
    ) + [{"role": "assistant", "content": [{"toolUse": {"name": "search_knowledge_base"}}]}]
    raw, history = query_planner.conversation(msgs)
    assert raw == "what about prod?"
    assert [(t.role, t.text) for t in history] == [
        ("user", "How do I get BCeID approval?"), ("assistant", "Submit a request.")
    ]


def test_pipeline_fast_path_skips_planner(kb, monkeypatch):
    monkeypatch.setattr(query_planner, "plan_query", lambda *a, **k: pytest.fail("planner called"))
    result = retrieval_pipeline.search("sso session timeout", ["session max"], messages=None,
                                       strategy="planner_iterative")
    assert result.stats["planner"] == "skipped"
    assert result.hits


def test_pipeline_uses_planner_for_follow_up_and_preserves_original(kb, monkeypatch):
    captured = {}

    def fake_plan(question, history, *a, **k):
        captured["question"], captured["history"] = question, history
        return QueryPlan(canonical_query="BCeID PROD session approval", query_variants=["sso session prod"]), 42

    monkeypatch.setattr(query_planner, "plan_query", fake_plan)
    msgs = messages(("user", "How do I get BCeID approval?"), ("assistant", "Submit a request."),
                    ("user", "what about prod?"))
    result = retrieval_pipeline.search("BCeID prod approval", ["bceid prod"], messages=msgs, strategy="planner")
    assert captured["question"] == "what about prod?"
    assert len(captured["history"]) == 2
    assert result.plan.original_question == "what about prod?"
    assert result.stats["planner"] == "ok" and result.stats["tokens"] == 42
    assert result.stats["stop"] == "single_round"


def test_pipeline_falls_back_when_planner_fails(kb, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("throttled")
    monkeypatch.setattr(query_planner, "plan_query", boom)
    q = "How do sessions work and also how do I register a second application in the same project?"
    result = retrieval_pipeline.search(q, ["sso session"], strategy="planner")
    assert result.stats["planner"] == "failed"
    assert result.hits


def test_pipeline_returns_clarification_only_without_hits(kb, monkeypatch):
    monkeypatch.setattr(query_planner, "plan_query", lambda *a, **k: (QueryPlan(
        canonical_query="nothing matches", needs_clarification=True,
        clarification_question="Which environment?"), 1))
    q = "It is broken again and also the thing from before, can you check it for me please?"
    result = retrieval_pipeline.search(q, [], strategy="planner")
    assert result.clarification == "Which environment?"


def test_baseline_strategy_matches_previous_behaviour(kb):
    result = retrieval_pipeline.search("q", ["sso session", "nothing here"], strategy="baseline")
    assert result.stats["strategy"] == "baseline"
    assert result.missing == ["nothing here"]


# -- tool -------------------------------------------------------------------

def test_tool_output_format(kb, monkeypatch):
    from app.tools import knowledge_base_tool as tool_mod
    citations: list = []
    ctx = SimpleNamespace(agent=SimpleNamespace(messages=messages(("user", "sso session timeout"))),
                          invocation_state={tool_mod.CITATIONS_KEY: citations})
    fn = tool_mod.search_knowledge_base._tool_func
    out = fn(question="sso session timeout", queries=["session"], tool_context=ctx)
    assert out.startswith("[1] ")
    assert "source: https://docs/sessions" in out
    assert citations and citations[0]["uri"] == "https://docs/sessions"


# -- loop guards ------------------------------------------------------------

def tool_use_msg(n=1):
    return {"role": "assistant", "content": [
        {"toolUse": {"name": "search_knowledge_base", "toolUseId": str(i), "input": {}}} for i in range(n)
    ]}


def tool_result_msg():
    return {"role": "user", "content": [{"toolResult": {"toolUseId": "0", "content": []}}]}


def test_searches_this_turn_counts_only_since_latest_user_message():
    from app.tools.knowledge_base_tool import searches_this_turn
    msgs = messages(("user", "old question")) + [tool_use_msg(), tool_result_msg()]
    msgs += messages(("user", "new question")) + [tool_use_msg(2), tool_result_msg(), tool_use_msg()]
    assert searches_this_turn(msgs) == 3
    assert searches_this_turn([]) == 0


def test_tool_refuses_after_search_limit(kb, monkeypatch):
    from app.tools import knowledge_base_tool as tool_mod
    monkeypatch.setattr(tool_mod.settings, "max_searches_per_turn", 2)
    monkeypatch.setattr(tool_mod, "search", lambda *a, **k: pytest.fail("searched past the limit"))
    msgs = messages(("user", "q")) + [tool_use_msg(), tool_result_msg(), tool_use_msg(), tool_result_msg(),
                                      tool_use_msg()]
    ctx = SimpleNamespace(agent=SimpleNamespace(messages=msgs), invocation_state={})
    out = tool_mod.search_knowledge_base._tool_func(question="q", queries=["q"], tool_context=ctx)
    assert out.startswith("SEARCH_LIMIT")


def test_invoke_times_out_and_signals_cancel():
    import threading
    cancelled = threading.Event()

    def slow_agent(prompt, cancel_signal, **kw):
        cancel_signal.wait(5)
        cancelled.set()

    with pytest.raises(TimeoutError):
        query_planner._invoke(slow_agent, "p", 0.05)
    assert cancelled.wait(1)


def test_invoke_with_no_budget_left_raises_immediately():
    with pytest.raises(TimeoutError):
        query_planner._invoke(lambda *a, **k: pytest.fail("called"), "p", 0)


def test_planner_timeout_falls_back_to_agent_queries(kb, monkeypatch):
    def slow(*a, **k):
        raise TimeoutError("model call exceeded")
    monkeypatch.setattr(query_planner, "plan_query", slow)
    q = "How do sessions work and also how do I register a second application in the same project?"
    result = retrieval_pipeline.search(q, ["sso session"], strategy="planner_iterative")
    assert result.stats["planner"] == "failed" and result.hits


def test_budget_remaining_is_capped():
    b = iterative.Budget(tokens=1, latency_ms=10_000)
    assert 0 < b.remaining_s(2_000) <= 2.0
    assert iterative.Budget(tokens=1, latency_ms=0).remaining_s(2_000) == 0
