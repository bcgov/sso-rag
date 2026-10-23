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
from app.prompts.system_prompt import repair_request
from app.services.grounding import NOT_SUPPORTED, Verdict, grounding_block, verify
from app.services.query_rewriter import query_rewriter, recent_history
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


@dataclass
class _Run:
    """The visible text of one agent invocation, and whether the model
    stopped at the output-token limit."""
    parts: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


async def _run_agent(
    agent,
    prompt: str,
    invocation_state: dict,
    cancel_signal: threading.Event | None,
    progress: _Progress,
    run: _Run,
) -> AsyncIterator[Status | Heartbeat]:
    """One agent invocation: collects the visible reply text (minus
    <thinking> blocks) in `run` and yields progress."""

    thinking = ThinkingFilter()
    try:
        async for event in agent.stream_async(
            prompt,
            invocation_state=invocation_state,
            cancel_signal=cancel_signal
        ):
            if not isinstance(event, dict):
                continue

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
            "max_tokens": settings.model_max_tokens,
            "chars": len(run.text),
        }))
        run.truncated = True

    run.parts.append(thinking.flush())


def _check(run: _Run, retrieved: list[dict], question: str) -> Verdict:
    if run.truncated:
        return Verdict(NO_DOCUMENTATION_RESPONSE, NOT_SUPPORTED, reasons=[TRUNCATED_REASON])
    return verify(run.text, retrieved, question)


def _log_rejection(verdict, retrieved: list[dict], attempt: str) -> None:
    logger.warning(json.dumps({
        "event": "answer_rejected",
        "attempt": attempt,
        "reasons": verdict.reasons,
        "retrieved": len(retrieved),
    }))


class ChatService:

    async def stream(
        self,
        question: str,
        session_id: str,
        cancel_signal: threading.Event | None = None
    ) -> AsyncIterator[Status | Heartbeat | Answer]:
        """Runs the agent, buffering its reply (minus <thinking> blocks) so it
        can be checked against the retrieved documentation before anything
        is shown. A reply that fails the checks or is cut off at the
        output-token limit, when the search did return results, gets one
        repair attempt: the agent is told why and rewrites it. Yields
        progress, then exactly one Answer."""

        # Agent construction restores the session snapshot synchronously, so
        # keep that storage I/O off the event loop.
        agent = await asyncio.to_thread(create_agent, session_id)
        # The query rewriter runs while the agent makes its first model call;
        # the search tool waits for it only if the agent searches. History is
        # read before stream_async appends this turn.
        turn = SearchTurn(
            prompt=question,
            rewrite=query_rewriter.submit(
                question,
                recent_history(agent.messages, settings.query_rewriter_history_turns)
            )
        )
        retrieved: list[dict] = []
        # The same state for a repair run: the tool answers ALREADY_SEARCHED,
        # so the evidence for the second check is unchanged.
        state = {CITATIONS_KEY: retrieved, SEARCH_TURN_KEY: turn}
        progress = _Progress()
        run = _Run()

        try:
            async for item in _run_agent(agent, question, state, cancel_signal, progress, run):
                yield item
        finally:
            # Drops the rewrite if it is still queued (e.g. client disconnected).
            turn.rewrite.cancel()

        verdict = _check(run, retrieved, question)

        feedback = None
        if (
            verdict.rejected
            and retrieved
            and settings.grounding_repair_enabled
            and not (cancel_signal and cancel_signal.is_set())
        ):
            _log_rejection(verdict, retrieved, "first")
            feedback = repair_request(verdict.reasons)
            run = _Run()
            async for item in _run_agent(agent, feedback, state, cancel_signal, progress, run):
                yield item
            verdict = _check(run, retrieved, question)
            drop_repair_exchange(agent, feedback)
            logger.info(json.dumps({"event": "answer_repair", "repaired": not verdict.replaced}))

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
