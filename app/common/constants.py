GLOSSARY = {
    # Core Platform
    "css": "Common Hosted Single Sign-On (CSS), self-service SSO integration portal",
    "sso": "Single Sign-On (SSO), Pathfinder SSO service",
    "pathfinder": "Pathfinder SSO team, BC Government SSO service",
    "pathfinder sso": "Common Hosted Single Sign-On CSS Keycloak service",
    "keycloak": "Keycloak identity and access management platform",
    "realm": "Keycloak realm, standard realm or custom realm",
    "standard realm": "Gold standard Keycloak realm used by CSS integrations",
    "custom realm": "Dedicated Keycloak realm managed separately from standard realm",
    "gold": "Gold Keycloak platform using loginproxy.gov.bc.ca",
    "silver": "Legacy Silver Keycloak platform being deprecated",
    "loginproxy": "BC Government Keycloak login service loginproxy.gov.bc.ca",

    # Identity Providers
    "idp": "identity provider",
    "identity provider": "authentication provider such as IDIR, BCeID, BCSC, GitHub",
    "idir": "IDIR BC Government employee identity provider",
    "idir mfa": "IDIR with MFA identity provider",
    "azureidir": "IDIR with MFA backed by Microsoft Entra ID",
    "entra": "Microsoft Entra ID",
    "azure": "Microsoft Entra ID identity provider",
    "entra guest": "Microsoft Entra guest account",
    "guest account": "Microsoft Entra guest account",
    "bceid": "BCeID identity provider",
    "basic bceid": "Basic BCeID citizen login",
    "business bceid": "Business BCeID organization login",
    "bceidboth": "Basic or Business BCeID combined identity provider",
    "bcsc": "BC Services Card identity provider",
    "services card": "BC Services Card identity provider",
    "bc services card": "BC Services Card citizen authentication",
    "digital credential": "Digital Credential identity provider",
    "bc wallet": "Digital Credential and BC Wallet provider",
    "github": "GitHub identity provider",
    "github bc gov": "GitHub BC Gov organization identity provider",
    "social": "Social login identity provider",
    "otp": "One Time Passcode identity provider",
    "one time passcode": "email-based authentication using OTP",

    # Authentication
    "mfa": "multi-factor authentication",
    "totp": "time-based one-time passcode",
    "auth": "authentication",
    "oidc": "OpenID Connect protocol",
    "openid": "OpenID Connect",
    "oauth": "OAuth 2.0 authorization framework",
    "oauth2": "OAuth 2.0 protocol",
    "pkce": "Proof Key for Code Exchange for public clients",
    "saml": "Security Assertion Markup Language",
    "authorization code": "OIDC Authorization Code Flow",
    "client credentials": "OAuth2 Client Credentials Grant",
    "browser login": "interactive user authentication flow",
    "service account": "machine-to-machine authentication client",
    "m2m": "machine-to-machine service account integration",

    # Clients
    "client": "Keycloak integration client",
    "client id": "unique identifier of Keycloak client",
    "client secret": "confidential client secret",
    "secret": "client secret used by confidential clients",
    "public client": "client without secret, typically SPA",
    "confidential client": "client with server-side secret",
    "installation json": "downloaded Keycloak client configuration file",
    "client adapter": "Keycloak integration library or adapter",
    "resource": "client identifier field in installation JSON",

    # Tokens
    "token": "OIDC or OAuth access token",
    "access token": "OAuth access token",
    "refresh token": "token used to obtain new access tokens",
    "id token": "OIDC identity token",
    "offline token": "long-lived Keycloak token",
    "jwt": "JSON Web Token",
    "bearer token": "OAuth bearer access token",
    "client_roles": "token claim containing assigned roles",
    "session_state": "OIDC session tracking identifier",
    "preferred_username": "username claim in token",
    "identity_provider": "token claim indicating authentication source",
    "sub": "subject identifier, unique user GUID",
    "audience": "JWT token audience claim",
    "issuer": "JWT token issuer claim",
    "amr": "authentication methods reference claim",

    # User Attributes
    "guid": "globally unique identifier",
    "idir guid": "unique IDIR user identifier",
    "idir_user_guid": "IDIR user GUID claim",
    "idir_username": "IDIR username claim",
    "preferred username": "preferred_username token claim",
    "display name": "user display name attribute",
    "email verified": "OIDC email_verified claim",

    # Roles & Access
    "roles": "application client roles",
    "role": "Keycloak client role",
    "role assignment": "mapping users to client roles",
    "client role": "application-specific role",
    "composite role": "role made up of other roles",
    "role mapping": "user to role association",
    "rbac": "role-based access control",
    "admin": "team administrator role",
    "member": "team member role",

    # Teams
    "team": "CSS project team",
    "project team": "group managing integrations",
    "team admin": "administrator of a CSS team",
    "team member": "member of a CSS team",

    # Integrations
    "integration": "SSO integration requested through CSS",
    "request": "SSO integration request",
    "request id": "integration identifier",
    "project": "application associated with an integration",

    # Environments
    "dev": "development environment",
    "test": "test environment",
    "prod": "production environment",
    "environment": "dev, test, or production Keycloak environment",

    # Configuration
    "redirect uri": "allowed application callback URL",
    "logout uri": "post logout redirect URL",
    "logout": "OIDC logout flow",
    "id_token_hint": "OIDC logout identifier",
    "kc_idp_hint": "Keycloak parameter to bypass login screen",
    "idphint": "identity provider hint parameter",
    "well known": "OpenID configuration discovery endpoint",
    "openid configuration": "OIDC discovery document",
    "discovery url": "OpenID Connect configuration endpoint",

    # CSS API
    "css api": "REST API for managing CSS integrations",
    "api account": "service account for CSS API access",
    "api token": "bearer token used with CSS API",
    "swagger": "OpenAPI documentation for CSS API",

    # Infrastructure
    "openshift": "Red Hat OpenShift Container Platform",
    "redis": "rate limiting datastore",
    "postgres": "PostgreSQL database",
    "cron": "scheduled background job",
    "terraform": "infrastructure-as-code used for provisioning",
    "kong": "API gateway frequently used with CSS integrations",
    "grafana": "monitoring platform integrated with SSO",
    "jenkins": "CI/CD platform integrated with SSO",

    # Supporting Systems
    "idim": "Identity and Information Management team",
    "cdt": "Cybersecurity and Digital Trust team",
    "adms": "Active Directory and Microsoft Services team",
    "wam": "Web Access Management team",
    "siteminder": "CA SiteMinder authentication infrastructure",
    "clp": "Common Logon Page",
    "graph api": "Microsoft Graph API",
    "cap": "BC Government CAP web service",
    "web service": "IDIM user lookup service",

    # Monitoring & Reliability
    "uptime": "service availability metric",
    "outage": "service disruption",
    "status page": "status.loginproxy.gov.bc.ca",
    "maintenance": "planned service maintenance",
    "dr": "disaster recovery",
    "golddr": "Gold disaster recovery cluster",
    "heartbeat": "system health endpoint",

    # BC Services Card Specific
    "privacy zone": "BC Services Card privacy context",
    "identity assurance": "identity assurance level",
    "ial": "identity assurance level",
    "authoritative party": "BCSC authoritative identity source",

    # Security
    "cors": "Cross-Origin Resource Sharing",
    "ssl": "Secure Sockets Layer certificate",
    "certificate": "TLS certificate",
    "keystore": "certificate trust store",
    "truststore": "certificate trust store",
    "jwk": "JSON Web Key",
    "jwks": "JSON Web Key Set",
    "public key": "JWT verification key",
    "signature": "cryptographic token signature",

    # Support
    "support": "Pathfinder SSO support",
    "faq": "frequently asked questions",
    "bcgov.sso": "bcgov.sso@gov.bc.ca support mailbox",
    "rocketchat": "SSO community support channel"
}