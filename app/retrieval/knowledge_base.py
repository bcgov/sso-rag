import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from app.common.config import settings
from app.common.constants import ACRONYMS
from app.retrieval.bedrock_client import (
    get_bedrock_runtime_client,
)
from app.retrieval.models import (
    RetrievedChunk
)

logger = logging.getLogger(__name__)

MAX_QUERIES = 4


def expand_query(q: str) -> str:
    """Append the spelled-out form of any acronym the query uses."""
    low = q.lower()
    extras = [
        v for k, v in ACRONYMS.items()
        if re.search(rf"\b{re.escape(k)}\b", low) and v.lower() not in low
    ]
    return q if not extras else f"{q} ({'; '.join(dict.fromkeys(extras))})"


def _source_uri(item: dict) -> str | None:
    location = item.get("location") or {}
    for value in location.values():
        if isinstance(value, dict) and value.get("uri"):
            return value["uri"]
    return None


def chunk_key(chunk: RetrievedChunk) -> str:
    return hashlib.sha1(chunk.text.encode()).hexdigest()


def retrieve(
    query: str,
    max_results: int | None = None,
    retrieval_filter: dict | None = None
) -> list[RetrievedChunk]:
    """Vector search against the knowledge base (no reranking)."""

    client = get_bedrock_runtime_client()

    vector_cfg = {"numberOfResults": max_results or settings.number_of_results}
    if settings.search_type:
        vector_cfg["overrideSearchType"] = settings.search_type
    if retrieval_filter:
        vector_cfg["filter"] = retrieval_filter

    response = client.retrieve(
        knowledgeBaseId=settings.knowledge_base_id,
        retrievalQuery={
            "text": query
        },
        retrievalConfiguration={
            "vectorSearchConfiguration": vector_cfg
        }
    )

    return [
        RetrievedChunk(
            text=item["content"]["text"],
            score=item.get("score", 0.0),
            source=_source_uri(item),
            metadata=item.get("metadata", {})
        )
        for item in response.get("retrievalResults", [])
    ]


def rerank(
    question: str,
    chunks: list[RetrievedChunk],
    top_n: int
) -> list[RetrievedChunk]:
    """Rerank chunks against the question with the Bedrock reranker.

    Returned chunks carry the reranker's relevance score.
    """

    if not chunks:
        return []

    response = get_bedrock_runtime_client().rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": question}}],
        sources=[
            {
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {"text": c.text}
                }
            }
            for c in chunks
        ],
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": settings.reranker_model_arn},
                "numberOfResults": min(top_n, len(chunks)),
            },
        },
    )

    return [
        chunks[r["index"]].model_copy(update={"score": r["relevanceScore"]})
        for r in sorted(response["results"], key=lambda r: r["relevanceScore"], reverse=True)
    ]


def search_round(
    queries: list[str],
    per_query: int,
    retrieval_filter: dict | None = None,
    pool: dict[str, RetrievedChunk] | None = None,
    labels: list[str] | None = None,
) -> tuple[dict[str, RetrievedChunk], set[str]]:
    """Search each query in parallel and merge the results into `pool`
    (deduplicated by chunk text, keeping the best vector score).

    Each chunk's metadata gets a "_queries" list naming the queries (or
    `labels`, if given) that found it, so callers can tell which facets came
    back empty. A filtered query that finds nothing is retried unfiltered.
    Returns the pool and the keys first seen in this round.
    """

    pool = {} if pool is None else pool
    labels = labels or queries
    if not queries:
        return pool, set()
    expanded = [expand_query(q) for q in queries]

    def run(q: str) -> list[RetrievedChunk]:
        results = retrieve(q, per_query, retrieval_filter)
        if retrieval_filter and not results:
            results = retrieve(q, per_query)
        return results

    with ThreadPoolExecutor(max_workers=len(queries)) as pool_exec:
        result_sets = list(pool_exec.map(run, expanded))

    new: set[str] = set()
    for label, results in zip(labels, result_sets):
        for r in results:
            key = chunk_key(r)
            if key not in pool:
                pool[key] = r.model_copy(update={"metadata": {**r.metadata, "_queries": []}})
                new.add(key)
            elif r.score > pool[key].score:
                pool[key].score = r.score
            if label not in pool[key].metadata["_queries"]:
                pool[key].metadata["_queries"].append(label)
    return pool, new


def rank(
    question: str,
    candidates: list[RetrievedChunk],
    top_n: int
) -> tuple[list[RetrievedChunk], bool]:
    """Rerank `candidates` against `question` and keep the relevant top_n.
    Falls back to vector scores if no reranker is configured or it rejects
    the request. Returns (hits, reranked)."""

    candidates = sorted(candidates, key=lambda c: c.score, reverse=True)
    if settings.reranker_model_arn and candidates:
        try:
            hits = rerank(question, candidates, top_n)
            return [h for h in hits if h.score >= settings.min_rerank_score], True
        except get_bedrock_runtime_client().exceptions.ValidationException as e:
            logger.warning("rerank rejected (%s); falling back to vector scores", e)
    return candidates[:top_n], False


def top_n_for(facets: int) -> int:
    # Give each extra facet some room so one facet can't crowd out the rest
    return min(12, settings.number_of_reranked_results + 2 * (max(1, facets) - 1))


def multi_search(
    question: str,
    queries: list[str]
) -> tuple[list[RetrievedChunk], dict]:
    """Search each query in parallel, dedupe, and rerank against the question.

    Each returned chunk's metadata gets a "_queries" list naming the queries
    that found it, so callers can tell which facets came back empty.
    """

    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:MAX_QUERIES] or [question]
    per_query = max(10, settings.number_of_results // len(queries))

    pool, _ = search_round(queries, per_query)
    hits, reranked = rank(question, list(pool.values()), top_n_for(len(queries)))

    stats = {
        "queries": queries,
        "expanded": [e for q in queries if (e := expand_query(q)) != q],
        "candidates": len(pool),
        "reranked": reranked,
        "scores": [round(h.score, 3) for h in hits],
    }
    return hits, stats
