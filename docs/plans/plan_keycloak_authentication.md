# Keycloak service-account authentication for the RAG API

## Problem and approach

The RAG endpoints currently allow unauthenticated requests that can invoke
Bedrock and access conversation storage. Require callers to present a Keycloak
access token using `Authorization: Bearer <token>`, and validate it before
retrieval, agent construction, session mutations, or SSE headers are sent.

Use local JWT verification against the configured Keycloak realm's public
signing keys. The API does not request tokens, hold client secrets, or call
token introspection on each request.

## Confirmed requirements

- Machine-to-machine access only. Browser login and a credential-holding
  server-side UI proxy are out of scope. The existing unauthenticated chat UI
  will intentionally stop working against a protected API.
- Validate the signature plus `iss`, `aud`, `azp`, and `exp`. No additional
  mandatory role, scope, username, or grant-type claims.
- Pin the accepted signing algorithm to RS256.
- Require deployment-time configuration placeholders for the full realm
  issuer URL, expected audience, and expected `azp` client ID.
- Require authentication in deployed environments. Allow an explicit
  local-only bypass, disabled by default.

## Authentication contract

### Configuration

Add settings in `app/common/config.py`:

| Variable | Default | Behavior |
| --- | --- | --- |
| `KEYCLOAK_ISSUER_URL` | unset | Required unless the local bypass is active; full HTTPS realm URL matching the token's `iss` exactly |
| `KEYCLOAK_AUDIENCE` | unset | Required unless bypassed; expected API audience |
| `KEYCLOAK_AUTHORIZED_PARTY` | unset | Required unless bypassed; one expected client ID matching `azp` exactly |
| `AUTH_LOCAL_BYPASS` | `false` | Effective only when `ENVIRONMENT=local`; reject startup if true in any other environment |

Reject missing, blank, or malformed authentication configuration before the
application can serve requests. Validate the issuer URL as trusted deployment
configuration: HTTPS, no embedded credentials, query, or fragment. Do not
derive the trusted issuer from token contents. Log an explicit warning when the
local bypass is enabled; do not enable it merely because configuration is absent.

### Token verification

- Accept credentials only through the Bearer authorization header, never
  through cookies, query strings, or request bodies.
- Require a signed JWT with an RS256 header and a nonempty signing-key `kid`.
  Header values are untrusted selectors, not authorization evidence.
- Fetch keys only from
  `<KEYCLOAK_ISSUER_URL>/protocol/openid-connect/certs`.
  Ignore token-provided `jku`, `x5u`, and other remote key locations; reject
  redirects away from the trusted endpoint.
- Use PyJWT with its cryptography extra for cryptographic verification and
  standard issuer, audience, and expiration checks. Never implement RSA or
  signature verification manually.
- Require all four selected claims. Match `iss` and `azp` exactly. Accept
  `aud` as a string or a list of strings containing the configured audience,
  as allowed by JWT. Require a numeric expiration and reject expired tokens,
  including `exp` at the current time, with zero expiration leeway.
- Reject unsigned tokens, symmetric algorithms, malformed claims, missing
  claims, wrong issuers/audiences/clients, and invalid signatures.
- Do not add additional mandatory claims. Explicitly set library verification
  options rather than relying on incidental defaults for unrelated claims.

### Signing keys and availability

- Add `PyJWT[crypto]` and declare `httpx` directly in `pyproject.toml`; update
  `uv.lock` with uv.
- Keep the verifier and bounded public-key cache in
  `app/services/authentication.py`. Use a synchronous HTTPX fetch and a
  synchronous FastAPI dependency so blocking network and crypto work run in
  FastAPI's threadpool, not on the event loop.
- Cache the JWKS per worker for 300 seconds. Guard refreshes with a lock and
  double-check the cache after acquiring it to coalesce concurrent requests.
- Allow a refresh for an unknown `kid` to support rotation, but bound outbound
  fetches with a 30-second minimum refresh interval, including failed fetches.
  Random `kid` values must not cause one HTTP request per API request.
- Use a five-second HTTP timeout. Select compatible RSA signing keys; handle
  unusable or malformed JWKS explicitly. Do not cache verified tokens or
  individual keys indefinitely.
- During a Keycloak outage, unexpired cached keys may still verify requests.
  If no usable fresh key set is available, fail closed with 503; never skip
  verification or use expired cache data. An unknown key after a successful
  permitted refresh is an invalid token (401).
- Document the bounded key-rotation delay and that JWKS caches are per worker.

### Routes and errors

- Add a reusable `HTTPBearer(auto_error=False)` dependency in
  `app/api/dependencies/authentication.py`, with a typed verified principal.
- Apply the dependency at the chat and document router levels:
  `POST /chat`, `GET /chat/config`, `DELETE /chat`,
  `DELETE /chat/history`, and `GET /documents/search`.
- Keep `/health` public for ECS and ALB health checks. CORS preflight remains
  public and does not execute endpoint work.
- Disable Swagger, ReDoc, and OpenAPI endpoints outside local development.
  Document local API explorer behavior, including Bearer authentication.
