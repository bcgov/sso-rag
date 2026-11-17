from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    prompt: str


class ChatConfig(BaseModel):
    """Limits the UI applies before sending (the API enforces them too)."""
    max_prompt_tokens: int
    # The token limit as characters (~4 per token), for the input counter.
    max_prompt_chars: int
    # Whether to show the thumbs up/down buttons (POST /chat/feedback).
    feedback_enabled: bool
    # How long questions and answers are kept, for the notice under the
    # input; null when turns aren't stored.
    analytics_retention_days: int | None


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The `id` of the answer's stream chunks.
    turn_id: str = Field(
        pattern=r"^chatcmpl-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    # null clears the rating.
    rating: Literal["up", "down"] | None
