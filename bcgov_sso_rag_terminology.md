# BC Gov SSO Documentation — RAG Terminology and Query-Rewriting Guide

**Purpose:** Help a RAG query rewriter map informal user language to the terminology used in the BC Government Common Hosted Single Sign-On (CSS) documentation, while preserving the user's intent and keeping distinct concepts separate.

> Important: This is a starter terminology resource assembled from the preceding response, not a guaranteed exhaustive or continuously updated extraction of the documentation website. Validate terminology and current platform behavior against the official documentation.

## 1. Design principles

1. Expand queries using synonyms, related technical concepts, and likely documentation terms.
2. Do not treat distinct concepts as interchangeable.
3. Use contextual hints to resolve ambiguous words such as “project,” “client,” “password,” “tenant,” “role,” and “logout.”
4. Preserve literal values, including URLs, client IDs, realm names, environment names, claim names, error codes, and exact error messages.
5. Keep the original query available alongside rewritten queries.
6. Use weighted expansion: direct aliases should receive higher confidence than context-dependent mappings.
7. Treat the dictionary as terminology support, not as a source of current policy or factual truth.

## 2. Core terminology dictionary

### A. Application, project, and integration terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| App, application, software, product, website, web app, mobile app | Application integrated with BC Gov SSO |
| Project, app project, system, service, solution | CSS project or project associated with an SSO integration; validate context |
| CSS project, My Project, project entry | Project or integration entry managed in the CSS App |
| Integration, SSO setup, SSO connection, login integration | CSS SSO integration |
| Register my app, onboard my app, connect my app to SSO | Request or provision a CSS integration |
| Add SSO, enable login, add government login | Configure an application to authenticate through BC Gov SSO |
| App registration, application registration | Potentially an external identity-provider registration; clarify if context is Microsoft Entra rather than CSS |
| Integration request, SSO request, onboarding request | CSS integration request |
| Integration status, provisioning status, request status | Integration lifecycle status: In Draft, Submitted, or Completed |
| Create another environment, add production, add test | Configure or provision the integration for another environment |
| Remove SSO, delete the app, retire an integration | Remove or decommission an integration; determine intended operation |

The CSS integration is a higher-level CSS-managed resource. A Keycloak client is the provisioned authentication configuration associated with an environment.

### B. Client and application configuration terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Client, SSO client, application client | Keycloak client provisioned for a CSS integration |
| Client ID, app ID for SSO, resource ID in installation config | Keycloak client identifier (`client_id`; `resource` in documented Installation JSON) |
| Client secret, app secret, SSO password, integration secret | Client secret for a confidential client |
| Client credentials, login credentials, integration credentials | Environment-specific client configuration and credentials |
| Installation file, config JSON, Keycloak JSON, setup file | Installation JSON |
| Client type, application type | Public client or confidential client |
| Frontend client, SPA client, browser-only app | Usually a public client using Authorization Code Flow with PKCE |
| Backend client, server-side app, app with a secure server | Usually a confidential client |
| Client configuration, SSO configuration, Keycloak config | Client settings and integration configuration |
| Change client type, switch from public to confidential | Update the integration's client type |
| Regenerate secret, reset secret, rotate password | Rotate or regenerate the client secret |
| Client secret no longer works, old password rejected | Possible client-secret rotation or credential mismatch |
| App cannot authenticate, invalid client | Investigate client ID, client type, secret, token endpoint, and environment |

**Do not conflate:** CSS integration, Keycloak client, CSS API Account, Microsoft Entra app registration, user password, access token.

### C. Realm and platform terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| SSO server, login server, authentication server | BC Gov SSO service / Keycloak service, depending on context |
| Tenant, SSO tenant, authentication domain | Possibly a Keycloak realm; do not assume every tenant means a realm |
| Standard SSO, shared SSO, common realm | Standard Realm |
| Custom SSO, dedicated realm, private realm | Custom Realm |
| Realm name, SSO domain, authentication realm | Keycloak realm name |
| Gold, Gold platform | Current CSS platform terminology; Standard Realm is the default for new integrations according to the source material |
| Silver, legacy platform | Older, deprecated CSS platform according to the source material |
| SSO URL, login URL, Keycloak URL | Environment-specific Keycloak server URL or SSO hostname |
| Login proxy, `loginproxy.gov.bc.ca` | BC Gov SSO hostname; identify the relevant environment |
| Realm administrator, realm admin | Realm-level administrative account or role, depending on context |
| Shared infrastructure, common authentication platform | Shared BC Gov SSO platform |
| Custom authentication, custom login flow | Custom Realm authentication-flow configuration |

