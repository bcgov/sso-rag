"""Keycloak service-account access-token verification.

Tokens are verified locally against the realm's public signing keys (JWKS):
RS256 signature, `iss`, `aud`, `azp` and `exp`. Keys are fetched only from
the configured issuer's certs endpoint, never from locations named in the
token, and cached per worker process.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import PyJWKError

from app.common.config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
REQUIRED_CLAIMS = ["iss", "aud", "azp", "exp"]
# Real access tokens are a few KB; anything far larger is rejected unparsed.
MAX_TOKEN_LENGTH = 16_384

JWKS_CACHE_SECONDS = 300.0
# Minimum time between JWKS fetches (successful or not), so tokens with
# made-up key ids can't make every request call Keycloak.
JWKS_MIN_REFRESH_SECONDS = 30.0
JWKS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ServiceAccount:
    """The verified caller."""
    client_id: str
    subject: str | None = None


class AuthenticationError(Exception):
    """The token is missing or invalid. `reason` is a short code safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SigningKeysUnavailable(Exception):
    """The realm's signing keys could not be loaded, so no token can be verified."""


def jwks_url(issuer_url: str) -> str:
    return f"{issuer_url}/protocol/openid-connect/certs"


def _fetch_jwks(url: str) -> dict[str, Any]:
    # Redirects are not followed, so keys only ever come from the trusted URL.
    response = httpx.get(url, timeout=JWKS_TIMEOUT_SECONDS, follow_redirects=False)
    response.raise_for_status()
    return response.json()


def _signing_keys(jwks: Any) -> dict[str, PyJWK]:
    """RS256 signing keys by kid. Keycloak also publishes encryption keys
    (`use: enc`) and other algorithms; those are skipped."""

    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError("JWKS has no keys list")

    keys: dict[str, PyJWK] = {}
    for jwk in jwks["keys"]:
        if not isinstance(jwk, dict):
            continue
        kid = jwk.get("kid")
        if (
            not isinstance(kid, str)
            or not kid
            or jwk.get("kty") != "RSA"
            or jwk.get("use", "sig") != "sig"
            or jwk.get("alg", ALGORITHM) != ALGORITHM
        ):
            continue
        try:
            keys[kid] = PyJWK(jwk, algorithm=ALGORITHM)
        except PyJWKError:
            logger.warning(json.dumps({"event": "jwks_key_unusable", "kid": kid[:64]}))

    if not keys:
        raise ValueError("JWKS has no usable RS256 signing keys")
    return keys


class JwksCache:
    """Signing keys from one JWKS URL, kept for `ttl` seconds.

    An unknown kid triggers a refresh (key rotation), but fetches are at
    least `min_refresh_interval` apart. Expired keys are never used: if they
    can't be refreshed, verification fails closed."""

    def __init__(
        self,
        url: str,
        *,
        ttl: float = JWKS_CACHE_SECONDS,
        min_refresh_interval: float = JWKS_MIN_REFRESH_SECONDS,
        fetch: Callable[[str], Any] = _fetch_jwks,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._url = url
        self._ttl = ttl
        self._min_refresh_interval = min_refresh_interval
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None

    def _fresh_keys(self) -> dict[str, PyJWK] | None:
        if self._fetched_at is None or self._clock() - self._fetched_at >= self._ttl:
            return None
        return self._keys

    def _may_refresh(self) -> bool:
        return (
            self._last_attempt is None
            or self._clock() - self._last_attempt >= self._min_refresh_interval
        )

    def _refresh(self) -> bool:
        self._last_attempt = self._clock()
        try:
            keys = _signing_keys(self._fetch(self._url))
        except Exception as e:
            logger.error(json.dumps({"event": "jwks_fetch_failed", "error": type(e).__name__}))
            return False
        self._keys = keys
        self._fetched_at = self._clock()
        return True

    def get_key(self, kid: str) -> PyJWK:
        keys = self._fresh_keys()
        if keys is not None and kid in keys:
            return keys[kid]

        with self._lock:
            # Another thread may have refreshed while this one waited.
            keys = self._fresh_keys()
            if keys is not None and kid in keys:
                return keys[kid]

            if self._may_refresh():
                if not self._refresh():
                    raise SigningKeysUnavailable()
                if kid in self._keys:
                    return self._keys[kid]
                raise AuthenticationError("unknown_kid")

            if keys is None:
                raise SigningKeysUnavailable()
            raise AuthenticationError("unknown_kid")


class TokenVerifier:

    def __init__(self, issuer: str, audience: str, authorized_party: str, keys: JwksCache):
        self._issuer = issuer
        self._audience = audience
        self._authorized_party = authorized_party
        self._keys = keys

    def verify(self, token: str) -> ServiceAccount:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise AuthenticationError("malformed")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise AuthenticationError("malformed") from None

        # The header only selects a key; the algorithm is fixed here, never
        # taken from the token.
        if header.get("alg") != ALGORITHM:
            raise AuthenticationError("algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("missing_kid")

        key = self._keys.get_key(kid)

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=0,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_nbf": True,
                    # Not required; with no leeway, clock skew would reject
                    # freshly issued tokens.
                    "verify_iat": False,
                    "verify_sub": False,
                    "verify_jti": False,
                    "require": REQUIRED_CLAIMS,
                    "strict_aud": False,
                    "enforce_minimum_key_length": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("expired") from None
        except jwt.MissingRequiredClaimError:
            raise AuthenticationError("missing_claim") from None
        except jwt.InvalidAudienceError:
            raise AuthenticationError("audience") from None
        except jwt.InvalidIssuerError:
            raise AuthenticationError("issuer") from None
        except jwt.InvalidSignatureError:
            raise AuthenticationError("signature") from None
        except jwt.InvalidTokenError:
            raise AuthenticationError("invalid") from None

        # PyJWT accepts numeric strings for exp; require a JSON number.
        exp = claims["exp"]
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            raise AuthenticationError("invalid")

        azp = claims["azp"]
        if not isinstance(azp, str) or azp != self._authorized_party:
            raise AuthenticationError("authorized_party")

        sub = claims.get("sub")
        return ServiceAccount(client_id=azp, subject=sub if isinstance(sub, str) else None)


@cache
def get_verifier() -> TokenVerifier:
    issuer = settings.keycloak_issuer_url
    audience = settings.keycloak_audience
    authorized_party = settings.keycloak_authorized_party
    if not (issuer and audience and authorized_party):
        raise RuntimeError("Keycloak authentication is not configured")
    return TokenVerifier(issuer, audience, authorized_party, JwksCache(jwks_url(issuer)))
