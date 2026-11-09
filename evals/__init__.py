"""Offline evaluation of the RAG pipeline with DeepEval. See evals/README.md.

Imported before deepeval anywhere in this package, so these take effect:
- no DeepEval telemetry (PostHog);
- DeepEval doesn't load .env into os.environ (the app's settings read .env
  themselves; nothing else should pick values up from it);
- LLM-only judging: no "System One" model (a hosted service) is used.
Nothing here logs in to Confident AI, so results stay on this machine.
"""

import os

os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_EVAL_MODE"] = "llm"
