from fastapi import APIRouter

from app.retrieval.knowledge_base import (
    retrieve
)

router = APIRouter()


@router.get("/documents/search")
async def search_documents(
    query: str
):
    return retrieve(query)