import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.agents.rag_agent import (
    conversation_id,
    delete_session,
    extract_model_id,
    is_valid_session_id,
    is_valid_tab_id,
    new_session_id
)

from app.api.dependencies.authentication import (
    require_service_account
)

from app.common.config import settings

from app.schemas.chat import (
    ChatConfig,
    ChatRequest
)

from app.services.chat_service import (
    Heartbeat,
    Status,
    TurnTimeout,
    chat_service
)

from app.services.prompt_policy import (
    PromptRejected,
    check_prompt,
    max_prompt_chars
)

from app.services.session_cleanup import (
    delete_browser_history
)

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(require_service_account)],
    responses={
        401: {"description": "Missing or invalid Keycloak access token"},
        503: {"description": "The realm's signing keys could not be loaded"}
    }
)

DONE = "data: [DONE]\n\n"

FAILED_MESSAGE = "The assistant failed to respond. Please try again."
TIMEOUT_MESSAGE = (
    "The assistant took too long to respond. Please try again, "
    "or ask a shorter or more specific question."
)

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
    # A client disconnect cancels this generator (Starlette), which stops the
    # agent loop at once; nothing of the unfinished turn is saved. The model
    # call already in flight may still finish in Strands' worker thread.
    cancel = threading.Event()

    try:
        yield _sse(completion_id, {"role": "assistant"})

        async for item in chat_service.stream(prompt, session_id, cancel_signal=cancel):
            if isinstance(item, Heartbeat):
                # SSE comment: ignored by clients, keeps proxies from timing out.
                yield ": keepalive\n\n"
            elif isinstance(item, Status):
                yield _sse(completion_id, {"status": item.value})
            else:
                yield _sse(completion_id, {"content": item.text})
                if item.grounding_status:
                    yield _sse(completion_id, {"grounding_status": item.grounding_status})
                if item.citations:
                    yield _sse(completion_id, {"citations": [
                        {
                            "title": c["title"],
                            "location": {"uri": c["uri"]},
                            "score": c["score"]
                        }
                        for c in item.citations
                    ]})

        yield _sse(completion_id, {}, finish_reason="stop")
    except TurnTimeout:
        logger.warning(json.dumps({"event": "turn_timeout", "seconds": settings.turn_timeout_seconds}))
        yield f"data: {json.dumps({'error': TIMEOUT_MESSAGE})}\n\n"
    except Exception:
        # Headers (status 200) are already sent, so report the failure in-band.
        logger.exception("chat stream failed")
        yield f"data: {json.dumps({'error': FAILED_MESSAGE})}\n\n"
    finally:
        cancel.set()

    yield DONE


@router.get(
    "/chat/config"
)
async def chat_config() -> ChatConfig:
    """Limits the UI applies to the message box before sending."""
    return ChatConfig(
        max_prompt_tokens=settings.max_prompt_tokens,
        max_prompt_chars=max_prompt_chars(),
    )


@router.post(
    "/chat",
    responses={422: {"description": "The message is blank or too long; `detail` says why"}}
)
async def chat(
    body: ChatRequest,
    request: Request,
    tab_id: str | None = Header(default=None, alias=TAB_ID_HEADER)
) -> StreamingResponse:
    """Streams the answer as server-sent events in the OpenAI
    `chat.completion.chunk` format, terminated by `data: [DONE]`.

    The answer is checked against the retrieved documentation before it is
    sent (app/services/grounding.py), so it arrives as one `content` chunk,
    preceded by `delta.status` progress chunks ("searching", "writing") and
    followed by `delta.grounding_status` (SUPPORTED, PARTIALLY_SUPPORTED,
    NOT_SUPPORTED, or GENERAL for general identity guidance that isn't from
    the docs; omitted for greetings and clarifying questions) and, when
    sources were used, `delta.citations`, then the `finish_reason: "stop"`
    chunk. Rejected answers are replaced with the no-documentation reply.

    History is kept per browser (session cookie) and, when the
    `X-Tab-Id` header is sent, per tab within that browser.

    A blank message, or one over MAX_PROMPT_TOKENS (estimated), gets a 422
    whose `detail` is a message for the user."""

    try:
        prompt = check_prompt(body.prompt)
    except PromptRejected as e:
        raise HTTPException(status_code=422, detail=str(e))

    session_id = request.cookies.get(settings.session_cookie_name)

    if not is_valid_session_id(session_id):
        session_id = new_session_id()

    response = StreamingResponse(
        _event_stream(prompt, conversation_id(session_id, tab_id)),
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

    _clear_session_cookie(response)


@router.delete(
    "/chat/history",
    status_code=204,
    responses={500: {"description": "Some stored history could not be deleted"}}
)
async def delete_history(request: Request, response: Response):
    """Deletes all of this browser's stored chat history — the conversations
    of every tab — and clears the session cookie so the next /chat call
    starts a new session. Idempotent: succeeds when nothing is stored."""

    session_id = request.cookies.get(settings.session_cookie_name)

    if is_valid_session_id(session_id):
        try:
            deleted = await delete_browser_history(session_id)
        except Exception:
            logger.exception("deleting chat history failed")
            # Keep the cookie so the user can retry deleting what remains.
            return JSONResponse(
                status_code=500,
                content={"detail": "Some history could not be deleted. Please try again."}
            )
        logger.info("deleted %d stored conversations on request", len(deleted))

    _clear_session_cookie(response)


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/"
    )
