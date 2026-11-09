"""Query rewriter: turns the user's message into search queries.

A small model (Amazon Nova 2 Lite by default, via the Bedrock Converse API)
owns all search-query writing. One call per question returns:

- intent: sso_question (BC Gov SSO / CSS), general_auth (OAuth, OIDC, SAML,
  Entra ID, Okta, Auth0, Clerk, Keycloak in general), conversational or
  off_topic;
- query rewriting: one specific, detailed, standalone query;
- bcgov_query (general_auth only): the same need met with BC Gov SSO, so
  retrieval surfaces the CSS way of doing it;
- step-back prompting: one broader query for background;
- sub-query decomposition: 2-4 simpler queries for multi-part questions;

and, on demand, it rewords queries that found nothing (`retry`).

The techniques used to be three parallel calls, each re-sending the same
system prompt and history; one call sends them once. The system prompts are
static and marked with a Bedrock cache point when the model supports it, so
repeat calls within the cache TTL read them from the cache.

The answering agent never writes queries: its search_knowledge_base tool runs
what this produces (app/tools/knowledge_base_tool.py). Everything fails open:
if the call errors, times out or returns invalid JSON, the prompt is searched
with the previous user messages prepended (`fallback_query`), so follow-ups
like "what about prod?" keep their topic.
"""

import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Literal

from app.common.config import settings
from app.common.trace import trace
from app.prompts.query_rewriter_prompt import (
    RETRY_SYSTEM,
    REWRITE_SYSTEM,
    retry_message,
    user_message,
)
from app.retrieval.bedrock_client import (
    cached_system,
    get_bedrock_model_client,
    supports_prompt_caching,
)
from app.services.grounding import split_grounding

logger = logging.getLogger(__name__)

SSO_QUESTION = "sso_question"
GENERAL_AUTH = "general_auth"
CONVERSATIONAL = "conversational"
OFF_TOPIC = "off_topic"
Intent = Literal["sso_question", "general_auth", "conversational", "off_topic"]
# Intents with nothing to search for.
NO_SEARCH_INTENTS = {CONVERSATIONAL, OFF_TOPIC}

MAX_QUERY_CHARS = 300
MAX_SUB_QUERIES = 4
HISTORY_MESSAGE_CHARS = 500
# Output budget for the one rewrite call (five short JSON fields).
REWRITE_MAX_TOKENS = 400
RETRY_MAX_TOKENS = 300
# Previous user messages prepended to the prompt when the rewrite fails.
FALLBACK_CONTEXT_MESSAGES = 2
# Below this many free characters, context would only be a fragment.
MIN_FALLBACK_CONTEXT_CHARS = 20

# Whole-message greetings, thanks and capability questions skip the model
# calls entirely. Kept conservative: anything else goes to the rewriter,
# which errs towards "sso_question".
_GREETING_RE = re.compile(
    r"^\s*(?:"
    r"(?:hi|hello|hey|hiya|howdy|greetings)(?:\s+there)?"
    r"|good\s+(?:morning|afternoon|evening|day)"
    r"|thanks?(?:\s+you)?(?:\s+(?:so\s+much|very\s+much|a\s+lot))?"
    r"|thx|ty|cheers|much\s+appreciated"
    r"|ok(?:ay)?(?:,?\s+thanks?(?:\s+you)?)?"
    r"|bye|goodbye"
    r"|who\s+are\s+you"
    r"|what\s+can\s+you\s+(?:do|help(?:\s+me)?\s+with)"
    r"|how\s+can\s+you\s+help(?:\s+me)?"
    r")\s*[!.?]*\s*$",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# Short model calls; separate from the orchestration pool so a `transform`
# waiting on its call can never starve it.
_calls = ThreadPoolExecutor(max_workers=16, thread_name_prefix="rewriter-call")
_jobs = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rewriter-job")


@dataclass(frozen=True)
class RewrittenQuery:
    intent: Intent
    # The main search query and the question the agent is shown; for
    # sso_question also the question the reranker scores against.
    rewritten_query: str
    step_back_query: str | None = None
    sub_queries: list[str] = field(default_factory=list)
    # general_auth only: the same need met with BC Gov SSO. Searched as the
    # main facet and used for reranking, so CSS results survive.
    bcgov_query: str | None = None
    # True when the rewrite failed and `fallback_query` is used instead.
    fallback: bool = False
    # True for whole-message greetings matched by the regex, the only
    # conversational result the search tool trusts over the agent's own call.
    short_circuit: bool = False

    @classmethod
    def raw(cls, prompt: str, history: list[tuple[str, str]] | None = None) -> RewrittenQuery:
        return cls(intent=SSO_QUESTION, rewritten_query=fallback_query(prompt, history), fallback=True)

    @classmethod
    def conversational(cls) -> RewrittenQuery:
        return cls(intent=CONVERSATIONAL, rewritten_query="")

    @classmethod
    def off_topic(cls) -> RewrittenQuery:
        return cls(intent=OFF_TOPIC, rewritten_query="")

    @classmethod
    def greeting(cls) -> RewrittenQuery:
        return cls(intent=CONVERSATIONAL, rewritten_query="", short_circuit=True)


def is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text or ""))


