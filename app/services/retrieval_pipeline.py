"""Query planning + retrieval for search_knowledge_base.

Strategies (settings.query_strategy, overridable per call for evals):
  baseline           the agent's own queries, one round (previous behaviour)
  planner            planner-rewritten queries, one round
  planner_iterative  planner + gap check + targeted follow-up rounds
"""

import logging
from dataclasses import dataclass, field

from app.common.config import settings
from app.retrieval import iterative
from app.retrieval.knowledge_base import multi_search
from app.retrieval.models import RetrievedChunk
from app.schemas.query_plan import QueryPlan, SubQuestion
from app.services import query_planner
from app.services.query_guards import (
    enforce_preservation,
    is_simple_query,
    to_retrieval_filter,
    validate_filters,
)

logger = logging.getLogger(__name__)

STRATEGIES = ("baseline", "planner", "planner_iterative")


@dataclass
class SearchResult:
    hits: list[RetrievedChunk]
    stats: dict
    plan: QueryPlan | None = None
    clarification: str | None = None
    missing: list[str] = field(default_factory=list)


def fast_plan(question: str, draft_question: str | None, draft_queries: list[str]) -> QueryPlan:
    """A plan without an LLM call: the question as written plus the agent's
    own queries, which are already in the docs' vocabulary. Several agent
    queries mean several facets, so each becomes a sub-question (keeping the
    per-facet result slots, coverage NOTE and gap check)."""
    drafts = list(dict.fromkeys(q.strip() for q in draft_queries if q and q.strip()))
    facets = [SubQuestion(facet=q, query=q) for q in drafts[:4]] if len(drafts) > 1 else []
    variants = [q for q in [draft_question, *drafts] if q and q.strip() != question.strip()]
    return QueryPlan(
        original_question=question,
        canonical_query=question,
        query_variants=list(dict.fromkeys(variants))[:4],
        sub_questions=facets,
        complexity="multi_facet" if facets else "simple",
    )


def _gap_check(budget: iterative.Budget) -> iterative.GapCheck:
    def check(plan, hits, searched, uncovered):
        return query_planner.assess_gaps(
            plan, hits, searched, uncovered,
            timeout_s=budget.remaining_s(settings.planner_call_timeout_ms),
        )
    return check


def search(
    question: str,
    queries: list[str],
    messages: list[dict] | None = None,
    strategy: str | None = None,
    hyde: bool | None = None,
) -> SearchResult:
    """`question`/`queries` are the agent's draft; `messages` is the agent's
    conversation, used to recover the user's verbatim message and history."""

    strategy = strategy or settings.query_strategy
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    hyde = settings.hyde_enabled if hyde is None else hyde
    queries = [q for q in queries or [] if q and q.strip()]

    if strategy == "baseline":
        hits, stats = multi_search(question, queries)
        found = {q for h in hits for q in h.metadata.get("_queries", [])}
        missing = [q for q in stats["queries"] if q not in found]
        return SearchResult(hits, {"strategy": strategy, **stats}, missing=missing)

    budget = iterative.Budget(settings.retrieval_token_budget, settings.retrieval_latency_budget_ms)
    raw, history = query_planner.conversation(messages or [])
    original = raw or question

    if is_simple_query(original, bool(history)):
        plan, planner_status = fast_plan(original, question, queries), "skipped"
    else:
        try:
            plan, tokens = query_planner.plan_query(
                original, history, question, queries, settings.planner_history_turns,
                timeout_s=budget.remaining_s(settings.planner_call_timeout_ms),
            )
            budget.spend(tokens)
            planner_status = "ok"
        except Exception as e:  # the planner must never break search
            logger.warning("planner failed (%s); using the agent's queries", type(e).__name__)
            plan, planner_status = fast_plan(original, question, queries), "failed"

    plan = enforce_preservation(plan, original)
    plan = plan.model_copy(update={"filters": validate_filters(plan.filters, original)})

    hyde_queries: list[str] = []
    if hyde and budget.tokens_left:
        try:
            passage, tokens = query_planner.hyde_passage(
                plan.canonical_query, timeout_s=budget.remaining_s(settings.planner_call_timeout_ms)
            )
            budget.spend(tokens)
            if passage:
                hyde_queries.append(passage)
        except Exception as e:
            logger.warning("HyDE failed (%s); continuing without it", type(e).__name__)

    hits, stats = iterative.run(
        plan,
        per_query=settings.candidates_per_query,
        max_rounds=settings.max_retrieval_rounds if strategy == "planner_iterative" else 1,
        min_new_evidence=settings.min_new_evidence,
        budget=budget,
        retrieval_filter=to_retrieval_filter(plan.filters),
        gap_check=_gap_check(budget) if strategy == "planner_iterative" else None,
        extra_queries=queries,
        hyde_queries=hyde_queries,
    )

    stats = {
        "strategy": strategy,
        "planner": planner_status,
        "complexity": plan.complexity,
        "canonical_query": plan.canonical_query,
        "filters": [f.model_dump() for f in plan.filters],
        **stats,
    }
    clarification = None
    if not hits and plan.needs_clarification and plan.clarification_question:
        clarification = plan.clarification_question
    return SearchResult(hits, stats, plan, clarification, missing=stats["missing"])
