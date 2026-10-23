import os

# Settings are read at import time; give required values so the app modules
# import without a real environment. Real env vars still win.
os.environ.setdefault("KNOWLEDGE_BASE_ID", "test-kb")
os.environ.setdefault("MODEL_ARN", "arn:aws:bedrock:ca-central-1::foundation-model/test-model")
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("AWS_DEFAULT_REGION", "ca-central-1")
