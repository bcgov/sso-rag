import json
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from strands import ToolContext, tool

from app.common.config import settings
from app.common.trace import preview, trace
from app.common.turn_report import record
from app.retrieval.knowledge_base import merge_hits, multi_search
from app.retrieval.models import RetrievedChunk, SearchQuery
from app.services.query_rewriter import (
    CONVERSATIONAL,
    GENERAL_AUTH,
    OFF_TOPIC,
    SSO_QUESTION,
    RewrittenQuery,
    query_rewriter,
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
    "NO_SEARCH_NEEDED: this message is a greeting and needs no documentation "
    "search; reply conversationally."
)

# Returned when the tool is called again in the same turn: the first call's
# output is already in the conversation, and repeating it would only grow the
# context (and the reply's token use).
ALREADY_SEARCHED = (
    "ALREADY_SEARCHED: the documentation was already searched for this "
    "question; use the results above and write your reply now."
)

NO_MATCH = (
    "NO_MATCH: the documentation search found nothing relevant for this "
    "question. Reply with the no-documentation text."
)

# A general identity question with no BC Gov result is still answerable.
NO_MATCH_GENERAL = (
    "NO_MATCH: the BC Gov SSO documentation has nothing on this. Answer from "
    "general knowledge (\"response_type\": \"general\", \"citations\": []) and "
    "leave out the \"For BC Gov teams\" part."
)

OFF_TOPIC_RESULT = (
    "OFF_TOPIC: this message is not about login, identity or access. Reply in "
    "one sentence with what you can help with (\"response_type\": \"conversational\")."
)

# Origins whose misses trigger a reworded retry; step-back queries only add
# background, and a general_auth question's generic query ("general") isn't
# expected to match the BC Gov docs, so their misses aren't worth a retry.
RETRYABLE_ORIGINS = {"rewrite", "sub"}


@dataclass
class SearchTurn:
    """Per-turn state shared between ChatService and the tool."""

    prompt: str
    # Resolves to the query rewriter's RewrittenQuery for this turn.
    rewrite: Future
    # The rewriter's history, reused to build a fallback query.
    history: list[tuple[str, str]] = field(default_factory=list)
    # The first call's output; later calls in the turn get ALREADY_SEARCHED.
    result: str | None = None
    # True once the knowledge base was actually searched this turn
    # (NO_SEARCH_NEEDED and OFF_TOPIC don't count).
    searched: bool = False
    # The intent the search ran with (the rewriter's, or sso_question when the
    # agent's call overrode a conversational one); None until the tool runs.
    # ChatService allows "general" answers only for general_auth.
    intent: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@tool(context=True)
def search_knowledge_base(tool_context: ToolContext) -> str:
    """Search the BC Gov SSO (Keycloak / CSS app) documentation for the
    user's current question.

    Call it once for every question about authentication, authorization or
    identity: BC Gov SSO and the CSS app, identity providers (IDIR, BCeID,
    BC Services Card, GitHub), roles, clients, redirects, tokens, OIDC, OAuth
    2.0, SAML, Keycloak, Entra ID, Okta, Auth0, Clerk, errors and uptime.
    Never call it for greetings, thanks or questions about what you can help
    with.

    Takes no arguments: the search queries are prepared automatically from
    the conversation.

    Returns:
        A header ("TYPE: sso_question | general_auth", "QUESTION: <the
        question, resolved from the conversation>"), then numbered result
        blocks separated by "---", each with the page and section, source URL
        and text, possibly followed by a NOTE naming parts of the question
        the documentation doesn't cover; or a string starting with
        "NO_MATCH" if nothing relevant was found, "NO_SEARCH_NEEDED" if the
        message is a greeting, "OFF_TOPIC" if it isn't about identity, or
        "ALREADY_SEARCHED" if it was already called this turn.
    """

    state = tool_context.invocation_state
    turn = state.get(SEARCH_TURN_KEY)
    if not isinstance(turn, SearchTurn):
        logger.error("search_knowledge_base called without a SearchTurn")
        return NO_MATCH

    with turn.lock:
        if turn.result is not None:
            logger.info(json.dumps({"event": "search_repeated"}))
            trace("search tool: repeated call in the same turn", returned="ALREADY_SEARCHED")
            return ALREADY_SEARCHED
        citations = state.get(CITATIONS_KEY)
        result = _search(turn, citations if isinstance(citations, list) else None)
        # NO_SEARCH_NEEDED and OFF_TOPIC are not cached: a later call in the
        # turn (e.g. a search repair) gets the same cheap answer, not
        # ALREADY_SEARCHED.
        if result not in (NO_SEARCH_NEEDED, OFF_TOPIC_RESULT):
            turn.result = result
        return result


def _resolve_rewrite(turn: SearchTurn) -> tuple[RewrittenQuery, int]:
    started = time.monotonic()
    try:
        rewrite = turn.rewrite.result(timeout=settings.query_rewriter_timeout_seconds)
    except Exception as e:
        logger.warning("query rewrite unavailable (%s); searching the prompt with recent history",
                       e or type(e).__name__)
        rewrite = RewrittenQuery.raw(turn.prompt, turn.history)
    return rewrite, int((time.monotonic() - started) * 1000)


def build_queries(rewrite: RewrittenQuery) -> list[SearchQuery]:
    """The searches for a rewrite, main facet first. For general_auth the
    BC Gov query is the main facet and the generic query only adds context."""

    if rewrite.intent == GENERAL_AUTH and rewrite.bcgov_query:
        queries = [
            SearchQuery(text=rewrite.bcgov_query, origin="rewrite"),
            SearchQuery(text=rewrite.rewritten_query, origin="general"),
        ]
    else:
        queries = [SearchQuery(text=rewrite.rewritten_query, origin="rewrite")]
    queries += [SearchQuery(text=q, origin="sub") for q in rewrite.sub_queries]
    if rewrite.step_back_query:
        queries.append(SearchQuery(text=rewrite.step_back_query, origin="step_back"))
    return queries


