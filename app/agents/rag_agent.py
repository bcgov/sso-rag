import uuid
from strands import Agent, ModelRetryStrategy
from strands.models.bedrock import BedrockModel
from strands.storage import S3Storage
from strands.storage import LocalFileStorage
from app.common.config import settings
from strands.session import SnapshotSessionManager

from app.prompts.system_prompt import (
    SYSTEM_PROMPT
)

from app.tools.knowledge_base_tool import (
    search_knowledge_base
)

LOCAL_STORAGE_DIR = "./.agent-store/"

storage = LocalFileStorage(LOCAL_STORAGE_DIR) if settings.environment == "local" else S3Storage(settings.agent_storage_s3_bucket)


def extract_model_id(model_arn: str) -> str:
    return model_arn.split("/")[-1]

model = BedrockModel(
    model_id=extract_model_id(settings.model_arn),
    region_name=settings.aws_region
)


SESSION_ID_PREFIX = "session_"


def new_session_id() -> str:
    return f"{SESSION_ID_PREFIX}{uuid.uuid4()}"


def is_valid_session_id(session_id: str | None) -> bool:
    """Cookie values are client-controlled and become storage keys, so only
    accept ids in the exact format we issue."""
    if not session_id or not session_id.startswith(SESSION_ID_PREFIX):
        return False
    return _is_uuid(session_id[len(SESSION_ID_PREFIX):])


def _is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def is_valid_tab_id(tab_id: str | None) -> bool:
    return _is_uuid(tab_id)


def conversation_id(session_id: str, tab_id: str | None) -> str:
    """Storage key for one conversation. The cookie is shared by every tab in
    the browser, so each tab's id is appended to keep their histories apart.
    The tab id is readable by page JS, but the HttpOnly cookie isn't, so the
    tab id alone can't be used to read another browser's history."""
    return f"{session_id}__{tab_id}" if is_valid_tab_id(tab_id) else session_id


async def delete_session(session_id: str) -> None:
    await SnapshotSessionManager(session_id, storage=storage).delete_session()


async def replace_last_answer(agent: Agent, session_id: str, text: str) -> None:
    """Swaps the text of the agent's last reply and re-saves the session, so
    a rejected answer isn't kept as context for the next turn."""

    for message in reversed(agent.messages):
        if message["role"] == "assistant":
            message["content"] = [{"text": text}]
            break
    await SnapshotSessionManager(session_id, storage=storage).save_snapshot(agent, is_latest=True)


def create_agent(session_id: str) -> Agent:
    """A fresh agent per request, restored from the session's stored snapshot:
    Agent keeps its message history, so sharing one instance would leak
    context between users."""

    return Agent(
        model=model,
        tools=[
            search_knowledge_base
        ],
        system_prompt=SYSTEM_PROMPT,
        context_manager="auto",
        retry_strategy=ModelRetryStrategy(
            max_attempts=3,
            initial_delay=2,
            max_delay=60
        ),
        session_manager=SnapshotSessionManager(session_id, storage=storage),
        # Output is streamed to the client; skip the default stdout printer.
        callback_handler=None,
    )
