"""Keycloak access-token verification and route protection.

Tokens are signed with locally generated RSA keys and the JWKS fetch is
faked, so no Keycloak or AWS access is needed."""

import importlib
import logging
import threading
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

import main
from app.api.dependencies import authentication as auth_dependency
from app.api.routes import chat as chat_route
from app.api.routes import documents as documents_route
from app.common.config import Settings, settings
from app.services import authentication as auth
from app.services.authentication import (
    AuthenticationError,
    JwksCache,
    ServiceAccount,
    SigningKeysUnavailable,
    TokenVerifier,
)
from app.services.chat_service import Answer

ISSUER = "https://keycloak.test/auth/realms/test"
AUDIENCE = "sso-rag-api"
CLIENT_ID = "sso-rag-client"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _rsa_key()
OTHER_KEY = _rsa_key()


def _jwk(private_key, kid, **extra):
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256", **extra}


def _jwks(*keys):
    return {"keys": list(keys)}


def _claims(**overrides):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "azp": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "sub": "service-account-uuid",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims=None, key=KEY, kid="k1", headers=None, algorithm="RS256"):
    all_headers = {"kid": kid} if kid is not None else {}
    all_headers.update(headers or {})
    return jwt.encode(
        claims if claims is not None else _claims(),
        key,
        algorithm=algorithm,
        headers=all_headers,
    )


class FakeFetch:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _verifier(fetch=None, clock=None):
    cache = JwksCache(
        JWKS_URL,
        fetch=fetch or FakeFetch(_jwks(_jwk(KEY, "k1"))),
        clock=clock or Clock(),
    )
    return TokenVerifier(ISSUER, AUDIENCE, CLIENT_ID, cache)


def _reason(verifier, token):
    with pytest.raises(AuthenticationError) as e:
        verifier.verify(token)
    return e.value.reason


# ── Claims and signature ──────────────────────────────────────────────────────

def test_valid_token_is_accepted():
    account = _verifier().verify(_token())
    assert account == ServiceAccount(client_id=CLIENT_ID, subject="service-account-uuid")


def test_audience_list_containing_the_api_is_accepted():
    assert _verifier().verify(_token(_claims(aud=["account", AUDIENCE]))).client_id == CLIENT_ID


@pytest.mark.parametrize("aud", ["other-api", ["account", "other-api"], [], ""])
def test_wrong_audience_is_rejected(aud):
    assert _reason(_verifier(), _token(_claims(aud=aud))) in {"audience", "missing_claim"}


@pytest.mark.parametrize("claim", ["iss", "aud", "azp", "exp"])
def test_missing_required_claim_is_rejected(claim):
    claims = _claims()
    del claims[claim]
    assert _reason(_verifier(), _token(claims)) == "missing_claim"


@pytest.mark.parametrize("iss", [ISSUER + "/", ISSUER.upper(), "https://evil.test/auth/realms/test"])
def test_wrong_issuer_is_rejected(iss):
    assert _reason(_verifier(), _token(_claims(iss=iss))) == "issuer"


@pytest.mark.parametrize("azp", ["other-client", CLIENT_ID.upper(), 123, ["sso-rag-client"]])
def test_wrong_authorized_party_is_rejected(azp):
    assert _reason(_verifier(), _token(_claims(azp=azp))) == "authorized_party"


def test_expired_token_is_rejected():
    assert _reason(_verifier(), _token(_claims(exp=int(time.time()) - 1))) == "expired"


def test_token_expiring_now_is_rejected():
    assert _reason(_verifier(), _token(_claims(exp=int(time.time())))) == "expired"


@pytest.mark.parametrize("exp", [str(int(time.time()) + 300), True, "soon"])
def test_non_numeric_expiry_is_rejected(exp):
    assert _reason(_verifier(), _token(_claims(exp=exp))) in {"invalid", "expired"}


def test_tampered_payload_is_rejected():
    header, _, signature = _token().split(".")
    forged = jwt.utils.base64url_encode(
        jwt.utils.force_bytes(
            '{"iss":"%s","aud":"%s","azp":"%s","exp":%d}'
            % (ISSUER, AUDIENCE, CLIENT_ID, int(time.time()) + 300)
        )
    ).decode()
    assert _reason(_verifier(), f"{header}.{forged}.{signature}") == "signature"


def test_token_signed_by_another_key_is_rejected():
    assert _reason(_verifier(), _token(key=OTHER_KEY)) == "signature"


def test_symmetric_algorithm_is_rejected():
    token = _token(key="x" * 64, algorithm="HS256")
    assert _reason(_verifier(), token) == "algorithm"


