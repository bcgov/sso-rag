import json
import logging

from strands import tool

from app.retrieval.knowledge_base import (
    multi_search
)

logger = logging.getLogger(__name__)


@tool
def search_knowledge_base(
    question: str,
    queries: list[str]
) -> str:
    """Search the BC Gov SSO (Keycloak / CSS app) documentation.

    Use for every question about SSO integrations, the CSS app, identity
    providers (IDIR, BCeID, BC Services Card, GitHub), roles, client types,
    redirects, OIDC/SAML, custom realms, service accounts, errors and uptime.

    Args:
        question: The user's need as one standalone sentence, with pronouns
            resolved from the conversation ("what about prod?" -> "How do I
            get PROD approval for my BCeID integration?"). Results are ranked
            against this.
        queries: 1 to 4 search queries in the documentation's own vocabulary.
            Use a single query for a single-topic question. Use one query per
            facet only when the question has genuinely independent parts
            (e.g. "BCeID PROD approval" and "service account client roles"),
            or for troubleshooting (exact error text, then the feature).

    Returns:
        Numbered result blocks separated by "---", each with the page and
        section, source URL, score and text, followed by a note naming any
        query that found nothing relevant; or a string starting with
        "NO_MATCH" if nothing relevant was found for any query.
    """

    hits, stats = multi_search(question, queries)
    logger.info(json.dumps({"question": question, "kept": len(hits), **stats}))

    if not hits:
        return ("NO_MATCH: nothing relevant for these queries. Try different "
                "wording (the docs' own terms, the identity provider name, or "
                "the error message text) before telling the user.")

    blocks = []
    for i, r in enumerate(hits, 1):
        meta = r.metadata
        title = meta.get("section") or meta.get("page_title", "")
        blocks.append(
            f"[{i}] {title}\n"
            f"source: {meta.get('source_url') or r.source}\n"
            f"score: {r.score:.3f}\n"
            f"{r.text}"
        )

    found = {q for r in hits for q in r.metadata.get("_queries", [])}
    missing = [q for q in stats["queries"] if q not in found]
    if missing:
        blocks.append(
            "NOTE: no relevant results for: "
            + "; ".join(f'"{q}"' for q in missing)
            + ". Retry those with different wording if they matter to the answer."
        )

    return "\n\n---\n\n".join(blocks)
