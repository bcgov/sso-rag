"""FastAPI application exposing the RAG system as an HTTP API."""

import os
import tempfile
from pathlib import Path
from typing import List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from langchain_text_splitters import MarkdownHeaderTextSplitter
from pydantic import BaseModel
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from parser import (
    DATABASE_URL,
    OLLAMA_BASE_URL,
    DocumentEmbedding,
    generate_embeddings,
    query_rag,
    search_similar,
    store_documents,
)

app = FastAPI(title="RAG API", description="HTTP API for the Ollama RAG system")

app.add_middleware(
    CORSMiddleware,
    # TODO (production): restrict allow_origins to specific domains
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    postgres: bool
    ollama: bool


class IngestResponse(BaseModel):
    status: str  # "ingested" | "skipped"
    filename: str
    chunks: Optional[int] = None


class QueryRequest(BaseModel):
    question: str


class SourceInfo(BaseModel):
    file: str
    headers: str
    distance: float


class QueryResponse(BaseModel):
    answer: str
    sources: List[SourceInfo]


class DeleteResponse(BaseModel):
    status: str
    filename: str
    chunks_removed: int


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _check_postgres() -> bool:
    try:
        engine = create_engine(DATABASE_URL, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as ex:
        print(ex)
        return False


def _check_ollama() -> bool:
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{OLLAMA_BASE_URL}/api/tags")
            return resp.status_code == 200
    except Exception as ex:
        print(ex)
        return False


def _parse_single_file(file_path: Path) -> list:
    """Parse a single markdown file into LangChain Document chunks."""
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
    content = file_path.read_text(encoding="utf-8")
    splits = splitter.split_text(content)
    for split in splits:
        split.metadata["source_file"] = str(file_path)
    return splits


def _is_already_ingested(source_file: str) -> bool:
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
    with Session(engine) as session:
        row = session.execute(
            select(DocumentEmbedding.id)
            .where(DocumentEmbedding.source_file == source_file)
            .limit(1)
        ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Check that PostgreSQL and Ollama are reachable."""
    pg_ok = _check_postgres()
    ollama_ok = _check_ollama()

    if pg_ok and ollama_ok:
        return HealthResponse(status="ok", postgres=True, ollama=True)

    raise HTTPException(
        status_code=503,
        detail={
            "status": "degraded",
            "postgres": pg_ok,
            "ollama": ollama_ok,
        },
    )


# TODO (production): add authentication/authorization (e.g. API key, OAuth2) to prevent unauthorized ingestion
@app.post("/documents", response_model=IngestResponse)
async def ingest_document(file: UploadFile = File(...)):
    """
    Upload a markdown file and ingest it into the vector store.
    Skips ingestion if the file has already been processed.
    """
    filename = file.filename or "upload.md"
    tmp_path: Path | None = None

    # TODO (production): validate file type and size limits before processing
    # Write to a system temp file so any UID can write (OpenShift safe)
    try:
        content = await file.read()
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {exc}")

    try:
        # Use the original filename as the de-dup key, not the temp path
        if _is_already_ingested(filename):
            return IngestResponse(status="skipped", filename=filename)

        chunks = _parse_single_file(tmp_path)
        if not chunks:
            raise HTTPException(status_code=422, detail="No parseable content found in file.")

        # Rewrite source_file metadata to the stable original filename
        for chunk in chunks:
            chunk.metadata["source_file"] = filename

        stored = store_documents(chunks, db_url=DATABASE_URL)
        return IngestResponse(status="ingested", filename=filename, chunks=stored)

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}")
    finally:
        # Always remove the temp file regardless of outcome
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


# TODO (production): add rate limiting to prevent abuse
@app.post("/query", response_model=QueryResponse)
def query_endpoint(body: QueryRequest):
    """Run a question through the RAG pipeline and return the answer with sources."""
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    try:
        sources_raw = search_similar(body.question, limit=5, db_url=DATABASE_URL)
        answer = query_rag(body.question, db_url=DATABASE_URL)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG pipeline error: {exc}")

    sources = []
    for r in sources_raw:
        header_parts = [
            h for h in [r.get("header_1"), r.get("header_2"), r.get("header_3")] if h
        ]
        sources.append(
            SourceInfo(
                file=r.get("source", ""),
                headers=" > ".join(header_parts),
                distance=round(r.get("similarity_score", 0.0), 6),
            )
        )

    return QueryResponse(answer=answer, sources=sources)


# TODO (production): add authentication/authorization to prevent unauthorized deletion
@app.delete("/documents/{filename}", response_model=DeleteResponse)
def delete_document(filename: str):
    """Delete all embeddings for a document identified by its filename."""
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
    with Session(engine) as session:
        rows = session.execute(
            select(DocumentEmbedding.id).where(
                (DocumentEmbedding.source_file == filename)
                | DocumentEmbedding.source_file.like(f"%/{filename}")
            )
        ).fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail=f"Document not found: {filename}")

        ids = [r.id for r in rows]
        session.execute(
            DocumentEmbedding.__table__.delete().where(DocumentEmbedding.id.in_(ids))
        )
        session.commit()

    return DeleteResponse(status="deleted", filename=filename, chunks_removed=len(ids))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
