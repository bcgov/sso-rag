from app.common.config import settings
import re
from app.common.constants import GLOSSARY
from app.retrieval.bedrock_client import (
    get_bedrock_runtime_client,
)
from app.retrieval.models import (
    RetrievedChunk
)

def expand_query(q: str) -> str:
    low = q.lower()
    extras = [v for k, v in GLOSSARY.items() if re.search(rf"\b{re.escape(k)}\b", low)]
    return q if not extras else f"{q}\n\nRelated terms: " + "; ".join(extras)

def retrieve(
    query: str,
    max_results: int | None = None,
    advanced: bool = True
) -> list[RetrievedChunk]:
    """advanced=False skips the search-type override and reranking."""

    client = get_bedrock_runtime_client()

    vector_cfg = {"numberOfResults": max_results or settings.number_of_results}
    if advanced and settings.search_type:
        vector_cfg["overrideSearchType"] = settings.search_type
    if advanced and settings.reranker_model_arn:
        vector_cfg["rerankingConfiguration"] = {
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": settings.reranker_model_arn},
                "numberOfRerankedResults": settings.number_of_reranked_results,
            },
        }

    response = client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={
            "text": query
        },
        retrievalConfiguration={
            "vectorSearchConfiguration": vector_cfg
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
                source=source,
                metadata=item.get("metadata", {})
            )
        )

    return chunks