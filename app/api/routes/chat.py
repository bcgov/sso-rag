import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from app.agents.rag_agent import (
    conversation_id,
    delete_session,
    extract_model_id,
    is_valid_session_id,
    is_valid_tab_id,
    new_session_id
)

from app.common.config import settings

from app.schemas.chat import (
    ChatRequest
)

from app.services.chat_service import (
    chat_service
)

logger = logging.getLogger(__name__)

router = APIRouter()

DONE = "data: [DONE]\n\n"

TAB_ID_HEADER = "X-Tab-Id"


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=settings.session_cookie_max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/"
    )


def _sse(completion_id: str, delta: dict, finish_reason: str | None = None) -> str:
    """One OpenAI-compatible `chat.completion.chunk` server-sent event."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": extract_model_id(settings.model_arn),
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }
        ]
    }
    return f"data: {json.dumps(chunk)}\n\n"


async def _event_stream(prompt: str, session_id: str) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4()}"
    # Set when the client disconnects (Starlette closes this generator), so
    # the agent stops instead of running to completion for nobody.
    cancel = threading.Event()

    try:
        yield _sse(completion_id, {"role": "assistant"})

        async for item in chat_service.stream(prompt, session_id, cancel_signal=cancel):
            if isinstance(item, str):
                yield _sse(completion_id, {"content": item})
            else:
                yield _sse(completion_id, {"citations": [
                    {
                        "title": c["title"],
                        "location": {"uri": c["uri"]},
                        "score": c["score"]
                    }
                    for c in item
                ]})

        yield _sse(completion_id, {}, finish_reason="stop")
    except Exception:
        # Headers (status 200) are already sent, so report the failure in-band.
        logger.exception("chat stream failed")
        yield f"data: {json.dumps({'error': 'The assistant failed to respond. Please try again.'})}\n\n"
    finally:
        cancel.set()

    yield DONE


@router.post(
    "/chat"
)
async def chat(
    body: ChatRequest,
    request: Request,
    tab_id: str | None = Header(default=None, alias=TAB_ID_HEADER)
) -> StreamingResponse:
    """Streams the answer as server-sent events in the OpenAI
    `chat.completion.chunk` format, terminated by `data: [DONE]`. When
    sources were used, a chunk with `delta.citations` precedes the final
    `finish_reason: "stop"` chunk.

    History is kept per browser (session cookie) and, when the
    `X-Tab-Id` header is sent, per tab within that browser."""

    session_id = request.cookies.get(settings.session_cookie_name)

    if not is_valid_session_id(session_id):
        session_id = new_session_id()

    response = StreamingResponse(
        _event_stream(body.prompt, conversation_id(session_id, tab_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stops nginx-style proxies buffering the stream.
            "X-Accel-Buffering": "no"
        }
    )

    # Headers go out before the first chunk, so the cookie is in place even
    # if the client disconnects mid-answer. Set on every response so the
    # expiry slides with activity.
    _set_session_cookie(response, session_id)

    return response


@router.delete(
    "/chat",
    status_code=204
)
async def reset_chat(
    request: Request,
    response: Response,
    tab_id: str | None = Header(default=None, alias=TAB_ID_HEADER)
):
    """Ends the current conversation by dropping its stored agent history.

    With `X-Tab-Id`, only that tab's conversation is removed and the cookie
    is kept, since the browser's other tabs still use it. Without it, the
    browser-level conversation is removed and the cookie cleared so the next
    /chat call starts a new session (the cookie is HttpOnly, so the UI can't
    clear it itself)."""

    session_id = request.cookies.get(settings.session_cookie_name)

    if not is_valid_session_id(session_id):
        session_id = None

    if session_id:
        await delete_session(conversation_id(session_id, tab_id))

    if is_valid_tab_id(tab_id):
        return

    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/"
    )
