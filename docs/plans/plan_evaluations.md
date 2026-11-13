# Plan: Evaluations Module

> **Superseded.** Evaluations were built with DeepEval instead of `strands-agents-evals`. See [evals/README.md](../../evals/README.md). Kept from this plan:
>
> - in-process runs through `ChatService.stream`;
> - isolated temporary storage, with the guard against S3 (`AGENT_STORE_DIR`);
> - page-level URL matching;
> - judging the delivered answer;
> - re-scoring without re-running the agent.
>
> The observer is a `ContextVar` `TurnReport` rather than a `stream(..., observer=)` parameter. The rest of this document is the original proposal, not the implementation.

Status: superseded · Scope: answer, retrieval and grounding quality · Library: `strands-agents-evals` 1.8.0 (`strands_evals`)

## 1. Summary

Add an `evals/` package that runs the real pipeline in-process, case by case, and scores it. It covers the query rewriter, `search_knowledge_base`, retrieval and rerank, the generation model and `grounding.verify`. Each run writes a JSON results file and a self-contained HTML report.

- Each case calls `ChatService.stream(text, conversation_id)` directly, not over HTTP. Rewriting, retrieval, rerank, the repair attempt and grounding verification all run as in production.
- Storage is a temp-dir `LocalFileStorage`. The run refuses to start if the agent storage is anything else, so it never writes to the S3 agent-context bucket.
- In-memory OpenTelemetry spans are mapped to a strands-evals `Session`, so trace- and tool-level evaluators work.
- Built-in LLM-judge evaluators score answer quality. Custom evaluators cover the architecture-specific parts: retrieval, grounding and citations, abstention and query rewriting. Deterministic checks are preferred wherever possible.
- Agent outputs are cached, so new evaluators can re-score old outputs without re-running the agent.

The app needs a few small hooks, all optional and off by default (section 4). Production behaviour doesn't change.

## 2. What the code does (verified)

Checked against `app/` at `cc88552`.

| Piece | Where | Notes |
|---|---|---|
| Shared entry point | [`ChatService.stream`](../../app/services/chat_service.py) | Yields `Status`/`Heartbeat`, then exactly one `Answer(text, grounding_status, citations)` |
| Agent factory | [`create_agent`](../../app/agents/rag_agent.py) | Fresh `Agent` per call. `SnapshotSessionManager(session_id, storage=storage)`. `storage` is a module global chosen at import: `LocalFileStorage("./.agent-store/")` when `ENVIRONMENT=local`, else `S3Storage` |
| Session helpers | `rag_agent.py` | `new_session_id()` → `session_<uuid4>`; `delete_session`, `save_session`, `replace_last_answer`, `drop_repair_exchange` all use the module-level `storage` |
| Thinking filter | `ThinkingFilter` in `chat_service.py` | Strips `<thinking>` before the reply is buffered |
| Query rewriter | [`query_rewriter.py`](../../app/services/query_rewriter.py) | `submit(prompt, history)` → `Future[RewrittenQuery]`. Three parallel Llama 3 8B `Converse` calls (rewrite+intent, step-back, decomposition), with a `retry()` for rewording |
| Rewriter result handoff | `SearchTurn` in `invocation_state["search_turn"]` | Also gets `recent_history(agent.messages, QUERY_REWRITER_HISTORY_TURNS)` |
| Search tool | [`knowledge_base_tool.py`](../../app/tools/knowledge_base_tool.py) | No arguments. Appends `{title, uri, score, text}` to `invocation_state["citations"]` |
| Retrieval | [`multi_search`](../../app/retrieval/knowledge_base.py) | Acronym expansion, parallel `Retrieve`, dedupe, Bedrock `Rerank`, `MIN_RERANK_SCORE` cut. Returns `(hits, stats)` |
| Verification | [`grounding.verify`](../../app/services/grounding.py) | Returns `Verdict(text, status, citations, reasons)`. `replaced` means the text is `NO_DOCUMENTATION_RESPONSE` |

### Differences from the original brief

