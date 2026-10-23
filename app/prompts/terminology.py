"""SSO vocabulary used to turn users' wording into the documentation's terms.

Used by the query rewriter prompts (app/prompts/query_rewriter_prompt.py),
which own all search-query writing. Condensed from
bcgov_sso_rag_terminology.md (the full guide, with its JSON seed dictionary)
to fit, with history, in every Llama 3 8B call: keep additions to aliases that
users actually type and mappings that change which page is retrieved.

This maps words only. It is not a source of facts: answers come from the
retrieved documentation.
"""

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
- register / onboard / connect my app, add SSO, add government login -> request a CSS integration (Quickstart)
- integration request status, pending, still processing -> integration status: In Draft, Submitted, Completed
- add prod / add test, promote to production -> configure the integration for the PROD / TEST environment
- remove SSO, delete or retire the app -> remove / decommission an integration

Clients and credentials
- client, SSO client -> Keycloak client (CSS-Integration)
- client ID, app ID, "resource" -> client_id (resource in the Installation JSON)
- client password, app secret, SSO password, integration secret -> client secret (confidential client), only when the context is a client, integration or token request
- installation file, config JSON, keycloak.json, setup file, credentials file -> Installation JSON
- frontend / SPA / browser-only / mobile app -> public client, Authorization Code Flow with PKCE, no client secret
- backend / server-side app -> confidential client, client secret
- reset / regenerate / rotate secret, old secret rejected, invalid client -> client secret rotation, client credentials mismatch

Realms and platform
- standard SSO, shared SSO, common realm -> Standard Realm (shared CSS-managed realm)
- custom SSO, dedicated / private realm, tenant -> Custom Realm (dedicated realm)
- Gold -> current platform; Silver -> legacy (deprecated) platform
- SSO / login / Keycloak URL, loginproxy.gov.bc.ca -> Keycloak server URL / SSO hostname for the environment
- outage, failover, DR, availability -> Service Uptime and Monitoring, High Availability, Gold disaster recovery

Identity providers (IdP)
- login provider, login option, sign-in method -> identity provider (IdP)
- government / staff login -> IDIR; MFA login, Azure IDIR, BCGOVIDIR, Entra IDIR -> IDIR - MFA (IDIR with MFA, Entra-backed IDIR)
- BCeID -> Basic BCeID, Business BCeID, or both (business login -> Business BCeID)
- Services Card, BCSC -> BC Services Card (Privacy Zone, PPID)
- digital ID, verifiable / person credential, wallet -> Digital Credential (BC Wallet)
- GitHub login -> GitHub BC Gov or GitHub Public
- email code, one-time code -> One-Time Passcode (OTP)
- skip the login selection page, default provider -> IdP hint (kc_idp_hint)
- login option missing -> configured identity providers for the integration and environment
- prod approval for a provider -> identity-provider production approval / onboarding

Environments
- dev, development -> DEV; QA, testing, staging -> TEST; live, production -> PROD
- works in DEV but not PROD -> environment-specific client ID, client secret, redirect URIs, IdP availability

Redirects and login flow
- callback / return URL, login callback -> redirect URI (redirect_uri), valid redirect URIs
- invalid redirect, redirect mismatch, URL not whitelisted -> redirect URI validation error (invalid parameter: redirect_uri)
- CORS, allowed origins -> Web Origins
- deep link, mobile callback -> custom-scheme redirect URI
- logout URL, end session -> OIDC end-session (logout) endpoint, post_logout_redirect_uri
- first-time login, user already exists -> First Broker Login, account linking
- login loop, logged out automatically, keep me logged in -> SSO session idle / max timeout, refresh token, browser cookies

Protocols and tokens
- OAuth / OpenID login -> OpenID Connect (OIDC); SAML login -> SAML 2.0
- login code, code exchange -> authorization code, token endpoint
- token, bearer token, API token -> access token (JSON Web Token, JWT); identity JWT -> ID token
- refresh / renew token -> refresh token; offline token -> offline session, long-lived refresh token
- token lifetime, token expired -> access token / refresh token lifespan
- signing keys, public keys, certificate -> JWKS endpoint (SAML: certificate)
- audience -> aud claim; issuer -> iss claim; user id in token -> sub claim
- user details / email / name missing from token -> token claims, UserInfo endpoint, IdP attribute mappers
- idp mapper -> identity provider mapper

Service accounts and APIs
- machine-to-machine, M2M, backend-to-backend, token without a user -> service account, Client Credentials grant (client credentials flow)
- automate user / role management, CSS automation -> CSS API
- CSS API key, API credentials -> CSS API Account (not the integration client secret)
- API returns 401 -> token validation: issuer, audience, expiry, signature; 403 -> roles and authorization

Roles, teams and access
- permissions, access level, user role -> client roles (role-based access control), client_roles claim
- give / remove someone's access, add user to my app -> assign / remove a user's client role (role mapping)
- role missing in token -> role assignment for the environment and IdP, client_roles claim
- assign a role before first login -> pre-assign roles / import IDIR (Entra-backed IDIR) user
- team, project team -> CSS team; team owner -> CSS Team Admin; collaborator -> CSS Team Member
- orphaned team, no admin left -> orphaned CSS team
- find a user -> CSS user search / IdP directory lookup
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
