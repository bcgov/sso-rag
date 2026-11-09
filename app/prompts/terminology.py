"""SSO vocabulary used to turn users' wording into the documentation's terms.

Used by the query rewriter prompts (app/prompts/query_rewriter_prompt.py),
which own all search-query writing. Condensed from
bcgov_sso_rag_terminology.md (the full guide, with its JSON seed dictionary).

TERMINOLOGY is not sent whole: `relevant_terminology()` picks the lines whose
aliases (the words left of "->") appear in the user's messages, so each
rewriter call carries a handful of lines instead of the full table. Hence:
- one mapping per line ("alias, alias -> documentation terms");
- list the words users actually type as aliases, including the documentation
  term itself when users type it ("client secret");
- keep mappings that change which page is retrieved.

This maps words only. It is not a source of facts: answers come from the
retrieved documentation.
"""

import re
from functools import lru_cache

REWRITING_RULES = """
Rewriting rules:
- Map the user's words to the documentation terms below, but keep the user's
  intent. Never add facts, requirements or details the user did not state.
- Copy literal values exactly: URLs, hostnames, client IDs, realm names,
  environment names, claim names, role names, error codes and error messages.
- Keep distinct concepts distinct (see DO NOT CONFLATE). If a word is
  ambiguous, use the conversation to pick the meaning; if it is still
  unclear, keep the user's word and add the most likely documentation term.
- Keep variants apart: IDIR vs IDIR - MFA; Basic vs Business BCeID vs BCeID both; GitHub
  BC Gov vs GitHub Public; DEV vs TEST vs PROD; Standard vs Custom Realm.
""".strip()

TERMINOLOGY = """
Documentation vocabulary (user wording -> documentation terms):

Applications and integrations
- app, application, project, system, my project -> CSS project / CSS SSO integration
- register my app, onboard, connect my app, add SSO, add government login, new integration, get started -> request an integration through CSS
- integration request status, request status, pending, still processing -> integration status: In Draft, Submitted, Completed
- add prod, add test, promote to production -> configure the integration for the PROD / TEST environment
- remove SSO, delete the app, retire the app, decommission -> remove / decommission an integration

Clients and credentials
- client, SSO client -> Keycloak client (CSS Integration)
- client ID, client_id, app ID, resource -> client_id (resource in the Installation JSON)
- client secret, client password, app secret, SSO password, integration secret -> client secret (confidential client), only when the context is a client, integration or token request
- installation file, installation JSON, config JSON, keycloak.json, setup file, credentials file -> Installation JSON
- frontend, SPA, single-page app, browser-only, mobile app, React, Angular, Vue -> public client, Authorization Code Flow with PKCE, no client secret
- backend, server-side app, API server -> confidential client, client secret
- reset secret, regenerate secret, rotate secret, old secret rejected, invalid client -> client secret rotation, client credentials mismatch

Realms and platform
- standard realm, standard SSO, shared SSO, common realm -> Standard Realm (shared CSS-managed realm)
- custom realm, custom SSO, dedicated realm, private realm, tenant -> Custom Realm (dedicated realm)
- Gold -> current platform
- Silver -> legacy (deprecated) platform
- SSO URL, login URL, Keycloak URL, loginproxy.gov.bc.ca -> Keycloak server URL / SSO hostname for the environment
- outage, down, failover, DR, availability -> Service Uptime and Monitoring, High Availability, Gold disaster recovery

Identity providers (IdP)
- login provider, login option, sign-in method, IdP -> identity provider (IdP)
- government login, staff login, IDIR -> IDIR
- MFA login, Azure IDIR, AzureIDIR, BCGOVIDIR, Entra IDIR -> IDIR - MFA (IDIR with MFA, Entra-backed IDIR)
- BCeID, business login -> Basic BCeID, Business BCeID, or both (Business BCeID for a business login)
- Services Card, BCSC -> BC Services Card (Privacy Zone, PPID)
- digital ID, verifiable credential, person credential, wallet -> Digital Credential (BC Wallet)
- GitHub -> GitHub BC Gov or GitHub Public
- email code, one-time code, OTP -> One-Time Passcode (OTP)
- skip the login selection page, default provider, idp hint, kc_idp_hint -> IdP hint (kc_idp_hint)
- login option missing -> configured identity providers for the integration and environment
- prod approval, production approval -> identity-provider production approval / onboarding

Environments
- dev, development -> DEV
- QA, testing, staging, test -> TEST
- live, production, prod -> PROD
- works in DEV but not PROD -> environment-specific client ID, client secret, redirect URIs, IdP availability

Redirects and login flow
- callback, return URL, login callback, redirect URI, redirect_uri -> redirect URI (redirect_uri), valid redirect URIs
- invalid redirect, redirect mismatch, URL not whitelisted -> redirect URI validation error (invalid parameter: redirect_uri)
- CORS, allowed origins, web origins -> Web Origins
- deep link, mobile callback -> custom-scheme redirect URI
- logout, logout URL, end session -> OIDC end-session (logout) endpoint, post_logout_redirect_uri
- first-time login, user already exists -> First Broker Login, account linking
- login loop, logged out automatically, keep me logged in, session timeout -> SSO session idle / max timeout, refresh token, browser cookies

Protocols and tokens
- OAuth, OpenID, OIDC -> OpenID Connect (OIDC)
- SAML -> SAML 2.0
- login code, code exchange -> authorization code, token endpoint
- token, bearer token, API token, access token -> access token (JSON Web Token, JWT)
- identity JWT, ID token -> ID token
- refresh token, renew token -> refresh token
- offline token -> offline session, long-lived refresh token
- token lifetime, token expired, token expiry -> access token / refresh token lifespan
- signing keys, public keys, certificate, JWKS -> JWKS endpoint (SAML: certificate)
- audience, aud -> aud claim
- issuer, iss -> iss claim
- user id in token, sub -> sub claim
- user details missing, email missing, name missing, claims -> token claims, UserInfo endpoint, IdP attribute mappers
- idp mapper, mapper -> identity provider mapper

Service accounts and APIs
- machine-to-machine, M2M, backend-to-backend, token without a user, service account, client credentials -> service account, Client Credentials grant (client credentials flow)
- automate user management, automate role management, CSS automation, CSS API -> CSS API
- CSS API key, API credentials -> CSS API Account (not the integration client secret)
- 401 -> token validation: issuer, audience, expiry, signature
- 403 -> roles and authorization

Roles, teams and access
- permissions, access level, user role, roles -> client roles (role-based access control), client_roles claim
- give access, remove access, add user to my app -> assign / remove a user's client role (role mapping)
- role missing in token -> role assignment for the environment and IdP, client_roles claim
- assign a role before first login -> pre-assign roles / import IDIR (Entra-backed IDIR) user
- team, project team -> CSS team
- team owner, team admin -> CSS Team Admin
- collaborator, team member -> CSS Team Member
- orphaned team, no admin left -> orphaned CSS team
- find a user, user search -> CSS user search / IdP directory lookup
""".strip()

