from fastapi import APIRouter

from app.schemas.chat import (
    ChatRequest,
    ChatResponse
)

from app.services.chat_service import (
    chat_service
)

router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse
)
def chat(
    request: ChatRequest
):
    # Sync handler: FastAPI runs it in a threadpool, so the blocking agent
    # call doesn't stall the event loop.

    answer = chat_service.ask(
        request.prompt
    )

    return ChatResponse(
        answer=answer
    )