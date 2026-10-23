"""Local-only trace logs for checking the pipeline's inputs and outputs.

Prints the query rewriter's output, what the search tool ran and found, and
the agent's raw reply before the grounding check, plus the check's verdict.
Enabled only when ENVIRONMENT=local (and DEBUG_TRACE is not false), so
prompts, history and document text never reach deployed logs.

Uses its own handler, so it prints under `uvicorn main:app` even though the
app's root logger isn't configured there.
"""

import json
import logging
import sys

from app.common.config import settings

_logger = logging.getLogger("sso_rag.trace")
_logger.propagate = False

PREVIEW_CHARS = 300


def enabled() -> bool:
    return settings.environment == "local" and settings.debug_trace


if enabled() and not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s TRACE %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.DEBUG)


def preview(text: str | None, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def trace(event: str, **fields) -> None:
    """Logs one event as indented JSON when tracing is enabled."""
    if not enabled():
        return
    body = json.dumps(fields, indent=2, ensure_ascii=False, default=str)
    _logger.debug("──── %s ────\n%s", event, body)
