"""
Production-ready FastAPI application using AWS Bedrock Retrieve-and-Generate (streaming).

Authentication uses the boto3 default credential chain (IAM role, env vars, ~/.aws/credentials).
Never hard-code credentials in source code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings (loaded from environment / .env file)
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # AWS
    aws_region: str = Field(default="ca-central-1", alias="AWS_REGION")

    # Bedrock Knowledge Base
    knowledge_base_id: str = Field(..., alias="KNOWLEDGE_BASE_ID")

    # Foundation model – Mistral AI 7B Instruct on Bedrock
    model_arn: str = Field(
        default="",
        alias="MODEL_ARN",
    )

    # Retrieval
    number_of_results: int = Field(default=5, alias="NUMBER_OF_RESULTS")

    # Reranking model (Bedrock native reranker)
    reranker_model_arn: str = Field(
        default="",
        alias="RERANKER_MODEL_ARN",
    )
    number_of_reranked_results: int = Field(default=3, alias="NUMBER_OF_RERANKED_RESULTS")

    # CORS
    cors_origins: list[str] = Field(default=["*"], alias="CORS_ORIGINS")


settings = Settings()  # type: ignore[call-arg]

_executor = ThreadPoolExecutor(max_workers=50, thread_name_prefix="bedrock-stream")


# ---------------------------------------------------------------------------
# AWS Bedrock client (singleton, created once at startup)
# ---------------------------------------------------------------------------

_bedrock_client_runtime: boto3.client | None = None

_bedrock_client: boto3.client | None = None

def get_bedrock_client() -> boto3.client:
    """Return a cached bedrock client."""
    global _bedrock_client
    if _bedrock_client is None:
        boto_config = Config(
            region_name=settings.aws_region,
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
        )
        _bedrock_client = boto3.client(
            "bedrock-agent",
            config=boto_config,
        )
        logger.info("Bedrock client initialised (region=%s)", settings.aws_region)
    return _bedrock_client


def get_bedrock_runtime_client() -> boto3.client:
    """Return a cached bedrock-agent-runtime client.

    boto3 uses the standard credential provider chain:
      1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)
      2. AWS config file (~/.aws/credentials)
      3. IAM instance role / ECS task role / EKS service account (IRSA)

    No credentials are ever stored in this file.
    """
    global _bedrock_client_runtime
    if _bedrock_client_runtime is None:
        boto_config = Config(
            region_name=settings.aws_region,
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
        )
        _bedrock_client_runtime = boto3.client(
            "bedrock-agent-runtime",
            config=boto_config,
        )
        logger.info("Bedrock agent-runtime client initialised (region=%s)", settings.aws_region)
    return _bedrock_client_runtime

def get_knowledge_base_data_sources() -> list[dict]:
    """Helper function to list data sources for a given knowledge base."""
    client = get_bedrock_client()
    knowledge_base_id = settings.knowledge_base_id
    response = client.list_data_sources(knowledgeBaseId=knowledge_base_id)
    return response.get("dataSourceSummaries", [])

def get_knowledge_base_documents() -> list[dict]:
    """Helper function to list documents for a given knowledge base data source."""
    client = get_bedrock_client()
    knowledge_base_id = settings.knowledge_base_id
    data_sources = get_knowledge_base_data_sources()
    if not data_sources:
        return []
    data_source_id = data_sources[0]["dataSourceId"]
    response = client.list_knowledge_base_documents(knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id)
    logger.info("Documents in knowledge base %s, data source %s: %s", knowledge_base_id, data_source_id, response)
    return response.get("documentDetails", [])

def delete_knowledge_base_documents() -> None:
    """Helper function to delete all documents from the knowledge base data source."""
    client = get_bedrock_client()
    knowledge_base_id = settings.knowledge_base_id
    if len(get_knowledge_base_data_sources()) == 0:
        logger.info("No data sources found in knowledge base %s – skipping document cleanup", knowledge_base_id)
        return
    documents = get_knowledge_base_documents()
    if len(documents) != 0:
        client.delete_knowledge_base_documents(knowledgeBaseId=knowledge_base_id, dataSourceId=get_knowledge_base_data_sources()[0]["dataSourceId"], documentIdentifiers=[{
            "dataSourceType": doc["identifier"]["dataSourceType"],
            "s3": {
                "uri": doc["identifier"]["s3"]["uri"],
            },
        } for doc in documents])
        

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting up – pre-warming Bedrock client …")
    get_bedrock_runtime_client()
    yield
    logger.info("Shutting down.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SSO RAG – AWS Bedrock",
    description="Streaming RAG API backed by AWS Bedrock Knowledge Base with Mistral AI 7B.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="User question")


class HealthResponse(BaseModel):
    status: str
    region: str
    knowledge_base_id: str


# ---------------------------------------------------------------------------
# Greeting detection
# ---------------------------------------------------------------------------

_GREETING_PATTERN = re.compile(
    r"^\s*("
    r"hi|hello|hey|howdy|greetings|good\s+morning|good\s+afternoon|good\s+evening|"
    r"what'?s\s+up|sup|yo|hiya|hola"
    r")[\s!.,?]*$",
    re.IGNORECASE,
)

_GREETING_REPLIES = [
    "Hello! How can I help you today?",
    "Hi there! What can I assist you with?",
    "Hey! How can I help?",
]


def _detect_greeting(text: str) -> str | None:
    """Return a friendly reply if *text* is a casual greeting, otherwise None."""
    if _GREETING_PATTERN.match(text.strip()):
        import random  # stdlib – fine for a lightweight choice
        return random.choice(_GREETING_REPLIES)  # noqa: S311
    return None


# ---------------------------------------------------------------------------
# Bedrock retrieve-and-generate configuration builder
# ---------------------------------------------------------------------------

def _build_retrieve_and_generate_config() -> dict:
    """
    Build the RetrieveAndGenerateConfiguration dict with:
      - Knowledge Base retrieval
      - Vector search reranking (Bedrock native reranker)
      - Mistral AI 7B as the generative foundation model
    """
    return {
        "type": "KNOWLEDGE_BASE",
        "knowledgeBaseConfiguration": {
            "knowledgeBaseId": settings.knowledge_base_id,
            "modelArn": settings.model_arn,
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "numberOfResults": settings.number_of_results,
                    "rerankingConfiguration": {
                        "type": "BEDROCK_RERANKING_MODEL",
                        "bedrockRerankingConfiguration": {
                            "modelConfiguration": {
                                "modelArn": settings.reranker_model_arn,
                            },
                            "numberOfRerankedResults": settings.number_of_reranked_results,
                        },
                    },
                }
            },
            "generationConfiguration": {
                "inferenceConfig": {
                    "textInferenceConfig": {
                        "maxTokens": 1024,
                        "temperature": 0.1,
                        "topP": 0.9,
                    }
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Streaming response generator
# ---------------------------------------------------------------------------

def _make_chunk(content: str, response_id: str, created: int) -> str:
    """Wrap a text delta in an OpenAI-compatible SSE data line."""
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": settings.model_arn,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _make_citation(citations: list, response_id: str, created: int) -> str:
    """Emit citations as an OpenAI-compatible chunk using a custom 'citations' field."""
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": settings.model_arn,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "", "citations": citations},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _make_stop(response_id: str, created: int) -> str:
    """Emit the final stop chunk."""
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": settings.model_arn,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_bedrock_response(query: str) -> AsyncGenerator[str, None]:
    """
    Call RetrieveAndGenerateStream and yield OpenAI-compatible SSE chunks.

    Stream format:
      data: {"id":"…","object":"chat.completion.chunk","choices":[{"delta":{"content":"…"}}]}
      data: {"id":"…","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}
      data: [DONE]
    """
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    client = get_bedrock_runtime_client()
    rag_config = _build_retrieve_and_generate_config()

    try:
        response = client.retrieve_and_generate_stream(
            input={"text": query},
            retrieveAndGenerateConfiguration=rag_config,
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        logger.exception("Bedrock ClientError [%s]: %s", error_code, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bedrock error: {error_code}",
        ) from exc
    except BotoCoreError as exc:
        logger.exception("BotoCoreError: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AWS connectivity error.",
        ) from exc

    stream = response.get("stream")
    if stream is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No stream returned from Bedrock.",
        )

    has_citations = False
    has_output = False

    try:
        # boto3 stream iteration is synchronous and blocks the event loop.
        # Run each `next()` call in a thread so ASGI can flush each yielded
        # chunk to the client immediately rather than buffering the whole response.
        loop = asyncio.get_running_loop()
        sync_iter = iter(stream)

        while True:
            try:
                event = await loop.run_in_executor(_executor, next, sync_iter)
            except StopIteration:
                break

            if "output" in event:
                text_chunk = event["output"].get("text", "")
                if text_chunk:
                    has_output = True
                    yield _make_chunk(text_chunk, response_id, created)

            elif "retrievalResults" in event:
                citations = event["retrievalResults"]
                if citations:
                    has_citations = True
                    yield _make_citation(citations, response_id, created)

    except (ClientError, BotoCoreError) as exc:
        logger.exception("Stream error: %s", exc)
        error_payload = json.dumps({
            "error": {
                "message": str(exc),
                "type": "bedrock_error",
                "code": getattr(exc, "response", {}).get("Error", {}).get("Code", "unknown"),
            }
        })
        yield f"data: {error_payload}\n\n"
    else:
        if not has_citations and not has_output:
            logger.info("No matching sources found for query.")
            yield _make_chunk(
                "I'm sorry, I couldn't find any relevant information for your question. "
                "Could you try rephrasing or provide more details?",
                response_id,
                created,
            )
    finally:
        yield _make_stop(response_id, created)
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Operations"])
async def health() -> HealthResponse:
    """Liveness / readiness probe."""
    return HealthResponse(
        status="ok",
        region=settings.aws_region,
        knowledge_base_id=settings.knowledge_base_id,
    )


@app.post("/query", tags=["RAG"])
async def query(request: QueryRequest) -> StreamingResponse:
    """
    Stream an answer from AWS Bedrock Knowledge Base using Mistral AI 7B.

    Response is **text/event-stream** (SSE) in OpenAI-compatible format:

    ```
    data: {"id":"chatcmpl-…","object":"chat.completion.chunk","choices":[{"delta":{"content":"…"}}]}
    data: {"id":"chatcmpl-…","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}
    data: [DONE]
    ```
    """
    logger.info("Query received (length=%d)", len(request.query))

    greeting_reply = _detect_greeting(request.query)
    if greeting_reply:
        logger.info("Greeting detected – responding directly.")

        async def _greeting_stream() -> AsyncGenerator[str, None]:
            _id = f"chatcmpl-{uuid.uuid4().hex}"
            _ts = int(time.time())
            yield _make_chunk(greeting_reply, _id, _ts)
            yield _make_stop(_id, _ts)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _greeting_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return StreamingResponse(
        _stream_bedrock_response(request.query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx proxy buffering
        },
    )

@app.delete("/documents", tags=["Operations"])
async def cleanup_docs(response: Response):
    """Clean up S3 vector store and indexes"""
    try:
        delete_knowledge_base_documents()
        response.status_code = status.HTTP_204_NO_CONTENT
    except Exception as exc:
        logger.exception("Error deleting documents: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting knowledge base documents.",
        ) from exc

@app.get("/documents", tags=["Operations"])
async def list_documents():
    """List documents in the knowledge base."""
    documents = get_knowledge_base_documents()
    return {"documents": documents}

# ---------------------------------------------------------------------------
# Entrypoint (development only – use gunicorn+uvicorn workers in production)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api:app",
        host="0.0.0.0",  # noqa: S104 – intentional for container use
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        log_level="info",
        access_log=True,
    )
