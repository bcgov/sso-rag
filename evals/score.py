"""Step 2: score a collected run with DeepEval metrics.

    uv run --group eval python -m evals.score evals/runs/<run id>
        [--deterministic-only] [--concurrency 4] [--metrics a,b]

Two kinds of metric, both DeepEval metrics run on LLMTestCases built from
results.jsonl:
- checks (evals/metrics.py): intent, outcome, retrieval hit@k / recall@k /
  MRR, citation precision, first-pass grounding acceptance. No model calls.
- judged, by the LLM judge (evals/judge.py; Kimi K3 by default):
  faithfulness, answer relevancy, contextual precision / recall,
  correctness, BC Gov bias and clarification quality, each only where it
  applies (see `plan_metrics`).

Writes scores.jsonl and summary.json next to results.jsonl, then report.md
(evals/report.py). Re-scoring never re-runs the agent.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from deepeval.metrics import (
    AnswerRelevancyMetric,
    BaseMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    FaithfulnessMetric,
    GEval,
)
from deepeval.test_case import LLMTestCase, SingleTurnParams

from evals import metrics as checks
from evals.config import JUDGE_THRESHOLD, JudgeConfig
from evals.dataset import EvalCase
from evals.judge import BedrockJudge

ANSWERED = ("answer", "general")


class CheckMetric(BaseMetric):
    """A deterministic check from evals/metrics.py as a DeepEval metric. The
    case and the scored turn travel in the test case's metadata."""

    def __init__(self, name: str, check, threshold: float, k: int):
        self._name = name
        self.check = check
        self.threshold = threshold
        self.k = k
        self.include_reason = True
        self.async_mode = False
        self.strict_mode = False
        self.evaluation_model = None
        self.error = None
        self.score = None
        self.reason = None
        self.success = None

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        case = EvalCase.model_validate(test_case.metadata["case"])
        result = self.check(case, test_case.metadata["turn"], self.k)
        self.score, self.reason = result if result else (None, "not applicable")
        self.success = self.score is not None and self.score >= self.threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self):
        return self._name


# GEval with explicit evaluation steps: no extra judge call to write them.
def _correctness(judge) -> GEval:
    return GEval(
        name="correctness",
        evaluation_steps=[
            "Compare the actual output with the expected output, which is a reference answer, "
            "possibly followed by 'Key facts' and 'Must not' lists.",
            "Penalize heavily any statement in the actual output that contradicts the reference "
            "or does something listed under 'Must not'.",
            "Penalize missing key facts in proportion to how many are missing.",
            "Do not penalize extra correct detail, different wording or different structure.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT,
                           SingleTurnParams.EXPECTED_OUTPUT],
        model=judge, threshold=JUDGE_THRESHOLD,
    )