DO_NOT_CONFLATE = """
DO NOT CONFLATE:
- CSS integration vs Keycloak client vs Keycloak realm vs CSS team
- client secret vs CSS API Account credentials vs user password vs access token
- CSS Team Admin / Team Member (who manages the integration) vs client roles
  (what a user can do in the application) vs realm administrator
- redirect URI vs post-logout redirect URI; OIDC logout vs SAML Single Logout
- token expiry vs SSO session expiry
- Client Credentials grant (service account, no user) vs Authorization Code
  Flow (interactive user login)
- local development vs the provisioned DEV environment
""".strip()

WHERE_TO_LOOK = """
Where to look, by problem type (useful for sub-queries and step-back queries):
- Set up SSO: Quickstart, requesting an integration, Installation JSON.
- Configure my app: integrating your application, Keycloak client setup,
  client types, redirect URIs.
- Can't sign in: identity provider + login flow + exact error text.
- Signed in but access denied: client roles, role mapping, team membership.
- Token contents: the exact claim name, token attributes, IdP attribute mappers.
- Service accounts / APIs: service account, client credentials, CSS API.
- Manage access: role management, teams and members.
- Realm: Standard Realm or Custom Realm (Custom Realm request and onboarding).
- Errors and known issues: CSS App errors, FAQs.
- Outages: Service Uptime and Monitoring, High Availability.
- New features and changes: release notes.
""".strip()

REWRITE_EXAMPLES = """
Examples of good rewrites:
- "How do I add my app?" -> "How do I request a new CSS SSO integration and get a Keycloak client for my application?"
- "Where is my client password?" -> "Where do I find the client secret for my confidential Keycloak client in the Installation JSON?"
- "Why doesn't my callback work?" -> "Why does my redirect URI (redirect_uri) fail validation against the valid redirect URIs configured for my CSS integration?"
- "How do I give someone access?" -> "How do I assign a client role to a user for my CSS integration?"
- "Can my backend log in without a user?" -> "How does a backend service get an access token with a service account and the Client Credentials grant?"
- "My user cannot see their permissions" -> "Why are a user's client roles missing from the client_roles claim in the access token?"
- "Need a secret for my React app" -> "Should a browser single-page app use a public client with PKCE instead of a client secret?"
""".strip()

# Most vocabulary lines sent in one rewriter call.
MAX_VOCABULARY_LINES = 12


@lru_cache(maxsize=1)
def _terminology_index() -> tuple[tuple[tuple[tuple[str, re.Pattern], ...], str], ...]:
    """(aliases as (text, word-boundary pattern), line) per mapping line."""
    index = []
    for line in TERMINOLOGY.splitlines():
        if not line.startswith("- ") or "->" not in line:
            continue
        aliases = []
        for alias in line[2:].split("->", 1)[0].split(","):
            alias = alias.strip().lower()
            if len(alias) > 1:
                aliases.append((alias, re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)")))
        index.append((tuple(aliases), line))
    return tuple(index)


def relevant_terminology(text: str, limit: int = MAX_VOCABULARY_LINES) -> str:
    """The TERMINOLOGY lines whose aliases appear in `text`, most specific
    match (longest alias) first, at most `limit`; "" if none match."""

    low = " ".join((text or "").lower().split())
    matches = []
    for position, (aliases, line) in enumerate(_terminology_index()):
        longest = max((len(a) for a, pattern in aliases if pattern.search(low)), default=0)
        if longest:
            matches.append((-longest, position, line))
    # Most specific first for the cut, then back in table order (grouped by topic).
    kept = sorted(sorted(matches)[:limit], key=lambda m: m[1])
    return "\n".join(line for _, _, line in kept)