**Do not automatically equate:** realm ≠ integration ≠ client. Multiple applications may share the Standard Realm, while a Custom Realm supports requirements that need separate realm-level configuration.

### D. Identity provider (IdP) terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Login provider, authentication provider, external login | Identity provider (IdP) |
| Login option, sign-in method, authentication source | Configured identity provider |
| Government employee login, staff login | IDIR or IDIR - MFA, depending on requirement |
| MFA login, two-factor government login | IDIR - MFA |
| Basic BCeID, personal BCeID | Basic BCeID |
| Business login, business account | Business BCeID |
| BCeID, BCeID login | BCeID; determine Basic, Business, or combined variant |
| BC Services Card login, BCSC | BC Services Card identity provider |
| Digital ID, verifiable credential login | Digital Credential identity provider |
| GitHub login, government GitHub | GitHub BC Gov or GitHub Public, depending on configuration |
| Email OTP, email code, one-time code | One-Time Passcode (OTP) identity provider |
| Multiple login options, multiple authentication sources | Multi-IdP federation |
| Default login provider, skip login selection | Identity-provider selection or IdP hint configuration |
| Identity provider is missing, login option not shown | Check configured IdPs, eligibility, environment, and integration status |
| Login through an external provider | Federated authentication through an upstream IdP |
| Login provider approval, production login approval | Identity-provider-specific production approval or onboarding |

Preserve distinctions between IDIR and IDIR - MFA, Basic and Business BCeID, and GitHub BC Gov and GitHub Public. Providers may differ in eligibility, approval, role-management, or SSO characteristics.

### E. Environment and provisioning terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Dev, development, local test environment | DEV environment; distinguish local development from provisioned DEV |
| QA, testing, staging | TEST environment when the user means the CSS TEST environment |
| Live, production, prod | PROD environment |
| Promote to production, deploy SSO to prod | Provision or configure the integration for PROD; application deployment may be separate |
| Integration pending, still processing | Integration status is Submitted |
| Request saved but not sent | In Draft |
| Integration ready, setup finished | Completed |
| SSO is not provisioned, setup is taking too long | Investigate provisioning status and required approvals |
| Production access approval, prod approval | Production onboarding or identity-provider owner approval |
| Different credentials per environment | Environment-specific client credentials |
| DEV works but PROD fails | Compare environment-specific client IDs, secrets, redirect URIs, IdP availability, and provisioning status |
| Copy development configuration to production | Configure PROD using its own Installation JSON; do not reuse DEV credentials |

### F. Redirect URIs and browser login terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Callback URL, return URL, login callback | Redirect URI (`redirect_uri`) |
| Allowed callback, registered callback | Configured valid redirect URI |
| Return to my application after login | Redirect back to the application's registered redirect URI |
| Invalid redirect, redirect mismatch | Redirect URI validation error |
| Callback URL works locally but not in prod | Compare registered redirect URIs across environments |
| URL not whitelisted, callback not allowed | Redirect URI is missing or does not match configured value |
| Allowed web origins, CORS origins | Keycloak Web Origins configuration |
| Wildcard callback, allow all paths | Redirect URI wildcard configuration; apply environment-specific restrictions |
| Login starts but callback fails | Investigate redirect URI, authorization response, and code exchange |
| Deep link, mobile callback, custom URL scheme | Custom-scheme redirect URI |
| Skip the login selection page | IdP hint / direct identity-provider selection, if supported |

Preserve exact URI details. Trailing slash, hostname, port, and HTTP/HTTPS differences may be significant. Confirm current wildcard restrictions in the official documentation before answering policy questions.

