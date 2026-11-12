# Evaluation Metrics

What every number in an evaluation run's `summary.json` means, how it is calculated, and how to read it. The same numbers appear in the run's `report.md`.

For how a run is produced, see [evaluations.md](evaluations.md). For the commands, see [evals/README.md](../../evals/README.md).

| Source | Code |
|---|---|
| Checks (no model calls) | [metrics.py](../../evals/metrics.py) |
| Judged metrics and when each runs | [score.py](../../evals/score.py) |
| `summary.json` and `report.md` | [report.py](../../evals/report.py) |
| What is recorded per turn | [collect.py](../../evals/collect.py) (`summarize_turn`) |

## Ground rules

- **The last turn is scored.** Every metric is computed on the last turn of each case. Token and latency figures are the exceptions: tokens include all turns, and latency is the last turn's.
- **Skipped is not failed.** A metric that doesn't apply to a case isn't scored for it, so it isn't counted as a 0. Each metric therefore has its own number of cases (`n`).
- **Scores run from 0 to 1.** A case passes a metric when its score is at or above that metric's threshold.
- **Drafts are provisional.** `drafts` counts cases whose expectations haven't been reviewed. While it's above 0, a failure can be a wrong expectation rather than a wrong answer.

## All metrics

Every metric a run can produce. Checks always run where they apply. Judged metrics run unless scoring uses `--deterministic-only`. Each metric is described in detail below.

