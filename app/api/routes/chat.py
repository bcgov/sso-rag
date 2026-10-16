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
async def chat(
    request: ChatRequest
):

    answer = chat_service.ask(
        request.prompt
    )

    return ChatResponse(
        answer=answer
    )