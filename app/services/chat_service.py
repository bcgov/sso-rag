import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from strands.types.exceptions import MaxTokensReachedException

from app.agents.rag_agent import (
    create_agent,
    drop_repair_exchange,
    replace_last_answer,
    save_session,
)
from app.common.config import settings
from app.common.constants import NO_DOCUMENTATION_RESPONSE
from app.common.trace import preview, trace
from app.prompts.system_prompt import repair_request, search_request, shorten_request
from app.services.grounding import NOT_SUPPORTED, Verdict, grounding_block, verify
from app.services.query_rewriter import (
    CONVERSATIONAL,
    RewrittenQuery,
    query_rewriter,
    recent_history,
)
from app.tools.knowledge_base_tool import CITATIONS_KEY, SEARCH_TURN_KEY, SearchTurn

logger = logging.getLogger(__name__)

OPEN_TAG = "<thinking>"
CLOSE_TAG = "</thinking>"


def _partial_tag_len(text: str, tag: str) -> int:
    """Length of the longest suffix of `text` that is a prefix of `tag`."""
    lower = text.lower()
    for n in range(min(len(tag) - 1, len(lower)), 0, -1):
        if tag.startswith(lower[-n:]):
            return n
    return 0


class ThinkingFilter:
    """Some models (e.g. Amazon Nova) write their reasoning into the answer
    text as <thinking>...</thinking>, especially around tool calls; this drops
    it from the stream. An unclosed tag means the reasoning ran to the end of
    the text, so everything after it goes too. Tags can be split across
    deltas, so a possible partial tag at the end of the buffer is held back
    until the next delta resolves it."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False
        self._started = False
        self._newlines = 0

    def feed(self, delta: str) -> str:
        """
        Feed a new chunk of text to the filter.

        Args:
            delta: The new text chunk.

        Returns:
            The text with <thinking>...</thinking> sections removed.
        """
        self._buffer += delta
        out = []

        while self._buffer:
            lower = self._buffer.lower()
            if self._in_thinking:
                end = lower.find(CLOSE_TAG)
                if end == -1:
                    keep = _partial_tag_len(self._buffer, CLOSE_TAG)
                    self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
                    break
                self._buffer = self._buffer[end + len(CLOSE_TAG):]
                self._in_thinking = False
                continue

            start = lower.find(OPEN_TAG)
            stray = lower.find(CLOSE_TAG)
            if stray != -1 and (start == -1 or stray < start):
                out.append(self._buffer[:stray])
                self._buffer = self._buffer[stray + len(CLOSE_TAG):]
                continue
            if start != -1:
                out.append(self._buffer[:start])
                self._buffer = self._buffer[start + len(OPEN_TAG):]
                self._in_thinking = True
                continue

            keep = max(
                _partial_tag_len(self._buffer, OPEN_TAG),
                _partial_tag_len(self._buffer, CLOSE_TAG),
            )
            cut = len(self._buffer) - keep
            out.append(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
            break

        return self._tidy("".join(out))

    def flush(self) -> str:
        """
        Flush the remaining text in the buffer.

        Returns:
            The text with <thinking>...</thinking> sections removed.
        """
        rest = "" if self._in_thinking else self._buffer
        self._buffer = ""
        return self._tidy(rest)

    def _tidy(self, text: str) -> str:
        # Drop leading whitespace and collapse runs of 3+ newlines, across
        # delta boundaries.
        if not self._started:
            text = text.lstrip()
            if not text:
                return ""
            self._started = True

        out = []
        for ch in text:
            if ch == "\n":
                self._newlines += 1
                if self._newlines > 2:
                    continue
            elif not ch.isspace():
                self._newlines = 0
            out.append(ch)
        return "".join(out)

@dataclass(frozen=True)
class Status:
    """Progress while the answer is prepared: "searching" (a knowledge base
    search is running) or "writing" (the model is writing the answer, which
    is checked before it is sent)."""
    value: str


@dataclass(frozen=True)
class Heartbeat:
    """Nothing to send yet; keeps idle proxies from closing the stream."""


@dataclass(frozen=True)
class Answer:
    """The checked answer: the model's text, or the no-documentation reply
    if it was rejected."""
    text: str
    grounding_status: str | None
    citations: list[dict]


HEARTBEAT_SECONDS = 10


class _Progress:
    """Status/heartbeat bookkeeping shared by the agent runs of one turn."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.last_sent = time.monotonic()

    def update(self, phase: str | None) -> Status | Heartbeat | None:
        if phase and phase != self.status:
            self.status = phase
            self.last_sent = time.monotonic()
            return Status(phase)
        if time.monotonic() - self.last_sent >= HEARTBEAT_SECONDS:
            self.last_sent = time.monotonic()
            return Heartbeat()
        return None


TRUNCATED_REASON = (
    "the reply was cut off at the maximum output length before it was "
    "finished; write a shorter, complete reply"
)

SKIPPED_SEARCH_REASON = "the reply was written without searching the documentation"

