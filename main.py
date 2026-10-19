import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.common.config import settings
from app.services.session_cleanup import run_session_cleanup_loop

from app.api.routes.chat import (
    router as chat_router
)

from app.api.routes.health import (
    router as health_router
)

from app.api.routes.documents import (
    router as document_router
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Periodically delete conversations older than SESSION_RETENTION_DAYS.
    cleanup = None
    if settings.session_cleanup_interval_hours > 0:
        cleanup = asyncio.create_task(
            run_session_cleanup_loop(settings.session_cleanup_interval_hours)
        )

    yield

    if cleanup:
        cleanup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup


app = FastAPI(
    title="Agentic RAG",
    lifespan=lifespan
)

# Answers CORS preflight (OPTIONS) requests; the UI's X-Tab-Id header and
# DELETE /chat both trigger one when the UI is on a different origin.
# Cookies are only allowed cross-origin for explicitly listed origins: with
# "*", Starlette would echo any origin back and let any site use a visitor's
# chat session.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Tab-Id"],
    max_age=300,
)

app.include_router(
    health_router
)

app.include_router(
    chat_router
)

app.include_router(
    document_router
)