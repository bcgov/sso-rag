from pydantic import BaseModel


class RetrievedChunk(BaseModel):
    text: str
    score: float
    source: str | None = None