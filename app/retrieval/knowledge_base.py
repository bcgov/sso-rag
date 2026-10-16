from app.common.config import settings
from app.retrieval.bedrock_client import (
    get_bedrock_runtime_client,
)
from app.retrieval.models import (
    RetrievedChunk
)


def retrieve(
    query: str,
    max_results: int | None = None
) -> list[RetrievedChunk]:

    client = get_bedrock_runtime_client()

    response = client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={
            "text": query
        },
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults":
                max_results
                or settings.number_of_results
            }
        }
    )

    chunks = []

    for item in response.get(
        "retrievalResults",
        []
    ):

        source = None

        if "location" in item:
            source = str(
                item["location"]
            )

        chunks.append(
            RetrievedChunk(
                text=item["content"]["text"],
                score=item.get(
                    "score",
                    0.0
                ),
                source=source
            )
        )

    return chunks