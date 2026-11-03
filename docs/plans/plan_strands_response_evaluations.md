# Plan: Strands response evaluations

Status: proposed for review. This document is a plan only; no evaluation
runner, dependency, dataset, or runtime behavior has been changed.

## 1. Objective and recommendation

Add a repeatable, **offline, live-AWS** response evaluation using
`strands-agents-evals`. Measure the answer the user actually receives, not
just the model's first draft. Keep evaluation code and its dependency out of
the production API image and request path.

Start with a small, human-reviewed regression set and a baseline report.
Do not treat an LLM judge, the model's own `grounding_status`, or today's
string-based grounding check as proof of factual correctness. Use all three
as complementary signals and review disputed cases against the source docs.

## 2. Existing behavior to evaluate

1. [ChatService.stream](../../app/services/chat_service.py) restores a fresh
   agent per request, starts the [query rewriter](../../app/services/query_rewriter.py),
   and invokes the no-argument [search tool](../../app/tools/knowledge_base_tool.py).
   Search uses the real Bedrock KB, optionally reranks, and collects evidence.
2. The agent's output may be repaired once. [grounding.verify](../../app/services/grounding.py)
   checks its grounding block, sources, URLs, and literal values. The stream
   yields one final `Answer(text, grounding_status, citations)`, including
   the no-documentation response if rejected. This is the evaluation target.
3. [chat.py](../../app/api/routes/chat.py) merely wraps that answer as SSE.
   Calling the agent directly, or using the SDK CLI's `--agent` factory,
   would skip the application's rewrite, search, repair, and delivery checks.
   Unit tests under [tests/](../../tests/) mock Bedrock; they do not measure
   live response quality. [evals/](../../evals/) has no tracked evaluation
   suite today.

## 3. Proposed implementation

| Area | Proposed change |
| --- | --- |
| Dependency | Add `strands-agents-evals` to the `dev` group in [pyproject.toml](../../pyproject.toml) and update [uv.lock](../../uv.lock). Confirm the resolved release works on Python 3.14 and with this project's Strands version. [Dockerfile](../../Dockerfile) uses `uv sync --no-dev`, so the API image stays unchanged. |
| Cases | Track `evals/cases.json` with stable, unique case IDs, category, input turn(s), a human-authored reference answer or success criteria, expected delivery status, and optional source-URL expectations. No real user chats, credentials, tokens, or private identifiers in fixtures. Record the documentation/KB ingestion version or date used to author the reference. |
| Runner | Add `evals/run.py`, invoked with `uv run python -m evals.run ...`. Build SDK `Case` objects and `Experiment` instances; a custom task runs one or more turns through `ChatService.stream`, consumes its final `Answer`, and returns SDK task output with `output=answer.text` and `environment_state` containing the delivered status and citation URIs. Use `Experiment.run_evaluations` or its async variant and save a machine-readable SDK report plus a per-category summary. |
| Quality judge | Use `OutputEvaluator` with a domain-specific rubric and an explicitly selected, approved Bedrock judge model. Compare the **delivered** answer to the human-authored reference, scoring factual consistency, completeness, relevant qualifications, and appropriate refusal. The judge must not supply facts from general Keycloak knowledge. Require a configured judge model rather than silently relying on the SDK's default Bedrock/Claude access. |
| Deterministic checks | Add a small custom SDK evaluator for case-specific delivery contracts: expected `grounding_status` (including `None` for greetings/clarifications), citation presence when an answer is supported, optional required source URLs, and the exact `NO_DOCUMENTATION_RESPONSE` for expected not-supported cases. Do not equate `SUPPORTED` with a correct answer or require one particular phrasing for valid answers. |
| Tests | Add mocked tests under [tests/](../../tests/) for case validation, isolated conversation handling, `Answer` extraction, evaluator pass/fail cases, and report creation without AWS or a judge call. Live cases are run explicitly, not from `pytest` or a deploy workflow. |

Use the SDK's `Case.metadata` for categories and case-specific assertions;
`Case.expected_output` supplies the reviewed answer to `OutputEvaluator`.
The SDK task output supports `environment_state`, and a custom evaluator can
read it alongside the case metadata. Split answerable cases (judge plus
deterministic checks) from greetings, clarifications, and genuine no-match
cases (deterministic checks plus a suitable response rubric only where useful).
This avoids charging a judge for mechanical checks.

The runner must load configuration from the repository root, require
`ENVIRONMENT=local`, and isolate each run in a temporary working directory
**before importing** [rag_agent.py](../../app/agents/rag_agent.py): its module-level
`LocalFileStorage("./.agent-store/")` otherwise points at a developer's real
sessions. Give each case a fresh valid `session_<uuid>`; for follow-up cases,
run the setup turns with that same ID and score only the final turn. Do not
mock the rewriter, retriever, reranker, answering model, or grounding checker
in live evaluations. Run cases sequentially initially to limit Bedrock
throttling, cost, and accidental session overlap.

