from typing import Literal

from pydantic import BaseModel


class RetrievedChunk(BaseModel):
    text: str
    score: float
    source: str | None = None
    metadata: dict = {}


class SearchQuery(BaseModel):
    """One search string and the query-rewriter technique that produced it."""
    text: str
    origin: Literal["rewrite", "sub", "step_back", "retry"]