def _bcgov_bias(judge) -> GEval:
    return GEval(
        name="bcgov_bias",
        evaluation_steps=[
            "The assistant serves BC Government technical teams. Decide whether the input is about "
            "building or running a BC Gov service, or a general identity question.",
            "For a BC Gov service, the actual output should recommend or explain the Common Hosted "
            "Single Sign-On (CSS) app and its identity providers (IDIR, BCeID, BC Services Card) "
            "rather than steering the user to a third-party identity provider.",
            "For a general identity question (OAuth, OIDC, SAML, Okta, Entra ID, Auth0, Clerk), the "
            "output may explain the topic generally but should say how it applies with CSS when that "
            "is relevant, and must not claim CSS supports something it does not state.",
            "Score low if the output recommends a third-party identity provider for a BC Gov service, "
            "ignores CSS where it clearly applies, or pushes CSS where it is irrelevant.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        model=judge, threshold=JUDGE_THRESHOLD,
    )


def _clarification_quality(judge) -> GEval:
    return GEval(
        name="clarification_quality",
        evaluation_steps=[
            "The actual output is a clarifying question asked instead of an answer.",
            "Check that the input really is missing a detail needed to answer (for example the "
            "error, the identity provider, the environment, or standard vs custom realm).",
            "Check that it asks one to three short, specific questions, most important first, "
            "that would let the assistant answer.",
            "Penalize clarifying questions that state unverified facts, ask for secrets or "
            "credentials, ask for details already given in the input, or are vague.",
        ],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        model=judge, threshold=JUDGE_THRESHOLD,
    )


def _reference(case: EvalCase) -> str | None:
    if not case.expected_output:
        return None
    parts = [case.expected_output.strip()]
    if case.key_facts:
        parts.append("Key facts:\n" + "\n".join(f"- {f}" for f in case.key_facts))
    if case.must_not:
        parts.append("Must not:\n" + "\n".join(f"- {f}" for f in case.must_not))
    return "\n\n".join(parts)


@dataclass
class Planned:
    name: str
    kind: str  # "check" | "judge"
    metric: BaseMetric
    test_case: LLMTestCase


def plan_metrics(case: EvalCase, turn: dict, k: int, judge=None,
                 only: set[str] | None = None) -> list[Planned]:
    """The metrics that apply to a case's scored turn. Without a judge, only
    the deterministic checks."""

    metadata = {"case": case.model_dump(), "turn": turn}
    answer = turn.get("answer") or ""
    context = [r.get("text") or "" for r in turn.get("retrieved") or []]
    reference = _reference(case)
    outcome = turn.get("outcome")

    def tc(**overrides) -> LLMTestCase:
        fields = {"input": case.question, "actual_output": answer, "expected_output": reference,
                  "retrieval_context": context or None, "metadata": metadata, "name": case.id}
        fields.update(overrides)
        return LLMTestCase(**fields)

    planned: list[Planned] = []

    def add(name: str, kind: str, factory) -> None:
        if only and name not in only:
            return
        metric, test_case = factory()
        planned.append(Planned(name, kind, metric, test_case))

    for name, (check, threshold) in checks.CHECKS.items():
        if check(case, turn, k) is not None:
            add(name, "check", lambda n=name, c=check, t=threshold: (CheckMetric(n, c, t, k), tc()))

    if judge is None or turn.get("error") or not answer:
        return planned

    common = {"model": judge, "threshold": JUDGE_THRESHOLD, "eval_mode": "llm"}
    if outcome == "answer" and context:
        add("faithfulness", "judge", lambda: (FaithfulnessMetric(**common), tc()))
    if outcome == "general" and context:
        # Only the "For BC Gov teams" part has to come from the documentation.
        bcgov_part = checks.split_bcgov_part(answer)[1]
        if bcgov_part:
            add("faithfulness", "judge",
                lambda: (FaithfulnessMetric(**common), tc(actual_output=bcgov_part)))
    if outcome in ANSWERED:
        add("answer_relevancy", "judge", lambda: (AnswerRelevancyMetric(**common), tc()))
    if reference and context:
        add("contextual_precision", "judge", lambda: (ContextualPrecisionMetric(**common), tc()))
        add("contextual_recall", "judge", lambda: (ContextualRecallMetric(**common), tc()))
    if reference and outcome in ANSWERED:
        add("correctness", "judge", lambda: (_correctness(judge), tc()))
    if outcome in ANSWERED and (case.expected_intent == "general_auth" or case.category == "onboarding"):
        add("bcgov_bias", "judge", lambda: (_bcgov_bias(judge), tc()))
    if outcome == "clarification":
        add("clarification_quality", "judge", lambda: (_clarification_quality(judge), tc()))
    return planned


async def _measure(p: Planned, semaphore: asyncio.Semaphore) -> dict:
    error = None
    started = time.monotonic()
    async with semaphore:
        try:
            if p.kind == "check":
                p.metric.measure(p.test_case)
            else:
                await p.metric.a_measure(p.test_case, _show_indicator=False)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    return {
        "metric": p.name,
        "kind": p.kind,
        "score": None if error else p.metric.score,
        "success": False if error else bool(p.metric.is_successful()),
        "threshold": p.metric.threshold,
        "reason": None if error else p.metric.reason,
        "error": error or (str(p.metric.error) if getattr(p.metric, "error", None) else None),
        "seconds": round(time.monotonic() - started, 2),
    }


async def _score_all(results: list[dict], k: int, judge, concurrency: int,
                     only: set[str] | None) -> list[dict]:
    semaphore = asyncio.Semaphore(concurrency)
    jobs = []
    for result in results:
        if not result.get("turns"):
            continue
        case = EvalCase.model_validate(result["case"])
        turn = result["turns"][-1]
        for p in plan_metrics(case, turn, k, judge, only):
            jobs.append((result, p))
    print(f"scoring {len(jobs)} metric runs over {len(results)} cases", flush=True)

    async def one(result: dict, p: Planned) -> dict:
        row = await _measure(p, semaphore)
        return {"id": result["id"], "category": result["case"]["category"], **row}

    return await asyncio.gather(*(one(r, p) for r, p in jobs))


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return meta, results


def main(argv: list[str] | None = None) -> Path:
    from evals.report import write_report

    parser = argparse.ArgumentParser(prog="python -m evals.score", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--deterministic-only", action="store_true", help="no judge calls")
    parser.add_argument("--concurrency", type=int, default=4, help="judge metrics run at once")
    parser.add_argument("--metrics", help="comma-separated metric names to run (default: all that apply)")
    parser.add_argument("--k", type=int, help="retrieval cut-off (default: the run's NUMBER_OF_RERANKED_RESULTS)")
    args = parser.parse_args(argv)

    meta, results = load_run(args.run_dir)
    k = args.k or meta.get("settings", {}).get("number_of_reranked_results") or 6
    judge = None if args.deterministic_only else BedrockJudge(JudgeConfig.from_env())
    only = {m.strip() for m in args.metrics.split(",")} if args.metrics else None

    started = time.monotonic()
    rows = asyncio.run(_score_all(results, k, judge, args.concurrency, only))
    (args.run_dir / "scores.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")

    scoring = {
        "k": k,
        "judge": judge.usage() if judge else None,
        "judge_config": vars(judge.config) if judge else None,
        "seconds": round(time.monotonic() - started, 1),
        "metric_errors": sum(1 for r in rows if r["error"]),
    }
    (args.run_dir / "scoring.json").write_text(json.dumps(scoring, indent=2, default=str), encoding="utf-8")
    report = write_report(args.run_dir)
    print(f"{len(rows)} scores, {scoring['metric_errors']} metric errors. Report: {report}")
    return report


if __name__ == "__main__":
    main()