### G. OAuth 2.0, OIDC, SAML, and tokens

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| OAuth login, SSO protocol, authentication protocol | OAuth 2.0 / OpenID Connect (OIDC), depending on context |
| OpenID, OIDC login | OpenID Connect |
| SAML login, enterprise SSO protocol | SAML 2.0 |
| Login code, authorization code | Authorization code |
| Code exchange, redeem the code | Authorization code exchange at the token endpoint |
| PKCE, code verifier, challenge | Proof Key for Code Exchange |
| Access token, bearer token, API token | OAuth 2.0 access token |
| ID token, identity JWT | OIDC ID token |
| Refresh token, renew token | Refresh token |
| Token expired, session expired | Distinguish token expiry from browser or Keycloak session expiry |
| Token lifetime, token timeout | Access-token or refresh-token lifespan, depending on context |
| Session timeout, idle logout | Online or offline session timeout; determine relevant session type |
| JWT validation, validate signature | Token signature and claims validation |
| Public keys endpoint, token verification keys | JSON Web Key Set (JWKS) endpoint |
| Token audience, intended API | `aud` claim |
| Token issuer, issuing realm | `iss` claim |
| User subject, user identifier claim | `sub` claim |
| Token revocation check, introspection | Token introspection endpoint, when revocation-aware checks are needed |
| Logout endpoint, end session URL | OIDC end-session endpoint |
| Logout from all apps, global logout | Determine whether OIDC logout, SAML Single Logout, and application-session termination are involved |

### H. Service accounts, API access, and machine-to-machine authentication

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Backend-to-backend login, machine login | Machine-to-machine authentication |
| Service account, technical account, application account | Service account |
| API client, background worker credentials | Service account or confidential client, depending on configuration |
| Client credentials grant, client credentials flow | OAuth 2.0 Client Credentials grant |
| Get an API token without user login | Obtain an access token using the Client Credentials grant |
| CSS automation, manage users using code | CSS API |
| CSS API key, API credentials | CSS API Account credentials; distinguish from integration client secret |
| Create an API account, automate CSS | Request or manage a CSS API Account |
| Assign permissions to a backend service | Configure relevant roles and service-account access |
| API returns 401, token rejected | Check token validity, issuer, audience, expiration, and authentication configuration |
| API returns 403, forbidden | Investigate authorization, roles, permissions, and access policies |
| Service account cannot access API | Check client credentials, grant type, roles, audience, and API authorization |

A service account is not a human user's interactive login. Do not conflate CSS API Account credentials with application integration credentials.

### I. Roles, permissions, teams, and access management

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| User permissions, access level, application permissions | Role-Based Access Control (RBAC) and role assignments |
| Role, application role, user role | Integration-scoped role |
| Admin role, administrator permission | Distinguish CSS Team Admin from application roles or Keycloak realm-admin role |
| Give someone access, grant access | Assign a user to a role or grant team membership, depending on intent |
| Remove someone's access | Remove role assignment, remove team membership, or revoke a session; clarify which |
| Add user to application, onboard a user | Assign a user to an integration role, if requested |
| User has no permissions after login | Check role assignment and token role claims |
| Role missing in JWT | Check role configuration, identity-provider limitations, and token claims |
| Client roles, application roles | Client roles / `client_roles` claim |
| Role not appearing in another environment | Check environment-specific role configuration and assignments |
| Team, project team, application team | CSS team |
| Team owner, project admin | CSS Team Admin |
| Team member, collaborator | CSS Team Member |
| Add another administrator | Add a team member and assign the Admin role |
| Orphaned team, no active admin | Orphaned CSS team |
| Assign a role before first login | Pre-login role assignment, where supported by the identity provider |
| User lookup, search user in CSS | CSS user search and identity-provider directory lookup |

Distinguish CSS team permissions (who administers integrations) from application roles (what an authenticated user can do in an application). Role configuration and availability may vary by environment and identity provider.

