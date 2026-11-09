# Evaluations

Offline evaluation of the SSO assistant with [DeepEval](https://deepeval.com). Every case runs through the real pipeline (query rewriter, Knowledge Base search, rerank, the agent and the grounding check with its repair attempt) against Bedrock. The run is then scored with deterministic checks and LLM-judged DeepEval metrics, and summarised in a markdown report.

The dataset in `datasets/goldens.jsonl` is marked `"draft": true` throughout. Its expectations have not been reviewed yet, so treat scores from it as provisional (see [Reviewing cases](#reviewing-cases)).

## Running

You need AWS credentials for the sandbox account, the app's `.env` (`KNOWLEDGE_BASE_ID`, `MODEL_ARN`, `RERANKER_MODEL_ARN`, …), and access to the judge model.

```bash
uv sync --group eval

# 1. Run the cases through the pipeline: evals/runs/<run id>/results.jsonl
uv run --group eval python -m evals.collect --label my-branch

# 2. Score them and write report.md (add --deterministic-only for no judge calls)
uv run --group eval python -m evals.score evals/runs/<run id>

# 3. Compare with an earlier run, e.g. one from main
uv run --group eval python -m evals.report evals/runs/<run id> --baseline evals/runs/<main run id>
```

Useful options:

| Command | Option | Effect |
|---|---|---|
| `collect` | `--ids a,b`, `--categories x,y`, `--limit N` | Run a subset |
| `collect` | `--exclude-drafts` | Only reviewed cases |
| `collect` | `--concurrency N` (default 2) | Cases at once; each makes several Bedrock calls |
| `score` | `--deterministic-only` | Checks only, no judge |
| `score` | `--metrics faithfulness,correctness` | Only these metrics |
| `score` | `--concurrency N` (default 4) | Judge metrics at once |

Scoring reads only the saved results, so you can change a metric and re-score without calling the agent again.

### What `collect` does to your environment

It sets `ENVIRONMENT=local`, `DEBUG_TRACE=false` and `AGENT_STORE_DIR=<temporary directory>` before importing the app. Conversations go to that directory, which is deleted at the end. The run refuses to start if the agent storage is anything else, so it never writes to the S3 conversation bucket. Every case starts a new conversation.

## Run output

`evals/runs/<YYYYMMDD-HHMMSS>-<commit>-<label>/` is git-ignored and contains:

| File | Contents |
|---|---|
| `run.json` | Git commit and branch (and whether there were uncommitted changes), dataset hash, models and retrieval/rewriter settings |
| `results.jsonl` | Per case, every turn's delivered answer, outcome, grounding status and citations. Also the rewriter output, search queries and ranked hits, the retrieved chunks, each reply attempt with its grounding verdict, tokens and latency |
| `scores.jsonl` | One row per (case, metric): score, pass/fail, reason, error |
| `scoring.json`, `summary.json` | Judge token use and the aggregates |
| `report.md` | The report: metrics, categories, outcomes, grounding, cost, latency, failures |

`results.jsonl` contains documentation text and the model's answers, so keep runs out of shared places that shouldn't hold them.

## Metrics

All metrics are scored on the **last turn** of each case. A metric that doesn't apply to a case is skipped, not scored 0.

**Checks.** These are deterministic ([metrics.py](metrics.py)) and make no model calls.

| Metric | Passes when | Applies when |
|---|---|---|
| `outcome_match` | The user got the expected outcome: `answer`, `general`, `clarification`, `conversational` or `not_supported` (the no-documentation reply) | `expected_outcome` set |
| `intent_match` | The rewriter's intent matches | `expected_intent` set |
| `retrieval_hit@k` | An expected page is in the top k reranked pages (k = `NUMBER_OF_RERANKED_RESULTS`) | `expected_sources` set; scores 0 if no search ran |
| `retrieval_recall@k` | At least half the expected pages are in the top k | ″ |
| `retrieval_mrr` | 1 / rank of the first expected page is at least 0.5 | ″ |
| `citation_precision` | At least half the cited pages are expected sources (informational: another page can also be right) | An answer with citations and `expected_sources` |
| `first_pass_accepted` | The grounding check accepted the first reply, so no repair was needed | A reply was checked |

Pages are matched by page, so `https://…/FAQs/css-app` matches every chunk of that page. The fragment, query string, trailing slash and case are ignored.

**Judged.** These use the LLM judge ([judge.py](judge.py)). Each passes at a score of 0.5 or more.

| Metric | DeepEval metric | Applies when |
|---|---|---|
| `faithfulness` | `FaithfulnessMetric` against the retrieved chunks | `answer` outcome. For a `general` answer, only its "For BC Gov teams" part is checked; the general explanation comes from the model's own knowledge |
| `answer_relevancy` | `AnswerRelevancyMetric` | `answer` or `general` |
| `contextual_precision`, `contextual_recall` | The same-named metrics, using the reference answer | `expected_output` set and something retrieved |
| `correctness` | GEval against the reference answer, `key_facts` and `must_not` | `expected_output` set, `answer` or `general` |
| `bcgov_bias` | GEval: recommends CSS for BC Gov services and doesn't push third-party identity providers | `general_auth` and `onboarding` cases that were answered |
| `clarification_quality` | GEval: one to three specific questions, no invented facts, no requests for secrets | The outcome is a clarifying question |

The GEval metrics use fixed evaluation steps, so DeepEval doesn't spend a judge call writing them.

## The judge

The default judge is **Kimi K3** (`global.moonshotai.kimi-k3`). It comes from a different model family from the agent (Grok) and the rewriter (Nova), so it doesn't favour its own model's style. It is called through the Bedrock Converse API with boto3. DeepEval's own `AmazonBedrockModel` needs `aiobotocore`, which pins `botocore` versions; this avoids that dependency. Only the text blocks of Kimi's reply are used, and an invalid JSON reply is retried once.

| Variable | Default | Purpose |
|---|---|---|
| `EVAL_JUDGE_MODEL_ID` | `global.moonshotai.kimi-k3` | Model ID, inference profile ID or ARN |
| `EVAL_JUDGE_REGION` | `AWS_REGION`, else `ca-central-1` | Bedrock region |
| `EVAL_JUDGE_MAX_TOKENS` | `8192` | Output limit. Kimi's reasoning tokens count towards it |
| `EVAL_JUDGE_REASONING_EFFORT` | `medium` | Sent as `reasoning_effort`; set it empty to send nothing |
| `EVAL_JUDGE_TEMPERATURE` | unset | Model default |

`global.` inference profiles may serve requests outside Canada. Judge prompts contain the questions, answers and documentation text.

Before trusting the judged scores, check about 20 of them by hand against the case and the judge's `reason` in `scores.jsonl`.

### DeepEval settings

[`__init__.py`](__init__.py) turns off DeepEval's telemetry (`DEEPEVAL_TELEMETRY_OPT_OUT`) and stops it loading `.env`. It also forces LLM-only judging, so no hosted "System One" model is called. Nothing logs in to Confident AI (don't run `deepeval login`), so results stay on this machine.

## The dataset

`datasets/goldens.jsonl` has one case per line (schema in [dataset.py](dataset.py)):

```json
{"id": "rotate-client-secret", "category": "sso_question",
 "turns": ["how do I rotate the client secret for my integration?"],
 "expected_intent": "sso_question", "expected_outcome": ["answer"],
 "expected_sources": ["https://bcgov.github.io/sso-docs/FAQs/css-app"],
 "expected_output": "For a confidential client: open the integration in the CSS app, ...",
 "key_facts": ["regenerate it under Secrets in the CSS app"],
 "must_not": ["includes a URL that is not in the BC Gov SSO documentation"],
 "notes": "...", "draft": true}
```

| Field | Meaning |
|---|---|
| `turns` | The user's messages in one conversation. Earlier turns run first, so the history holds the system's own replies |
| `expected_outcome` | One acceptable outcome or a list, e.g. `["answer", "clarification"]` for a borderline question |
| `expected_sources` | Documentation pages that answer the question |
| `expected_output` | Reference answer. Without it, correctness and the contextual metrics are skipped |
| `key_facts`, `must_not` | Added to the reference for the correctness judge. `must_not` also goes to the BC Gov bias judge |

The seed set has 40 hand-written cases in nine categories:
- `sso_question` and `onboarding`;
- `general_auth`;
- `clarification` (vague questions);
- `not_supported`;
- `conversational` and `off_topic`;
- `multi_turn` (follow-ups);
- `adversarial` ("don't search", prompt injection, a pasted secret).

Reference answers come from the FAQ pages. Cases with only a `notes` hint still need one.

### Drafting more cases

```bash
uv run --group eval python -m evals.synthesize --limit 30 --per-page 1
```

This uses DeepEval's `Synthesizer` with the judge to write one question and a reference answer per chunk. It reads the local `chunker/chunks/` files, which are git-ignored and produced by `chunker/preprocess.py`, and skips release notes and category pages. The output is `datasets/drafts.jsonl`. Every draft is `"draft": true`, expects an `answer`, and lists its chunk's page as the expected source.

### Reviewing cases

For each draft:
1. Check that the question is something a team would really ask, and reword it if not.
2. Check the reference answer against the documentation page, and trim it to what a good answer must say.
3. Fix `expected_sources` (add other pages that also answer it) and `expected_outcome`.
4. Add `key_facts` and `must_not` where they make the judge's job clear.
5. Set `"draft": false`, and move synthesized drafts into `goldens.jsonl`.

Run with `--exclude-drafts` to score only reviewed cases.

## Tests

The tests run with the rest of the suite and make no Bedrock calls. Those that need DeepEval are skipped unless the `eval` group is installed:

```bash
uv run --group eval pytest tests/test_evals_*.py tests/test_turn_report.py
```

## How it hooks into the app

[`app/common/turn_report.py`](../app/common/turn_report.py) holds an optional per-turn `TurnReport` in a `ContextVar`. Several places call `record()`:
- `ChatService`: each model call's usage, and each reply attempt with its verdict and retrieved chunks;
- the search tool: queries and ranked hits;
- the query rewriter: its output, usage and latency. Its thread pool runs jobs in a copy of the caller's context, so the record reaches the right report.

With no report attached, which is always the case in the deployed service, `record()` does nothing. The only other app change is the `AGENT_STORE_DIR` setting.

## Miscellaneous

- `collect` sends each case through ChatService.stream, the same path /chat uses, in a new conversation. Conversations are saved to a temporary folder that's deleted afterwards, and the run refuses to start if storage would be S3.
- `score` reads only the saved results, so you can change metrics and re-score without paying for the agent again. --deterministic-only skips the judge and costs nothing.
- `Free checks` (no model calls): intent and outcome match, retrieval hit@k, recall@k and MRR (how high the first correct page ranks), citation precision, and whether the grounding check accepted the first reply.
- `Judged by Kimi`, only where they apply: faithfulness, answer relevancy, contextual precision and recall, correctness, BC Gov bias and clarification quality. For general answers, faithfulness checks only the "For BC Gov teams" part.
- `Judge class`: I wrote my own in evals/judge.py, on your existing boto3. DeepEval's built-in Bedrock class needs aiobotocore, which pins botocore and would clash with your boto3 version.
- `Data stays local`: DeepEval telemetry is off, it doesn't load .env, and nothing logs in to Confident AI