import os

# Settings are read at import time; tests never reach AWS.
os.environ.setdefault("KNOWLEDGE_BASE_ID", "test-kb")
os.environ.setdefault("MODEL_ARN", "arn:aws:bedrock:ca-central-1::foundation-model/test-model")
os.environ.setdefault("AWS_REGION", "ca-central-1")
os.environ.setdefault("ENVIRONMENT", "local")