# Why a reply was rejected, for the logs and the repair request.
CAUSE_SKIPPED_SEARCH = "skipped_search"
CAUSE_TRUNCATED = "truncated"
CAUSE_GROUNDING = "grounding"


@dataclass
class _Run:
    """The visible text of one agent invocation, and whether the model
    stopped at the output-token limit."""
    parts: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _log_model_call(attempt: str, stop_reason: str | None, usage: dict) -> None:
    """One line per model call, so the output-token distribution (reasoning
    included) can be compared with MODEL_MAX_TOKENS, and the input-token
    growth with the conversation window."""
    logger.info(json.dumps({
        "event": "model_call",
        "attempt": attempt,
        "stop_reason": stop_reason,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "max_tokens": settings.model_max_tokens,
    }))


async def _run_agent(
    agent,
    prompt: str,
    invocation_state: dict,
    cancel_signal: threading.Event | None,
    progress: _Progress,
    run: _Run,
    attempt: str,
) -> AsyncIterator[Status | Heartbeat]:
    """One agent invocation: collects the visible reply text (minus
    <thinking> blocks) in `run` and yields progress."""

    thinking = ThinkingFilter()
    stop_reason = None
    try:
        async for event in agent.stream_async(
            prompt,
            invocation_state=invocation_state,
            cancel_signal=cancel_signal
        ):
            if not isinstance(event, dict):
                continue

            # Raw model chunks: Bedrock sends messageStop, then the usage.
            chunk = event.get("event")
            if isinstance(chunk, dict):
                if "messageStop" in chunk:
                    stop_reason = chunk["messageStop"].get("stopReason")
                elif "metadata" in chunk:
                    _log_model_call(attempt, stop_reason, chunk["metadata"].get("usage") or {})

            phase = None
            if event.get("current_tool_use"):
                phase = "searching"
            elif text := event.get("data"):
                if visible := thinking.feed(text):
                    run.parts.append(visible)
                    phase = "writing"

            if item := progress.update(phase):
                yield item
    except MaxTokensReachedException:
        # Strands keeps the partial reply in agent.messages; the caller
        # replaces or removes it.
        logger.warning(json.dumps({
            "event": "max_tokens_reached",
            "attempt": attempt,
            "max_tokens": settings.model_max_tokens,
            "chars": len(run.text),
        }))
        run.truncated = True

    run.parts.append(thinking.flush())


async def _wait_for_rewrite(turn: SearchTurn) -> RewrittenQuery | None:
    """The rewriter's result for the turn, waiting for it (up to the rewriter
    timeout) if it is still running. None if it failed, was cancelled or
    timed out: the intent is then unknown and treated as needing a search."""

    future = turn.rewrite
    if not future.done():
        await asyncio.wait(
            [asyncio.wrap_future(future)],
            timeout=settings.query_rewriter_timeout_seconds,
        )
    if not future.done() or future.cancelled() or future.exception() is not None:
        return None
    return future.result()


def _ok_without_search(verdict: Verdict, conversational: bool) -> bool:
    """Whether a reply written without a search may be delivered: a
    clarifying question, or a conversational reply to a message classified
    as conversational. Anything else (an answer, a no-documentation reply, a
    reply labelled conversational for an SSO question) skipped the search."""

    if verdict.rejected or verdict.replaced:
        return False
    if verdict.response_type == "clarification":
        return True
    return conversational and verdict.response_type == "conversational"


def _check(
    run: _Run,
    retrieved: list[dict],
    question: str,
    turn: SearchTurn,
    conversational: bool,
) -> tuple[Verdict, bool]:
    """The verdict for one agent run, and whether it skipped a search the
    turn needed (the verdict is then the no-documentation reply)."""

    if turn.searched:
        conversational = False
    if run.truncated:
        verdict = Verdict(NO_DOCUMENTATION_RESPONSE, NOT_SUPPORTED, reasons=[TRUNCATED_REASON])
    else:
        verdict = verify(run.text, retrieved, question, conversational=conversational)

    if turn.searched or _ok_without_search(verdict, conversational):
        return verdict, False
    return Verdict(
        NO_DOCUMENTATION_RESPONSE,
        NOT_SUPPORTED,
        reasons=[SKIPPED_SEARCH_REASON, *verdict.reasons],
        response_type=verdict.response_type,
    ), True


def _trace_attempt(attempt: str, prompt: str, run: _Run, retrieved: list[dict], verdict: Verdict) -> None:
    """Local-only: the agent's reply as the grounding check received it
    (thinking removed, grounding block still attached), and the verdict."""
    trace(
        f"agent reply before grounding check ({attempt} attempt)",
        prompt=prompt,
        cut_off_at_max_tokens=run.truncated,
        raw_reply=run.text,
        evidence=[f"{r.get('score', 0):.3f} {r.get('uri')}" for r in retrieved],
    )
    trace(
        f"grounding verdict ({attempt} attempt)",
        rejected=verdict.rejected,
        reasons=verdict.reasons,
        replaced_with_no_documentation_reply=verdict.replaced,
        grounding_status=verdict.status,
        citations=[c.get("uri") for c in verdict.citations],
        delivered_text=preview(verdict.text, 600),
    )


