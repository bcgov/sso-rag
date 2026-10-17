import json
import logging

from strands import tool

from app.common.config import settings
from app.retrieval.bedrock_client import get_bedrock_runtime_client
from app.retrieval.knowledge_base import (
    expand_query,
    retrieve
)

logger = logging.getLogger(__name__)


@tool
def search_knowledge_base(
    question: str
) -> str:
    """Search the BC Gov SSO (Keycloak / CSS app) documentation.

    Use for every question about SSO integrations, the CSS app, identity
    providers (IDIR, BCeID, BC Services Card, GitHub), roles, client types,
    redirects, OIDC/SAML, custom realms, service accounts, errors and uptime.

    Args:
        question: A standalone, specific search query in the documentation's own
            vocabulary. Resolve pronouns from the conversation first
            ("what about prod?" -> "PROD environment approval for BCeID
            integration"). One topic per query; call again for each part of a
            multi-part question.

    Returns:
        Numbered result blocks separated by "---", each with the doc title and
        section, source, score and text; or a string starting with "NO_MATCH"
        if nothing relevant was found.
    """

    expanded = expand_query(question)
    reranked = bool(settings.reranker_model_arn)

    try:
        results = retrieve(expanded)
    except get_bedrock_runtime_client().exceptions.ValidationException as e:
        # e.g. reranker or HYBRID not available for this region / vector store
        logger.warning("retrieve config rejected (%s); falling back to plain search", e)
        results = retrieve(expanded, advanced=False)[:settings.number_of_reranked_results]
        reranked = False

    # min_rerank_score is calibrated for reranker scores, not raw vector scores
    if reranked:
        hits = [r for r in results if r.score >= settings.min_rerank_score]
    else:
        hits = results

    logger.info(json.dumps({
        "query": question,
        "expanded": expanded != question,
        "reranked": reranked,
        "scores": [round(r.score, 3) for r in results],
        "kept": len(hits),
    }))

    if not hits:
        return ("NO_MATCH: nothing relevant for this query. Try different "
                "wording (the docs' own terms, the identity provider name, or "
                "the error message text) before telling the user.")

    blocks = []
    for i, r in enumerate(hits, 1):
        meta = r.metadata
        blocks.append(
            f"[{i}] {meta.get('doc_title', '')} > {meta.get('section', '')}\n"
            f"source: {meta.get('source_url', r.source)}\n"
            f"score: {r.score:.3f}\n"
            f"{r.text}"
        )
    return "\n\n---\n\n".join(blocks)
