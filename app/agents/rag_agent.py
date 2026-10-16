from strands import Agent
from strands.models.bedrock import BedrockModel
from app.common.config import settings

from app.prompts.system_prompt import (
    SYSTEM_PROMPT
)

from app.tools.knowledge_base_tool import (
    search_knowledge_base
)

def extract_model_id(model_arn: str) -> str:
    return model_arn.split("/")[-1]

model = BedrockModel(
    model_id=extract_model_id(settings.model_arn),
    region_name=settings.aws_region
)

agent = Agent(
    model=model,
    tools=[
        search_knowledge_base
    ],
    system_prompt=SYSTEM_PROMPT
)