def _cause(verdict: Verdict) -> str:
    """The main reason a reply was rejected: a skipped search, the
    output-token limit, or the grounding checks."""
    if SKIPPED_SEARCH_REASON in verdict.reasons:
        return CAUSE_SKIPPED_SEARCH
    if TRUNCATED_REASON in verdict.reasons:
        return CAUSE_TRUNCATED
    return CAUSE_GROUNDING


def _log_rejection(verdict, retrieved: list[dict], attempt: str) -> None:
    logger.warning(json.dumps({
        "event": "answer_rejected",
        "attempt": attempt,
        "cause": _cause(verdict),
        "reasons": verdict.reasons,
        "retrieved": len(retrieved),
    }))


def _repair_prompt(cause: str, reasons: list[str]) -> str:
    if cause == CAUSE_SKIPPED_SEARCH:
        return search_request()
    if cause == CAUSE_TRUNCATED:
        return shorten_request()
    return repair_request(reasons)


class ChatService:

    async def stream(
        self,
        question: str,
        session_id: str,
        cancel_signal: threading.Event | None = None
    ) -> AsyncIterator[Status | Heartbeat | Answer]:
        """Runs the agent, buffering its reply (minus <thinking> blocks) so it
        can be checked against the retrieved documentation before anything
        is shown. Gets one repair attempt:

        - a reply that fails the checks or is cut off at the output-token
          limit, when the search did return results: the agent is told why
          and rewrites it;
        - a reply written without searching, when the turn needs a search
          (the rewriter didn't classify it as conversational, or the reply
          isn't conversational): the agent is told to search and answer.
          Only clarifying questions may skip the search.

        Yields progress, then exactly one Answer."""

        # Agent construction restores the session snapshot synchronously, so
        # keep that storage I/O off the event loop.
        agent = await asyncio.to_thread(create_agent, session_id)
        # The query rewriter runs while the agent makes its first model call;
        # the search tool waits for it only if the agent searches. History is
        # read before stream_async appends this turn.
        history = recent_history(agent.messages, settings.query_rewriter_history_turns)
        turn = SearchTurn(
            prompt=question,
            rewrite=query_rewriter.submit(question, history),
            history=history,
        )
        retrieved: list[dict] = []
        # The same state for a repair run: after a search the tool answers
        # ALREADY_SEARCHED, so the evidence for the second check is unchanged.
        state = {CITATIONS_KEY: retrieved, SEARCH_TURN_KEY: turn}
        progress = _Progress()
        run = _Run()

        try:
            async for item in _run_agent(agent, question, state, cancel_signal, progress, run, "first"):
                yield item

            conversational = False
            if not turn.searched:
                rewrite = await _wait_for_rewrite(turn)
                conversational = rewrite is not None and rewrite.intent == CONVERSATIONAL
            verdict, skipped = _check(run, retrieved, question, turn, conversational)
            _trace_attempt("first", question, run, retrieved, verdict)
            if skipped:
                logger.warning(json.dumps({
                    "event": "search_skipped_by_model",
                    "rewriter_intent": CONVERSATIONAL if conversational else "sso_question_or_unknown",
                    "response_type": verdict.response_type,
                }))

            feedback = None
            if (
                verdict.rejected
                and (retrieved or skipped)
                and settings.grounding_repair_enabled
                and not (cancel_signal and cancel_signal.is_set())
            ):
                _log_rejection(verdict, retrieved, "first")
                cause = _cause(verdict)
                feedback = _repair_prompt(cause, verdict.reasons)
                run = _Run()
                async for item in _run_agent(agent, feedback, state, cancel_signal, progress, run, "repair"):
                    yield item
                verdict, _ = _check(run, retrieved, question, turn, conversational)
                _trace_attempt("repair", feedback, run, retrieved, verdict)
                drop_repair_exchange(agent, feedback)
                logger.info(json.dumps({
                    "event": "answer_repair",
                    "kind": cause,
                    "searched": turn.searched,
                    "repaired": not verdict.replaced,
                }))
        finally:
            # Drops the rewrite if it is still queued (e.g. client disconnected).
            turn.rewrite.cancel()

        if verdict.rejected:
            _log_rejection(verdict, retrieved, "repair" if feedback else "first")

        if verdict.replaced:
            # Keep the delivered reply, not the rejected one, as context for
            # the next turn.
            await replace_last_answer(
                agent,
                session_id,
                f"{NO_DOCUMENTATION_RESPONSE}\n\n{grounding_block(NOT_SUPPORTED)}"
            )
        elif feedback:
            await save_session(agent, session_id)

        yield Answer(verdict.text, verdict.status, verdict.citations)


chat_service = ChatService()
