from fastapi import APIRouter, Depends

from app.api.dependencies.authentication import (
    require_service_account
)

from app.retrieval.knowledge_base import (
    retrieve
)

router = APIRouter(
    dependencies=[Depends(require_service_account)],
    responses={
        401: {"description": "Missing or invalid Keycloak access token"},
        503: {"description": "The realm's signing keys could not be loaded"}
    }
)


@router.get("/documents/search")
async def search_documents(
    query: str
):
    return retrieve(query)