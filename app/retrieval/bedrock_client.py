import boto3

from botocore.config import Config

from app.common.config import settings

_client = None
_model_client = None


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
    """`bedrock-agent-runtime` client for Knowledge Base retrieve and rerank."""
    global _client

    if _client is None:
        _client = boto3.client(
            "bedrock-agent-runtime",
            config=Config(
                region_name=settings.aws_region,
                retries={
                    "max_attempts": 3,
                    "mode": "adaptive"
                }
            )
        )

    return _client