1. **There is a grounding repair attempt.** When `verify` rejects an answer, retrieval returned results and `GROUNDING_REPAIR_ENABLED` is set (the default), `ChatService` sends the reasons back to the agent once and verifies again. Answers truncated at the output-token limit also go through this path. The evals capture both attempts.
2. **The reworded retry is per query.** It fires for each `rewrite` or `sub` query with no surviving hit, not only when the whole search is empty. Missing `step_back` queries don't trigger it.
3. **Only regex-matched greetings skip the rewriter's model calls.** Other small talk goes to Llama and may come back `conversational`. If the agent still calls the tool, it gets `NO_SEARCH_NEEDED`. A second call in the same turn gets `ALREADY_SEARCHED`.
4. **`verify` has more cases than the brief lists.** Conversational replies without a grounding block are delivered if nothing was searched and they contain no checkable values. Truncated replies are replaced.
5. **Citation entries carry no separate `section`.** Entries in `invocation_state["citations"]` use `title` = section or page title, and also carry `text`. Per-hit `section`, `found_by`, missing queries, retry details and the candidate count exist only inside `_search` and its log line.
6. **The built-in judges read the raw model text.** Every built-in judge except `ToolSelectionAccuracyEvaluator` is trace-level. They read the agent response from the OTel trace: the model's text *before* `verify`, with the grounding block, not replaced, and with any repair run as an extra trace. See section 6.4.
7. **`ToolSelectionAccuracyEvaluator` is tool-level.** It produces one row per tool call. A case with no tool call gets score 0 with "No evaluation outputs produced", so it can't confirm that a greeting *didn't* search.
8. **Strands doesn't put a conversation id on spans.** The evals attach `session.id` as OTel baggage, the same mechanism the strands-evals CLI uses. `StrandsEvalsTelemetry.setup_in_memory_exporter()` registers a span processor that copies baggage onto every span.
9. **`ENVIRONMENT=local` also turns on the verbose `trace()` logs.** The evals set `DEBUG_TRACE=false`.

## 3. strands-agents-evals API used (1.8.0, read from source)

| API | Signature / behaviour |
|---|---|
| `Case[InputT, OutputT]` | `name`, `session_id`, `input`, `expected_output`, `expected_assertion`, `expected_trajectory`, `metadata` |
| `Experiment(cases, evaluators, *, report_cls=None)` | `await run_evaluations_async(task, max_workers=10, evaluation_data_store=None)` returns one flattened `EvaluationReport`. Each row in `cases` carries `evaluator` |
| Task return | `{"output", "trajectory": Session \| list, "interactions", "environment_state": [EnvironmentState(name, state)], "input"}` |
| Retries | Task and evaluators are each retried on throttling (`is_throttling_error`): 6 attempts, exponential 4s → 240s. Other errors are isolated per case and recorded as failed rows with the reason |
| `Evaluator` | Override `evaluate` / `evaluate_async(EvaluationData) -> list[EvaluationOutput]`. `name=` sets the instance name. `aggregator` defaults to the mean. `_aggregate_dropping_na` ignores N/A rows |
| `EvaluationOutput` | `score`, `test_pass`, `reason`, `label`, `metadata`. `label == NOT_APPLICABLE` marks "nothing to judge" (excluded from means) |
| `EvaluationDataStore` | Protocol with `load(case_name)` / `save(case_name, EvaluationData)`. `LocalFileTaskResultStore(dir)` is the file implementation. Case names must be unique |
| `StrandsEvalsTelemetry().setup_in_memory_exporter()` | Sets the global tracer provider. `.in_memory_exporter.get_finished_spans()` |
| `StrandsInMemorySessionMapper().map_to_session(spans, session_id)` | Filters spans by `session.id` / `gen_ai.conversation.id` |
| Built-in judges | `model: Model \| str \| None` (default `global.anthropic.claude-sonnet-4-6`). Most also take `version`, `system_prompt`, `include_inputs` and `name` |
| Span usage | Strands model-invoke spans carry `gen_ai.request.model` and `gen_ai.usage.input_tokens` / `output_tokens`. Agent spans repeat the totals, so only model-invoke spans are summed |

## 4. App changes (optional, off by default)