### J. Session, logout, and authentication failures

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| Keep me logged in, remember login | Session lifetime and refresh-token behavior |
| User gets logged out automatically | Investigate online session idle/max lifetime, token expiry, and application session handling |
| Logout does not log out other apps | Investigate SSO session termination and protocol-specific single logout behavior |
| Logout from Keycloak, logout from SSO | Keycloak session logout |
| Logout from identity provider too | Upstream IdP logout; distinguish from Keycloak session logout |
| Login prompts every time | Investigate existing SSO session, IdP selection, cookies, and session configuration |
| Session expired but token still works | Distinguish token validity from browser and server-side session state |
| Cannot log in after switching accounts | Investigate cross-session issues, provider selection, and browser cookies |
| First login, first-time user login | First Broker Login flow / initial federated-user provisioning |
| User already exists, duplicate user | Potential account-linking or identity-matching conflict |
| Login loops back to Keycloak | Authentication-flow or broker-return issue; investigate browser session and client configuration |
| Login works in one app but not another | Compare client settings, redirect URIs, IdPs, session state, and environment |
| Custom login flow, custom authentication | Custom Realm authentication flow |
| MFA required, multifactor authentication | Identity-provider or authentication-flow MFA policy |

Do not expand every logout issue into SAML Single Logout. Identify the protocol and session layer first.

### K. Security, token validation, and configuration troubleshooting

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| SSO security, secure login | Authentication and authorization configuration |
| App accepts someone else's token | Investigate issuer, audience, signature, and authorization checks |
| Token works for another app | Potential missing or incorrect audience validation |
| Roles are the same across apps | Verify integration-scoped role names and assignments |
| JWT signature invalid | Validate signing keys, JWKS, issuer, and token integrity |
| Certificate or signing key issue | Token signing key / JWKS configuration, or SAML certificate depending on protocol |
| Secrets in frontend, expose secret | Confidential-client secret exposure |
| Need client secret for React app | Public-client architecture and PKCE; browser apps must not embed confidential secrets |
| Access token in URL | Investigate unsafe token handling and redirect configuration |
| Need user details from token | OIDC claims or UserInfo endpoint |
| Missing email/name/username | Check identity-provider attribute mapping and available token claims |
| User identity is not persisted | Check identity-provider privacy and user-storage characteristics |
| Rate or traffic limits | Shared SSO platform capacity and high-volume integration guidance |
| Disaster recovery, failover, secondary site | SSO high availability and site failover |

Some identity providers have special privacy characteristics, which can affect stored user attributes, role management, or cross-application SSO. Confirm provider-specific behavior in the official docs.

### L. Documentation navigation and support terminology

| User wording / synonym | Preferred terminology / expansion |
|---|---|
| How do I set up SSO? | Quickstart / requesting an integration |
| How do I configure my app? | Integrating Your Application / Keycloak Client Setup |
| Where do I get the JSON? | Integration Credentials / Installation JSON |
| How do I manage access? | Role Management / Teams & Members |
| How do I automate user management? | CSS API |
| Where are the known issues? | Frequently Asked Questions (FAQs) / CSS App Errors |
| Who approves production access? | Identity-provider owner or relevant onboarding team |
| Need a dedicated realm | Custom Realm request and onboarding |
| Login error troubleshooting | CSS App Errors and the relevant protocol or configuration guide |
| SSO outage, failover, availability | Service Uptime and Monitoring / High Availability |
| New feature, release, platform change | Releases documentation |

## 3. High-value query-rewriting examples

| Original query | Suggested expanded query |
|---|---|
| “How do I add my app?” | “How do I request a new CSS SSO integration and provision a Keycloak client for my application?” |
| “Where is my client password?” | “Where can I retrieve the client secret for my confidential Keycloak client from the CSS Installation JSON?” |
| “Why doesn't my callback work?” | “Why is my OIDC redirect URI (`redirect_uri`) failing validation against the URI configured for my CSS integration in this environment?” |
| “How do I give someone access?” | “How do I assign a user to an integration role in CSS, or add them as a CSS Team Member or Team Admin?” |
| “Can my backend log in without a user?” | “How can a backend service authenticate using a service account and the OAuth 2.0 Client Credentials grant?” |
| “Why does prod need approval?” | “Does this CSS integration require identity-provider-specific production onboarding or approval before PROD provisioning?” |
| “Why does logout not work everywhere?” | “Does logging out terminate only the application's session, the Keycloak SSO session, or other service-provider sessions through SLO?” |
| “Why is the user missing from my token?” | “Why are expected OIDC claims or integration-scoped roles missing from the access token or ID token?” |
| “Can I use the same SSO setup for multiple apps?” | “Can multiple Keycloak clients share the Standard Realm, and what configuration is required for each application integration?” |
| “My user cannot see their permissions” | “Are the user's integration role assignments configured for the correct environment and identity provider, and are roles included in the access token's `client_roles` claim?” |

