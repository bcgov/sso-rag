# Sent instead of the model's answer when the documentation doesn't cover the
# question (grounding_status NOT_SUPPORTED) or the answer fails the grounding
# checks in app/services/grounding.py. The system prompt quotes it verbatim.
NO_DOCUMENTATION_RESPONSE = """
The available SSO documentation does not appear to cover this scenario.

For further assistance, contact:

- Microsoft Teams: [Keycloak How-to](https://teams.microsoft.com/l/channel/19%3A35d0b3389e39479590ba45a19a67a3ba%40thread.tacv2/SSOKeycloak-howto?groupId=a80418da-c27b-406e-89ab-7695b61924d8&tenantId=6fdb5200-3d0d-4a8a-b036-d3685e359adc)
- Email: bcgov.sso@gov.bc.ca

Include the following details, with secrets and tokens removed:

- Integration ID
- Environment
- Identity provider
- Client ID
- Exact error message
- Timestamp
""".strip()

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
