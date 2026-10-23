import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from strands import ToolContext, tool

from app.common.config import settings
from app.retrieval.knowledge_base import (
    merge_hits,
    multi_search
)
from app.retrieval.models import (
    RetrievedChunk,
    SearchQuery
)
from app.services.query_rewriter import (
    CONVERSATIONAL,
    RewrittenQuery,
    query_rewriter
)

logger = logging.getLogger(__name__)

# invocation_state key: pass a list here when invoking the agent to collect
# every result this tool returned, as {"title", "uri", "score", "text"} (see
# ChatService.stream and app/services/grounding.py).
CITATIONS_KEY = "citations"

# invocation_state key: the turn's SearchTurn (query rewriter output and the
# cached search result).
SEARCH_TURN_KEY = "search_turn"

NO_SEARCH_NEEDED = (
    "NO_SEARCH_NEEDED: this message needs no documentation search; "
    "reply conversationally."
)

NO_MATCH = (
    "NO_MATCH: the documentation search found nothing relevant for this "
    "question. Reply with the no-documentation text."
)

# Origins whose misses trigger a reworded retry; step-back queries only add
# background, so missing background isn't worth a retry.
RETRYABLE_ORIGINS = {"rewrite", "sub"}


@dataclass
class SearchTurn:
    """Per-turn state shared between ChatService and the tool."""

    prompt: str
    # Resolves to the query rewriter's RewrittenQuery for this turn.
    rewrite: Future
    # The tool's output, cached so repeated calls in a turn don't search again.
    result: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@tool(context=True)
def search_knowledge_base(tool_context: ToolContext) -> str:
    """Search the BC Gov SSO (Keycloak / CSS app) documentation for the
    user's current question.

    Call it once whenever the user needs SSO facts: integrations, the CSS
    app, identity providers (IDIR, BCeID, BC Services Card, GitHub), roles,
    client types, redirects, OIDC/SAML, custom realms, service accounts,
    errors and uptime. Never call it for greetings, thanks or questions about
    what you can help with.

    Takes no arguments: the search queries are prepared automatically from
    the conversation.

    Returns:
        Numbered result blocks separated by "---", each with the page and
        section, source URL, score and text, possibly followed by a NOTE
        naming parts of the question the documentation doesn't cover; or a
        string starting with "NO_MATCH" if nothing relevant was found, or
        "NO_SEARCH_NEEDED" if the message needs no search.
    """

    state = tool_context.invocation_state
    turn = state.get(SEARCH_TURN_KEY)
    if not isinstance(turn, SearchTurn):
        logger.error("search_knowledge_base called without a SearchTurn")
        return NO_MATCH

    with turn.lock:
        if turn.result is None:
            citations = state.get(CITATIONS_KEY)
            turn.result = _search(turn, citations if isinstance(citations, list) else None)
        return turn.result


def _resolve_rewrite(turn: SearchTurn) -> tuple[RewrittenQuery, int]:
    started = time.monotonic()
    try:
        rewrite = turn.rewrite.result(timeout=settings.query_rewriter_timeout_seconds)
    except Exception as e:
        logger.warning("query rewrite unavailable (%s); searching the raw prompt", e or type(e).__name__)
        rewrite = RewrittenQuery.raw(turn.prompt)
    return rewrite, int((time.monotonic() - started) * 1000)


def build_queries(rewrite: RewrittenQuery) -> list[SearchQuery]:
    queries = [SearchQuery(text=rewrite.rewritten_query, origin="rewrite")]
    queries += [SearchQuery(text=q, origin="sub") for q in rewrite.sub_queries]
    if rewrite.step_back_query:
        queries.append(SearchQuery(text=rewrite.step_back_query, origin="step_back"))
    return queries


def _search(turn: SearchTurn, citations: list | None) -> str:
    rewrite, waited_ms = _resolve_rewrite(turn)

    if rewrite.intent == CONVERSATIONAL:
        logger.info(json.dumps({"event": "search_skipped", "reason": CONVERSATIONAL}))
        return NO_SEARCH_NEEDED

    question = rewrite.rewritten_query or turn.prompt
    queries = build_queries(rewrite)
    hits, stats = multi_search(question, queries)

    origin_of = dict(zip(stats["queries"], stats["origins"]))
    failed = [q for q in stats["missing"] if origin_of.get(q) in RETRYABLE_ORIGINS]
    still_missing = set(failed)
    retry_stats = None

    if failed:
        retry_texts = query_rewriter.retry(question, failed)
        known = {q.lower() for q in stats["queries"]}
        pairs = [(f, r) for f, r in zip(failed, retry_texts) if r.lower() not in known]
        if pairs:
            retry_hits, retry_stats = multi_search(
                question, [SearchQuery(text=r, origin="retry") for _, r in pairs]
            )
            hits = merge_hits(hits, retry_hits)
            still_missing -= {f for f, r in pairs if r not in retry_stats["missing"]}

    logger.info(json.dumps({
        "question": question,
        "kept": len(hits),
        "waited_ms": waited_ms,
        "fallback": rewrite.fallback,
        "retry": retry_stats,
        **stats,
    }))

    if not hits:
        return NO_MATCH

    if citations is not None:
        citations.extend(_citation(r) for r in hits)

    blocks = [_block(i, r) for i, r in enumerate(hits, 1)]

    uncovered = [q for q in stats["queries"] if q in still_missing and origin_of.get(q) == "sub"]
    if uncovered:
        blocks.append(
            "NOTE: the documentation returned nothing for: "
            + "; ".join(f'"{q}"' for q in uncovered)
            + ". Say this part is not covered (PARTIALLY_SUPPORTED)."
        )

    return "\n\n---\n\n".join(blocks)


def _title(r: RetrievedChunk) -> str:
    return r.metadata.get("section") or r.metadata.get("page_title", "")


def _uri(r: RetrievedChunk) -> str | None:
    return r.metadata.get("source_url") or r.source


def _citation(r: RetrievedChunk) -> dict:
    return {
        "title": _title(r),
        "uri": _uri(r),
        "score": r.score,
        # Grounding checks compare the answer against this text.
        "text": r.text
    }


def _block(i: int, r: RetrievedChunk) -> str:
    return (
        f"[{i}] {_title(r)}\n"
        f"source: {_uri(r)}\n"
        f"score: {r.score:.3f}\n"
        f"{r.text}"
    )
