from typing import Literal

from pydantic import BaseModel


class RetrievedChunk(BaseModel):
    text: str
    score: float
    source: str | None = None
    metadata: dict = {}


class SearchQuery(BaseModel):
    """One search string and the query-rewriter technique that produced it.
    "general" is a general_auth question's generic query, searched alongside
    its BC Gov query ("rewrite")."""
    text: str
    origin: Literal["rewrite", "general", "sub", "step_back", "retry"]