## 4. Machine-readable JSON starter dictionary

The following JSON provides a seed schema. Extend it with additional entries from documentation headings, FAQs, error messages, and examples.

```json
{
  "domain": "bcgov-sso",
  "version": "1.0",
  "terms": [
    {
      "id": "css.integration",
      "canonical": "CSS SSO integration",
      "aliases": [
        "integration",
        "SSO setup",
        "SSO connection",
        "app integration",
        "project integration"
      ],
      "related_terms": [
        "Keycloak client",
        "CSS App",
        "DEV environment",
        "TEST environment",
        "PROD environment"
      ],
      "not_equivalent_to": [
        "Keycloak realm",
        "identity provider",
        "CSS team"
      ],
      "context_hints": [
        "request",
        "provision",
        "manage",
        "status",
        "configure"
      ],
      "rewrite_hint": "Expand toward requesting, provisioning, or managing a CSS SSO integration."
    },
    {
      "id": "keycloak.client",
      "canonical": "Keycloak client",
      "aliases": [
        "client",
        "SSO client",
        "application client",
        "client configuration"
      ],
      "related_terms": [
        "client_id",
        "client secret",
        "public client",
        "confidential client",
        "Installation JSON"
      ],
      "not_equivalent_to": [
        "CSS integration",
        "CSS API Account",
        "Microsoft Entra app registration"
      ],
      "context_hints": [
        "client ID",
        "secret",
        "redirect URI",
        "PKCE",
        "token endpoint"
      ],
      "rewrite_hint": "Resolve to Keycloak client configuration when the query concerns authentication credentials or protocol settings."
    },
    {
      "id": "keycloak.client_secret",
      "canonical": "Keycloak client secret",
      "aliases": [
        "client password",
        "app secret",
        "SSO password",
        "integration secret"
      ],
      "related_terms": [
        "confidential client",
        "Installation JSON",
        "secret rotation",
        "token endpoint"
      ],
      "not_equivalent_to": [
        "CSS API Account credentials",
        "user password",
        "access token"
      ],
      "context_hints": [
        "invalid client",
        "401",
        "regenerate",
        "rotate",
        "expired credential"
      ],
      "rewrite_hint": "Expand toward retrieving, storing, rotating, or troubleshooting the confidential client's secret."
    },
    {
      "id": "keycloak.realm",
      "canonical": "Keycloak realm",
      "aliases": [
        "realm",
        "SSO realm",
        "authentication domain"
      ],
      "related_terms": [
        "Standard Realm",
        "Custom Realm",
        "realm name",
        "issuer URL",
        "realm signing keys"
      ],
      "not_equivalent_to": [
        "CSS integration",
        "Keycloak client",
        "CSS team"
      ],
      "context_hints": [
        "shared",
        "dedicated",
        "issuer",
        "custom authentication",
        "realm administrator"
      ],
      "rewrite_hint": "Expand toward realm-level configuration, hosting, or authentication requirements."
    },
    {
      "id": "oidc.redirect_uri",
      "canonical": "OIDC redirect URI",
      "aliases": [
        "callback URL",
        "return URL",
        "login callback",
        "redirect URL"
      ],
      "related_terms": [
        "redirect_uri",
        "authorization code flow",
        "web origins",
        "environment configuration"
      ],
      "not_equivalent_to": [
        "post_logout_redirect_uri",
        "SAML Single Logout URI"
      ],
      "context_hints": [
        "invalid parameter",
        "redirect mismatch",
        "trailing slash",
        "wrong hostname",
        "localhost"
      ],
      "rewrite_hint": "Expand toward registered redirect URI matching and environment-specific configuration."
    },
    {
      "id": "css.roles",
      "canonical": "CSS integration role management",
      "aliases": [
        "user permissions",
        "application roles",
        "give access",
        "assign permissions"
      ],
      "related_terms": [
        "client_roles",
        "role assignment",
        "RBAC",
        "CSS Team Admin",
        "CSS Team Member"
      ],
      "not_equivalent_to": [
        "realm administration",
        "CSS team membership",
        "OAuth scopes"
      ],
      "context_hints": [
        "user cannot access",
        "missing role",
        "first login",
        "access token",
        "environment"
      ],
      "rewrite_hint": "Distinguish application role assignment from CSS team membership and realm administration."
    },
    {
      "id": "oauth.client_credentials",
      "canonical": "OAuth 2.0 Client Credentials grant",
      "aliases": [
        "machine-to-machine login",
        "backend authentication",
        "service-to-service authentication",
        "login without a user"
      ],
      "related_terms": [
        "service account",
        "access token",
        "token endpoint",
        "client authentication"
      ],
      "not_equivalent_to": [
        "Authorization Code Flow",
        "interactive user login",
        "CSS API Account"
      ],
      "context_hints": [
        "background worker",
        "backend API",
        "scheduled job",
        "client_credentials"
      ],
      "rewrite_hint": "Expand toward service-account authentication and token acquisition without interactive user login."
    }
  ]
}
```

