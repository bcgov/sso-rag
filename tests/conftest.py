import os

# Settings are read at import time; give required values so the app modules
# import without a real environment. Real env vars still win.
os.environ.setdefault("KNOWLEDGE_BASE_ID", "test-kb")
os.environ.setdefault("MODEL_ARN", "arn:aws:bedrock:ca-central-1::foundation-model/test-model")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "ca-central-1")
# Fake Keycloak settings: tests sign their own tokens (tests/test_authentication.py).
# Authentication stays on; a local .env must not switch it off for the suite.
os.environ.setdefault("KEYCLOAK_ISSUER_URL", "https://keycloak.test/auth/realms/test")
os.environ.setdefault("KEYCLOAK_AUDIENCE", "sso-rag-api")
os.environ.setdefault("KEYCLOAK_AUTHORIZED_PARTY", "sso-rag-client")
os.environ["AUTH_LOCAL_BYPASS"] = "false"
