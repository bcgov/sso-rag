import json
import logging

from strands import ToolContext, tool

from app.common.config import settings
from app.services.retrieval_pipeline import search

logger = logging.getLogger(__name__)

# invocation_state key: pass a list here when invoking the agent to collect
# every result this tool returned, as {"title", "uri", "score", "text"} (see
# ChatService.stream and app/services/grounding.py).
CITATIONS_KEY = "citations"

TOOL_NAME = "search_knowledge_base"


def searches_this_turn(messages: list[dict] | None) -> int:
    """Calls to this tool since the user's latest message, including the one
    being executed (it is already in the assistant message)."""
    count = 0
    for m in reversed(messages or []):
        content = m.get("content") or []
        if m.get("role") == "user" and any(isinstance(b, dict) and "text" in b for b in content):
            break
        if m.get("role") == "assistant":
            count += sum(
                1 for b in content
                if isinstance(b, dict) and (b.get("toolUse") or {}).get("name") == TOOL_NAME
            )
    return count


@tool(context=True)
def search_knowledge_base(
    question: str,
    queries: list[str],
    tool_context: ToolContext
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
        query that found nothing relevant; a string starting with "NO_MATCH"
        if nothing relevant was found for any query; or a string starting
        with "CLARIFY:" followed by a question to ask the user; or
        "SEARCH_LIMIT" if this message has used up its searches.
    """

    messages = getattr(tool_context.agent, "messages", None)
    calls = searches_this_turn(messages)
    if calls > settings.max_searches_per_turn:
        logger.warning(json.dumps({"event": "search_limit", "calls": calls}))
        return (f"SEARCH_LIMIT: already searched {calls - 1} times for this message. "
                "Do not search again. Answer from the results you already have, or "
                "give the no-documentation reply if they don't cover the question.")

    result = search(question, queries, messages=messages)
    hits, stats = result.hits, result.stats
    logger.info(json.dumps({"question": question, "kept": len(hits), **stats}, default=str))

    if result.clarification:
        return f"CLARIFY: {result.clarification}"
    if not hits:
        return ("NO_MATCH: nothing relevant for these queries. Try different "
                "wording (the docs' own terms, the identity provider name, or "
                "the error message text) before telling the user.")

    citations = tool_context.invocation_state.get(CITATIONS_KEY)

    blocks = []
    for i, r in enumerate(hits, 1):
        meta = r.metadata
        title = meta.get("section") or meta.get("page_title", "")
        if isinstance(citations, list):
            citations.append({
                "title": title,
                "uri": meta.get("source_url") or r.source,
                "score": r.score,
                # Grounding checks compare the answer against this text.
                "text": r.text
            })
        blocks.append(
            f"[{i}] {title}\n"
            f"source: {meta.get('source_url') or r.source}\n"
            f"score: {r.score:.3f}\n"
            f"{r.text}"
        )

    missing = result.missing
    if missing:
        blocks.append(
            "NOTE: no relevant results for: "
            + "; ".join(f'"{q}"' for q in missing)
            + ". Retry those with different wording if they matter to the answer."
        )

    return "\n\n---\n\n".join(blocks)
