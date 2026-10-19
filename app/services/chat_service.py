import asyncio
import re
import threading
from collections.abc import AsyncIterator

from app.agents.rag_agent import create_agent
from app.tools.knowledge_base_tool import CITATIONS_KEY

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


def _links_to(answer: str, uri: str) -> bool:
    """True if the answer contains `uri` (with or without its #fragment) as a
    whole URL, not as a prefix of a longer one."""
    for candidate in {uri, uri.split("#", 1)[0]}:
        if re.search(re.escape(candidate) + r"(?=[)#\s>\"'\]]|$)", answer):
            return True
    return False


def select_citations(retrieved: list[dict], answer: str) -> list[dict]:
    """Sources for the answer: the retrieved results the answer links to
    (the prompt asks for markdown links from each result's "source:" line),
    or every retrieved result if it links none. Deduplicated by URL."""
    unique: dict[str, dict] = {}
    for c in retrieved:
        uri = c.get("uri")
        if uri and (uri not in unique or c["score"] > unique[uri]["score"]):
            unique[uri] = c

    cited = [c for uri, c in unique.items() if _links_to(answer, uri)]
    return cited or list(unique.values())


class ChatService:

    async def stream(
        self,
        question: str,
        session_id: str,
        cancel_signal: threading.Event | None = None
    ) -> AsyncIterator[str | list[dict]]:
        """Yields the answer text (str) as it is generated, minus <thinking>
        blocks, then once at the end the list of cited sources, if any."""

        # Agent construction restores the session snapshot synchronously, so
        # keep that storage I/O off the event loop.
        agent = await asyncio.to_thread(create_agent, session_id)
        thinking = ThinkingFilter()
        retrieved: list[dict] = []
        answer: list[str] = []

        async for event in agent.stream_async(
            question,
            invocation_state={CITATIONS_KEY: retrieved},
            cancel_signal=cancel_signal
        ):
            text = event.get("data") if isinstance(event, dict) else None
            if text:
                if visible := thinking.feed(text):
                    answer.append(visible)
                    yield visible

        if tail := thinking.flush():
            answer.append(tail)
            yield tail

        if citations := select_citations(retrieved, "".join(answer)):
            yield citations


chat_service = ChatService()
