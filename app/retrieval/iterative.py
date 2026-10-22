"""Multi-round retrieval around the existing search/rerank functions.

Round 1 searches every planned query, merges and dedupes the candidates, and
reranks the combined set. Later rounds run only targeted follow-up searches
for gaps in the evidence, then rerank the union again. The loop stops at the
round limit, when the token or latency budget runs out, or when a round adds
no meaningful new evidence.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.retrieval.knowledge_base import chunk_key, rank, search_round, top_n_for
from app.retrieval.models import RetrievedChunk
from app.schemas.query_plan import GapAssessment, QueryPlan

MAX_ROUND_QUERIES = 6
# Round-1 slots kept for the agent's own queries, so its retry wording is
# always searched even when the plan alone would fill the cap.
RESERVED_AGENT_QUERIES = 2
MAX_FOLLOW_UPS = 3

# (plan, current hits, queries searched so far, uncovered facet queries)
#   -> (assessment, tokens used)
GapCheck = Callable[[QueryPlan, list[RetrievedChunk], list[str], list[str]], tuple[GapAssessment, int]]


@dataclass
class Budget:
    tokens: int
    latency_ms: int
    started: float = field(default_factory=time.monotonic)
    tokens_used: int = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def remaining_s(self, cap_ms: int | None = None) -> float:
        left = self.latency_ms - self.elapsed_ms()
        return max(0, min(left, cap_ms) if cap_ms else left) / 1000

    def spend(self, tokens: int) -> None:
        self.tokens_used += max(0, tokens)

    @property
    def time_left(self) -> bool:
        return self.elapsed_ms() < self.latency_ms

    @property
    def tokens_left(self) -> bool:
        return self.tokens_used < self.tokens


def _dedupe(queries: list[str], exclude: set[str] = frozenset()) -> list[str]:
    seen = {q.lower() for q in exclude}
    out = []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def round_one_queries(plan: QueryPlan, extra: list[str]) -> list[str]:
    """Planned queries in priority order (facets before paraphrases), capped,
    with a few slots reserved for the agent's own queries."""
    facets = _dedupe([plan.canonical_query, *(s.query for s in plan.sub_questions)])
    agent = _dedupe(extra, set(facets))
    reserved = min(RESERVED_AGENT_QUERIES, len(agent))
    head = facets[:MAX_ROUND_QUERIES - reserved] + agent[:reserved]
    rest = _dedupe([*plan.query_variants, *agent[reserved:]], set(head))
    return (head + rest)[:MAX_ROUND_QUERIES]


def uncovered_facets(plan: QueryPlan, hits: list[RetrievedChunk]) -> list[str]:
    """Sub-question queries that contributed no kept hit."""
    found = {q for h in hits for q in h.metadata.get("_queries", [])}
    return [s.query for s in plan.sub_questions if s.query not in found]


def run(
    plan: QueryPlan,
    *,
    per_query: int,
    max_rounds: int,
    min_new_evidence: int,
    budget: Budget,
    retrieval_filter: dict | None = None,
    gap_check: GapCheck | None = None,
    extra_queries: list[str] | None = None,
    hyde_queries: list[str] | None = None,
) -> tuple[list[RetrievedChunk], dict]:
    queries = round_one_queries(plan, extra_queries or [])
    hyde_queries = _dedupe(hyde_queries or [], set(queries))
    top_n = top_n_for(len(plan.sub_questions))
    rank_query = plan.canonical_query

    pool, _ = search_round(
        queries + hyde_queries, per_query, retrieval_filter,
        labels=queries + ["HyDE"] * len(hyde_queries),
    )
    hits, reranked = rank(rank_query, list(pool.values()), top_n)
    searched = list(queries)
    rounds = [{"round": 1, "queries": queries, "candidates": len(pool), "kept": len(hits),
               "ms": budget.elapsed_ms()}]
    stop = "max_rounds" if max_rounds > 1 and gap_check else "single_round"

    for round_no in range(2, max_rounds + 1):
        if gap_check is None:
            break
        if not (budget.time_left and budget.tokens_left):
            stop = "budget"
            break

        uncovered = uncovered_facets(plan, hits)
        if plan.complexity == "simple" and not uncovered and hits:
            stop = "simple_and_covered"
            break

        try:
            gaps, tokens = gap_check(plan, hits, searched, uncovered)
        except Exception as e:  # never fail the search because of the gap check
            rounds.append({"round": round_no, "gap_check_error": type(e).__name__})
            stop = "gap_check_failed"
            break
        budget.spend(tokens)

        follow_ups = _dedupe(gaps.follow_up_queries, set(searched))[:MAX_FOLLOW_UPS]
        if gaps.sufficient or not follow_ups:
            stop = "sufficient" if gaps.sufficient else "no_follow_ups"
            break
        if not budget.time_left:
            stop = "budget"
            break

        pool, new = search_round(follow_ups, per_query, retrieval_filter, pool=pool)
        hits, reranked = rank(rank_query, list(pool.values()), top_n)
        searched += follow_ups
        new_in_top = sum(chunk_key(h) in new for h in hits)
        rounds.append({
            "round": round_no,
            "missing": gaps.missing_facets,
            "queries": follow_ups,
            "new_candidates": len(new),
            "new_in_top": new_in_top,
            "ms": budget.elapsed_ms(),
        })
        if new_in_top < min_new_evidence:
            stop = "no_new_evidence"
            break

    stats = {
        "queries": searched,
        "missing": uncovered_facets(plan, hits),
        "hyde": bool(hyde_queries),
        "candidates": len(pool),
        "reranked": reranked,
        "scores": [round(h.score, 3) for h in hits],
        "rounds": rounds,
        "stop": stop,
        "tokens": budget.tokens_used,
        "ms": budget.elapsed_ms(),
    }
    return hits, stats