| File | Change | Why |
|---|---|---|
| [`config.py`](../../app/common/config.py), [`rag_agent.py`](../../app/agents/rag_agent.py), README env table | New `AGENT_STORE_DIR` setting (default `./.agent-store/`) used for `LocalFileStorage` | Points eval storage at a temp dir without monkeypatching the module global. The default keeps current behaviour. Terraform doesn't need it (non-local environments use S3) |
| New `app/services/pipeline_observer.py` | `PipelineObserver` base class with no-op `on_rewrite(prompt, rewrite)`, `on_search(record)`, `on_attempt(attempt, raw_reply, truncated, verdict)`. `OBSERVER_KEY` constant | One explicit, typed seam for intermediate values |
| [`chat_service.py`](../../app/services/chat_service.py) | `stream(..., *, observer: PipelineObserver \| None = None)`. Adds the observer to `invocation_state` and calls `on_rewrite` (when the future resolved) and `on_attempt` (first and repair) | Raw reply, verdict and repair details only exist here |
| [`knowledge_base_tool.py`](../../app/tools/knowledge_base_tool.py) | If an observer is in `invocation_state`, `_search` reports the record it already logs. It adds per-hit `source_url`, `section`, `page_title`, rerank `score` and `found_by`, the retry queries, and `elapsed_ms` for the search and the retry | Missing queries, retry, candidates and per-hit sections only exist inside `_search` |
| [`query_rewriter.py`](../../app/services/query_rewriter.py) | `RewrittenQuery.diagnostics: dict` (per-technique `latency_ms` and `status`), `field(default_factory=dict, compare=False)` | Already computed for the `query_rewrite` log line. Lets the evals flag partial rewriter failures |

With `observer=None` the code paths are unchanged. Existing tests must keep passing. New tests cover an observer receiving the events and the default path being unaffected.

**Alternative considered:** a Strands `HookProvider` (`AfterToolCallEvent`). It can read the `SearchTurn` and the citations list, but not missing queries, the retry, the candidate count, the rerank flag, per-hit sections or timings. It would also need dependency injection to register on the per-request agent. Monkeypatching was ruled out.

## 5. Module layout

`evals/` is a namespace package at the repo root, like `app/` (no `__init__.py`). Tests live under `tests/evals/` to match `testpaths`.

```text
evals/
  README.md                 adding queries, adding evaluators, reading the report
  config.toml               defaults (judge, evaluator set, concurrency, k, URL matching, prices, paths)
  config.py                 EvalConfig (pydantic; stdlib tomllib) + CLI overrides
  dataset.py                JSONL → validated EvalRecord → strands_evals Case (name = record id)
  capture.py                EvalObserver (PipelineObserver) + TurnCapture / CaseCapture models
  task.py                   isolated storage, baggage, per-turn ChatService.stream, Session mapping
  telemetry.py              in-memory OTel, token usage from spans, botocore usage hooks
  cache.py                  EvaluationDataStore keyed by a system fingerprint
  metrics.py                pure deterministic metric functions
  evaluators/
    base.py                 Gated wrapper (NOT_APPLICABLE + reason), registry, judge factory
    tool_use.py             ExpectedToolUse, gated ToolSelectionAccuracy
    retrieval.py            HitRate@k, Recall@k, MRR, ContextPrecision
    grounding.py            CitationValidity, HasCitation, VerifyAccepted
    abstention.py           Abstention
    rewriting.py            RewriteFaithfulness (judge), RewriterHealth
  run.py                    CLI: python -m evals.run
  report.py                 CLI: python -m evals.report (aggregates + HTML rendering)
  data/queries.jsonl        seed dataset (see 5.1)
tests/evals/
  fixtures/results_small.json
  test_dataset.py  test_metrics.py  test_evaluators.py  test_report.py
```

No new runtime dependencies. `strands-agents-evals>=1.8.0` is already in `pyproject.toml` and `uv.lock`, and it brings `pyyaml`, `rich` and `tenacity`. Chart.js loads from a CDN inside the report.

`.gitignore` gets `evals/results/`, `evals/.cache/` and `reports/`.

### 5.1 Seed dataset

