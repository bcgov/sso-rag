"""Checks on the user's message before any model call: blank and too-long
messages are rejected with a friendly reason, and messages that are only
links are answered with a fixed clarifying question (LINK_ONLY_RESPONSE)."""

import math
import re

from app.common.config import settings

# Tokens are estimated, not counted: the deployed model's tokenizer isn't
# available locally. ~4 characters per token is the usual estimate for
# English; the UI uses the same character limit (GET /chat/config).
CHARS_PER_TOKEN = 4

_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


class PromptRejected(ValueError):
    """The message can't be sent; `str(error)` is shown to the user."""


def max_prompt_chars() -> int:
    return settings.max_prompt_tokens * CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def check_prompt(prompt: str) -> str:
    """The message with surrounding whitespace removed, or PromptRejected."""

    prompt = prompt.strip()
    if not prompt:
        raise PromptRejected("Please enter a question.")
    if len(prompt) > max_prompt_chars():
        raise PromptRejected(
            f"Your message is too long (about {estimate_tokens(prompt):,} tokens). "
            f"Please shorten it to about {settings.max_prompt_tokens:,} tokens "
            f"({max_prompt_chars():,} characters) and try again."
        )
    return prompt


def is_links_only(prompt: str) -> bool:
    """True when the message has at least one link and nothing else but
    punctuation and whitespace."""
    if not _LINK_RE.search(prompt):
        return False
    return not re.search(r"\w", _LINK_RE.sub(" ", prompt))
