# Evaluations with DeepEval

How the offline evaluation in [evals/](../../evals/) measures the assistant: how a test case reaches the real pipeline, what is recorded on the way, how DeepEval turns it into scores, and how the LLM judge (Kimi K3 on Bedrock) is called.

This page explains the design. For the commands, options, run files and the dataset schema, see [evals/README.md](../../evals/README.md).

| Stage | Code |
|---|---|
| Test cases and their schema | [goldens.jsonl](../../evals/datasets/goldens.jsonl), [dataset.py](../../evals/dataset.py) |
| Run cases through the pipeline | [collect.py](../../evals/collect.py) |
| Per-turn recording inside the app | [turn_report.py](../../app/common/turn_report.py) |
| Deterministic checks | [metrics.py](../../evals/metrics.py) |
| DeepEval test cases and metrics | [score.py](../../evals/score.py) |
| LLM judge (Bedrock Converse) | [judge.py](../../evals/judge.py), [config.py](../../evals/config.py) |
| Report and run comparison | [report.py](../../evals/report.py) |
| Drafting new cases | [synthesize.py](../../evals/synthesize.py) |

## Overview

The evaluation runs in three steps. Each step reads the previous step's files, so a run can be re-scored or re-reported without calling the agent again.

```text
evals/datasets/goldens.jsonl           hand-written cases (turns + expectations)
    ↓
1. collect                              real pipeline, Bedrock agent + rewriter + KB + reranker
    │  ChatService.stream(turn)  ── same entry point as POST /chat
    │  TurnReport (ContextVar)   ── rewrite, search hits, model calls, reply attempts
    ↓
evals/runs/<run id>/results.jsonl      one line per case: every turn + what happened inside it
    ↓
2. score                                DeepEval
    │  LLMTestCase per (case, metric)
    │  ├─ CheckMetric (no model calls) ── intent, outcome, retrieval, citations, grounding
    │  └─ DeepEval metrics + GEval ───── BedrockJudge ──► Bedrock Converse ──► Kimi K3
    ↓
evals/runs/<run id>/scores.jsonl       one row per (case, metric): score, pass, reason, error
    ↓
3. report                               report.md, summary.json, compare-<baseline>.md
```

Collecting is the expensive step: every case makes the same Bedrock calls a user's question would. Scoring makes judge calls only. `--deterministic-only` scoring makes no model calls at all.

## Which parts of DeepEval are used

DeepEval is used as a library of metrics, not as a test runner or a platform.

