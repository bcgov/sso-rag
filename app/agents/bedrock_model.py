from botocore.config import Config
from strands import ModelRetryStrategy
from strands.models.bedrock import BedrockModel

from app.common.config import settings


def extract_model_id(model_arn: str) -> str:
    return model_arn.split("/")[-1]


def create_model(read_timeout: int, max_attempts: int, **config) -> BedrockModel:
    return BedrockModel(
        model_id=extract_model_id(settings.model_arn),
        region_name=settings.aws_region,
        boto_client_config=Config(
            connect_timeout=5,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": "standard"},
        ),
        **config,
    )


model = create_model(settings.bedrock_read_timeout_s, max_attempts=2)

# Planning and gap checks are short classification/rewrite tasks: keep them
# deterministic, cheap and quick to give up (callers fall back without them).
planner_model = create_model(
    max(1, settings.planner_call_timeout_ms // 1000), max_attempts=1, temperature=0.0, max_tokens=800
)

# Strands' default retries throttled calls 6 times with up to 240s between
# attempts, which looks like a hang to the user.
agent_retry_strategy = ModelRetryStrategy(
    max_attempts=settings.model_max_attempts, initial_delay=2, max_delay=10
)
planner_retry_strategy = ModelRetryStrategy(max_attempts=2, initial_delay=1, max_delay=2)