def fallback_query(prompt: str, history: list[tuple[str, str]] | None = None) -> str:
    """The query searched when there is no rewrite: the prompt, preceded by
    up to FALLBACK_CONTEXT_MESSAGES previous user messages (most recent kept
    first) within MAX_QUERY_CHARS, so a follow-up keeps its topic.
    Standalone questions get some extra context; the reranker scores the
    results against the same text."""

    prompt = " ".join((prompt or "").split())
    previous = [text for role, text in history or [] if role == "user"]
    context: list[str] = []
    room = MAX_QUERY_CHARS - len(prompt) - 1
    for text in reversed(previous[-FALLBACK_CONTEXT_MESSAGES:]):
        if room < MIN_FALLBACK_CONTEXT_CHARS:
            break
        if len(text) > room:
            text = text[:room].rsplit(" ", 1)[0].rstrip(" …")
        context.insert(0, text)
        room -= len(text) + 1
    return " ".join(context + [prompt]).strip()


def recent_history(messages: list[dict], turns: int) -> list[tuple[str, str]]:
    """The last `turns` user/assistant exchanges as (role, text), oldest
    first: tool calls and results skipped, grounding blocks removed, each
    message truncated."""

    if turns <= 0:
        return []

    history: list[tuple[str, str]] = []
    for message in reversed(messages):
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = " ".join(
            block["text"] for block in message.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        if role == "assistant":
            text = split_grounding(text)[0]
        text = " ".join(text.split())
        if not text:
            continue
        if len(text) > HISTORY_MESSAGE_CHARS:
            text = text[:HISTORY_MESSAGE_CHARS].rstrip() + "…"
        history.append((role, text))
        if len(history) >= turns * 2:
            break

    return list(reversed(history))


def _parse_json(text: str) -> dict | None:
    """The first JSON object in a model reply, tolerating code fences and
    surrounding prose."""

    text = _FENCE_RE.sub("", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_query(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if len(value) < 2:
        return None
    return value[:MAX_QUERY_CHARS].rstrip()


def _clean_queries(values, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        query = _clean_query(value)
        if query and query.lower() not in {q.lower() for q in out}:
            out.append(query)
    return out[:limit]


def _sub_queries(values) -> list[str]:
    """2-4 sub-queries; [] for a single-topic question or unusable input."""
    queries = _clean_queries(values, MAX_SUB_QUERIES)
    return queries if len(queries) >= 2 else []


def parse_rewrite(text: str) -> RewrittenQuery | None:
    """The rewriter's reply as a RewrittenQuery, or None if it is unusable
    (no JSON object, or a question without a query).

    Only intent and query are required: a missing or malformed step_back,
    sub_queries or bcgov_query is dropped rather than failing the rewrite.
    Anything but an explicit conversational, off_topic or general_auth intent
    is treated as an sso_question."""

    data = _parse_json(text)
    if data is None:
        return None
    intent = data.get("intent")
    intent = intent.strip().lower() if isinstance(intent, str) else ""
    if intent == CONVERSATIONAL:
        return RewrittenQuery.conversational()
    if intent == OFF_TOPIC:
        return RewrittenQuery.off_topic()
    intent = GENERAL_AUTH if intent == GENERAL_AUTH else SSO_QUESTION

    # "rewritten_query" is the field name the separate rewrite call used.
    query = _clean_query(data.get("query")) or _clean_query(data.get("rewritten_query"))
    if not query:
        return None
    bcgov_query = _clean_query(data.get("bcgov_query")) if intent == GENERAL_AUTH else None
    step_back = _clean_query(data.get("step_back"))
    return RewrittenQuery(
        intent=intent,
        rewritten_query=query,
        step_back_query=step_back if step_back and step_back.lower() != query.lower() else None,
        sub_queries=_sub_queries(data.get("sub_queries")),
        bcgov_query=bcgov_query if bcgov_query and bcgov_query.lower() != query.lower() else None,
    )


def parse_retry(text: str, limit: int) -> list[str]:
    data = _parse_json(text)
    return _clean_queries(data.get("retry_queries"), limit) if data else []


def _prompt_caching() -> bool:
    return settings.query_rewriter_prompt_caching and supports_prompt_caching(settings.query_rewriter_model_id)


def _converse(system: str, user: str, max_tokens: int) -> tuple[str, dict]:
    """One Converse call: (reply text, usage). The system prompt is static,
    so it is followed by a cache point when the model supports caching."""

    response = get_bedrock_model_client().converse(
        modelId=settings.query_rewriter_model_id,
        system=cached_system(system, _prompt_caching()),
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"temperature": 0, "topP": 0.9, "maxTokens": max_tokens},
    )
    text = "".join(
        block.get("text", "") for block in response["output"]["message"]["content"]
    )
    return text, response.get("usage") or {}


@dataclass(frozen=True)
class _Reply:
    text: str
    latency_ms: int
    usage: dict


def _timed_converse(system: str, user: str, max_tokens: int) -> _Reply:
    started = time.monotonic()
    text, usage = _converse(system, user, max_tokens)
    return _Reply(text, int((time.monotonic() - started) * 1000), usage)


def _usage_log(usage: dict) -> dict:
    """Token counts for the logs, including prompt-cache reads and writes."""
    return {
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "cache_read_input_tokens": usage.get("cacheReadInputTokens"),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens"),
    }


def _call(system: str, user: str, max_tokens: int) -> tuple[_Reply | None, str]:
    """Runs one rewriter call within the rewriter timeout:
    (reply, "ok") or (None, "timeout" | "error")."""

    future = _calls.submit(_timed_converse, system, user, max_tokens)
    try:
        return future.result(timeout=settings.query_rewriter_timeout_seconds), "ok"
    except FutureTimeout:
        future.cancel()
        return None, "timeout"
    except Exception as e:
        logger.warning("query rewriter call failed: %s", e)
        return None, "error"


class QueryRewriter:

    def submit(self, prompt: str, history: list[tuple[str, str]]) -> Future:
        """Starts `transform` in the background. Greetings resolve at once,
        without a model call."""

        if is_greeting(prompt):
            future: Future = Future()
            future.set_result(RewrittenQuery.greeting())
            logger.info(json.dumps({
                "event": "query_rewrite", "intent": CONVERSATIONAL, "short_circuit": True
            }))
            trace("query rewriter: greeting short-circuit (no model call)", prompt=prompt)
            return future
        return _jobs.submit(self.transform, prompt, history)

    def transform(self, prompt: str, history: list[tuple[str, str]]) -> RewrittenQuery:
        """One rewriter call: intent, query, bcgov_query, step-back and
        sub-queries."""

        try:
            return self._transform(prompt, history)
        except Exception:
            logger.exception("query rewrite failed; searching the prompt with recent history")
            return RewrittenQuery.raw(prompt, history)

    def _transform(self, prompt: str, history: list[tuple[str, str]]) -> RewrittenQuery:
        reply, status = _call(REWRITE_SYSTEM, user_message(prompt, history), REWRITE_MAX_TOKENS)

        result = parse_rewrite(reply.text) if reply else None
        if reply and result is None:
            status = "parse_error"
        if result is None:
            result = RewrittenQuery.raw(prompt, history)
        else:
            # The toggles now filter the one call's output.
            if not settings.query_rewriter_step_back_enabled:
                result = replace(result, step_back_query=None)
            if not settings.query_rewriter_decomposition_enabled:
                result = replace(result, sub_queries=[])

        logger.info(json.dumps({
            "event": "query_rewrite",
            "intent": result.intent,
            "short_circuit": False,
            "fallback": result.fallback,
            "status": status,
            "latency_ms": reply.latency_ms if reply else None,
            **_usage_log(reply.usage if reply else {}),
            "n_sub_queries": len(result.sub_queries),
            "step_back": result.step_back_query is not None,
            "bcgov_query": result.bcgov_query is not None,
        }))
        trace(
            "query rewriter: output",
            prompt=prompt,
            history=[f"{role}: {text}" for role, text in history],
            intent=result.intent,
            rewritten_query=result.rewritten_query,
            bcgov_query=result.bcgov_query,
            sub_queries=result.sub_queries,
            step_back_query=result.step_back_query,
            fallback=result.fallback,
            status=status,
            latency_ms=reply.latency_ms if reply else None,
            usage=reply.usage if reply else None,
            raw_reply=reply.text if reply else None,
        )
        return result

    def retry(self, question: str, failed: list[str]) -> list[str]:
        """Alternative wording for queries that found nothing, aligned with
        `failed` (at most one per failed query). [] on any failure."""

        if not failed or not settings.query_rewriter_retry_enabled:
            return []

        reply, status = _call(RETRY_SYSTEM, retry_message(question, failed), RETRY_MAX_TOKENS)
        if reply is None:
            logger.warning("query rewriter retry %s", status)
            return []

        queries = parse_retry(reply.text, len(failed))
        trace("query rewriter: retry", question=question, failed_queries=failed,
              retry_queries=queries, raw_reply=reply.text, usage=reply.usage)
        logger.info(json.dumps({
            "event": "query_rewrite_retry",
            "failed": len(failed),
            "retry_queries": len(queries),
            "latency_ms": reply.latency_ms,
            **_usage_log(reply.usage),
        }))
        return queries


query_rewriter = QueryRewriter()