| DeepEval feature | Used | How |
|---|---|---|
| `LLMTestCase` | Yes | Built from `results.jsonl` at scoring time ([From results to test cases](#from-results-to-test-cases)) |
| `FaithfulnessMetric`, `AnswerRelevancyMetric`, `ContextualPrecisionMetric`, `ContextualRecallMetric` | Yes | With `eval_mode="llm"` and the Bedrock judge |
| `GEval` | Yes | Correctness, BC Gov bias and clarification quality, each with fixed `evaluation_steps` |
| `BaseMetric` | Yes | `CheckMetric` wraps the deterministic checks so every score has the same shape |
| `DeepEvalBaseLLM` | Yes | `BedrockJudge`, the judge model |
| `Synthesizer` | Yes | Only in `evals.synthesize`, to draft new cases from documentation chunks |
| `evaluate()`, `assert_test`, `deepeval test run` | No | Each metric is called directly with `a_measure()`, so a case gets only the metrics that apply to it, a failing metric is recorded as an error row instead of stopping the run, and concurrency is controlled by `--concurrency` |
| `EvaluationDataset` / `Golden` | No | Cases live in a JSONL file with a stricter schema (`EvalCase`) that also holds multi-turn conversations and expected outcomes |
| `AmazonBedrockModel` | No | It needs `aiobotocore`, which pins `botocore` and would conflict with the app's `boto3` |
| Confident AI (`deepeval login`), telemetry | No | Turned off in [evals/\_\_init\_\_.py](../../evals/__init__.py) |

## Step 1: collecting a run

`python -m evals.collect` runs each selected case in a new conversation and saves what happened.

### Running a case

1. **Isolate storage.** Before importing the app, `collect` sets `ENVIRONMENT=local`, `DEBUG_TRACE=false` and `AGENT_STORE_DIR` to a new temporary directory. The app chooses its conversation storage at import time, so this has to happen first; `collect` raises if any `app` module was already imported. It then checks that the agent's storage really is a `LocalFileStorage` in that directory, so a run never writes to the S3 conversation bucket. The directory is deleted at the end.
2. **One conversation per case.** Each case gets a new session id. Its `turns` are sent one after another, so later turns see the assistant's real earlier replies in the history. The session is deleted when the case finishes.
3. **The real pipeline.** Each turn calls `ChatService.stream()`, the same method the `/chat` route uses. The query rewriter, Knowledge Base search, reranking, the agent, the grounding check and its repair attempt all run exactly as in production, with the models and settings in your `.env`.
4. **Capture the turn.** The turn runs inside `turn_report.capture()`, which collects events recorded by the app (next section). When the stream ends, `summarize_turn()` combines the delivered `Answer` and those events into one turn result.
5. **Save as you go.** Each finished case is appended to `results.jsonl`, so an interrupted run keeps what it finished. A turn that raises is saved with its `error`, and the run continues with the next case.

`run.json` records the git commit (and whether there were uncommitted changes), the dataset's hash, and the model and retrieval settings, so two runs can be compared fairly.

### What the app records

The delivered answer alone doesn't say which intent the rewriter chose, what was retrieved, or whether the first reply was rejected. To score those, the app calls `record()` at a few points. It is defined in [turn_report.py](../../app/common/turn_report.py):

| Event | Recorded in | Fields used by the evaluation |
|---|---|---|
| `rewrite` | [query_rewriter.py](../../app/services/query_rewriter.py) | Intent, rewritten query, sub-queries, fallback, latency, token usage |
| `rewrite_retry` | query_rewriter.py | Retry queries and their token usage |
| `search` | [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) | Queries sent, ranked hits (URI, score, which query found them) |
| `search_skipped` | knowledge_base_tool.py | Why the tool didn't search (e.g. greeting) |
| `model_call` | [chat_service.py](../../app/services/chat_service.py) | Agent token usage per call, including cache reads and writes |
| `attempt` | chat_service.py | Each reply (first and repair): raw text, `response_type`, grounding status, rejection reasons, citations, and the retrieved chunks it was checked against |
| `link_only` | chat_service.py | The reply was a documentation link instead of an answer |

`record()` writes to the `TurnReport` held in a `ContextVar`. In the deployed service nothing calls `capture()`, so there is no report and `record()` returns immediately.

A `ContextVar` keeps concurrent turns separate: each asyncio task, and each `asyncio.to_thread` call (Strands runs sync tools that way), gets a copy of the caller's context. The rewriter's `ThreadPoolExecutor` doesn't copy context by itself, so `QueryRewriter.submit()` runs its job through `contextvars.copy_context().run`. Without that, the rewriter's events would be lost.

### The outcome of a turn

Every turn is classified by what the user actually got. Most checks and the choice of judged metrics depend on it.

| Outcome | When |
|---|---|
| `not_supported` | Grounding status `NOT_SUPPORTED`: the user got the no-documentation reply (Teams and email contacts) |
| `general` | Grounding status `GENERAL`: an answer to a general identity question |
| `clarification` | A link-only reply, a reply with no checked attempt, or a reply the model marked `clarification` |
| `conversational` | A greeting or small-talk reply |
| `answer` | A documented answer (`SUPPORTED` or `PARTIALLY_SUPPORTED`) |

See [grounding-status.md](grounding-status.md) for how the statuses and response types are set.

### Conversation history in multi-turn cases

A multi-turn case runs like a real conversation: each later question reaches the agent together with the earlier questions and the replies that were actually delivered. DeepEval isn't involved at this point; `collect` sends every turn of a case through `ChatService.stream` with the same session id ([collect.py](../../evals/collect.py), `_run_case`). On turn 2 and later:

1. **The conversation is restored.** The agent loads the session's saved history from the temporary store.
   - The history holds the reply the user received. If the grounding check replaced a reply with the no-documentation message, the agent sees that message, not the rejected draft ([chat_service.py](../../app/services/chat_service.py)).
   - Earlier turns' search calls and results are removed on restore ([rag_agent.py](../../app/agents/rag_agent.py)), so a follow-up has to search again.
2. **The history is trimmed.** `CONVERSATION_WINDOW_MESSAGES` (default 10) keeps the most recent messages, about the last 5 questions and answers.
3. **The rewriter gets recent history too.** `ChatService` passes the last `QUERY_REWRITER_HISTORY_TURNS` exchanges (default 3) to the query rewriter. That's how a follow-up like "how do I rotate it?" becomes a complete search query.

Each case starts a new session, so nothing carries over between cases, and single-turn cases have no history. Each turn's result, including the earlier ones, is saved in `results.jsonl`.

## Step 2: scoring

`python -m evals.score <run dir>` reads `results.jsonl`, builds DeepEval test cases, runs the metrics that apply, and writes `scores.jsonl`. Only the **last turn** of each case is scored. Earlier turns exist to set up the conversation.

### From results to test cases

Each metric gets an `LLMTestCase` built from the case and its last turn:

| `LLMTestCase` field | Comes from |
|---|---|
| `input` | The last user message (`turns[-1]`) |
| `actual_output` | The answer the user received, after the grounding check. For a `general` answer's faithfulness, only its "For BC Gov teams" part |
| `expected_output` | `expected_output`, followed by `Key facts:` and `Must not:` lists when the case has them. Empty when the case has no reference answer |
| `retrieval_context` | The text of the chunks the first reply was checked against, best first |
| `metadata` | The whole case and turn result, for the deterministic checks |
| `name` | The case id |

Two consequences:

- The judge sees the last message only, not the earlier turns ([What the judge sees in multi-turn cases](#what-the-judge-sees-in-multi-turn-cases)).
- `retrieval_context` is what the grounding check saw, so faithfulness measures the same thing the grounding check tries to enforce, but with a semantic judge instead of string matching.

### What the judge sees in multi-turn cases

The agent answered with the full conversation, but scoring uses the last turn only:

- **The test case has no history.** `input` is the last question, `actual_output` the last reply, and `retrieval_context` the documentation retrieved for that turn. The earlier turns aren't included.
- **Judge calls are independent.** Every call to the judge is one stateless Converse request with that metric's prompt. Nothing from one judge call is carried into the next.

So for a follow-up like "how do I rotate it?", `answer_relevancy` and `clarification_quality` judge the reply without knowing what "it" is, and can mark it down even when the agent resolved it correctly from the earlier turns. `outcome_match`, the retrieval checks and `correctness` against a reference answer aren't affected this way, so the `multi_turn` cases rely mostly on those.

There are two ways to let the judge see the conversation:

1. **Prefix the earlier turns to `input`** for multi-turn cases (for example "Earlier conversation: … Current question: …"). This is a small change in `plan_metrics()` and keeps the current metrics. This is the simpler option.
2. **Use DeepEval's conversational test cases and metrics** (`ConversationalTestCase`, e.g. `TurnRelevancyMetric`). They need the earlier replies too, which `results.jsonl` already stores for every turn.

### Choosing metrics per case

A metric that doesn't apply to a case is skipped, not scored 0. `plan_metrics()` decides:

| Metric | Kind | Runs when |
|---|---|---|
| `intent_match`, `outcome_match` | Check | The case sets `expected_intent` / `expected_outcome` |
| `retrieval_hit@k`, `retrieval_recall@k`, `retrieval_mrr` | Check | The case sets `expected_sources`. Scores 0 if no search ran |
| `citation_precision` | Check | An `answer` with citations, and `expected_sources` set |
| `first_pass_accepted` | Check | A reply was grounding-checked |
| `faithfulness` | Judge | `answer` with retrieved chunks; or `general` with a "For BC Gov teams" part and retrieved chunks |
| `answer_relevancy` | Judge | `answer` or `general` |
| `contextual_precision`, `contextual_recall` | Judge | A reference answer and retrieved chunks |
| `correctness` | Judge | A reference answer, and `answer` or `general` |
| `bcgov_bias` | Judge | `answer` or `general` for a `general_auth` or `onboarding` case |
| `clarification_quality` | Judge | `clarification` |

Judged metrics are skipped for a turn that errored or has no answer. `k` is the run's `NUMBER_OF_RERANKED_RESULTS` (default 6) unless `--k` is given. Retrieval and citations are compared by page: the fragment, query string, trailing slash and case are ignored, so any chunk of an expected page counts.

### Deterministic checks as DeepEval metrics

The checks in [metrics.py](../../evals/metrics.py) are plain functions with no app or DeepEval imports, so they are easy to test. `CheckMetric` wraps each one as a DeepEval `BaseMetric`. It reads the case and turn from the test case's `metadata`, and sets `score`, `reason` and `success` like any DeepEval metric, so checks and judged metrics produce the same row in `scores.jsonl`.

### How a judged metric works

A DeepEval metric is a fixed sequence of prompts. Each prompt asks the judge for JSON in a schema, and the metric computes the score from the answers. For example, `FaithfulnessMetric`:

1. asks the judge to list the facts in `retrieval_context`;
2. asks it to list the claims in `actual_output`;
3. asks for a verdict per claim: supported, contradicted or not stated;
4. scores the share of claims that aren't contradicted, then asks for a one-paragraph reason.

The others work the same way. Approximate judge calls per metric:

| Metric | Judge calls | Score |
|---|---|---|
| `faithfulness` | about 4 | Share of the answer's claims that the retrieved chunks don't contradict |
| `answer_relevancy` | about 3 | Share of the answer's statements that address the question |
| `contextual_precision` | about 2 | Whether the chunks that matter for the reference answer are ranked first |
| `contextual_recall` | about 2 | Share of the reference answer's sentences that the retrieved chunks support |
| `correctness`, `bcgov_bias`, `clarification_quality` (GEval) | 1 | The judge's 0–10 score on the fixed steps, divided by 10 |

GEval normally spends an extra call writing its evaluation steps from a description. The three GEval metrics here have their steps written out in [score.py](../../evals/score.py), so that call is skipped and the steps don't vary between runs.

Every metric passes at 0.5 (`JUDGE_THRESHOLD` in [config.py](../../evals/config.py)). The judged metrics run concurrently, `--concurrency` at a time (default 4).

### One chunk list, not two

It's tempting to read the pipeline as "the agent retrieves chunks, then the judge also produces chunks, and the two are compared" — that's not what happens. There is exactly **one** set of chunks per turn, retrieved once by the real Knowledge Base search during `collect`, and the judge only ever receives that same list as text in a prompt:

```text
collect (real pipeline, once per turn)                 score (offline, after the fact)
────────────────────────────────────                   ──────────────────────────────
search_knowledge_base() runs the real
KB search + rerank → real chunks
   │
   ▼
invocation_state["citations"]
(uri, title, score, text per chunk)
   │
   ▼
grounding.verify() checks the agent's        ← deterministic string/URL
reply against THESE chunks                     matching, no LLM involved
   │
   ▼
record("attempt", ..., retrieved=[...])      ← chat_service.py (_trace_attempt)
   │
   ▼
results.jsonl: turn["retrieved"] =           ← collect.py (summarize_turn):
  the first attempt's retrieved chunks         the first attempt's chunks, since
   │                                           a repair attempt searches nothing new
   ▼
turn["retrieved"]'s text becomes             ← score.py (plan_metrics)
  LLMTestCase.retrieval_context                 "context = [r.get('text') ... ]"
   │
   ▼
Kimi is given THIS SAME retrieval_context
in every judge prompt that needs it
```

The judge (`BedrockJudge` / Kimi K3) never calls the Knowledge Base, the reranker, or `search_knowledge_base()` itself — it has no way to retrieve anything. It only ever sees chunks that were already retrieved by the real pipeline and copied into the prompt as plain text. So "are the chunks compared?" is true in a narrower sense than it might sound: **the one real chunk list is compared against the answer's claims (`faithfulness`) or against the reference answer (`contextual_precision`/`contextual_recall`)** — never against a second, judge-produced chunk list, because there isn't one.

This is also why the grounding check and `faithfulness` can disagree even though they look at the same evidence: the grounding check is exact string/URL matching on the agent's self-reported `<grounding>` block ([grounding.py](../../app/services/grounding.py)), while `faithfulness` asks an LLM to extract claims from the prose and judge whether the same retrieved text supports their *meaning*. A low `faithfulness` score on a reply the grounding check accepted usually means a plain-language claim the documentation doesn't actually back, not a disagreement about which chunks exist.

**Worked example.** Say a turn retrieved 3 chunks and the agent answered:

```text
retrieval_context (the 3 chunks, unmodified, handed to Kimi):
[1] "...client secrets are found in the Installation JSON under the client's credentials tab..."
[2] "...PROD approval requires the integration to have been tested in TEST first..."
[3] "...Business BCeID is approved per-integration by the IDIM team..."

actual_output (the agent's delivered answer, also handed to Kimi):
"Your client secret is in the Installation JSON. For PROD, you need PROD
approval, which requires TEST sign-off first, and for Business BCeID this is
approved by the Service BC team."
```

`faithfulness`'s claim-by-claim pass: "client secret is in the Installation JSON" → supported by [1]; "PROD approval requires TEST sign-off first" → supported by [2]; "Business BCeID is approved by the **Service BC** team" → [3] says **IDIM team**, not Service BC → contradicted. Score = 2 of 3 claims not contradicted ≈ `0.67`. Kimi never retrieved anything here — it was handed the 3 chunks once and asked to adjudicate each claim against them.

## The judge

DeepEval calls the judge through `a_generate(prompt, schema)`. [`BedrockJudge`](../../evals/judge.py) implements that with the Bedrock Converse API and `boto3`:

```text
DeepEval metric
  └─ a_generate(prompt, schema)            in a worker thread (asyncio.to_thread)
       └─ converse(modelId=global.moonshotai.kimi-k3,
                   system="reply with only the JSON object",
                   inferenceConfig={maxTokens: 8192},
                   additionalModelRequestFields={reasoning_effort: "medium"})
            ├─ keep the text blocks, drop Kimi's reasoning blocks
            ├─ parse the JSON object (code fences and surrounding prose are tolerated)
            └─ validate it against DeepEval's schema; if invalid, ask once more
```

- **Why Kimi K3.** The agent is Grok 4.7 and the rewriter is Nova 2 Lite. A judge from a different model family is less likely to favour the agent's own style.
- **Retries.** The `boto3` client uses adaptive retries (up to 8 attempts) for throttling. An invalid JSON reply is retried once with a reminder. If that also fails, the metric's row records the error and has no score.
- **Token use.** The judge counts its calls, errors and tokens. `scoring.json` and the report show them, so the cost of judging is visible next to the cost of the agent.
- **Configuration.** The `EVAL_JUDGE_*` variables set the model, region, output limit, `reasoning_effort` and temperature (table in [evals/README.md](../../evals/README.md#the-judge)). Set `EVAL_JUDGE_REASONING_EFFORT` to an empty value if a model rejects that field.

The judge is a model too: before trusting its scores, read about 20 of the `reason` fields in `scores.jsonl` and check that you agree.

## Step 3: reports and comparisons

[report.py](../../evals/report.py) turns `results.jsonl` and `scores.jsonl` into `summary.json` and `report.md`:

- each metric's mean, pass rate and error count, overall and by category;
- an outcome table (expected → actual);
- grounding: first-pass acceptance, repairs, and how many repairs succeeded;
- tokens per case for the agent and the rewriter, including cache reads, and the judge's tokens;
- turn latency at p50 and p95;
- the failing cases with their reasons.

What each number in `summary.json` means, with a worked example, is in [evaluation-metrics.md](evaluation-metrics.md).

`python -m evals.report <run> --baseline <other run>` writes `compare-<baseline>.md`. It lists settings that changed between the runs, metric deltas, cost and latency changes, and per-case regressions and fixes. It warns when the two runs used different datasets.

The agent isn't deterministic, so two runs of the same code differ a little. Treat a change of one or two cases as noise unless it repeats.

## Drafting cases with the Synthesizer

`python -m evals.synthesize` uses DeepEval's `Synthesizer`, with the same judge, to draft cases from the documentation:

1. It reads the chunk files that `chunker/preprocess.py` writes to `chunker/chunks/`, skipping release notes and category pages, and picks chunks from different pages in turn.
2. For each chunk, the Synthesizer writes a question, rewrites it once to be more concrete or broader, filters out low-quality questions with the judge, and writes a reference answer from the chunk.
3. Each result is saved to `evals/datasets/drafts.jsonl` with `"draft": true`, an expected outcome of `answer`, and the chunk's page as the expected source.

Drafts are a starting point. A person reviews each one and moves it into `goldens.jsonl` (steps in [evals/README.md](../../evals/README.md#reviewing-cases)). Until then, `--exclude-drafts` leaves them out of a run.

## Where data goes

| Data | Sent to |
|---|---|
| Questions, retrieved documentation, agent answers | Bedrock (agent, rewriter, reranker), as in production |
| The same, in judge prompts | Bedrock (Kimi K3 through the `global.` inference profile, which may serve requests outside Canada) |
| Run files (`results.jsonl`, `scores.jsonl`, reports) | `evals/runs/` on your machine, git-ignored |
| Anything else | Nothing. DeepEval telemetry is off, `.env` loading by DeepEval is off, hosted "System One" judging is off, and nothing logs in to Confident AI |

## Limits

- **Last turn only.** Earlier turns of a multi-turn case aren't scored, and the judge doesn't see them ([details](#what-the-judge-sees-in-multi-turn-cases)).
- **Page-level retrieval matching.** A hit on the right page but the wrong section still counts. `contextual_precision` and `contextual_recall` are the finer measure.
- **Citation precision is informational.** A page that isn't in `expected_sources` can still be a correct citation.
- **Context from the first reply.** `retrieval_context` is what the first reply was checked against. If a repair attempt searched again, those extra chunks aren't included.
- **Drafts.** Every case in `goldens.jsonl` is currently `"draft": true`, so scores are provisional until the cases are reviewed.

## Extending

| To add | Do this |
|---|---|
| A test case | Add a line to [goldens.jsonl](../../evals/datasets/goldens.jsonl). `tests/test_evals_dataset.py` validates the file |
| A deterministic check | Write a function `(case, turn, k)` that returns `(score, reason)`, or `None` when it doesn't apply, in [metrics.py](../../evals/metrics.py) and add it to `CHECKS` with its threshold |
| A judged metric | Add it in `plan_metrics()` in [score.py](../../evals/score.py) with the condition it applies under. Prefer GEval with explicit `evaluation_steps` |
| A new value from the pipeline | Call `record("<event>", ...)` where the value is known, and read it in `summarize_turn()` in [collect.py](../../evals/collect.py) |
| A different judge | Set `EVAL_JUDGE_MODEL_ID` to any Bedrock model or inference profile that supports Converse |
