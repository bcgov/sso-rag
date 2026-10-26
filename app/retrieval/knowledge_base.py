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
    RetrievedChunk,
    SearchQuery
)

logger = logging.getLogger(__name__)

# 1 rewritten query + up to 4 sub-queries + 1 step-back query.
MAX_QUERIES = 6

MAX_RERANKED = 12

# Origins that stand for a part of the question. Each widens the reranked
# result count; step-back queries only add background, so they don't.
FACET_ORIGINS = {"rewrite", "sub", "retry"}


def _chunk_key(chunk: RetrievedChunk) -> str:
    return hashlib.sha1(chunk.text.encode()).hexdigest()


def _dedupe(queries: list[SearchQuery]) -> list[SearchQuery]:
    seen: set[str] = set()
    unique: list[SearchQuery] = []
    for q in queries:
        text = " ".join(q.text.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            unique.append(SearchQuery(text=text, origin=q.origin))
    return unique


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


def retrieve(
    query: str,
    max_results: int | None = None
) -> list[RetrievedChunk]:
    """Vector search against the knowledge base (no reranking)."""

    client = get_bedrock_runtime_client()

    vector_cfg = {"numberOfResults": max_results or settings.number_of_results}
    if settings.search_type:
        vector_cfg["overrideSearchType"] = settings.search_type

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


def multi_search(
    question: str,
    queries: list[SearchQuery]
) -> tuple[list[RetrievedChunk], dict]:
    """Search each query in parallel, dedupe, and rerank against the question.

    `queries` come from the query rewriter, tagged with their origin
    ("rewrite", "sub", "step_back" or "retry"). Each returned chunk's
    metadata gets a "_queries" list naming the queries that found it, and
    stats["missing"] lists the queries with no surviving hit, so callers can
    tell which parts of the question came back empty.
    """

    queries = _dedupe(queries)[:MAX_QUERIES] or [SearchQuery(text=question, origin="rewrite")]
    per_query = max(5, settings.number_of_results // len(queries))
    expanded = [expand_query(q.text) for q in queries]

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        result_sets = list(pool.map(lambda q: retrieve(q, per_query), expanded))

    merged: dict[str, RetrievedChunk] = {}
    for query, results in zip(queries, result_sets):
        for r in results:
            key = _chunk_key(r)
            if key not in merged:
                merged[key] = r.model_copy(update={"metadata": {**r.metadata, "_queries": []}})
            elif r.score > merged[key].score:
                merged[key].score = r.score
            if query.text not in merged[key].metadata["_queries"]:
                merged[key].metadata["_queries"].append(query.text)

    candidates = sorted(merged.values(), key=lambda c: c.score, reverse=True)
    # Give each extra facet some room so one facet can't crowd out the rest
    facets = max(1, sum(q.origin in FACET_ORIGINS for q in queries))
    top_n = min(MAX_RERANKED, settings.number_of_reranked_results + 2 * (facets - 1))

    reranked = False
    if settings.reranker_model_arn:
        try:
            hits = rerank(question, candidates, top_n)
            hits = [h for h in hits if h.score >= settings.min_rerank_score]
            reranked = True
        except get_bedrock_runtime_client().exceptions.ValidationException as e:
            logger.warning("rerank rejected (%s); falling back to vector scores", e)
            hits = candidates[:top_n]
    else:
        hits = candidates[:top_n]

    found = {q for h in hits for q in h.metadata.get("_queries", [])}
    stats = {
        "queries": [q.text for q in queries],
        "origins": [q.origin for q in queries],
        "expanded": [e for q, e in zip(queries, expanded) if e != q.text],
        "candidates": len(candidates),
        "reranked": reranked,
        "scores": [round(h.score, 3) for h in hits],
        "missing": [q.text for q in queries if q.text not in found],
    }
    return hits, stats


def merge_hits(
    first: list[RetrievedChunk],
    second: list[RetrievedChunk],
    limit: int = MAX_RERANKED
) -> list[RetrievedChunk]:
    """Union of two hit lists (same chunk text = same hit, best score and all
    finding queries kept), highest score first."""

    merged: dict[str, RetrievedChunk] = {}
    for hit in [*first, *second]:
        key = _chunk_key(hit)
        if key not in merged:
            merged[key] = hit.model_copy(update={"metadata": {
                **hit.metadata, "_queries": list(hit.metadata.get("_queries", []))
            }})
            continue
        kept = merged[key]
        kept.score = max(kept.score, hit.score)
        for q in hit.metadata.get("_queries", []):
            if q not in kept.metadata["_queries"]:
                kept.metadata["_queries"].append(q)

    return sorted(merged.values(), key=lambda c: c.score, reverse=True)[:limit]