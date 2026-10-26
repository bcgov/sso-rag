"""Query rewriter: turns the user's message into search queries.

A small model (Meta Llama 3 8B Instruct by default, via the Bedrock Converse
API) owns all search-query writing. It runs three techniques in parallel:

- query rewriting (with intent): one specific, detailed, standalone query,
  and whether the message needs a documentation search at all;
- step-back prompting: one broader query for background;
- sub-query decomposition: 2-4 simpler queries for multi-part questions.

and, on demand, rewords queries that found nothing (`retry`).

The answering agent never writes queries: its search_knowledge_base tool runs
what this produces (app/tools/knowledge_base_tool.py). Everything fails open:
if a call errors, times out or returns invalid JSON, that technique is
dropped, and without a rewrite the raw prompt is searched as is.
"""

import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Literal

from app.common.config import settings
from app.common.trace import trace
from app.prompts.query_rewriter_prompt import (
    DECOMPOSE_SYSTEM,
    RETRY_SYSTEM,
    REWRITE_SYSTEM,
    STEP_BACK_SYSTEM,
    retry_message,
    user_message,
)
from app.retrieval.bedrock_client import get_bedrock_model_client
from app.services.grounding import split_grounding

logger = logging.getLogger(__name__)

SSO_QUESTION = "sso_question"
CONVERSATIONAL = "conversational"
Intent = Literal["sso_question", "conversational"]

MAX_QUERY_CHARS = 300
MAX_SUB_QUERIES = 4
HISTORY_MESSAGE_CHARS = 500

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
# waiting on its calls can never starve them.
_calls = ThreadPoolExecutor(max_workers=16, thread_name_prefix="rewriter-call")
_jobs = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rewriter-job")


@dataclass(frozen=True)
class RewrittenQuery:
    intent: Intent
    # The main search query; also the question the reranker scores against.
    rewritten_query: str
    step_back_query: str | None = None
    sub_queries: list[str] = field(default_factory=list)
    # True when the rewrite failed and the raw prompt is used instead.
    fallback: bool = False

    @classmethod
    def raw(cls, prompt: str) -> RewrittenQuery:
        return cls(intent=SSO_QUESTION, rewritten_query=prompt.strip(), fallback=True)

    @classmethod
    def conversational(cls) -> RewrittenQuery:
        return cls(intent=CONVERSATIONAL, rewritten_query="")


def is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text or ""))


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


def parse_rewrite(text: str) -> tuple[Intent, str] | None:
    """(intent, rewritten_query), or None if the reply is unusable."""
    data = _parse_json(text)
    if data is None:
        return None
    intent = data.get("intent")
    intent = intent.strip().lower() if isinstance(intent, str) else ""
    if intent == CONVERSATIONAL:
        return CONVERSATIONAL, ""
    # Anything but an explicit "conversational" is treated as a question.
    query = _clean_query(data.get("rewritten_query"))
    return (SSO_QUESTION, query) if query else None


def parse_step_back(text: str) -> str | None:
    data = _parse_json(text)
    return _clean_query(data.get("step_back_query")) if data else None


def parse_sub_queries(text: str) -> list[str] | None:
    """2-4 sub-queries, [] for a single-topic question, None if unusable."""
    data = _parse_json(text)
    if data is None or not isinstance(data.get("sub_queries"), list):
        return None
    queries = _clean_queries(data["sub_queries"], MAX_SUB_QUERIES)
    return queries if len(queries) >= 2 else []


def parse_retry(text: str, limit: int) -> list[str]:
    data = _parse_json(text)
    return _clean_queries(data.get("retry_queries"), limit) if data else []


def _converse(system: str, user: str, max_tokens: int) -> str:
    response = get_bedrock_model_client().converse(
        modelId=settings.query_rewriter_model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"temperature": 0, "topP": 0.9, "maxTokens": max_tokens},
    )
    return "".join(
        block.get("text", "") for block in response["output"]["message"]["content"]
    )


def _timed_converse(system: str, user: str, max_tokens: int) -> tuple[str, int]:
    started = time.monotonic()
    text = _converse(system, user, max_tokens)
    return text, int((time.monotonic() - started) * 1000)


