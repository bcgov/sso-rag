import boto3

from botocore.config import Config

from app.common.config import settings

_client = None
_model_client = None

# Model families with explicit Bedrock prompt caching (cachePoint blocks in
# `system` / `messages`). Others (e.g. Meta Llama, Moonshot Kimi) reject
# cache points, so they are only sent to these.
_PROMPT_CACHING_MODEL_MARKERS = ("amazon.nova", "anthropic.claude")


def supports_prompt_caching(model_id: str | None) -> bool:
    """Whether a Bedrock model id, inference profile id or ARN belongs to a
    model family that accepts cache points."""
    model_id = (model_id or "").lower()
    return any(marker in model_id for marker in _PROMPT_CACHING_MODEL_MARKERS)


def cached_system(text: str, enabled: bool) -> list[dict]:
    """Converse `system` blocks: the text, followed by a cache point when
    caching is enabled. The cache point marks the whole prefix before it
    (tool definitions and system prompt) as cacheable; a prefix below the
    model's minimum (1K tokens for Nova) is simply not cached."""
    blocks: list[dict] = [{"text": text}]
    if enabled:
        blocks.append({"cachePoint": {"type": "default"}})
    return blocks


def get_bedrock_model_client():
    """`bedrock-runtime` client for direct model calls (Converse), used by the
    query rewriter. Timeouts are short: the rewriter fails open, so a slow
    call should give up rather than hold the search."""
    global _model_client

    if _model_client is None:
        timeout = settings.query_rewriter_timeout_seconds
        _model_client = boto3.client(
            "bedrock-runtime",
            config=Config(
                region_name=settings.aws_region,
                connect_timeout=timeout,
                read_timeout=timeout * 2,
                retries={
                    "max_attempts": 2,
                    "mode": "adaptive"
                }
            )
        )

    return _model_client


def get_bedrock_runtime_client():
    """`bedrock-agent-runtime` client for Knowledge Base retrieve and rerank.
    Timeouts are well below botocore's 60s defaults: these calls normally
    take about a second, and a stalled one would hold the whole turn (see
    TURN_TIMEOUT_SECONDS)."""
    global _client

    if _client is None:
        _client = boto3.client(
            "bedrock-agent-runtime",
            config=Config(
                region_name=settings.aws_region,
                connect_timeout=5,
                read_timeout=20,
                retries={
                    "max_attempts": 3,
                    "mode": "adaptive"
                }
            )
        )

    return _client