`evals/data/queries.jsonl` starts with about 20 records **marked as seed data**. The first line of the README says so, and each record has `"seed": true`. They're derived from `chunker/chunks/*.metadata.json` (`question`, `source_url`, chunk text). The categories:

- integration how-to
- CSS app
- Keycloak config
- troubleshooting
- acronyms
- out-of-scope
- greetings
- multi-turn follow-ups

Schema (one JSON object per line):

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | str | yes | Unique. Used as the case name and cache key |
| `query` | str | yes | First user turn |
| `category` | str | yes | Free text, filterable with `--categories` |
| `answerable` | bool | yes | `false` means the no-documentation reply is the correct answer |
| `expected_answer` | str | no | Enables `CorrectnessEvaluator` with a reference |
| `expected_source_urls` | list[str] | no | Enables the retrieval metrics |
| `follow_ups` | list[str \| object] | no | Later turns in the same conversation. An object form `{query, answerable?, expected_answer?, expected_source_urls?}` gives the turn its own expectations |
| `expect_search` | bool | no | Overrides the category rule (`conversational_categories` in config → `false`) |
| `seed` | bool | no | Provenance marker |

Unknown fields are kept in `metadata`. The loader validates required fields and unique ids, and reports every problem with its line number. Evaluators score the **last turn** by default. Per-turn captures are kept and shown in the report.

## 6. Task function

### 6.1 Isolation and setup (once per run)

1. Before any `app` import, set:
   - `ENVIRONMENT=local`
   - `DEBUG_TRACE=false`
   - `AGENT_STORE_DIR=<mkdtemp>`
   - `AWS_RETRY_MODE=adaptive`
   - `AWS_MAX_ATTEMPTS` (configurable)
2. Create `StrandsEvalsTelemetry().setup_in_memory_exporter()` *before* the agent module creates its tracers.
3. Import `app` and **assert** that `rag_agent.storage` is a `LocalFileStorage` rooted in the temp dir. Abort otherwise. This is the guard against ever writing to S3.
4. Register botocore `after-call` handlers on the app's cached Bedrock clients. They give run-level token and call counts for rewriter `Converse`, `Rerank` and `Retrieve`. These are documented botocore event hooks, not patches.

### 6.2 Per case

```text
conversation_id = new_session_id()                    # session_<uuid4>; new per attempt
attach baggage session.id = conversation_id
for turn in [query, *follow_ups]:
    obs = EvalObserver()
    async for item in chat_service.stream(turn.text, conversation_id, observer=obs):
        Status("searching") → t_search; Status("writing") → t_first_token; Answer → final
    TurnCapture(...)
session = StrandsInMemorySessionMapper().map_to_session(spans, conversation_id)
session = normalize(session, captures)                # see 6.4
finally: delete_session(conversation_id); detach baggage
return {"output": last_turn.final_text,
        "trajectory": session,
        "environment_state": [EnvironmentState(name="capture", state=CaseCapture.model_dump())]}
```

The temp dir is removed at the end of the run. A throttled case retried by `Experiment` starts a fresh conversation id, so a half-finished follow-up chain is never reused.

### 6.3 Captured per turn

| Group | Values |
|---|---|
| Answer | Delivered text, `grounding_status`, delivered citations (URLs), `abstained` (text == `NO_DOCUMENTATION_RESPONSE`) |
| Verification | Raw reply per attempt (thinking stripped, grounding block kept), the parsed grounding block (status, citations, response type), verdict reasons, `replaced`, `repair_attempted`, `repaired`, `truncated` |
| Rewriter | Intent, `rewritten_query`, `step_back_query`, `sub_queries`, `fallback`, `short_circuit`, per-technique status and latency, history-aware flag (turn > 1) |
| Retrieval | Rerank question, queries with origins, acronym expansions, candidate count, `reranked` flag, hits (`source_url`, `section`, `page_title`, score, `found_by`), missing queries, `retry_fired`, retry queries and misses, `waited_ms`, search and retry `elapsed_ms` |
| Tools | Tool calls from `ToolExecutionSpan` (name, result prefix such as `NO_MATCH`, `NO_SEARCH_NEEDED` or `ALREADY_SEARCHED`, error) |
| Timing | Total, time to `searching`, time to first visible model token (`writing`), time to `Answer` (when the user sees text, since answers are buffered), rewriter wait, retrieval. Generation = total − rewriter wait − retrieval |
| Tokens | Per model from model-invoke spans for this session (generation). Rewriter call count per turn (tokens at run level; see 7.3) |
| Errors | Exception type and message, and a tool-level Bedrock error flag. Infrastructure errors are reported separately from quality failures |

