"""Evaluation settings. Read from environment variables (EVAL_*), so they
don't mix with the app's own settings in app/common/config.py."""

import os
from dataclasses import dataclass
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_DIR = EVALS_DIR.parent
DEFAULT_DATASET = EVALS_DIR / "datasets" / "goldens.jsonl"
DEFAULT_DRAFTS = EVALS_DIR / "datasets" / "drafts.jsonl"
RUNS_DIR = EVALS_DIR / "runs"
CHUNKS_DIR = REPO_DIR / "chunker" / "chunks"

# The judge: a different model family from the agent (Grok) and the query
# rewriter (Nova), so it doesn't favour its own model's wording.
DEFAULT_JUDGE_MODEL_ID = "global.moonshotai.kimi-k3"

# Score at or above which a judged metric passes (DeepEval's default).
JUDGE_THRESHOLD = 0.5


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class JudgeConfig:
    model_id: str = DEFAULT_JUDGE_MODEL_ID
    region: str = "ca-central-1"
    # Kimi K3's reasoning tokens count towards maxTokens, so leave room.
    max_tokens: int = 8192
    # Sent as additionalModelRequestFields.reasoning_effort, like the agent's
    # model; None sends nothing.
    reasoning_effort: str | None = "medium"
    # None leaves the model's default (reasoning models often fix it).
    temperature: float | None = None
    read_timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "JudgeConfig":
        temperature = _optional("EVAL_JUDGE_TEMPERATURE")
        effort = os.environ.get("EVAL_JUDGE_REASONING_EFFORT", cls.reasoning_effort or "")
        return cls(
            model_id=_optional("EVAL_JUDGE_MODEL_ID") or cls.model_id,
            region=_optional("EVAL_JUDGE_REGION") or _optional("AWS_REGION") or cls.region,
            max_tokens=int(_optional("EVAL_JUDGE_MAX_TOKENS") or cls.max_tokens),
            reasoning_effort=effort.strip() or None,
            temperature=float(temperature) if temperature is not None else None,
        )