Fail explicitly when AWS access, the KB/model IDs, the judge model, case
references, or an agent turn fail; an infrastructure error is **not** a
failed-quality score. Keep generated reports under a git-ignored
`evals/results/` directory: they can contain prompts, model output and source
URLs. No automatic telemetry export or production traffic replay.

## 4. Initial case coverage

Author approximately 20-30 cases from the *ingested* SSO documentation and
realistic, sanitized questions. Review expected facts and source URLs by hand
before baseline measurement:

- Straightforward answerable integration and configuration questions.
- Follow-ups requiring history resolution ("what about PROD?").
- Multi-part questions requiring both parts, and partially documented ones.
- Exact error strings, environment/IdP distinctions, and ambiguous requests
  that should elicit a clarification.
- Greetings (no search) and genuinely undocumented requests (appropriate
  no-documentation response).
- At least a few cases where the documented answer needs a link or precise
  literal value, to exercise citation and grounding behavior.

Tag cases by category so regressions are visible rather than hidden in one
mean. Avoid exact-string comparisons for normal prose. Keep source references
and expected behavior in sync with KB ingestion changes.

## 5. Running and interpreting the evaluations

Proposed command, from the repository root after implementation:

```bash
uv sync
ENVIRONMENT=local DEBUG_TRACE=false \
  uv run python -m evals.run \
    --cases evals/cases.json \
    --judge-model <approved-bedrock-model-id> \
    --output evals/results/baseline.json
```

Provide AWS credentials with access to the configured answer, rewriter,
reranker (if enabled), KB, and judge models; configure the existing
`KNOWLEDGE_BASE_ID`, `MODEL_ARN`, and optional `RERANKER_MODEL_ARN` using the
same local setup as the API. This is a **paid live run**. If the judge is not
available, a separately documented deterministic-only mode can run contract
checks but must not be reported as a quality evaluation.

Report per-case question/category, final response and status, citation URIs,
judge score and explanation, deterministic failures, elapsed time, and
aggregate pass rate **by category**. Include enough run metadata (time,
model IDs, SDK version, KB ingestion version, case-set revision) to compare
runs. Start with a baseline; before setting any release threshold, inspect
judge disagreements and non-determinism across repeat runs. A quality gate
should fail on critical factual/citation regressions, not only a mean score;
choose numeric thresholds after observing the baseline.

## 6. Documentation to update when implemented

- [README.md](../../README.md): install, AWS/judge prerequisites, the exact
  command, expected costs, report location, and how to compare runs under
  Testing. Distinguish mocked unit tests from live evaluations.
- [architecture.md](../architecture/architecture.md): add an **offline
  evaluation flow** beside the existing chat flow: curated cases -> isolated
  `ChatService` turns -> delivered answer -> SDK evaluators -> local report.
  State that it is not an API endpoint or deployed service. The interactive
  [architecture.html](../architecture/architecture.html) depicts deployed and
  local runtime flows; do not imply evaluations run on live traffic or alter
  its topology just to show an offline command.
- [request-flow.md](../components/request-flow.md) and
  [grounding-status.md](../components/grounding-status.md): brief links to the
  evaluation process and the distinction between deterministic grounding
  verification and reference-based response evaluation, if needed to keep
  their descriptions accurate.

## 7. Verification and rollout

1. Validate fixture schema and duplicate IDs; run focused mocked tests, then
   the existing `uv run pytest` suite.
2. Run a small paid smoke set (answer, greeting, no-match, follow-up) with
   live Bedrock; confirm the report reflects the **post-repair** answer,
   actual status/citations, and no files appear in `./.agent-store/`.
3. Run the reviewed full case set, inspect failed cases against the retrieved
   documentation and the project's rewrite/search logs, and retain the first
   report as a baseline. No CI or deployment gate in this first iteration.

SDK references used for this design: [quickstart](https://strandsagents.com/docs/user-guide/evals-sdk/quickstart/),
[custom tasks](https://strandsagents.com/docs/user-guide/evals-sdk/how-to/eval_task/),
[output evaluator](https://strandsagents.com/docs/user-guide/evals-sdk/evaluators/output_evaluator/),
and [custom evaluators](https://strandsagents.com/docs/user-guide/evals-sdk/evaluators/custom_evaluator/).
Trace-level evaluators such as
[FaithfulnessEvaluator](https://strandsagents.com/docs/user-guide/evals-sdk/evaluators/faithfulness_evaluator/)
need a session trajectory; they are deliberately out of scope until we can
capture the **delivered** post-verification answer together with retrieval
evidence, rather than accidentally grading a rejected raw agent reply.