### 6.4 What the judges see

By default (`judge_sees = "delivered"`), the mapped `Session` is normalised so the trace-level judges score **what the user received**:

- Each turn's `AgentInvocationSpan.agent_response` is set to the delivered text.
- The repair-run trace (its user prompt is the repair feedback) is dropped.
- Tool results, which are the retrieved blocks the judges ground against, are left untouched.

The raw text stays in the capture and is shown in the report. Set `judge_sees = "raw"` to judge the model's own text instead.

## 7. Evaluators

Judge: `BedrockModel(model_id=<judge>, region_name=<judge_region>, temperature=0)`. The default `judge_model_id` is `arn:aws:bedrock:ca-central-1:820251787117:inference-profile/global.xai.grok-4.7`. It's set in `config.toml` and can be overridden with `--judge-model`. It is a different and stronger model family than Nova Lite. As a `global.` inference profile, requests may be served outside Canada.

`Gated(evaluator, skip_if)` wraps any evaluator and returns one `NOT_APPLICABLE` row whose `reason` explains the skip. Those rows are left out of the means and counted as "skipped" in the report.

### 7.1 Built-in

| Evaluator | Level | Skipped when |
|---|---|---|
| `FaithfulnessEvaluator` | trace | No retrieval results, or the answer is the no-documentation reply |
| `CorrectnessEvaluator` | trace | No `expected_answer` for the scored turn |
| `ResponseRelevanceEvaluator`, `HelpfulnessEvaluator`, `CoherenceEvaluator`, `ConcisenessEvaluator` | trace | — |
| `ToolSelectionAccuracyEvaluator` | tool | No tool call (N/A rather than a misleading 0; `ExpectedToolUse` covers it) |
| `ToolParameterAccuracyEvaluator` | — | **Not run**: `search_knowledge_base` takes no arguments. The report says so |
| `RefusalEvaluator`, `HarmfulnessEvaluator` | trace | Only with `--safety`. Refusal will flag the no-documentation reply by design; read it next to abstention |

### 7.2 Custom

| Evaluator | Kind | Pass rule | Skipped when |
|---|---|---|---|
| `ExpectedToolUse` | deterministic | `search_knowledge_base` called if `expect_search` (category rule: greetings must not search) | — |
| `RetrievalHitRate@k` | deterministic | ≥1 expected URL in the top-k reranked hits | No `expected_source_urls` |
| `RetrievalRecall@k` | deterministic | Share of expected URLs in the top-k ≥ threshold | ″ |
| `RetrievalMRR` | deterministic | 1/rank of the first relevant hit ≥ threshold | ″ |
| `ContextPrecision@k` | deterministic | Rank-weighted precision (RAGAS-style, URL match as the relevance label) ≥ threshold | ″ |
| `CitationValidity` | deterministic | The model's **raw** grounding-block citations ⊆ retrieved URLs | No grounding block. (Delivered citations are always among the retrieved ones because `verify` rejects anything else, so the raw block is the one worth checking) |
| `HasCitation` | deterministic | Answered SSO question has ≥1 delivered citation | Not an answer, conversational, or abstained |
| `VerifyAccepted` | deterministic | First attempt not rejected. Metadata includes reasons and whether the repair fixed it | — |
| `Abstention` | deterministic | Abstained iff `answerable == false`. Label TP / FP / TN / FN (positive = should abstain) | `answerable` missing |
| `RewriteFaithfulness` | custom LLM judge | The rewritten query (plus step-back and sub-queries) keeps the user's intent and adds no facts. For follow-ups it must also be standalone. Structured output `{reasoning, faithful: Yes\|Partial\|No, standalone: Yes\|No\|NA}` | Greeting short-circuit, or conversational intent |
| `RewriterHealth` | deterministic | No fallback, every technique `ok`, and intent matches `expect_search` | — |

