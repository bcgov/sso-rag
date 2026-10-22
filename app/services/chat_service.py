import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.agents.rag_agent import create_agent, replace_last_answer
from app.common.config import settings
from app.common.constants import NO_DOCUMENTATION_RESPONSE
from app.services.grounding import NOT_SUPPORTED, Verdict, grounding_block, split_grounding, verify
from app.tools.knowledge_base_tool import CITATIONS_KEY

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
REPLY_PREVIEW_CHARS = 300
LOGGED_SOURCES = 5


def replacement_log(
    verdict: Verdict,
    reply: str,
    retrieved: list[dict],
    stop_reason: str | None,
) -> dict:
    """Why the model's reply was swapped for the no-documentation reply:
    the grounding checks rejected it ("answer_rejected", with reasons), or
    the model itself labelled it NOT_SUPPORTED ("answer_not_supported").
    Includes what retrieval returned, to spot NOT_SUPPORTED despite strong
    results."""

    body, grounding = split_grounding(reply)
    scores = [r.get("score") or 0 for r in retrieved]
    return {
        "event": "answer_rejected" if verdict.rejected else "answer_not_supported",
        "reasons": verdict.reasons,
        "model_status": grounding.status if grounding else None,
        "model_citations": grounding.citations if grounding else [],
        # e.g. "limit_turns" when AGENT_MAX_TURNS cut the reply off.
        "stop_reason": stop_reason,
        "retrieved": len(retrieved),
        "top_score": round(max(scores), 3) if scores else None,
        "sources": list(dict.fromkeys(r.get("uri") for r in retrieved if r.get("uri")))[:LOGGED_SOURCES],
        "reply_chars": len(body),
        "reply_preview": body[:REPLY_PREVIEW_CHARS],
    }


class ChatService:

    async def stream(
        self,
        question: str,
        session_id: str,
        cancel_signal: threading.Event | None = None
    ) -> AsyncIterator[Status | Heartbeat | Answer]:
        """Runs the agent, buffering its reply (minus <thinking> blocks) so it
        can be checked against the retrieved documentation before anything
        is shown. Yields progress, then exactly one Answer."""

        # Agent construction restores the session snapshot synchronously, so
        # keep that storage I/O off the event loop.
        agent = await asyncio.to_thread(create_agent, session_id)
        thinking = ThinkingFilter()
        retrieved: list[dict] = []
        reply: list[str] = []
        status: str | None = None
        stop_reason: str | None = None
        last_sent = time.monotonic()

        async for event in agent.stream_async(
            question,
            invocation_state={CITATIONS_KEY: retrieved},
            cancel_signal=cancel_signal,
            # Hard stop for a model that keeps calling tools; the unfinished
            # reply then fails the grounding check and the no-docs reply is sent.
            limits={"turns": settings.agent_max_turns},
        ):
            if not isinstance(event, dict):
                continue
            if "result" in event:
                stop_reason = getattr(event["result"], "stop_reason", None)

            phase = None
            if event.get("current_tool_use"):
                phase = "searching"
            elif text := event.get("data"):
                if visible := thinking.feed(text):
                    reply.append(visible)
                    phase = "writing"

            if phase and phase != status:
                status = phase
                last_sent = time.monotonic()
                yield Status(phase)
            elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                last_sent = time.monotonic()
                yield Heartbeat()

        reply.append(thinking.flush())
        raw_reply = "".join(reply)

        verdict = verify(raw_reply, retrieved, question)
        if verdict.replaced:
            logger.warning(json.dumps(
                replacement_log(verdict, raw_reply, retrieved, stop_reason), default=str
            ))
            # Keep the delivered reply, not the rejected one, as context for
            # the next turn.
            await replace_last_answer(
                agent,
                session_id,
                f"{NO_DOCUMENTATION_RESPONSE}\n\n{grounding_block(NOT_SUPPORTED)}"
            )

        yield Answer(verdict.text, verdict.status, verdict.citations)


chat_service = ChatService()