def rerank_question(rewrite: RewrittenQuery, prompt: str) -> str:
    """What results are scored against: the BC Gov query for general_auth,
    so CSS results survive the reranker, else the rewritten question."""
    if rewrite.intent == GENERAL_AUTH and rewrite.bcgov_query:
        return rewrite.bcgov_query
    return rewrite.rewritten_query or prompt


def _header(intent: str, question: str) -> str:
    """Tells the agent how to answer and what was asked (follow-ups resolved)."""
    return f"TYPE: {intent}\nQUESTION: {question}"


def _skip_search(turn: SearchTurn, rewrite: RewrittenQuery) -> str | None:
    """The tool's reply when this turn needs no search: off-topic messages
    and greetings caught by the rewriter's short-circuit. None otherwise."""

    if rewrite.intent == OFF_TOPIC:
        result, reason = OFF_TOPIC_RESULT, OFF_TOPIC
    elif rewrite.intent == CONVERSATIONAL and rewrite.short_circuit:
        result, reason = NO_SEARCH_NEEDED, CONVERSATIONAL
    else:
        return None
    turn.intent = rewrite.intent
    record("search_skipped", reason=reason)
    logger.info(json.dumps({"event": "search_skipped", "reason": reason}))
    trace(f"search tool: skipped ({reason})", prompt=turn.prompt, returned=result.split(":", 1)[0])
    return result


def _search(turn: SearchTurn, citations: list | None) -> str:
    rewrite, waited_ms = _resolve_rewrite(turn)

    if (skipped := _skip_search(turn, rewrite)) is not None:
        return skipped

    overridden = False
    if rewrite.intent == CONVERSATIONAL:
        # The rewriter's classification is a guess; the agent calling the
        # tool says otherwise. A search is cheap, an ungrounded answer isn't.
        overridden = True
        logger.info(json.dumps({"event": "search_intent_override", "rewriter_intent": CONVERSATIONAL}))
        rewrite = RewrittenQuery.raw(turn.prompt, turn.history)

    turn.searched = True
    turn.intent = GENERAL_AUTH if rewrite.intent == GENERAL_AUTH else SSO_QUESTION
    question = rerank_question(rewrite, turn.prompt)
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

    _record_search(turn, question, stats, retry_stats, hits,
                   waited_ms=waited_ms, fallback=rewrite.fallback, intent_override=overridden)
    logger.info(json.dumps({
        "question": question,
        "intent": turn.intent,
        "kept": len(hits),
        "waited_ms": waited_ms,
        "fallback": rewrite.fallback,
        "intent_override": overridden,
        "retry": retry_stats,
        **stats,
    }))

    trace(
        "search tool: queries and results",
        prompt=turn.prompt,
        waited_for_rewriter_ms=waited_ms,
        fallback_query=rewrite.fallback,
        overrode_conversational_intent=overridden,
        rerank_question=question,
        queries=[f"[{o}] {q}" for q, o in zip(stats["queries"], stats["origins"])],
        acronym_expanded=stats.get("expanded", []),
        candidates=stats.get("candidates"),
        reranked=stats.get("reranked"),
        no_hits=stats["missing"],
        retry_queries=retry_stats["queries"] if retry_stats else [],
        retry_no_hits=retry_stats["missing"] if retry_stats else [],
        uncovered_sub_queries=[q for q in stats["queries"] if q in still_missing and origin_of.get(q) == "sub"],
        hits=[
            {
                "score": round(r.score, 3),
                "title": _title(r),
                "uri": _uri(r),
                "found_by": r.metadata.get("_queries", []),
                "text": preview(r.text),
            }
            for r in hits
        ],
        returned="NO_MATCH" if not hits else f"{len(hits)} result blocks",
    )

    header = _header(turn.intent, rewrite.rewritten_query or turn.prompt)
    if not hits:
        return f"{header}\n\n{NO_MATCH_GENERAL if turn.intent == GENERAL_AUTH else NO_MATCH}"

    if citations is not None:
        citations.extend(_citation(r) for r in hits)

    blocks = [header, *(_block(i, r) for i, r in enumerate(hits, 1))]

    uncovered = [q for q in stats["queries"] if q in still_missing and origin_of.get(q) == "sub"]
    if uncovered:
        blocks.append(
            "NOTE: the documentation returned nothing for: "
            + "; ".join(f'"{q}"' for q in uncovered)
            + ". Say this part is not covered (PARTIALLY_SUPPORTED)."
        )

    return "\n\n---\n\n".join(blocks)


def _record_search(turn: SearchTurn, question: str, stats: dict, retry_stats: dict | None,
                   hits: list[RetrievedChunk], **flags) -> None:
    """The search, for an attached TurnReport (app/common/turn_report.py)."""
    record(
        "search",
        intent=turn.intent,
        rerank_question=question,
        queries=[{"text": q, "origin": o} for q, o in zip(stats["queries"], stats["origins"])],
        missing=stats["missing"],
        retry_queries=retry_stats["queries"] if retry_stats else [],
        candidates=stats.get("candidates"),
        reranked=stats.get("reranked"),
        # Best first, as the agent sees them.
        hits=[
            {"uri": _uri(r), "title": _title(r), "score": r.score,
             "found_by": r.metadata.get("_queries", [])}
            for r in hits
        ],
        **flags,
    )


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
    # Results arrive best first; the score itself is only for the logs.
    return (
        f"[{i}] {_title(r)}\n"
        f"source: {_uri(r)}\n"
        f"{r.text}"
    )