URL matching for the retrieval metrics defaults to **page level**, normalised like `grounding._url_key` (fragment, trailing slash and case ignored). Chunk `source_url`s carry `#anchors`, so `url_match = "anchor"` gives stricter matching. `k` defaults to the configured number of reranked results (6). Thresholds live in `config.toml`.

Not evaluators, computed from the captures in the report:

- Grounding status distribution
- Verify rejection and repair rates
- Rerank score stats per case (min, mean, max, top-1, gap to `MIN_RERANK_SCORE`)
- Retry rate, fallback rate and latency

### 7.3 Tokens and cost

- **Generation:** per case, from model-invoke spans.
- **Judge:** per run and per evaluator, from spans under each `evaluator <name>` span.
- **Rewriter, rerank, retrieve:** run-level token and call totals from the botocore hooks. Per-case rewriter cost is estimated as the case's call count × the run's mean tokens per call. Exact per-case attribution would need a further change to `_converse`.
- **Prices:** a `[prices]` table in `config.toml` per model id (input/output per 1K tokens, rerank per 1K queries, retrieve per 1K queries). Defaults are taken from the AWS pricing pages and marked "verify". All costs in the report are labelled *estimated*.

## 8. Running

```bash
# Every flag has a default in evals/config.toml.
# --evaluators: default | all | comma-separated names
uv run python -m evals.run \
  --queries evals/data/queries.jsonl \
  --evaluators default \
  --categories "CSS app,greetings" \
  --ids q01,q07 --limit 20 \
  --concurrency 4 \
  --output-dir evals/results \
  --judge-model <id or ARN> \
  --config evals/config.toml \
  [--safety] [--reuse-outputs evals/results/<run_id>] [--no-cache]

uv run python -m evals.report --results evals/results/<run_id>/results.json \
  [--baseline evals/results/<other>/results.json] [--out reports/eval_<run_id>.html]
```

- The run ID is `YYYYMMDD-HHMMSS-<short sha>`. `run` writes `evals/results/<run_id>/results.json` and then renders `reports/eval_<run_id>.html`.
- Concurrency maps to `run_evaluations_async(max_workers=N)`. Throttling back-off is described in section 3, plus adaptive botocore retries. A failing case never aborts the run.
- **Caching:** `evals/.cache/<fingerprint>/<case_id>.json`, implemented as an `EvaluationDataStore`. The fingerprint hashes:
  - generation, rewriter and reranker model ids, and the KB id;
  - retrieval, rerank, rewriter and repair settings;
  - the contents of `app/**/*.py` (prompts included);
  - the record itself.

  Any code or config change gives a cache miss. `--reuse-outputs` re-scores a previous run's outputs with the current evaluators without calling the agent. `--no-cache` forces a fresh run.
- Logs go through `logging`. A `rich` progress line shows cases done, errors and elapsed time.

### 8.1 Results JSON

```text
{
  "run": {run_id, started_at, finished_at, git_commit, git_dirty, dataset: {path, sha256, n_records},
          models: {generation, rewriter, reranker, judge}, region, judge_region, kb_id,
          retrieval: {number_of_results, number_of_reranked_results, min_rerank_score, search_type},
          rewriter: {history_turns, step_back, decomposition, retry, timeout_s},
          grounding_repair_enabled, judge_sees, url_match, k, thresholds,
          evaluators: [Evaluator.to_dict()...], skipped_evaluators: [{name, reason}],
          concurrency, cache: {fingerprint, hits, misses}},
  "cases":   [{id, category, query, follow_ups, answerable, expected_answer, expected_source_urls,
               capture: CaseCapture, error,
               evaluations: [{evaluator, score, test_pass, label, reason, skipped, detailed: [...]}]}],
  "usage":   {by_model: {model_id: {input_tokens, output_tokens, calls, est_cost_usd}}, judge: {...}},
  "summary": {...precomputed aggregates used by the report...}
}
```

## 9. HTML report