def test_unsigned_token_is_rejected():
    token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "k1"})
    assert _reason(_verifier(), token) == "algorithm"


def test_token_without_kid_is_rejected():
    assert _reason(_verifier(), _token(kid=None)) == "missing_kid"


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "x" * (auth.MAX_TOKEN_LENGTH + 1)])
def test_malformed_token_is_rejected(token):
    assert _reason(_verifier(), token) == "malformed"


def test_token_supplied_key_locations_are_ignored():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")))
    token = _token(
        key=OTHER_KEY,
        kid="attacker",
        headers={"jku": "https://evil.test/jwks", "x5u": "https://evil.test/cert"},
    )
    assert _reason(_verifier(fetch), token) == "unknown_kid"
    assert fetch.urls == [JWKS_URL]


# ── Signing-key cache ────────────────────────────────────────────────────────

def test_keys_are_cached():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")))
    verifier = _verifier(fetch)
    verifier.verify(_token())
    verifier.verify(_token())
    assert len(fetch.urls) == 1


def test_keys_are_refetched_after_the_cache_expires():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")))
    clock = Clock()
    verifier = _verifier(fetch, clock)
    verifier.verify(_token())
    clock.now += auth.JWKS_CACHE_SECONDS
    verifier.verify(_token())
    assert len(fetch.urls) == 2


def test_rotated_key_is_picked_up():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")), _jwks(_jwk(KEY, "k1"), _jwk(OTHER_KEY, "k2")))
    clock = Clock()
    verifier = _verifier(fetch, clock)
    verifier.verify(_token())
    clock.now += auth.JWKS_MIN_REFRESH_SECONDS
    assert verifier.verify(_token(key=OTHER_KEY, kid="k2")).client_id == CLIENT_ID
    assert len(fetch.urls) == 2


def test_unknown_kids_do_not_cause_a_fetch_per_request():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")))
    clock = Clock()
    verifier = _verifier(fetch, clock)
    verifier.verify(_token())
    clock.now += auth.JWKS_MIN_REFRESH_SECONDS
    for i in range(20):
        assert _reason(verifier, _token(key=OTHER_KEY, kid=f"random-{i}")) == "unknown_kid"
    assert len(fetch.urls) == 2
    assert verifier.verify(_token()).client_id == CLIENT_ID


def test_concurrent_refreshes_are_coalesced():
    calls = []
    barrier = threading.Barrier(8)

    def slow_fetch(url):
        calls.append(url)
        time.sleep(0.2)
        return _jwks(_jwk(KEY, "k1"))

    verifier = TokenVerifier(ISSUER, AUDIENCE, CLIENT_ID, JwksCache(JWKS_URL, fetch=slow_fetch))
    token = _token()
    results = []

    def worker():
        barrier.wait()
        results.append(verifier.verify(token))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [
    httpx.ConnectTimeout("timed out"),
    httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", JWKS_URL), response=httpx.Response(500)
    ),
    ValueError("not json"),
])
def test_keys_unavailable_fails_closed(failure):
    with pytest.raises(SigningKeysUnavailable):
        _verifier(FakeFetch(failure)).verify(_token())


@pytest.mark.parametrize("jwks", [
    {},
    {"keys": "nope"},
    [],
    {"keys": []},
    {"keys": [_jwk(KEY, "k1", use="enc", alg="RSA-OAEP")]},
    {"keys": [{**_jwk(KEY, "k1"), "kty": "EC"}]},
    {"keys": [{**_jwk(KEY, "k1"), "n": "!!"}]},
])
def test_unusable_jwks_fails_closed(jwks):
    with pytest.raises(SigningKeysUnavailable):
        _verifier(FakeFetch(jwks)).verify(_token())


def test_encryption_keys_are_skipped():
    jwks = _jwks(_jwk(OTHER_KEY, "enc-key", use="enc", alg="RSA-OAEP"), _jwk(KEY, "k1"))
    verifier = _verifier(FakeFetch(jwks))
    assert verifier.verify(_token()).client_id == CLIENT_ID
    assert _reason(verifier, _token(key=OTHER_KEY, kid="enc-key")) == "unknown_kid"


def test_expired_keys_are_not_used_during_an_outage():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")), httpx.ConnectError("down"))
    clock = Clock()
    verifier = _verifier(fetch, clock)
    verifier.verify(_token())
    clock.now += auth.JWKS_CACHE_SECONDS
    with pytest.raises(SigningKeysUnavailable):
        verifier.verify(_token())
    # Still unavailable while further fetches are throttled.
    clock.now += 1
    with pytest.raises(SigningKeysUnavailable):
        verifier.verify(_token())


