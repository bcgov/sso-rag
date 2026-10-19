# Acronyms and shorthand that the docs usually spell out. When a query uses
# the short form, the expansion is appended so the embedding also matches the
# documentation's wording. Keep this to genuine abbreviations: general words
# ("client", "token", "test") match almost every query and dilute retrieval.
# Conceptual vocabulary mapping lives in the system prompt instead.
ACRONYMS = {
    # Platform
    "css": "Common Hosted Single Sign-On",
    "css api": "Common Hosted Single Sign-On API",
    "sso": "Single Sign-On",
    "kc": "Keycloak",
    "dr": "disaster recovery",
    "golddr": "Gold disaster recovery",
    "rhbk": "Red Hat Build Of Keycloak",
    "spi": "Service Provider Interface / Custom Plugins",

    # Identity providers
    "idp": "identity provider",
    "azureidir": "IDIR with MFA / Entra-backed IDIR / Legacy IDIR",
    "azure idir": "IDIR with MFA",
    "bcsc": "BC Services Card",
    "otp": "One Time Passcode",
    "bcgovidir": "Entra-backed IDIR with MFA",

    # Authentication
    "mfa": "multi-factor authentication",
    "m2m": "service account client credentials",
    "pkce": "Proof Key for Code Exchange",
    "oidc": "OpenID Connect",
    "jwt": "JSON Web Token",
    "jwk": "JSON Web Key",
    "jwks": "JSON Web Key Set",
    "rbac": "role-based access control",
    "ial": "identity assurance level",
    "saml": "Security Assertion Markup Language",

    # Supporting teams and systems
    "idim": "Identity Information Management",
    "cdt": "Cybersecurity and Digital Trust",
    "clp": "Common Logon Page",
    "sub": "subject claim",
    "aud": "audience claim",
    "iat": "issued at claim",
    "citz": "Ministry of Citizens' Services",
    "ppid": "pairwise pseudonymous identifier",
    "pz": "bc services card privacy zone",
    "ias": "bc services card identity and authentication service",
    "siem": "Security Information and Event Management and Splunk system",
    "wam": "Web Access Management team",
    "adfs": "Active Directory Federation Services",
}
