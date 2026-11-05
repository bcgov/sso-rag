import json
import logging
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.common.config import settings
from app.services.authentication import (
    AuthenticationError,
    ServiceAccount,
    SigningKeysUnavailable,
    get_verifier
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Keycloak access token from the client-credentials grant of an allowed service account."
)

LOCAL_BYPASS_ACCOUNT = ServiceAccount(client_id="local-bypass")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid or missing access token.",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}
    )


# Sync on purpose: fetching signing keys and RSA verification block, so
# FastAPI runs this in its threadpool instead of on the event loop.
def require_service_account(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
) -> ServiceAccount:
    if settings.auth_bypassed:
        return LOCAL_BYPASS_ACCOUNT

    if credentials is None:
        logger.info(json.dumps({"event": "auth_rejected", "reason": "missing"}))
        raise _unauthorized()

    try:
        account = get_verifier().verify(credentials.credentials)
    except AuthenticationError as e:
        logger.info(json.dumps({"event": "auth_rejected", "reason": e.reason}))
        raise _unauthorized() from None
    except SigningKeysUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Authentication is temporarily unavailable. Please try again."
        ) from None

    return account


ServiceAccountDep = Annotated[ServiceAccount, Depends(require_service_account)]