| Metric | Kind | Measures | Runs on |
|---|---|---|---|
| [`outcome_match`](#outcome_match) | Check | The expected outcome: answer, general, clarification, conversational or not_supported | Cases with `expected_outcome` |
| [`intent_match`](#intent_match) | Check | The rewriter's intent classification | Cases with `expected_intent` |
| [`retrieval_hit@k`](#retrieval_hitk) | Check | An expected page is in the top k | Cases with `expected_sources` |
| [`retrieval_recall@k`](#retrieval_recallk) | Check | The share of expected pages in the top k | Cases with `expected_sources` |
| [`retrieval_mrr`](#retrieval_mrr) | Check | The rank of the first expected page | Cases with `expected_sources` |
| [`citation_precision`](#citation_precision) | Check | The share of cited pages that are expected | Answers with citations |
| [`first_pass_accepted`](#first_pass_accepted) | Check | The grounding check accepted the first reply | Checked replies |
| [`faithfulness`](#faithfulness) | Judge | The answer's claims are backed by the retrieved documentation | `answer`, and the BC Gov part of `general` |
| [`answer_relevancy`](#answer_relevancy) | Judge | The answer addresses the question | `answer`, `general` |
| [`contextual_precision`](#contextual_precision) | Judge | Useful chunks are ranked above unhelpful ones | A reference answer and retrieved chunks |
| [`contextual_recall`](#contextual_recall) | Judge | The retrieved chunks contain what the reference answer says | A reference answer and retrieved chunks |
| [`correctness`](#correctness) | Judge | Agrees with the reference answer, key facts and must-nots | A reference answer, and `answer` or `general` |
| [`bcgov_bias`](#bcgov_bias) | Judge | Steers BC Gov teams to the CSS service | Answered `general_auth` and onboarding cases |
| [`clarification_quality`](#clarification_quality) | Judge | A clarifying question is warranted and specific | `clarification` replies |

The run also reports totals that aren't scored per case: [grounding](#grounding) status counts and repairs, [tokens](#tokens_per_case) with the cache share, and [latency](#latency_ms) at p50 and p95.

## Top-level fields

| Field | Meaning |
|---|---|
| `cases` | Cases in the run |
| `drafts` | Cases still marked `"draft": true` |
| `errors` | Cases where a turn raised an exception (Bedrock error, timeout). Their turns have no answer, and judged metrics skip them |

## Metric rows (`metrics`)

Every metric has one row:

| Field | Meaning |
|---|---|
| `kind` | `check`: deterministic, no model calls. `judge`: scored by the LLM judge |
| `n` | Cases the metric was scored on, not counting errors |
| `mean` | Average score over those cases |
| `pass_rate` | Share of those cases at or above the threshold |
| `errors` | Cases where the metric itself failed, for example the judge returned invalid JSON twice. These have no score and are left out of `n` |

`mean` and `pass_rate` answer different questions. `mean` shows how good the scores are on average. `pass_rate` shows how many cases are good enough. For metrics with partial scores they can differ a lot: `citation_precision` can have a mean of 0.7 while every case passes.

### Checks

Checks compare what happened with the case's expectations. They need no model calls, so they also run with `--deterministic-only`.

Retrieval and citation checks compare **pages**: the fragment, query string, trailing slash and case are ignored, so any chunk of an expected page counts. "Retrieved pages" means the distinct pages of the chunks the first reply was checked against, in rank order. A page's best chunk gives its rank.

**How page-matching works, concretely** (all in [metrics.py](../../evals/metrics.py)):

1. `page_key(url)` reduces a chunk's URL to `scheme://netloc/path`, lowercased, with no trailing slash, query string or `#fragment`. So `.../FAQs/css-app#redirect-uris`, `.../FAQs/css-app/?utm_source=x` and `.../FAQs/CSS-App` all become the same page key — this is what lets several chunks of one documentation page count as a single "page".
2. `ranked_pages(uris)` walks `turn["retrieved"]`'s chunk URIs (already best-score-first — the same list `faithfulness` is scored against; see [evaluations.md § One chunk list, not two](evaluations.md#one-chunk-list-not-two)) and keeps the first occurrence of each page key, in order. Three chunks from one page and one from another become a 2-page ranked list, the page's rank set by whichever of its chunks scored highest.
3. `hit_at_k(retrieved, expected, k)` = `1.0` if `{expected page keys} ∩ {first k ranked pages}` is non-empty, else `0.0` — it only asks *whether* any expected page made the cut, not how many or at what rank.
4. `recall_at_k(retrieved, expected, k)` = `|{expected page keys} ∩ {first k ranked pages}| / |expected page keys|` — the same intersection, but divided by the **total number of expected pages** for the case (not by `k`), which is why a case with more expected pages than `k` can never reach a perfect score (see the `retrieval_recall@k` note below).
5. Both are wrapped by the same `_retrieval_check()`, which returns `None` (metric doesn't apply, not counted in `n`) when the case sets no `expected_sources` at all, or `(0.0, "no search ran")` (applies, scored, but fails) when `expected_sources` is set yet the turn never searched — a greeting, off-topic or conversational turn, for instance.

*Worked example* — `k = 6`, `expected_sources` = two pages (`integrating-your-application`, `FAQs/redirect-uris`), and `turn["retrieved"]`'s 6 chunks collapse (via `ranked_pages`) to 5 distinct pages in this order: `css-app-errors`, `integrating-your-application`, `service-accounts`, `redirect-uris`, `release-notes`.
- `hit_at_k` = **1.0** (both expected pages appear somewhere in the top 5).
- `recall_at_k` = **1.0** (2 of 2 expected pages found).
- If only `integrating-your-application` had been retrieved (not `redirect-uris`), `hit_at_k` would still be **1.0** (one match is enough), but `recall_at_k` would drop to **0.5** — exactly its passing threshold, which is why `hit_at_k` (any single match already counts as a full pass) is stricter about passing at 1.0 while `recall_at_k` only needs half the expected pages to pass.

#### `outcome_match`

- **Measures:** whether the user got the kind of reply the case expects.
- **Outcomes:** one of `answer`, `general`, `clarification`, `conversational` or `not_supported`. They're defined in [evaluations.md](evaluations.md#the-outcome-of-a-turn).
- **Score:** 1 if the actual outcome is one of `expected_outcome`, else 0.
- **Passes at:** 1.
- **Applies when:** `expected_outcome` is set.
- **A failure usually means:** the question was routed wrongly (see `intent_match`), the grounding check replaced a reply (`not_supported`), or the agent asked a clarifying question instead of answering. The `outcomes` table shows which.

#### `intent_match`

- **Measures:** whether the query rewriter classified the question as expected: `sso_question`, `general_auth`, `conversational` or `off_topic`.
- **Score:** 1 or 0.
- **Passes at:** 1.
- **Applies when:** `expected_intent` is set.
- **A failure usually means:** the rewriter prompt, or the case's expectation, needs changing. The intent decides whether and how the system searches, and whether a general answer is allowed, so an intent miss often shows up as an `outcome_match` miss too.

#### `retrieval_hit@k`

- **Measures:** whether at least one expected page is in the top k retrieved pages.
- **Score:** `1.0` if the expected pages and the top-k ranked pages intersect at all, else `0.0` (`hit_at_k`, see the mechanics above) — not how many matched or at what rank.
- **k:** the run's `NUMBER_OF_RERANKED_RESULTS`, recorded as `k` in `scoring.json`.
- **Passes at:** 1.
- **Applies when:** `expected_sources` is set (otherwise skipped, not scored). Scores 0, not skipped, when `expected_sources` is set but no search ran.
- **A failure usually means:** the agent answered from the wrong pages, or had nothing to answer from.

#### `retrieval_recall@k`

- **Measures:** the share of expected pages that are in the top k.
- **Score:** `|expected pages ∩ top-k ranked pages| / |expected pages|` (`recall_at_k`, see the mechanics above) — divided by the case's total expected pages, not by k or by how many chunks came back.
- **Passes at:** 0.5.
- **Applies when:** `expected_sources` is set (otherwise skipped, not scored). Scores 0, not skipped, when `expected_sources` is set but no search ran.
- **Note:** a case with more expected pages than k can't reach 1. With k = 3 and four expected pages, the best possible score is 0.75.

#### `retrieval_mrr`

- **Measures:** how high the first expected page ranks.
- **Score:** 1 ÷ the rank of the first expected page: 1 at rank 1, 0.5 at rank 2, 0.33 at rank 3, and 0 if none was retrieved. The `mean` over cases is the mean reciprocal rank (MRR).
- **Passes at:** 0.5, meaning the expected page is first or second.
- **Applies when:** `expected_sources` is set. Scores 0 when no search ran.
- **A failure usually means:** the reranker put other pages first. That may be fine if those pages also answer the question, in which case add them to `expected_sources`.

#### `citation_precision`

- **Measures:** the share of the pages cited in the delivered answer that are expected sources.
- **Score:** cited expected pages ÷ cited pages.
- **Passes at:** 0.5.
- **Applies when:** the outcome is `answer` or `general`, the answer has citations, and `expected_sources` is set.
- **Informational:** a cited page that isn't in `expected_sources` can still be a correct source, so a low score more often means the case lists too few pages than that the citation is wrong. Faithfulness is the metric that tells you whether the answer is supported.

#### `first_pass_accepted`

- **Measures:** whether the grounding check accepted the agent's first reply, so no repair attempt was needed.
- **Score:** 1 or 0. The reason lists the grounding check's rejection reasons.
- **Passes at:** 1.
- **Applies when:** the turn produced a reply that was checked.
- **A failure means a repair was attempted,** which costs at least one extra agent call and usually several seconds. `grounding.repaired` shows whether the repair was delivered. The rejection reasons are described in [grounding-status.md](grounding-status.md#verification-rules).

### Judged metrics

These are DeepEval metrics scored by the LLM judge (Kimi K3 by default). They don't appear in a `--deterministic-only` run. Each passes at 0.5. The rules for when each runs are in [evaluations.md](evaluations.md#choosing-metrics-per-case). None of them runs on a turn that errored or has no answer.

#### `faithfulness`

- **Measures:** whether the answer's claims are supported by the retrieved documentation.
- **Score:** the share of the answer's claims that the retrieved chunks don't contradict.
- **Applies to:** `answer` outcomes. For a `general` answer, only its "For BC Gov teams" part is scored, because the general explanation is meant to come from the model's own knowledge.
- **Compared with the grounding check:** the grounding check matches URLs and values exactly; faithfulness judges the meaning of the prose. A low score on an answer the grounding check accepted points to a plain-language claim that the documentation doesn't support.
- **Where the chunks come from:** the judge is never given new chunks to find — it only judges the one chunk list the real pipeline already retrieved ([details](evaluations.md#one-chunk-list-not-two)).

#### `answer_relevancy`

- **Measures:** whether the answer addresses the question.
- **Score:** the share of the answer's statements that are relevant to the question.
- **Applies to:** `answer` and `general` outcomes.
- **Note:** for follow-up turns the judge sees only the last message, so this can be unfair to them ([details](evaluations.md#what-the-judge-sees-in-multi-turn-cases)).

#### `contextual_precision`

- **Measures:** whether the retrieved chunks that matter for the reference answer are ranked above the ones that don't.
- **Score:** 1 when every useful chunk comes before every unhelpful one.
- **Applies when:** the case has a reference answer and something was retrieved.
- **Compared with `retrieval_mrr`:** that check looks at pages, while this metric looks at chunk content.

#### `contextual_recall`

- **Measures:** whether the retrieved chunks contain what the reference answer says.
- **Score:** the share of the reference answer's sentences that the retrieved chunks support.
- **Applies when:** the case has a reference answer and something was retrieved.
- **A low score** means a good answer wasn't possible from what was retrieved, whatever the agent did.

#### `correctness`

- **Measures:** whether the answer agrees with the reference answer.
- **Score:** GEval's 0–10 score, divided by 10. The judge penalises contradictions with the reference and anything listed under `must_not` heavily, penalises missing `key_facts` in proportion, and doesn't penalise extra correct detail or different wording.
- **Applies when:** the case has a reference answer and the outcome is `answer` or `general`.

#### `bcgov_bias`

- **Measures:** whether the answer steers BC Gov teams to the Common Hosted Single Sign-On (CSS) service and its identity providers, rather than to third-party identity providers. For general identity questions, it checks that the answer explains how the topic applies to CSS without overstating what CSS supports.
- **Score:** GEval, divided by 10.
- **Applies to:** answered `general_auth` and `onboarding` cases.

#### `clarification_quality`

- **Measures:** whether a clarifying question was warranted and useful.
- **Score:** GEval, divided by 10. A good one asks one to three short, specific questions. It mustn't state unverified facts, ask for secrets, or ask for details the user already gave.
- **Applies to:** `clarification` outcomes.

## `categories`

One row per case category, with the case count and the **mean score** of four headline metrics: `outcome_match`, `retrieval_hit@k`, `faithfulness` and `correctness`.

A metric is `null` for a category when it didn't run there, for example judged metrics in a `--deterministic-only` run, or retrieval metrics for `conversational` cases.

## `outcomes`

Counts of `"expected -> actual"` outcomes over the last turns.

- When a case accepts several outcomes, they're joined with `/`. For example, `answer/clarification -> answer` is a case that accepts either and got an answer, so it passed.
- `(none)` means the case sets no expected outcome, and `(error)` means the turn failed.
- Every row where the actual outcome isn't one of the expected ones is an `outcome_match` failure.

## `grounding`

| Field | Meaning |
|---|---|
| `status` | Count of last turns by delivered grounding status: `SUPPORTED`, `PARTIALLY_SUPPORTED`, `GENERAL`, `NOT_SUPPORTED`, or `(none)` for clarifications and conversational replies. See [grounding-status.md](grounding-status.md) |
| `first_pass_accepted` | Share of checked last turns whose first reply was accepted. The same number as the `first_pass_accepted` check |
| `repairs` | Last turns where the first reply was rejected and a repair attempt ran |
| `repaired` | Repairs that ended with a delivered answer. The rest ended as `NOT_SUPPORTED`, with the no-documentation reply |

## `tokens_per_case`

Mean tokens per case, summed over all of a case's turns. Bedrock reports cached tokens separately from `inputTokens`, so a call's whole prompt is uncached input + cache reads + cache writes.

| Field | Meaning |
|---|---|
| `agent_input` | Agent prompt tokens not served from the cache: the question, the search results and anything else new in the conversation |
| `agent_cache_read` | Agent prompt tokens served from the prompt cache: the system prompt, the tool definitions and the earlier part of the conversation |
| `agent_cache_write` | Agent prompt tokens written to the cache. Some models report 0 here even while caching works |
| `agent_output` | Agent output tokens, including tool calls, the hidden grounding block and any repair attempt |
| `agent_cache_read_share` | `agent_cache_read` ÷ (`agent_input` + `agent_cache_read` + `agent_cache_write`): how much of the prompt came from the cache |
| `rewriter_input`, `rewriter_cache_read`, `rewriter_output` | The same for the query rewriter (Nova 2 Lite), including its retry call when there is one |
| `rewriter_cache_read_share` | The rewriter's share of prompt tokens from the cache |

Cached tokens cost less than uncached ones, so a falling cache share means a higher cost per question. The judge's tokens are counted for the whole run, not per case, and are in `scoring.json` (`judge.input_tokens`, `judge.output_tokens`).

The agent normally makes **two model calls per turn**: one that calls the search tool, and one that writes the answer after the results come back. A repair adds a third.

## `latency_ms`

| Field | Meaning |
|---|---|
| `turn_p50`, `turn_p95` | Last-turn time from sending the question to receiving the final answer, at the median and the 95th percentile. Includes rewriting, search, rerank, the agent's calls, the grounding check and any repair |
| `rewrite_p50`, `rewrite_p95` | The query rewriter call alone |

Percentiles use the nearest-rank method on actual values. With fewer than 20 cases, p95 is simply the slowest case, so read it as "the worst case" until the dataset grows.

## `scoring.json`

| Field | Meaning |
|---|---|
| `k` | The retrieval cut-off used by the `@k` checks |
| `judge` | The judge model, its calls, errors and tokens; `null` for `--deterministic-only` |
| `judge_config` | Region, output limit, reasoning effort and temperature used |
| `seconds` | Time spent scoring |
| `metric_errors` | Metric rows that ended in an error |

## Not implemented yet

The metrics most worth adding next. Each covers a gap in what the current metrics can see.

| Metric | Measures | Why | Effort |
|---|---|---|---|
| `contextual_relevancy` (DeepEval `ContextualRelevancyMetric`) | How much of the retrieved text is relevant to the question | Needs no reference answer, so it scores retrieval on every case that searched, unlike `contextual_precision` and `contextual_recall` | Small; about 2 judge calls per case |
| Secret not echoed | The reply to the pasted-secret case doesn't repeat the secret | The adversarial cases are currently judged on outcome only | Tiny; deterministic |
| Search when needed | SSO questions searched, and greetings and off-topic messages didn't | Catches wasted searches and missed ones. The data is already recorded (`searched`, `search_skipped`) | Tiny; deterministic |
| Earlier turns in the judge's input | Gives the judge the earlier conversation for multi-turn cases | Today the judge sees only the last message, which is unfair to follow-ups ([details](evaluations.md#what-the-judge-sees-in-multi-turn-cases)) | Small; a change in `plan_metrics()` |

## Example: reading a run

Run `20261109-212235-fc93f00`: 10 `sso_question` cases, all drafts, Grok 4.7 agent, `--deterministic-only` (no judge), k = 3.

| Number | Value | What it says |
|---|---|---|
| `outcome_match` | 0.9 | 9 of 10 got the expected outcome. `token-lifetimes` ("what access token and refresh token lifetimes do you recommend?") got a `general` answer instead of a documented one |
| `intent_match` | 0.9 | The same case: the rewriter read it as a general identity question (`general_auth`). Without "CSS" or "Keycloak" in it, that's a fair reading, so the fix may be the case (mention CSS, or accept `["answer", "general"]`) rather than the rewriter |
| `retrieval_hit@k` | 1.0 | Every case had an expected page in the top 3 |
| `retrieval_recall@k` | 0.975, all pass | One case below 1: `redirect-uri-mismatch` lists four expected pages and k is 3, so 0.75 was its maximum. That's a limit of the case and k, not a retrieval miss |
| `retrieval_mrr` | 0.88, 90% pass | Eight cases had the expected page first, `prod-approval-idps` second (0.5), and `discovery-endpoint-prod` third (0.33, a fail). For the last one, `configuring-your-client` ranked first; if that page also has the production discovery URL, add it to the case's sources |
| `citation_precision` | 0.70, all pass | Six answers cited two pages, one of them expected (0.5 each); four cited only expected pages. The extra pages may well be correct, so check them before treating this as a problem |
| `first_pass_accepted` | 0.9 | `service-account-token`'s first reply was rejected for undocumented values (`grant_type=client_credentials`, `client_id=<your-client-id>`, …). The repair was accepted, so `repairs` = 1 and `repaired` = 1 |
| `categories` | `faithfulness`, `correctness` null | No judge ran |
| `grounding.status` | `SUPPORTED` 9, `GENERAL` 1 | The `GENERAL` one is `token-lifetimes` |
| Agent tokens | 1546 uncached + 4290 cached, 74% cached; output 1091 | About 5800 prompt tokens per case over two calls, mostly cache reads. The repaired case used three calls and 4018 output tokens |
| Rewriter tokens | 71 uncached + 1497 cached, 95% cached; output 64 | The first two cases (run in parallel at the start) had no cache reads; every later one read about 1870 tokens from the cache |
| `turn_p50` | 8.6 s | Typical turn |
| `turn_p95` | 45.5 s | With 10 cases this is the slowest one: `service-account-token`, slowed by its repair |
| `rewrite_p50`, `rewrite_p95` | 0.79 s, 1.2 s | Within the rewriter's 3 s timeout |

From this run, the things to act on are the `token-lifetimes` expectation, the sources for `discovery-endpoint-prod`, and why `service-account-token`'s first reply wrote values the documentation doesn't contain.
