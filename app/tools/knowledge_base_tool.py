import json

from strands import tool

from app.retrieval.knowledge_base import (
    retrieve
)


@tool
def search_knowledge_base(
    question: str
) -> str:
    """
    Search enterprise documents.

    Use whenever factual
    information is required.
    """

    results = retrieve(question)

    payload = []

    for chunk in results:
        payload.append(
            {
                "source":
                    chunk.source,
                "score":
                    chunk.score,
                "text":
                    chunk.text
            }
        )

    return json.dumps(
        payload,
        indent=2
    )