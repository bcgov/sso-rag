import pytest

from app.services import grounding
from evals import metrics
from evals.dataset import EvalCase

DOCS = "https://bcgov.github.io/sso-docs/"


def _case(**fields) -> EvalCase:
    return EvalCase(id="c", category="sso_question", turns=["q"], **fields)


def test_page_key_ignores_fragment_query_trailing_slash_and_case():
    assert metrics.page_key(DOCS + "FAQs/CSS-App/#rotate?x=1") == metrics.page_key(DOCS + "faqs/css-app")
    assert metrics.page_key(None) == ""


def test_ranked_pages_dedupes_chunks_of_a_page():
    uris = [DOCS + "a#1", DOCS + "b", DOCS + "a#2", None, DOCS + "c"]
    assert metrics.ranked_pages(uris) == [metrics.page_key(DOCS + p) for p in ("a", "b", "c")]


def test_retrieval_scores():
    retrieved = [DOCS + "a#1", DOCS + "a#2", DOCS + "b", DOCS + "c"]
    assert metrics.hit_at_k(retrieved, [DOCS + "b"], k=2) == 1.0
    assert metrics.hit_at_k(retrieved, [DOCS + "c"], k=2) == 0.0
    assert metrics.recall_at_k(retrieved, [DOCS + "a", DOCS + "c", DOCS + "z"], k=3) == pytest.approx(2 / 3)
    assert metrics.reciprocal_rank(retrieved, [DOCS + "c"]) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank(retrieved, [DOCS + "z"]) == 0.0


def test_split_bcgov_part():
    answer = "PKCE protects the code exchange.\n\n**For BC Gov teams:** CSS public clients use PKCE."
    general, bcgov = metrics.split_bcgov_part(answer)
    assert general == "PKCE protects the code exchange."
    assert bcgov.startswith("**For BC Gov teams:**")
    assert metrics.split_bcgov_part("no part here") == ("no part here", "")


def test_bcgov_part_pattern_matches_grounding():
    assert metrics.BCGOV_PART_RE.pattern == grounding._BCGOV_PART_RE.pattern
    assert metrics.BCGOV_PART_RE.flags == grounding._BCGOV_PART_RE.flags


def test_percentile_and_mean():
    assert metrics.percentile([5, 1, 3, None, 2, 4], 50) == 3
    assert metrics.percentile([5, 1, 3, 2, 4], 95) == 5
    assert metrics.percentile([], 50) is None
    assert metrics.mean([1, None, 3]) == 2
    assert metrics.mean([None]) is None


def test_checks_skip_cases_without_expectations():
    turn = {"intent": "sso_question", "outcome": "answer", "searched": True, "retrieved": [],
            "citations": [], "attempts": []}
    case = _case()
    for name, (check, _) in metrics.CHECKS.items():
        assert check(case, turn, 6) is None, name


def test_intent_and_outcome_checks():
    case = _case(expected_intent="general_auth", expected_outcome=["general", "answer"])
    assert metrics.intent_match(case, {"intent": "sso_question"}, 6)[0] == 0.0
    assert metrics.outcome_match(case, {"outcome": "answer"}, 6)[0] == 1.0
    assert metrics.outcome_match(case, {"outcome": "not_supported"}, 6)[0] == 0.0


def test_retrieval_checks_score_zero_without_a_search():
    case = _case(expected_sources=[DOCS + "a"])
    assert metrics.retrieval_hit(case, {"searched": False}, 6) == (0.0, "no search ran")
    turn = {"searched": True, "retrieved": [{"uri": DOCS + "b"}, {"uri": DOCS + "a#x"}]}
    assert metrics.retrieval_hit(case, turn, 6)[0] == 1.0
    assert metrics.retrieval_mrr(case, turn, 6)[0] == 0.5


def test_citation_precision_only_for_answers_with_citations():
    case = _case(expected_sources=[DOCS + "a"])
    turn = {"outcome": "answer", "citations": [DOCS + "a#1", DOCS + "b"]}
    assert metrics.citation_precision(case, turn, 6)[0] == 0.5
    assert metrics.citation_precision(case, {**turn, "outcome": "clarification"}, 6) is None
    assert metrics.citation_precision(case, {**turn, "citations": []}, 6) is None


def test_first_pass_accepted():
    case = _case()
    assert metrics.first_pass_accepted(case, {"attempts": [{"reasons": []}]}, 6) == (1.0, "accepted")
    score, reason = metrics.first_pass_accepted(case, {"attempts": [{"reasons": ["undocumented URLs"]}, {}]}, 6)
    assert score == 0.0 and "undocumented URLs" in reason
