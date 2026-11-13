"""The evaluation dataset: one JSON object per line (JSONL), validated here.

    {"id": "rotate-secret", "category": "sso_question",
     "turns": ["how do I rotate my client secret?"],
     "expected_intent": "sso_question", "expected_outcome": "answer",
     "expected_sources": ["https://bcgov.github.io/sso-docs/FAQs/css-app"],
     "expected_output": "...", "key_facts": ["..."], "must_not": ["..."]}

- turns: the user's messages in one conversation. All but the last run first
  (so the history holds the system's own replies); the last one is scored.
- expected_outcome: what the user should get, one value or a list of
  acceptable ones (e.g. ["answer", "clarification"]).
- expected_sources: documentation pages that answer it. Matched by page
  (fragment, query, trailing slash and case ignored), so a page URL matches
  every chunk of that page.
- expected_output / key_facts / must_not: the reference answer and what the
  judge checks it against. Without expected_output, correctness isn't judged.
- draft: true until a person has reviewed the case (see evals/README.md).
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Intent = Literal["sso_question", "general_auth", "conversational", "off_topic"]
Outcome = Literal["answer", "general", "clarification", "conversational", "not_supported"]
OUTCOMES: tuple[str, ...] = ("answer", "general", "clarification", "conversational", "not_supported")


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=80)
    category: str = Field(min_length=1)
    turns: list[str] = Field(min_length=1)
    expected_intent: Intent | None = None
    expected_outcome: list[Outcome] = Field(default_factory=list)
    expected_sources: list[str] = Field(default_factory=list)
    expected_output: str | None = None
    key_facts: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    notes: str | None = None
    draft: bool = False

    @field_validator("expected_outcome", mode="before")
    @classmethod
    def _one_or_many(cls, value):
        if value is None:
            return []
        return [value] if isinstance(value, str) else value

    @field_validator("turns")
    @classmethod
    def _non_blank_turns(cls, value: list[str]) -> list[str]:
        if any(not t.strip() for t in value):
            raise ValueError("turns must not be blank")
        return value

    @property
    def question(self) -> str:
        """The scored turn."""
        return self.turns[-1]


class DatasetError(ValueError):
    """Every problem found in a dataset file, one per line of the message."""


def load_cases(path: Path | str) -> list[EvalCase]:
    path = Path(path)
    cases: list[EvalCase] = []
    problems: list[str] = []
    seen: dict[str, int] = {}

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            case = EvalCase.model_validate(json.loads(line))
        except json.JSONDecodeError as e:
            problems.append(f"{path.name}:{number}: invalid JSON ({e.msg})")
            continue
        except ValidationError as e:
            for err in e.errors():
                where = ".".join(str(p) for p in err["loc"]) or "line"
                problems.append(f"{path.name}:{number}: {where}: {err['msg']}")
            continue
        if case.id in seen:
            problems.append(f"{path.name}:{number}: duplicate id {case.id!r} (first on line {seen[case.id]})")
            continue
        seen[case.id] = number
        cases.append(case)

    if problems:
        raise DatasetError("\n".join(problems))
    return cases


def select(
    cases: list[EvalCase],
    ids: list[str] | None = None,
    categories: list[str] | None = None,
    include_drafts: bool = True,
    limit: int | None = None,
) -> list[EvalCase]:
    if ids:
        unknown = set(ids) - {c.id for c in cases}
        if unknown:
            raise DatasetError(f"unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in ids]
    if categories:
        cases = [c for c in cases if c.category in categories]
    if not include_drafts:
        cases = [c for c in cases if not c.draft]
    return cases[:limit] if limit else cases


def write_cases(path: Path | str, cases: list[EvalCase]) -> None:
    lines = [json.dumps(c.model_dump(exclude_defaults=True), ensure_ascii=False) for c in cases]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