One file at `reports/eval_<run_id>.html` with inline CSS and JS and Chart.js from a CDN. `evals.report` can regenerate it from the saved JSON. Python computes the aggregates, which keeps them unit-testable. The per-case data is embedded as `<script type="application/json">`, and inline JS renders the table, filters, expanders and charts. Charts appear only when there's data for them.

1. **Summary.** Run metadata, case count, overall pass rate, and mean score per evaluator (N/A excluded, skipped counts shown). Also abstention accuracy, precision and recall, citation validity, verify rejection and repair rates, the error count (infrastructure vs task), latency (mean/p50/p95 for total and TTFT), and tokens and estimated cost by model.
2. **Charts.**
   - Mean score per evaluator, and pass/fail per evaluator
   - Score histograms
   - Category × evaluator heatmap
   - Abstention confusion matrix and grounding status breakdown
   - Retrieval hit rate and MRR by category
   - Rerank score distribution
   - Stacked latency (rewriter wait / retrieval / generation)
3. **Per-case table.** Sortable and filterable by category, pass/fail, evaluator and grounding status, with a text search.
4. **Case detail**, expandable:
   - the query and follow-ups, with each turn's rewriter output;
   - the retrieved chunks with source URL, section, score and `found_by`;
   - raw vs delivered answer side by side, highlighted when `verify` replaced it, with the reasons and repair outcome;
   - the expected answer and sources;
   - each evaluator's score, label and judge reasoning, plus skip reasons.
5. **Failure analysis.** The lowest-scoring cases, grouped by likely cause. Rules are deterministic and applied in order:
   - infrastructure error
   - wrong abstention (FN/FP)
   - retrieval miss (hit@k = 0 or `NO_MATCH`)
   - ungrounded answer (verify rejected, or faithfulness below threshold)
   - rewriter drift (`RewriteFaithfulness` failed, or a fallback)
   - other
6. **Baseline comparison** (with `--baseline`). Per-metric deltas (score, pass rate, abstention, citation validity, rejection rate, latency, cost), cases matched by id with per-case regressions listed, and a note when the two runs' configs differ.
7. **Notes.** Evaluators not run and why (`ToolParameterAccuracy`), and the seed-data caveat.

## 10. Testing (no live calls)

- `tests/evals/test_dataset.py`: valid and invalid JSONL, duplicate ids, follow-up forms, category/id/limit filters, `expect_search` defaults.
- `tests/evals/test_metrics.py`: hit@k, recall@k, MRR, context precision, URL normalisation (page vs anchor), citation validity, abstention labels and confusion matrix, latency percentiles, cost estimate.
- `tests/evals/test_evaluators.py`: the deterministic evaluators and `Gated` on hand-built `EvaluationData`. Judge evaluators get a stub model.
- `tests/evals/test_report.py`: renders `fixtures/results_small.json`, checks the sections, embedded JSON, baseline deltas, and that no charts are emitted for empty data.
- `tests/test_chat_service.py` / `tests/test_knowledge_base_tool.py`: observer events fire, and `observer=None` behaviour is unchanged.

All of these run with `uv run pytest`, alongside the existing suite.

## 11. Smoke run

After implementation: `uv run python -m evals.run --ids <answerable>,<out-of-scope>,<greeting> --concurrency 1`. This needs AWS credentials for the sandbox account (the shell used for planning had none: run `aws login` / SSO first). It also needs `KNOWLEDGE_BASE_ID`, `MODEL_ARN` and `RERANKER_MODEL_ARN` from `.env`, and access to the judge inference profile. The deliverable is the report path, `reports/eval_<run_id>.html`.

## 12. Decisions to confirm

1. The five app changes in section 4, versus a no-app-change variant. That variant would lose per-hit sections, `found_by`, missing-query and retry detail, and per-attempt raw replies, and would need a `chdir` into a temp dir for storage isolation.
2. `judge_sees = "delivered"` as the default (section 6.4).
3. Page-level URL matching as the default for the retrieval metrics.
4. Run-level, not exact per-case, token attribution for the rewriter (section 7.3).
5. `reports/`, `evals/results/` and `evals/.cache/` are git-ignored.
