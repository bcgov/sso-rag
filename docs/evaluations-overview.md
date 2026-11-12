# SSO RAG Evaluations with DeepEval

How we measure whether the SSO assistant gives correct, grounded answers, and how we catch regressions before they reach users.

The detailed references are:
- [evals/README.md](../evals/README.md): commands, options and the dataset schema;
- [components/evaluations.md](components/evaluations.md): the design, including recording hooks, test cases and the judge;
- [components/evaluation-metrics.md](components/evaluation-metrics.md): every number in a run's summary.

> **About the examples.** Examples marked **(real)** come from run `20261109-215753-fc93f00-feat-deepevals-setup`: 10 `sso_question` cases, scored for `correctness` only. A few come from an earlier deterministic-only run that is documented in [evaluation-metrics.md](components/evaluation-metrics.md#example-reading-a-run). Examples marked **(illustrative)** show the shape of an output that hasn't been produced by a saved run yet. All 40 cases are still `"draft": true`, so the scores are provisional.

---

## Contents

1. [What DeepEval is and why we chose it](#1-what-deepeval-is-and-why-we-chose-it)
2. [How it helps](#2-how-it-helps)
3. [How an evaluation runs for sso-rag](#3-how-an-evaluation-runs-for-sso-rag)
4. [The Synthesizer: drafting new test cases](#4-the-synthesizer-drafting-new-test-cases)
5. [Metrics we measure](#5-metrics-we-measure)
6. [Metrics we can add next](#6-metrics-we-can-add-next)
7. [Benefits of the HTML report](#7-benefits-of-the-html-report)

---

## 1. What DeepEval is and why we chose it

[DeepEval](https://deepeval.com) is an open-source Python framework for evaluating LLM applications. It provides ready-made metrics for RAG systems, such as faithfulness, answer relevancy and contextual precision and recall. It also has **GEval**, which turns criteria written in plain English into a scored metric, and a **Synthesizer** that drafts test cases from documents. We use version 4.3.3.

We use DeepEval **as a library of metrics**. We don't use its test runner or its hosted platform.

### Why DeepEval

| Need in sso-rag | What DeepEval gives us |
|---|---|
| Measure RAG quality: is the answer backed by the retrieved docs, and did retrieval find the right docs? | Built-in `FaithfulnessMetric`, `AnswerRelevancyMetric`, `ContextualPrecisionMetric` and `ContextualRecallMetric` |
| Check rules specific to the project, such as "recommend CSS rather than Okta for BC Gov services" | `GEval` with our own fixed evaluation steps |
| Use a judge on **AWS Bedrock**, not OpenAI | `DeepEvalBaseLLM` lets us plug in our own judge class ([judge.py](../evals/judge.py)), which calls Kimi K3 through the Bedrock Converse API with our existing `boto3` |
| Keep BC Gov documentation and answers on our machines and in our AWS account | Runs fully locally. Telemetry is turned off, `.env` loading is turned off, and nothing logs in to Confident AI ([evals/\_\_init\_\_.py](../evals/__init__.py)) |
| Grow the dataset without writing every case by hand | `Synthesizer` drafts question and answer pairs from our documentation chunks |
| Put deterministic checks and LLM-judged metrics in one report | `BaseMetric` lets us wrap our own checks (intent, retrieval hit@k, …) so every score has the same shape |
| Every score explained | Every DeepEval metric returns a **reason** with its score, so a failure can be read rather than guessed |

### What we deliberately don't use

| DeepEval feature | Why not |
|---|---|
| `evaluate()` / `deepeval test run` | We call each metric directly with `a_measure()`. That way each case gets only the metrics that apply to it, a failing metric becomes an error row instead of stopping the run, and we control concurrency ourselves |
| `EvaluationDataset` / `Golden` | Our cases need multi-turn conversations and expected outcomes, so they live in a stricter JSONL schema ([dataset.py](../evals/dataset.py)) |
| `AmazonBedrockModel` | It needs `aiobotocore`, which pins `botocore` versions that conflict with the app's `boto3` |
| Confident AI (hosted dashboard) | The data must stay local |

---

## 2. How it helps

- **Catches regressions before merge.** Changing a prompt, the rewriter, the reranker cut-off or the model can quietly break answers. Running a branch and comparing it with `main` (`--baseline`) lists per-case regressions and fixes, metric deltas, and changes in cost and latency.
- **Separates retrieval problems from generation problems.** A bad answer has one of three causes:
  - retrieval didn't find the right page (`retrieval_hit@k`, `contextual_recall`);
  - the agent ignored or contradicted the documentation (`faithfulness`);
  - the question was routed wrongly (`intent_match`, `outcome_match`).

  Each one points to a different fix.
- **Tests the real pipeline.** Every case goes through `ChatService.stream()`, the same path as `POST /chat`, so it covers the query rewriter, Knowledge Base search, the reranker, the agent, the grounding check and the repair attempt. We aren't testing a mock.
- **Makes cost and latency visible.** Tokens per case (including the prompt cache share), p50 and p95 latency and judge tokens are reported next to quality. A prompt change that doubles cost shows up straight away.
- **Re-scores for free.** Scoring reads only the saved results. You can change or add a metric and re-score without calling the agent again. `--deterministic-only` scoring makes no model calls at all.
- **Explains its scores.** Every judged score includes the judge's reason, so the team can check it rather than trust a number.
- **Covers the edge cases.** The dataset includes vague questions, unsupported topics, greetings, off-topic requests, follow-ups, "don't search" requests, prompt injection and a pasted secret. These cover behaviour we promised in [edge-cases.md](edge-cases.md).

---

## 3. How an evaluation runs for sso-rag

### What happens to one user message

Every evaluation case goes through the same path as a real user's question:

![How a user message is processed: the query rewriter and main agent start together, the search tool waits for the rewriter's queries, the agent's answer is checked by grounding.verify, and it is delivered, repaired once, or replaced by the no-documentation reply](assets/sso_rag_message_flow.svg)

1. **The query rewriter** (Nova 2 Lite) runs in the background. It works out the intent and writes the search queries: the rewritten query, a step-back query and any sub-queries.
2. **The main agent** (Grok 4.7) starts at the same time. It calls `search_knowledge_base()` with no arguments; it never writes search queries itself.
3. **The search tool** waits for the rewriter's queries, then searches the Knowledge Base and reranks the results. The top k chunks go back to the agent, which writes the answer and ends it with a hidden `<grounding>` block.
4. **The grounding check** tests the answer against those same chunks:
   - every citation must be a chunk retrieved this turn;
   - every URL and code or config value must appear in the chunks.

   If the answer passes, it is delivered. If it fails, the agent gets one repair attempt. If that fails too, the user gets the no-documentation reply with the support contacts.

The evaluation records each of these steps, which is how the metrics below can tell a routing problem from a retrieval or answer problem.

The evaluation wraps that pipeline in three steps:

```text
evals/datasets/goldens.jsonl              Step 0: test cases (questions + expectations)
    │
    ▼
python -m evals.collect                    Step 1: run every case through the REAL pipeline
    │   ChatService.stream(turn)            same entry point as POST /chat
    │   TurnReport (ContextVar)             records rewrite, search hits, model calls, reply attempts
    ▼
evals/runs/<run id>/results.jsonl          one line per case: answer + everything that happened inside
    │
    ▼
python -m evals.score <run dir>            Step 2: score with checks and DeepEval metrics
    │   LLMTestCase per (case, metric)
    │   ├─ checks (no model calls)          intent, outcome, retrieval, citations, grounding
    │   └─ DeepEval metrics + GEval ─────►  BedrockJudge ─► Bedrock Converse ─► Kimi K3
    ▼
evals/runs/<run id>/scores.jsonl           one row per (case, metric): score, pass, reason
    │
    ▼
report.md, summary.json                    Step 3: report (+ compare-<baseline>.md with --baseline)
```

Each step reads only the files the previous step wrote, so any step can be re-run on its own.

### Step 0: the test cases

`evals/datasets/goldens.jsonl` holds **40 hand-written cases in 9 categories**: `sso_question` (16), `onboarding` (3), `general_auth` (5), `clarification` (4), `not_supported` (2), `conversational` (2), `off_topic` (2), `multi_turn` (3) and `adversarial` (3).

**Example (real).** One case from `goldens.jsonl`:

```json
{
  "id": "rotate-client-secret",
  "category": "sso_question",
  "turns": ["how do I rotate the client secret for my integration?"],
  "expected_intent": "sso_question",
  "expected_outcome": ["answer"],
  "expected_sources": ["https://bcgov.github.io/sso-docs/FAQs/css-app"],
  "expected_output": "For a confidential client: open the integration in the CSS app, go to Secrets, regenerate the secret for the environment, download the updated Installation JSON from Technical Details and update the application's configuration. Rotating the secret invalidates the previous secret immediately.",
  "key_facts": [
    "regenerate it under Secrets in the CSS app",
    "download the updated Installation JSON",
    "the old secret stops working immediately"
  ],
  "draft": true
}
```

| Field | What it's for |
|---|---|
| `turns` | The user's messages. A multi-turn case such as `followup-rotate-secret` has two: `"I have a confidential client in CSS"`, then `"how do I rotate its secret?"` |
| `expected_intent`, `expected_outcome` | How the question should be routed, and what kind of reply the user should get |
| `expected_sources` | The documentation pages that answer it, used by the retrieval checks |
| `expected_output`, `key_facts`, `must_not` | The reference answer for the correctness judge |

### Step 1: collect, running the real pipeline

```bash
uv run --group eval python -m evals.collect --label feat/deepevals-setup
```

What happens for each case:
1. **Storage is isolated.** `collect` points conversation storage at a temporary folder, which is deleted afterwards. The run refuses to start if storage would be the S3 bucket.
2. Each case gets **a new conversation**. Its turns are sent one after another, so follow-ups see the assistant's real earlier replies.
3. Each turn runs through `ChatService.stream()`, the same code as production.
4. While the turn runs, the app **records** what happened inside it: the rewriter output, the search queries and ranked hits, each model call's tokens, and each reply attempt with its grounding verdict. In production nothing is recorded, so `record()` does nothing.
5. The case is appended to `results.jsonl` as soon as it finishes.

**Console output (real):**

```text
run 20261109-215753-fc93f00-feat-deepevals-setup: 10 cases -> evals/runs/20261109-215753-fc93f00-feat-deepevals-setup
[1/10] provisioning-time: answer SUPPORTED (5.2s)
[2/10] rotate-client-secret: answer SUPPORTED (6.0s)
[3/10] idps-without-roles: answer SUPPORTED (7.9s)
...
[7/10] token-lifetimes: general GENERAL (14.2s)
[8/10] service-account-token: answer SUPPORTED (25.8s)
...
done: 10 ok, 0 failed. Next: uv run --group eval python -m evals.score evals/runs/20261109-215753-fc93f00-feat-deepevals-setup
```

**`results.jsonl`, last turn of `rotate-client-secret` (real, shortened):**

```json
{
  "question": "how do I rotate the client secret for my integration?",
  "answer": "For a confidential client, rotate the client secret in the CSS app. Rotating it invalidates the previous secret immediately.\n\n1. Open your integration in [CSS](...).\n2. Go to **Secrets**.\n3. Regenerate the secret for the target environment.\n4. Download th…",
  "outcome": "answer",
  "intent": "sso_question",
  "grounding_status": "SUPPORTED",
  "citations": ["https://bcgov.github.io/sso-docs/FAQs/css-app#how-do-i-rotate-a-client-secret"],
  "rewrite": {
    "query": "How do I rotate the client secret for my CSS integration?",
    "step_back_query": "client secret management and rotation process",
    "latency_ms": 1317
  },
  "search": {
    "queries": [
      {"text": "How do I rotate the client secret for my CSS integration?", "origin": "rewrite"},
      {"text": "client secret management and rotation process", "origin": "step_back"}
    ],
    "candidates": 8,
    "hits": [{"uri": "https://bcgov.github.io/sso-docs/FAQs/css-app#how-do-i-rotate-a-client-secret", "score": 0.998}]
  },
  "retrieved": [{"title": "Frequently Asked Questions > CSS App > How do I rotate a client secret?", "score": 0.998, "text": "..."}],
  "attempts": [{"attempt": "first", "response_type": "answer", "grounding_status": "SUPPORTED", "reasons": []}],
  "repair_attempted": false,
  "tokens": {"agent_calls": 2, "agent_input": 95, "agent_cache_read": 4433, "agent_output": 434,
             "rewriter_input": 83, "rewriter_cache_read": 1873, "rewriter_output": 61},
  "latency_ms": 6042
}
```

The run also writes `run.json`, so two runs can be compared fairly. It records:
- the git commit, and whether there were uncommitted changes;
- the dataset's hash;
- the agent model (Grok 4.7), the rewriter model (Nova 2 Lite) and the reranker;
- retrieval settings such as `number_of_reranked_results: 3`.

### Step 2: score, with checks and DeepEval metrics

```bash
uv run --group eval python -m evals.score evals/runs/<run id>                       # all metrics
uv run --group eval python -m evals.score evals/runs/<run id> --deterministic-only  # checks only, no model calls
uv run --group eval python -m evals.score evals/runs/<run id> --metrics correctness # just these metrics
```

What happens:
1. For each case, the **last turn** becomes a DeepEval `LLMTestCase`:

   | `LLMTestCase` field | Filled from |
   |---|---|
   | `input` | The last question |
   | `actual_output` | The answer the user received |
   | `expected_output` | The reference answer, plus the key facts and must-nots |
   | `retrieval_context` | The chunks the pipeline actually retrieved |

2. `plan_metrics()` picks **only the metrics that apply**. For example, `clarification_quality` runs only when the assistant asked a clarifying question. A metric that doesn't apply is skipped, not scored 0.
3. The checks run instantly. The judged metrics call Kimi K3 on Bedrock, 4 at a time.

**Console output (real):**

```text
scoring 10 metric runs over 10 cases
10 scores, 0 metric errors. Report: evals/runs/20261109-215753-fc93f00-feat-deepevals-setup/report.md
```

**`scores.jsonl` row (real):**

```json
{"id": "rotate-client-secret", "category": "sso_question", "metric": "correctness", "kind": "judge",
 "score": 1.0, "success": true, "threshold": 0.5,
 "reason": "The actual output covers all three key facts: regenerating the secret under Secrets in the CSS app, downloading the updated Installation JSON from Technical Details, and noting that the previous secret is invalidated immediately. It matches the expected output's guidance for a confidential client with no contradictions. Extra correct details (direct CSS link, step to update application configuration, and a docs reference link) are permissible and not penalized.",
 "error": null, "seconds": 11.75}
```

### Step 3: report and compare

`score` writes `summary.json` and `report.md` automatically. To compare a branch with `main`:

```bash
uv run --group eval python -m evals.report evals/runs/<branch run> --baseline evals/runs/<main run>
```

**`report.md` (real, shortened):**

```markdown
# Evaluation run 20261109-215753-fc93f00-feat-deepevals-setup

- Agent global.xai.grok-4.7, rewriter global.amazon.nova-2-lite-v1:0, judge global.moonshotai.kimi-k3
- 10 cases (10 drafts not yet reviewed), 0 failed to run; retrieval cut-off k=3
- **Draft cases are included: their expectations haven't been reviewed, so treat these scores as provisional.**

## Metrics
| Metric      | Kind  | Cases | Mean | Pass rate | Errors |
|-------------|-------|-------|------|-----------|--------|
| correctness | judge | 10    | 0.99 | 100%      | 0      |

## Outcomes (expected -> actual)
| Expected -> actual             | Cases |
|--------------------------------|-------|
| answer -> answer               | 8     |
| answer -> general              | 1     |
| answer/clarification -> answer | 1     |

## Grounding, cost and latency
- Grounding status: GENERAL 1, SUPPORTED 9
- First reply accepted by the grounding check: 100%; repairs 0, of which delivered 0
- Agent tokens per case: input 476 uncached + 4755 cache reads (91% of the prompt from cache), output 847
- Rewriter tokens per case: input 71 uncached + 1498 cache reads (95% of the prompt from cache), output 65
- Turn latency p50 7922 ms, p95 25834 ms; rewriter p50 862 ms, p95 1317 ms
- Judge: 10 calls, 7371 input and 3120 output tokens, 0 errors
```

The comparison report (`compare-<baseline>.md`) lists:
- the settings that changed between the two runs;
- the delta for each metric;
- cost and latency changes;
- the cases that regressed or were fixed.

It warns when the two runs used different datasets.

---

## 4. The Synthesizer: drafting new test cases

Writing good test cases by hand is the slowest part of evaluation. `evals.synthesize` uses DeepEval's `Synthesizer`, with the same Kimi judge, to **draft** cases from our documentation chunks. A person then reviews them.

```bash
uv run --group eval python -m evals.synthesize --limit 30 --per-page 1
# options: --pages css-app,FAQs/keycloak   --out evals/datasets/drafts.jsonl   --concurrency 4
```

What it does:
1. **Picks chunks.** It reads `chunker/chunks/*.md` and their metadata, which `chunker/preprocess.py` produces. It skips release notes and category pages, takes up to `--per-page` chunks per page, and goes round-robin across pages, so 30 drafts cover 30 different pages.
2. **Writes a question as a BC Gov developer would.** A fixed scenario tells the Synthesizer that "a developer or product owner on a BC Government technical team" asks a short question (under 30 words) in everyday words, without copying the documentation's headings.
3. **Evolves the question once.** It rewrites the question to be more concrete or broader, so questions don't simply mirror the section titles.
4. **Filters.** The judge acts as a critic and drops low-quality questions.
5. **Writes a reference answer.** The answer is at most about 120 words and uses only facts from the chunk, keeping names, values and URLs exactly as written.
6. **Saves drafts.** Each draft is saved to `evals/datasets/drafts.jsonl` with `"draft": true`, an expected outcome of `answer`, and the chunk's page as the expected source.

**Input chunk (real):** `chunker/chunks/FAQs__css-app__009.md`

```text
page_url: https://bcgov.github.io/sso-docs/FAQs/css-app
section:  Frequently Asked Questions > CSS App > How do I rotate a client secret?

## How do I rotate a client secret?
For confidential clients:
1. Open your integration in [CSS](https://sso-requests.apps.gold.devops.gov.bc.ca/)
2. Go to **Secrets**
3. Regenerate the secret for the target environment
4. Download updated Installation JSON from **Technical Details**
5. Update your application configuration

> Rotating the secret invalidates the previous secret immediately.
```

**Console output (illustrative):**

```text
drafting from 30 chunks with global.moonshotai.kimi-k3
28 drafts -> evals/datasets/drafts.jsonl. Judge usage: {'model': 'global.moonshotai.kimi-k3', 'calls': ..., 'errors': 0, ...}
```

The number of drafts can be lower than the number of chunks, because the filter drops weak questions.

**Draft in `drafts.jsonl` (illustrative):**

```json
{
  "id": "draft-faqs-css-app-9",
  "category": "sso_question",
  "turns": ["Our app's client secret might have leaked. How do I generate a new one for production?"],
  "expected_intent": "sso_question",
  "expected_outcome": ["answer"],
  "expected_sources": ["https://bcgov.github.io/sso-docs/FAQs/css-app"],
  "expected_output": "For a confidential client, open the integration in CSS, go to Secrets and regenerate the secret for the target environment, then download the updated Installation JSON and update your application. The previous secret stops working immediately.",
  "notes": "Synthesized from https://bcgov.github.io/sso-docs/FAQs/css-app#how-do-i-rotate-a-client-secret (Frequently Asked Questions > CSS App > How do I rotate a client secret?). Review before use.",
  "draft": true
}
```

### Before a draft becomes a golden case

A draft is a starting point, not a test. For each one:
1. Check that the question is something a team would really ask, and reword it if not.
2. Check the reference answer against the page and trim it to what a good answer must say.
3. Add any other pages that also answer it to `expected_sources`, and fix `expected_outcome`.
4. Add `key_facts` and `must_not` where they make the judge's job clear.
5. Set `"draft": false` and move the case into `goldens.jsonl`.

`--exclude-drafts` on `collect` runs only reviewed cases.

---

## 5. Metrics we measure

There are **14 metrics** in two groups:
- **7 checks** are deterministic: plain Python, no model calls, free and repeatable.
- **7 judged metrics** are scored by the LLM judge (Kimi K3) through DeepEval.

Some rules apply to all of them:
- Scores run from 0 to 1, and every metric records a reason.
- Only the **last turn** of each case is scored.
- A metric that doesn't apply to a case is **skipped**, not counted as 0.
- Retrieval is compared by **page**: any chunk of `FAQs/css-app` counts as `FAQs/css-app`.

### At a glance

| Metric | Kind | Question it answers | Passes at |
|---|---|---|---|
| `outcome_match` | Check | Did the user get the right *kind* of reply? | 1 |
| `intent_match` | Check | Did the rewriter route the question correctly? | 1 |
| `retrieval_hit@k` | Check | Is at least one right page in the top k? | 1 |
| `retrieval_recall@k` | Check | What share of the right pages is in the top k? | 0.5 |
| `retrieval_mrr` | Check | How high does the first right page rank? | 0.5 (rank 1 or 2) |
| `citation_precision` | Check | What share of the cited pages are expected ones? | 0.5 |
| `first_pass_accepted` | Check | Did the grounding check accept the first reply? | 1 |
| `faithfulness` | Judge | Are the answer's claims backed by the retrieved docs? | 0.5 |
| `answer_relevancy` | Judge | Does the answer address the question? | 0.5 |
| `contextual_precision` | Judge | Are the useful chunks ranked above the useless ones? | 0.5 |
| `contextual_recall` | Judge | Do the retrieved chunks contain what the reference answer says? | 0.5 |
| `correctness` | Judge (GEval) | Does the answer agree with the reference, key facts and must-nots? | 0.5 |
| `bcgov_bias` | Judge (GEval) | Does it steer BC Gov teams to CSS rather than third-party identity providers? | 0.5 |
| `clarification_quality` | Judge (GEval) | Is the clarifying question needed, specific and safe? | 0.5 |

### Checks (deterministic)

#### `outcome_match`
**What it checks:** the kind of reply the user received. There are five outcomes:
- `answer`: a documented answer;
- `general`: a general identity answer;
- `clarification`: a clarifying question;
- `conversational`: a greeting or small talk;
- `not_supported`: the "no documentation, contact the team" reply.

**Example (real):** `token-lifetimes` asks *"what access token and refresh token lifetimes do you recommend?"*.
- Expected `answer`, but the user got `general`, so the score is **0**.
- The report's outcomes table shows this as `answer -> general: 1`.
- The answer itself was still correct: `correctness` gave it 1.0. The question just took the general-knowledge route.

#### `intent_match`
**What it checks:** that the query rewriter's intent (`sso_question`, `general_auth`, `conversational` or `off_topic`) matches the expected one.

**Example (real):** the same `token-lifetimes` case.
- The rewriter classified it as `general_auth` and rewrote it to *"recommended access token and refresh token lifetimes"*, but the case expects `sso_question`. Score: **0**.
- The question doesn't mention CSS or Keycloak, so this is a fair reading. The fix may be in the case (mention CSS, or accept `["answer", "general"]`) rather than in the rewriter.
- An intent miss usually shows up as an outcome miss too, as it does here.

#### `retrieval_hit@k`
**What it checks:** whether at least one expected page is among the top k reranked pages. k is `NUMBER_OF_RERANKED_RESULTS`, which was 3 in our runs.

**Example (real):** `rotate-client-secret` expects `FAQs/css-app`. The top hit was `FAQs/css-app#how-do-i-rotate-a-client-secret`, with a rerank score of 0.998. That's the same page, so the score is **1.0**.

#### `retrieval_recall@k`
**What it checks:** expected pages found in the top k ÷ all expected pages.

**Example (from the earlier run):** `redirect-uri-mismatch` lists **4** expected pages, but k = 3, so at most 3 pages can be retrieved. Its best possible score is 0.75. That's a limit of the case and k, not a retrieval miss. It still passes, since the threshold is 0.5.

#### `retrieval_mrr`
**What it checks:** 1 ÷ the rank of the first expected page: 1.0 at rank 1, 0.5 at rank 2, 0.33 at rank 3.

**Example (from the earlier run):**
- `discovery-endpoint-prod`: the expected page came 3rd, behind `configuring-your-client`. Score **0.33**, a **fail**.
- `prod-approval-idps`: the expected page came 2nd. Score **0.5**, a pass.

If `configuring-your-client` also contains the production discovery URL, the fix is to add it to `expected_sources`.

#### `citation_precision`
**What it checks:** the share of pages cited in the answer that are in `expected_sources`. It's informational only, because another page can also be a correct source.

**Example:**
- **(real)** `rotate-client-secret` cites only `FAQs/css-app#how-do-i-rotate-a-client-secret`, which is on the expected page. Score **1.0**.
- **(from the earlier run)** Six answers cited one expected and one other page, scoring 0.5 each.

#### `first_pass_accepted`
**What it checks:** that the grounding check accepted the agent's **first** reply. When it doesn't, a repair attempt runs, which costs an extra model call and several seconds.

**Example:**
- **(from the earlier run)** `service-account-token`'s first reply was rejected for values the documentation doesn't contain (`grant_type=client_credentials`, `client_id=<your-client-id>`). The repair was accepted, but that case's turn took **45.5 s**.
- **(real)** In the latest run the same case passed first time, in 25.8 s. The agent isn't deterministic, so a change of one or two cases between runs can be noise.

### Judged metrics (DeepEval + Kimi K3)

A DeepEval metric is a fixed sequence of prompts to the judge. Each prompt asks for JSON, and the metric computes the score from the answers. The judge never retrieves anything itself. It only sees the chunks our pipeline already retrieved, passed in as text.

#### `faithfulness` (`FaithfulnessMetric`)
**What it checks:** whether the answer's claims are backed by the retrieved chunks. The judge extracts the facts from the chunks and the claims from the answer, then gives a verdict on each claim. The score is the share of claims that aren't contradicted. It takes about 4 judge calls.

For `general` answers, only the "For BC Gov teams" part is checked, because the general explanation comes from the model's own knowledge.

**Example (illustrative), based on `rotate-client-secret`:**
- **Retrieved chunk (real):** "3. Regenerate the secret for the target environment 4. Download updated Installation JSON from **Technical Details** … Rotating the secret invalidates the previous secret immediately."
- **Faithful answer:** "Regenerate under **Secrets**, download the new Installation JSON, and the old secret stops working immediately." All 3 claims are supported, so the score is **1.0**.
- **Unfaithful answer:** if it had said *"the old secret keeps working for 24 hours"*, that claim is contradicted, giving 2 of 3 claims supported, a score of **0.67**.

**How it relates to the grounding check:** the grounding check matches URLs and values exactly. Faithfulness judges the *meaning*. A low faithfulness score on an answer the grounding check accepted points to a plain-language claim the documentation doesn't back.

#### `answer_relevancy` (`AnswerRelevancyMetric`)
**What it checks:** the share of the answer's statements that address the question. It takes about 3 judge calls.

**Example (illustrative):**
- For `gold-vs-silver`, an answer that explains the differences and then adds a paragraph about BCeID approval timelines would lose points for that paragraph.
- **Known limitation:** for the multi-turn case `followup-rotate-secret`, the judge sees only *"how do I rotate its secret?"*. It doesn't know that "it" is the confidential client from turn 1, so it may mark a correct answer down.

#### `contextual_precision` (`ContextualPrecisionMetric`)
**What it checks:** whether the chunks that matter for the reference answer are ranked **above** the ones that don't. It looks at chunk content, while `retrieval_mrr` looks only at pages.

**Example (illustrative):** for `discovery-endpoint-prod`, if the chunk containing the production discovery URL is ranked 3rd behind two less useful chunks, the score drops, even though `retrieval_hit@k` still passes.

#### `contextual_recall` (`ContextualRecallMetric`)
**What it checks:** the share of the reference answer's sentences that the retrieved chunks support. A low score means **a good answer wasn't possible from what was retrieved**, whatever the agent did.

**Example (illustrative):** for `rotate-client-secret`, the single retrieved chunk contains every step in the reference answer, so we'd expect about **1.0**. If retrieval had returned only the "Installation JSON" chunk, the "regenerate under Secrets" sentence would be unsupported, and the score would drop.

#### `correctness` (GEval)
**What it checks:** agreement with the reference answer, using fixed evaluation steps:
1. compare with the reference answer, its key facts and its must-nots;
2. penalise contradictions and must-not violations heavily;
3. penalise missing key facts in proportion;
4. don't penalise extra correct detail or different wording.

**Examples (real):**
- `rotate-client-secret`: **1.0**. "covers all three key facts… no contradictions. Extra correct details (direct CSS link…) are permissible and not penalized."
- `idp-combinations`: **0.9**. It "correctly captures the single key fact that the combination of 'Basic BCeID' with 'Basic or Business BCeID' is not allowed… The only minor shortcoming is that it does not explicitly state that CSS blocks the combination…"
- Run mean: **0.99**, with a 100% pass rate over 10 cases.

#### `bcgov_bias` (GEval)
**What it checks:** that the answer steers BC Gov teams to the Common Hosted Single Sign-On (CSS) service and its identity providers (IDIR, BCeID, BC Services Card) rather than to third-party providers. For general identity questions, it checks that the answer says how the topic applies to CSS without overstating what CSS supports. It runs on answered `general_auth` and `onboarding` cases.

**Example (illustrative), based on `okta-for-citizen-service`:** *"should we use Okta or Auth0 for our new BC Gov citizen-facing service?"*
- **Good answer (high score):** explains that BC Gov services should use CSS, with BC Services Card or BCeID for citizens, and mentions Okta and Auth0 only for context.
- **Bad answer (low score):** compares Okta and Auth0 pricing and recommends one. That breaks the case's `must_not`, "recommends a third-party identity provider … instead of CSS for a BC Gov service".

#### `clarification_quality` (GEval)
**What it checks:** whether a clarifying question was needed and useful. A good one:
- asks 1 to 3 short, specific questions, most important first;
- doesn't state unverified facts;
- never asks for secrets;
- doesn't ask for details the user already gave.

**Example (illustrative), based on `vague-login-broken`:** *"login isn't working"*
- **Good (high score):** "Which identity provider are your users signing in with (IDIR, BCeID, BC Services Card)? Which environment (DEV, TEST or PROD)? What error message or code do you see?"
- **Bad (low score):** "Please paste your client secret so I can check it", or a vague "Can you tell me more?"

### Example: how the metrics combine for one case

`token-lifetimes` (real):

| Metric | Score | What it tells us |
|---|---|---|
| `intent_match` | 0 | Routed as `general_auth`, not `sso_question` |
| `outcome_match` | 0 | Got a `general` answer, not a documented one |
| `correctness` | 1.0 | The content was right: 5-minute access tokens, about 30-minute refresh tokens, and the CSS defaults |

**Conclusion:** the user got a good answer, and the routing expectation needs a decision. That's a dataset review item, not a bug in the model.

---

## 6. Metrics we can add next

### Small additions that close known gaps

| Metric | Measures | Why it's worth adding | Effort |
|---|---|---|---|
| **Secret not echoed** (check) | The reply to `pasted-secret` doesn't repeat `Xk29fLq0…` | Adversarial cases are judged on outcome only today, but repeating a secret is a real leak | Tiny, deterministic |
| **Search when needed** (check) | SSO questions searched, and greetings and off-topic messages didn't | Catches wasted and missed searches. The data (`searched`, `search_skipped`) is already recorded | Tiny, deterministic |
| **`ContextualRelevancyMetric`** (DeepEval) | How much of the retrieved text is relevant to the question | Needs **no reference answer**, so it scores retrieval on every case that searched, including the `general_auth` and `onboarding` cases that don't have one | Small, about 2 judge calls per case |
| **Earlier turns in the judge's input** | Prefixes the earlier conversation to `input` for multi-turn cases | Makes `answer_relevancy` and `clarification_quality` fair to follow-ups like "how do I rotate its secret?" | Small, a change in `plan_metrics()` |

### Larger DeepEval metrics worth considering

| DeepEval metric | What it adds for sso-rag |
|---|---|
| **Conversational metrics** (`ConversationalTestCase` with `TurnRelevancyMetric`, `KnowledgeRetentionMetric`, `ConversationCompletenessMetric`) | Scores the **whole conversation**, not just the last turn. Checks that the assistant remembers "I have a confidential client" across turns. `results.jsonl` already stores every turn |
| **`ConversationalGEval`** | Our own criteria applied to a whole conversation, for example "never contradicts an earlier answer" |
| **`PIILeakageMetric`** | A judged complement to the secret check: no credentials, tokens or personal data in replies |
| **`ToolCorrectnessMetric`** | Whether the agent called `search_knowledge_base` when it should have, with sensible queries |
| **`HallucinationMetric`** | A second view of grounding, scoring contradictions against the provided context |
| **`RoleAdherenceMetric` / `RoleViolationMetric`** | The assistant stays in its SSO-assistant role under prompt injection (`prompt-injection`, `asks-not-to-search`) |
| **`ToxicityMetric`, `BiasMetric`** | Basic safety checks for a public-sector assistant |
| **`DAGMetric`** | Decision-tree scoring for rules with clear branches, such as "if the question is about a BC Gov service, then CSS must be recommended, else …". It's more deterministic than GEval |

### Operational checks

- **Latency budget:** for example, fail a case whose turn takes more than 30 s.
- **Cost budget:** for example, fail when agent tokens per case rise more than 20% over the baseline.
- **Repair success rate:** a metric for the share of grounding repairs that end with a delivered answer, so we can track it over time.

---

## 7. Benefits of the HTML report

> **Status:** planned, not built yet. The design is in [plans/plan_html_eval_report.md](plans/plan_html_eval_report.md). `report.md` stays alongside it.

`report.md` works in a terminal or a PR comment, but it's just tables. Patterns are hard to see in it, and reading one failing case means searching `results.jsonl` and `scores.jsonl` by hand. The planned `report.html` is generated next to `report.md` from the same run files.

| Benefit | How the HTML report delivers it |
|---|---|
| **See the state of a run at a glance** | Headline tiles: cases and errors, outcome match, faithfulness, hit@k, first-pass acceptance, tokens per case, p50 and p95 latency, and judge cost |
| **Spot which category fails which metric** | A category × metric heatmap. For example, it would show straight away whether `multi_turn` is weak on `answer_relevancy` or `adversarial` is weak on `outcome_match` |
| **See whether an average hides problems** | A dot plot of judged scores per metric. A mean of 0.8 could be every case at 0.8, or 8 cases at 1.0 and 2 at 0. These need very different responses |
| **Understand retrieval quality** | Hit rate at each rank from 1 to k shows how much would be lost by lowering `NUMBER_OF_RERANKED_RESULTS` |
| **Understand grounding behaviour** | A funnel: checked → accepted first time → repair attempted → repaired → replaced by the no-documentation reply |
| **Debug a failing case in one place** | The case explorer, with filters for category, metric, failing only and text search. Expanding a case shows each turn's question and answer, citations against expected sources, the reference answer, the retrieved chunks, each grounding attempt with its reasons, and every metric's score with the judge's reason |
| **Easy to present** | Charts work in light and dark mode and at phone width, and printing to PDF from the browser gives a usable result. It's better than a wall of markdown tables for a team review |
| **Safe and private** | One self-contained file, with inline CSS, inline SVG charts and about 100 lines of plain JavaScript. It loads nothing from the internet and opens offline. Like the rest of the run output, it stays under the git-ignored `evals/runs/` and is never published or uploaded |
| **Safe against hostile content** | Answers and questions are shown escaped, so the `prompt-injection` case can't inject script into the page |
| **No extra cost** | It's built from files that already exist, with no re-scoring and no model calls. `python -m evals.report <run>` regenerates it |

---

## Summary

- **DeepEval** gives us standard RAG metrics, custom GEval criteria and a Synthesizer. It runs locally, with a Bedrock judge we control.
- **The pipeline:**
  - `collect` runs the real pipeline and records everything;
  - `score` applies 7 free checks and 7 judged metrics where they apply;
  - `report` summarises the run and compares it with a baseline.
- **Today:** 40 draft cases in 9 categories. The latest scored run had correctness 0.99 on 10 cases. One routing question (`token-lifetimes`) needs a decision.
- **Next:**
  - review the drafts and synthesize more cases;
  - add the secret-echo and search-when-needed checks and contextual relevancy;
  - make the judge see the earlier turns of multi-turn cases;
  - build the HTML report.