class QueryRewriter:

    def submit(self, prompt: str, history: list[tuple[str, str]]) -> Future:
        """Starts `transform` in the background. Greetings resolve at once,
        without a model call."""

        if is_greeting(prompt):
            future: Future = Future()
            future.set_result(RewrittenQuery.conversational())
            logger.info(json.dumps({
                "event": "query_rewrite", "intent": CONVERSATIONAL, "short_circuit": True
            }))
            trace("query rewriter: greeting short-circuit (no model call)", prompt=prompt)
            return future
        return _jobs.submit(self.transform, prompt, history)

    def transform(self, prompt: str, history: list[tuple[str, str]]) -> RewrittenQuery:
        """Runs rewrite+intent, step-back and decomposition in parallel."""

        try:
            return self._transform(prompt, history)
        except Exception:
            logger.exception("query rewrite failed; searching the raw prompt")
            return RewrittenQuery.raw(prompt)

    def _transform(self, prompt: str, history: list[tuple[str, str]]) -> RewrittenQuery:
        user = user_message(prompt, history)
        techniques = {"rewrite": (REWRITE_SYSTEM, 200)}
        if settings.query_rewriter_step_back_enabled:
            techniques["step_back"] = (STEP_BACK_SYSTEM, 200)
        if settings.query_rewriter_decomposition_enabled:
            techniques["decomposition"] = (DECOMPOSE_SYSTEM, 300)

        futures = {
            name: _calls.submit(_timed_converse, system, user, max_tokens)
            for name, (system, max_tokens) in techniques.items()
        }
        done, _ = wait(futures.values(), timeout=settings.query_rewriter_timeout_seconds)

        replies: dict[str, str] = {}
        status: dict[str, str] = {}
        latency: dict[str, int] = {}
        for name, future in futures.items():
            if future not in done:
                future.cancel()
                status[name] = "timeout"
                continue
            try:
                replies[name], latency[name] = future.result()
                status[name] = "ok"
            except Exception as e:
                status[name] = "error"
                logger.warning("query rewriter %s call failed: %s", name, e)

        rewrite = parse_rewrite(replies["rewrite"]) if "rewrite" in replies else None
        if "rewrite" in replies and rewrite is None:
            status["rewrite"] = "parse_error"

        step_back = None
        if "step_back" in replies:
            step_back = parse_step_back(replies["step_back"])
            if step_back is None:
                status["step_back"] = "parse_error"

        sub_queries: list[str] = []
        if "decomposition" in replies:
            parsed = parse_sub_queries(replies["decomposition"])
            if parsed is None:
                status["decomposition"] = "parse_error"
            else:
                sub_queries = parsed

        if rewrite is None:
            result = RewrittenQuery(
                intent=SSO_QUESTION,
                rewritten_query=prompt.strip(),
                step_back_query=step_back,
                sub_queries=sub_queries,
                fallback=True,
            )
        elif rewrite[0] == CONVERSATIONAL:
            result = RewrittenQuery.conversational()
        else:
            result = RewrittenQuery(
                intent=SSO_QUESTION,
                rewritten_query=rewrite[1],
                step_back_query=step_back,
                sub_queries=sub_queries,
            )

        logger.info(json.dumps({
            "event": "query_rewrite",
            "intent": result.intent,
            "short_circuit": False,
            "fallback": result.fallback,
            "latency_ms": latency,
            "status": status,
            "n_sub_queries": len(result.sub_queries),
            "step_back": result.step_back_query is not None,
        }))
        trace(
            "query rewriter: output",
            prompt=prompt,
            history=[f"{role}: {text}" for role, text in history],
            intent=result.intent,
            rewritten_query=result.rewritten_query,
            sub_queries=result.sub_queries,
            step_back_query=result.step_back_query,
            fallback=result.fallback,
            status=status,
            latency_ms=latency,
            raw_replies=replies,
        )
        return result

    def retry(self, question: str, failed: list[str]) -> list[str]:
        """Alternative wording for queries that found nothing, aligned with
        `failed` (at most one per failed query). [] on any failure."""

        if not failed or not settings.query_rewriter_retry_enabled:
            return []

        future = _calls.submit(_timed_converse, RETRY_SYSTEM, retry_message(question, failed), 300)
        try:
            text, latency = future.result(timeout=settings.query_rewriter_timeout_seconds)
        except Exception as e:
            future.cancel()
            logger.warning("query rewriter retry failed: %s", e)
            return []

        queries = parse_retry(text, len(failed))
        trace("query rewriter: retry", question=question, failed_queries=failed,
              retry_queries=queries, raw_reply=text)
        logger.info(json.dumps({
            "event": "query_rewrite_retry",
            "failed": len(failed),
            "retry_queries": len(queries),
            "latency_ms": latency,
        }))
        return queries


query_rewriter = QueryRewriter()