- Return 401 with `WWW-Authenticate: Bearer` and a generic JSON `detail` for
  absent or invalid credentials. Return a generic 503 for signing-key service
  failures. Auth failures happen before streaming starts, not inside SSE.
- Log bounded reason codes for authentication failures and explicit key-fetch
  failures. Never log tokens, authorization headers, secrets, or full claims.
- Preserve successful SSE payloads, cookies, tab IDs, and deletion behavior.
  Cookies remain conversation identifiers; they do not authenticate callers.

## Implementation todos

1. **Adding authentication settings and dependencies**
   - Update `app/common/config.py`, `pyproject.toml`, and `uv.lock`.
   - Enforce fail-closed configuration and the local-only bypass restriction.
2. **Implementing JWT verification and protected routes**
   - Add the authentication service and API dependency.
   - Protect the existing chat and document routers.
   - Update `main.py` for deployed API explorer visibility and any necessary
     authentication initialization.
   - Preserve existing SSE and session contracts.
3. **Wiring deployment configuration and documentation**
   - Add required Terraform variables and validation in
     `terraform/variables.tf`; pass them to the API task in
     `terraform/ecs.tf`.
   - Add placeholders in `terraform/terraform.tfvars.example`. Do not modify
     ignored real tfvars or local `.env` values.
   - Never pass or enable the bypass for deployed ECS tasks.
   - Update README environment and Terraform tables, endpoint authentication,
     local bypass instructions, and the intentional UI compatibility change.
   - Include client-credentials token acquisition and authenticated curl
     examples using placeholders, including cookie persistence for chat.
4. **Adding deterministic authentication and regression tests**
   - Add `tests/test_authentication.py` using locally generated RSA keys and
     mocked JWKS responses, with no Keycloak or AWS access.
   - Adapt `tests/conftest.py` to provide fake required configuration rather
     than silently enabling bypass for the suite.
   - Keep existing chat behavior tests focused through fixture-scoped
     dependency overrides; authentication integration tests use the real
     dependency and signed tokens.
   - Cover every protected method/path and prove rejected requests perform
     no chat, retrieval, or deletion work and set no session cookie.
   - Test missing/wrong/malformed claims, audience string/list behavior,
     expiration boundaries, tampering, wrong keys/algorithms, missing/unknown
     `kid`, and valid tokens.
   - Test cache reuse, rotation, refresh throttling, concurrent refresh
     coalescing, HTTP timeout/failure, malformed JWKS, cache expiry during an
     outage, and safe logging.
   - Test public health/preflight, deployed API explorer visibility,
     OpenAPI Bearer scheme locally, valid authenticated SSE, and bypass
     configuration rejection outside local development.
5. **Validating application and deployment behavior**
   - Run focused authentication and chat route tests first.
   - Run the existing full pytest suite because settings and router
     dependencies affect application imports and all API entry points.
   - Run the existing Python diagnostics/type-check tooling where available;
     do not introduce an unrelated lint/type-check framework.
   - Format touched Terraform files and run `terraform validate` using the
     existing initialized configuration when available; do not run apply.
   - Once real deployment values are supplied, verify network reachability
     from the ECS subnets and smoke-test valid/invalid tokens through API
     Gateway, including a successful SSE response. This live smoke test
     cannot be completed with placeholders and must be reported separately
     from deterministic test results.

## Dependencies between todos

- JWT verification and route protection depend on settings and dependencies.
- Deployment/docs depend on the agreed settings and implemented contract.
- Tests depend on settings and route protection.
- Final validation depends on implementation, deployment/docs, and tests.

## Notes and boundaries

- No API Gateway authorizer is needed: authentication in FastAPI also protects
  requests reaching the API through the internal ALB. Existing REST response
  streaming remains unchanged.
- CORS already permits `Authorization`. Existing API paths and routing are
  sufficient; no new ALB, nginx, Vite, IAM, or Gateway routes are planned.
  Verify header forwarding through existing proxies during deployment.
- The API needs outbound HTTPS reachability to Keycloak. Existing security
  groups allow egress, but this does not prove DNS, routing, or firewall access
  from the actual ECS network.
- Configure the Keycloak client as a confidential service-account client,
  enable service accounts, disable user login grant flows, and configure an
  audience mapper so access tokens contain the intended API audience.
  Matching `azp` is not itself cryptographic proof of the client-credentials
  grant; enforcing that operational boundary belongs in Keycloak.
- Offline JWT validation does not immediately detect token revocation or a
  disabled client. Use short-lived access tokens; introspection is not in scope.
- Authentication blocks unauthenticated RAG usage, but cannot guarantee that
  valid callers cannot abuse the API. Rate limits, quotas, concurrency limits,
  and spending controls are separate follow-ups, not part of this change.
- Existing random session-cookie storage behavior is preserved. This change
  does not introduce per-client session ownership or a new multi-tenant
  authorization model; do not represent it as doing so.
- No repository files have been changed during planning.
