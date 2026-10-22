import boto3

from botocore.config import Config

from app.common.config import settings

_client = None


def get_bedrock_runtime_client():
    global _client

    if _client is None:
        _client = boto3.client(
            "bedrock-agent-runtime",
            config=Config(
                region_name=settings.aws_region,
                connect_timeout=5,
                read_timeout=15,
                retries={
                    "max_attempts": 3,
                    "mode": "adaptive"
                }
            )
        )

    return _client