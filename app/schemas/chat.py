from pydantic import BaseModel


class ChatRequest(BaseModel):
    prompt: str


class ChatConfig(BaseModel):
    """Limits the UI applies before sending (the API enforces them too)."""
    max_prompt_tokens: int
    # The token limit as characters (~4 per token), for the input counter.
    max_prompt_chars: int