def test_cached_keys_keep_working_during_an_outage():
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")), httpx.ConnectError("down"))
    clock = Clock()
    verifier = _verifier(fetch, clock)
    verifier.verify(_token())
    clock.now += auth.JWKS_MIN_REFRESH_SECONDS
    with pytest.raises(SigningKeysUnavailable):
        verifier.verify(_token(key=OTHER_KEY, kid="k2"))
    assert verifier.verify(_token()).client_id == CLIENT_ID


def test_fetch_uses_a_timeout_and_does_not_follow_redirects(monkeypatch):
    seen = {}

    def get(url, **kwargs):
        seen.update(kwargs, url=url)
        return httpx.Response(
            302, headers={"location": "https://evil.test/jwks"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(auth.httpx, "get", get)
    with pytest.raises(httpx.HTTPStatusError):
        auth._fetch_jwks(JWKS_URL)
    assert seen == {"url": JWKS_URL, "timeout": auth.JWKS_TIMEOUT_SECONDS, "follow_redirects": False}


def test_jwks_url_is_derived_from_the_issuer():
    assert auth.jwks_url(ISSUER) == JWKS_URL


# ── Configuration ────────────────────────────────────────────────────────────

def _settings(**env):
    base = {
        "KNOWLEDGE_BASE_ID": "kb",
        "ENVIRONMENT": "sandbox",
        "KEYCLOAK_ISSUER_URL": ISSUER,
        "KEYCLOAK_AUDIENCE": AUDIENCE,
        "KEYCLOAK_AUTHORIZED_PARTY": CLIENT_ID,
        "AUTH_LOCAL_BYPASS": False,
    }
    base.update(env)
    return Settings(_env_file=None, **base)


def test_bypass_is_rejected_outside_local():
    with pytest.raises(ValidationError):
        _settings(AUTH_LOCAL_BYPASS=True)


def test_bypass_is_allowed_locally_without_keycloak_settings():
    s = _settings(
        ENVIRONMENT="local", AUTH_LOCAL_BYPASS=True,
        KEYCLOAK_ISSUER_URL="", KEYCLOAK_AUDIENCE="", KEYCLOAK_AUTHORIZED_PARTY="",
    )
    assert s.auth_bypassed
    s.require_auth_config()


@pytest.mark.parametrize("name", ["KEYCLOAK_ISSUER_URL", "KEYCLOAK_AUDIENCE", "KEYCLOAK_AUTHORIZED_PARTY"])
@pytest.mark.parametrize("environment", ["sandbox", "local"])
def test_missing_keycloak_setting_fails_startup(name, environment):
    with pytest.raises(RuntimeError, match=name):
        _settings(ENVIRONMENT=environment, **{name: "  "}).require_auth_config()


@pytest.mark.parametrize("url", [
    "http://keycloak.test/auth/realms/test",
    "https://user:pw@keycloak.test/auth/realms/test",
    "https://keycloak.test/auth/realms/test?x=1",
    "https://keycloak.test/auth/realms/test#frag",
    "https://keycloak.test/auth",
    "keycloak.test/auth/realms/test",
])
def test_malformed_issuer_is_rejected(url):
    with pytest.raises(ValidationError):
        _settings(KEYCLOAK_ISSUER_URL=url)


def test_issuer_trailing_slash_is_dropped():
    assert _settings(KEYCLOAK_ISSUER_URL=ISSUER + "/").keycloak_issuer_url == ISSUER


# ── Routes ───────────────────────────────────────────────────────────────────

PROTECTED = [
    ("POST", "/chat", {"json": {"prompt": "how do I request an integration?"}}),
    ("GET", "/chat/config", {}),
    ("DELETE", "/chat", {}),
    ("DELETE", "/chat/history", {}),
    ("GET", "/documents/search", {"params": {"query": "integration"}}),
]

SESSION = "session_11111111-1111-4111-8111-111111111111"


@pytest.fixture
def api(monkeypatch):
    """The app with real authentication and the work behind each route spied on."""
    monkeypatch.setattr(settings, "auth_local_bypass", False)
    fetch = FakeFetch(_jwks(_jwk(KEY, "k1")))
    verifier = _verifier(fetch)
    monkeypatch.setattr(auth_dependency, "get_verifier", lambda: verifier)

    work = []

    async def stream(question, session_id, cancel_signal=None):
        work.append("chat")
        yield Answer("ok", None, [])

    async def delete_session(conversation):
        work.append("delete_session")

    async def delete_browser_history(session_id):
        work.append("delete_history")
        return []

    def retrieve(query):
        work.append("retrieve")
        return []

    monkeypatch.setattr(chat_route.chat_service, "stream", stream)
    monkeypatch.setattr(chat_route, "delete_session", delete_session)
    monkeypatch.setattr(chat_route, "delete_browser_history", delete_browser_history)
    monkeypatch.setattr(documents_route, "retrieve", retrieve)

    client = TestClient(main.app, cookies={settings.session_cookie_name: SESSION})
    client.work = work
    client.fetch = fetch
    return client


def _call(client, method, path, kwargs, headers=None):
    return client.request(method, path, headers=headers, **kwargs)


def _assert_rejected(response, status=401):
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)
    assert "set-cookie" not in response.headers
    if status == 401:
        assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
@pytest.mark.parametrize("headers", [
    None,
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": "Basic dXNlcjpwYXNz"},
    {"Authorization": f"Token {_token()}"},
])
def test_protected_routes_reject_missing_or_invalid_credentials(api, method, path, kwargs, headers):
    _assert_rejected(_call(api, method, path, kwargs, headers))
    assert api.work == []


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_protected_routes_reject_tokens_failing_claim_checks(api, method, path, kwargs):
    for claims in (_claims(aud="other"), _claims(azp="other"), _claims(exp=int(time.time()) - 5)):
        headers = {"Authorization": f"Bearer {_token(claims)}"}
        _assert_rejected(_call(api, method, path, kwargs, headers))
    assert api.work == []


def test_token_is_not_accepted_from_cookies_or_query(api):
    token = _token()
    api.cookies.set("access_token", token)
    response = api.get("/documents/search", params={"query": "x", "access_token": token})
    _assert_rejected(response)
    assert api.work == []


@pytest.mark.parametrize("method,path,kwargs", PROTECTED)
def test_keys_unavailable_returns_503(api, monkeypatch, method, path, kwargs):
    verifier = _verifier(FakeFetch(httpx.ConnectError("down")))
    monkeypatch.setattr(auth_dependency, "get_verifier", lambda: verifier)
    response = _call(api, method, path, kwargs, {"Authorization": f"Bearer {_token()}"})
    _assert_rejected(response, status=503)
    assert api.work == []


@pytest.mark.parametrize("method,path,kwargs,status,work", [
    ("POST", "/chat", PROTECTED[0][2], 200, ["chat"]),
    ("GET", "/chat/config", {}, 200, []),
    ("DELETE", "/chat", {}, 204, ["delete_session"]),
    ("DELETE", "/chat/history", {}, 204, ["delete_history"]),
    ("GET", "/documents/search", PROTECTED[4][2], 200, ["retrieve"]),
])
def test_valid_token_reaches_the_route(api, method, path, kwargs, status, work):
    response = _call(api, method, path, kwargs, {"Authorization": f"Bearer {_token()}"})
    assert response.status_code == status
    assert api.work == work


def test_authenticated_chat_streams_and_keeps_the_session_cookie(api):
    response = api.post(
        "/chat",
        json={"prompt": "hello"},
        headers={"Authorization": f"bearer {_token()}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.rstrip().endswith("data: [DONE]")
    assert f"{settings.session_cookie_name}={SESSION}" in response.headers["set-cookie"]


def test_health_is_public(api):
    assert api.get("/health").status_code == 200


def test_cors_preflight_is_public(api, monkeypatch):
    response = api.options(
        "/chat",
        headers={
            "Origin": "https://example.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert api.work == []


def test_rejections_are_logged_without_the_token(api, caplog):
    token = _token(_claims(azp="other"))
    with caplog.at_level(logging.INFO):
        api.get("/chat/config", headers={"Authorization": f"Bearer {token}"})
    assert "auth_rejected" in caplog.text
    assert "authorized_party" in caplog.text
    assert token not in caplog.text
    assert token.split(".")[1] not in caplog.text


def test_local_bypass_skips_authentication(api, monkeypatch):
    monkeypatch.setattr(settings, "auth_local_bypass", True)
    assert settings.auth_bypassed
    assert api.get("/chat/config").status_code == 200


def test_local_openapi_documents_bearer_auth(api):
    schema = api.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/chat"]["post"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]


@pytest.fixture
def deployed_app(monkeypatch):
    monkeypatch.setattr(settings, "environment", "sandbox")
    yield importlib.reload(main).app
    monkeypatch.undo()
    importlib.reload(main)


def test_api_explorer_is_disabled_when_deployed(deployed_app):
    client = TestClient(deployed_app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
