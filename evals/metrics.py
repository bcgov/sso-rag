"""Deterministic metrics: plain functions over a collected turn (see
evals/collect.py) and its case. No model calls; no app or DeepEval imports.

Each check returns (score, reason) with score in [0, 1], or None when it
doesn't apply to the case.
"""

import math
import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from evals.dataset import EvalCase

# Mirrors app.services.grounding._BCGOV_PART_RE (tests/test_evals_metrics.py
# keeps them in step): where a general answer's "For BC Gov teams" part starts.
BCGOV_PART_RE = re.compile(r"^[ \t>#*_]*for bc gov(?:ernment)? teams\b", re.I | re.M)

Check = Callable[[EvalCase, dict, int], tuple[float, str] | None]


def page_key(url: str | None) -> str:
    """A documentation URL reduced to its page: no fragment, query, trailing
    slash or case, so every chunk of a page matches the page."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/").lower()


def ranked_pages(uris: Iterable[str | None]) -> list[str]:
    """Distinct pages in retrieval order (a page's best chunk gives its rank)."""
    pages: list[str] = []
    for uri in uris:
        key = page_key(uri)
        if key and key not in pages:
            pages.append(key)
    return pages


def hit_at_k(retrieved: list[str | None], expected: list[str], k: int) -> float:
    wanted = {page_key(u) for u in expected}
    return 1.0 if wanted & set(ranked_pages(retrieved)[:k]) else 0.0


def recall_at_k(retrieved: list[str | None], expected: list[str], k: int) -> float:
    wanted = {page_key(u) for u in expected}
    if not wanted:
        return 0.0
    return len(wanted & set(ranked_pages(retrieved)[:k])) / len(wanted)


def reciprocal_rank(retrieved: list[str | None], expected: list[str]) -> float:
    wanted = {page_key(u) for u in expected}
    for rank, page in enumerate(ranked_pages(retrieved), 1):
        if page in wanted:
            return 1.0 / rank
    return 0.0


def split_bcgov_part(answer: str) -> tuple[str, str]:
    """(general explanation, "For BC Gov teams" part); the part is "" when
    the answer has none."""
    match = BCGOV_PART_RE.search(answer or "")
    if not match:
        return answer or "", ""
    return answer[: match.start()].rstrip(), answer[match.start():].strip()


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    rank = max(1, math.ceil(p / 100 * len(values)))
    return values[rank - 1]


def mean(values: Iterable[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


# --- Checks -----------------------------------------------------------------
# (case, scored turn, k) -> (score, reason) | None


def intent_match(case: EvalCase, turn: dict, k: int):
    if not case.expected_intent:
        return None
    actual = turn.get("intent")
    ok = actual == case.expected_intent
    return float(ok), f"rewriter intent {actual!r}, expected {case.expected_intent!r}"


def outcome_match(case: EvalCase, turn: dict, k: int):
    if not case.expected_outcome:
        return None
    actual = turn.get("outcome")
    ok = actual in case.expected_outcome
    return float(ok), f"outcome {actual!r}, expected {' or '.join(case.expected_outcome)}"


def _retrieved_uris(turn: dict) -> list[str | None]:
    return [r.get("uri") for r in turn.get("retrieved") or []]


def _retrieval_check(score_fn) -> Check:
    def check(case: EvalCase, turn: dict, k: int):
        if not case.expected_sources:
            return None
        if not turn.get("searched"):
            return 0.0, "no search ran"
        uris = _retrieved_uris(turn)
        score = score_fn(uris, case.expected_sources, k)
        found = ranked_pages(uris)[:k]
        return score, f"top pages: {found or 'none'}"
    return check


retrieval_hit = _retrieval_check(hit_at_k)
retrieval_recall = _retrieval_check(recall_at_k)
retrieval_mrr = _retrieval_check(lambda uris, expected, k: reciprocal_rank(uris, expected))


def citation_precision(case: EvalCase, turn: dict, k: int):
    """Share of the delivered answer's cited pages that are expected sources.
    Informational: another page may also be a correct source."""
    cited = ranked_pages(turn.get("citations") or [])
    if not case.expected_sources or not cited or turn.get("outcome") not in ("answer", "general"):
        return None
    wanted = {page_key(u) for u in case.expected_sources}
    good = [c for c in cited if c in wanted]
    return len(good) / len(cited), f"cited {cited}"


def first_pass_accepted(case: EvalCase, turn: dict, k: int):
    """The grounding check accepted the first reply (no repair needed)."""
    attempts = turn.get("attempts") or []
    if not attempts:
        return None
    reasons = attempts[0].get("reasons") or []
    return (0.0, "; ".join(reasons)[:500]) if reasons else (1.0, "accepted")


CHECKS: dict[str, tuple[Check, float]] = {
    # name: (check, pass threshold)
    "intent_match": (intent_match, 1.0),
    "outcome_match": (outcome_match, 1.0),
    "retrieval_hit@k": (retrieval_hit, 1.0),
    "retrieval_recall@k": (retrieval_recall, 0.5),
    "retrieval_mrr": (retrieval_mrr, 0.5),
    "citation_precision": (citation_precision, 0.5),
    "first_pass_accepted": (first_pass_accepted, 1.0),
}