## 5. Recommended RAG query-rewriter flow

1. **Accept the original query.** Keep it unchanged for traceability.
2. **Extract intent and entities.** Example: “Why can't my app log in after changing the secret?” → intent: authentication failure; entity: client secret; possible context: CSS integration.
3. **Resolve terminology.** Map aliases to canonical terms using contextual hints and exclusions.
4. **Generate several query variants.**
   - Exact: `client secret rotation`
   - Semantic: `application authentication failure after regenerating confidential client credentials`
   - Diagnostic: `old secret invalidated, updated Installation JSON, wrong environment`
5. **Retrieve.** Search AWS Knowledge Bases with the original and selected expanded queries.
6. **Merge and deduplicate.** Preserve source metadata and document identifiers.
7. **Rerank.** Apply the Bedrock reranker to candidate passages.
8. **Answer with evidence.** Do not allow terminology expansion alone to establish facts that the retrieved documentation does not support.

## 6. Production refinements

- **Weighted expansion:** direct aliases such as `callback URL → redirect URI` should receive higher confidence than ambiguous mappings such as `project → integration`.
- **Negative mappings:** keep `CSS API Account`, `client secret`, and `user password` distinct even if users call all of them credentials.
- **Conditional expansion:** expand “password” to “client secret” only when context mentions a client, integration, token request, or similar concept.
- **Identifier preservation:** retain literal values such as `client_id`, realm names, environment names, URLs, error codes, claim names, and exact error messages.
- **Acronyms and spelling variants:** include `IdP` / `IDP` / `identity provider`; `BCSC` / `BC Services Card`; `OTP` / `one-time passcode`; and `CSS` / `Common Hosted Single Sign-On`.
- **Current facts vs. terminology:** use the dictionary to normalize language, but retrieve the current docs to determine whether a configuration or policy is supported.
- **Evaluation:** create paraphrase tests where user wording shares few literal words with relevant documentation. Measure retrieval recall@K, reranker effectiveness, and answer correctness before and after adding mappings.
- **Versioning:** add `source_url`, `last_verified`, `confidence`, and `doc_version` fields when operationalizing the dictionary.
- **Review ambiguous terms:** use clarification questions when the context cannot safely distinguish a realm, integration, client, team role, or API credential.

## 7. Official documentation starting points

- CSS SSO documentation: https://bcgov.github.io/sso-docs/
- CSS App FAQ: https://bcgov.github.io/sso-docs/FAQs/css-app
- Client Types: https://bcgov.github.io/sso-docs/css-application/client-types
- Redirect URIs: https://bcgov.github.io/sso-docs/css-application/redirects
- Role Management: https://bcgov.github.io/sso-docs/css-application/roles
- Keycloak Client Setup: https://bcgov.github.io/sso-docs/integrating-your-application/configuring-your-client

## 8. Recommended next step

Start with this dictionary, then expand it systematically from the site's headings, glossary terms, FAQs, troubleshooting content, and exact error messages. Give each mapping confidence and contextual conditions. This helps a Strands query-rewriter agent elaborate user questions without accidentally changing their meaning.
