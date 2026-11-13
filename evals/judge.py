"""The LLM judge for DeepEval metrics: a Bedrock model called through the
Converse API with boto3 (already an app dependency).

DeepEval's own AmazonBedrockModel needs aiobotocore, which pins botocore
versions; this class avoids that, keeps only the text blocks of a reasoning
model's reply (Kimi K3 also returns reasoning blocks), and counts tokens.

DeepEval calls `a_generate(prompt, schema=...)` and accepts a validated
schema instance (what this returns) or a JSON string.
"""

import asyncio
import json
import re
import threading

import boto3
from botocore.config import Config
from deepeval.models import DeepEvalBaseLLM
from pydantic import BaseModel, ValidationError

from evals.config import JudgeConfig

JSON_SYSTEM = (
    "You are a careful evaluator. Follow the instructions in the user message "
    "exactly. Reply with only the JSON object it asks for: no prose, no "
    "markdown fences."
)
JSON_RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON for the requested format. "
    "Reply again with only the JSON object."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class JudgeError(RuntimeError):
    pass


def parse_json_reply(text: str, schema: type[BaseModel]) -> BaseModel:
    """The JSON object in a judge reply, validated against `schema`. Accepts
    a fenced block or prose around the object. Raises ValueError."""
    text = _FENCE_RE.sub("", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        return schema.model_validate(json.loads(text[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(str(e)) from e


class BedrockJudge(DeepEvalBaseLLM):

    def __init__(self, config: JudgeConfig | None = None, client=None):
        self.config = config or JudgeConfig.from_env()
        self._client = client
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.errors = 0
        super().__init__(self.config.model_id)

    def load_model(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime",
                config=Config(
                    region_name=self.config.region,
                    connect_timeout=10,
                    read_timeout=self.config.read_timeout_seconds,
                    # Judging runs many calls at once; back off on throttling.
                    retries={"max_attempts": 8, "mode": "adaptive"},
                ),
            )
        return self._client

    def get_model_name(self) -> str:
        return self.config.model_id

    def usage(self) -> dict:
        with self._lock:
            return {
                "model": self.config.model_id,
                "calls": self.calls,
                "errors": self.errors,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }

    def _converse(self, prompt: str, json_only: bool) -> str:
        request: dict = {
            "modelId": self.config.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": self.config.max_tokens},
        }
        if json_only:
            request["system"] = [{"text": JSON_SYSTEM}]
        if self.config.temperature is not None:
            request["inferenceConfig"]["temperature"] = self.config.temperature
        if self.config.reasoning_effort:
            request["additionalModelRequestFields"] = {"reasoning_effort": self.config.reasoning_effort}

        try:
            response = self.load_model().converse(**request)
        except Exception:
            with self._lock:
                self.errors += 1
            raise

        usage = response.get("usage") or {}
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.get("inputTokens") or 0
            self.output_tokens += usage.get("outputTokens") or 0

        blocks = response.get("output", {}).get("message", {}).get("content") or []
        # Text blocks only: reasoning blocks are the model's working.
        text = "\n".join(b["text"] for b in blocks if isinstance(b.get("text"), str) and b["text"].strip())
        if not text:
            raise JudgeError(f"judge returned no text (stopReason={response.get('stopReason')})")
        return text

    def generate(self, prompt: str, schema: type[BaseModel] | None = None, **_) -> str | BaseModel:
        if schema is None:
            return self._converse(prompt, json_only=False)
        reply = self._converse(prompt, json_only=True)
        try:
            return parse_json_reply(reply, schema)
        except ValueError:
            # One retry: reasoning models occasionally wrap or truncate JSON.
            return parse_json_reply(self._converse(prompt + JSON_RETRY_SUFFIX, json_only=True), schema)

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None, **kwargs):
        return await asyncio.to_thread(self.generate, prompt, schema, **kwargs)
