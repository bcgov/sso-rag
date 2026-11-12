# Query Rewriter: Stage-by-Stage Walkthrough

This document walks through [`query_rewriter.py`](../../app/services/query_rewriter.py) one stage at a time, with concrete input/output examples for each. For the full architecture (the tool, `multi_search`, configuration, logging) see [query-decomposition.md](./query-decomposition.md); this document focuses on *what happens, in order, to one question* as it moves through the rewriter.

| File | Role |
|---|---|
| [query_rewriter.py](../../app/services/query_rewriter.py) | Stages below: greeting check, vocabulary selection, the model call, parsing/cleanup, fail-open |
| [query_rewriter_prompt.py](../../app/prompts/query_rewriter_prompt.py) | `REWRITE_SYSTEM` / `RETRY_SYSTEM` prompts, `user_message()` / `retry_message()` |
| [terminology.py](../../app/prompts/terminology.py) | `TERMINOLOGY`, `relevant_terminology()`, rewrite examples |
| [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) | `build_queries()`, `rerank_question()` — turns a `RewrittenQuery` into searches |
| [knowledge_base.py](../../app/retrieval/knowledge_base.py) | `multi_search()` — normalise, expand, retrieve, merge, rerank |

## Pipeline overview

```text
prompt + history
   │
   ▼
Stage 0  is_greeting(prompt)?  ──yes──▶  RewrittenQuery.greeting()  (no model call)
   │ no
   ▼
Stage 1  relevant_terminology(prompt + previous user turns)   → <vocabulary> lines
   ▼
Stage 2  user_message(prompt, history)                        → <vocabulary><conversation><question>
   ▼
Stage 3  Converse call: REWRITE_SYSTEM + user message          → raw JSON text
   ▼
Stage 4  parse_rewrite(text)                                   → RewrittenQuery, or None → fail-open
   ▼
Stage 5  build_queries(rewrite) + rerank_question(rewrite)     → ordered SearchQuery list + rerank target
   ▼
Stage 6  multi_search(question, queries)                       → hits + stats (candidates, missing, scores)
   ▼
Stage 7  retry(question, missing)  (only if stats["missing"])  → reworded queries → multi_search again → merge
   ▼
Stage 8  tool assembles header + result blocks + NOTE           → text handed to the agent
```

The rewriter (stages 0-4) runs in a background thread as soon as the agent is restored (`QueryRewriter.submit`); the tool (stages 5-8) only runs if and when the agent decides to call `search_knowledge_base()`.

## Stage 0 — Greeting short-circuit

`is_greeting()` matches a conservative regex against the **whole** message (`_GREETING_RE` in [query_rewriter.py](../../app/services/query_rewriter.py)). A match skips every other stage — no model call, no vocabulary lookup.

```python
>>> is_greeting("thanks!")
True
>>> is_greeting("thanks, and what about prod?")   # not the whole message
False
```

| Input | Result |
|---|---|
| `"hi"`, `"Good morning"`, `"ok thanks"`, `"who are you"` | `RewrittenQuery(intent="conversational", rewritten_query="", short_circuit=True)` |
| `"hi, how do I add a redirect URI?"` | not a greeting — falls through to Stage 1 |

A short-circuited greeting is logged as `{"event": "query_rewrite", "intent": "conversational", "short_circuit": true}` with `latency_ms=0`.

## Stage 1 — Vocabulary selection

`relevant_terminology()` (in [terminology.py](../../app/prompts/terminology.py)) scans `TERMINOLOGY` for lines whose aliases (the words left of `->`) appear as whole words in the question **and** in earlier user turns (so a follow-up like "what about prod?" still pulls in vocabulary from "BCeID" said two turns ago). Matches are picked longest-alias-first (most specific wins the cut when there are more than `MAX_VOCABULARY_LINES=12`), then the kept lines are put back in **table order** (grouped by topic) for display — the examples below are real output from `relevant_terminology()`, not hand-written.

**Example 1 — several aliases in one question**

```text
question: "Why does my callback fail with invalid redirect_uri in prod?"
```
```
Documentation vocabulary (user wording -> documentation terms):
- live, production, prod -> PROD
- callback, return URL, login callback, redirect URI, redirect_uri -> redirect URI (redirect_uri), valid redirect URIs
```
Note the table order: "Environments" (`prod`) comes before "Redirects and login flow" (`callback`) in `TERMINOLOGY`, so `prod` is listed first even though `callback` appears first in the question. (`"invalid redirect_uri"` doesn't match the separate `invalid redirect, redirect mismatch, URL not whitelisted ->` line — that line's aliases are about the *error*, e.g. "redirect mismatch", not the raw parameter name.)

**Example 2 — vocabulary carried over from an earlier turn**

```text
earlier user turn: "How do I get a Business BCeID integration set up for my backend app?"
current question:  "what about prod?"
```
`user_message()` feeds `relevant_terminology()` the question **plus every previous user turn** (not just the latest one), so:
```
- app, application, project, system, my project -> CSS project / CSS SSO integration
- backend, server-side app, API server -> confidential client, client secret
- BCeID, business login -> Basic BCeID, Business BCeID, or both (Business BCeID for a business login)
- live, production, prod -> PROD
```
Without the earlier turn, `"what about prod?"` alone would only match the last line. This is what lets the rewriter later resolve "prod" as "PROD approval **for the BCeID backend integration**" in Stage 3, not just "PROD" in isolation.

**Example 3 — ambiguous word, condition noted in the mapping**

```text
question: "Where is my client password for my backend app?"
```
```
- app, application, project, system, my project -> CSS project / CSS SSO integration
- client, SSO client -> Keycloak client (CSS Integration)
- client secret, client password, app secret, SSO password, integration secret -> client secret (confidential client), only when the context is a client, integration or token request
- backend, server-side app, API server -> confidential client, client secret
```
"password" alone isn't an alias — only "client password" is — specifically because a bare "password" is ambiguous with a user's login password; the line is also written with a condition ("only when the context is a client, integration or token request") so the rewriter maps it to *client secret* only when that context is present (see `DO_NOT_CONFLATE` in [terminology.py](../../app/prompts/terminology.py)).

**Example 4 — more matches than the 12-line cap**

```text
question: "How do I set up my app with IDIR, BCeID, BC Services Card and GitHub login, configure
           redirect URIs and CORS, get a client secret, check client roles, use a service account
           for my backend, read the aud and iss claims, and request PROD approval for my Custom
           Realm team in Gold?"
```
19 lines match in total; only the 12 most specific (longest alias) survive the cut — here that drops the single-word, less specific `app`/`government login, staff login, IDIR`/`redirect URI`/`CORS`/`aud claim`/`iss claim`/`team` lines in favour of longer, more specific aliases like `client secret, client password, ...` and `prod approval, production approval`:
```
- client, SSO client -> Keycloak client (CSS Integration)
- client secret, client password, app secret, SSO password, integration secret -> client secret (confidential client), only when the context is a client, integration or token request
- backend, server-side app, API server -> confidential client, client secret
- custom realm, custom SSO, dedicated realm, private realm, tenant -> Custom Realm (dedicated realm)
- Gold -> current platform
- BCeID, business login -> Basic BCeID, Business BCeID, or both (Business BCeID for a business login)
- Services Card, BCSC -> BC Services Card (Privacy Zone, PPID)
- GitHub -> GitHub BC Gov or GitHub Public
- prod approval, production approval -> identity-provider production approval / onboarding
- user details missing, email missing, name missing, claims -> token claims, UserInfo endpoint, IdP attribute mappers
- machine-to-machine, M2M, backend-to-backend, token without a user, service account, client credentials -> service account, Client Credentials grant (client credentials flow)
- permissions, access level, user role, roles -> client roles (role-based access control), client_roles claim
```
This is also why IDIR doesn't appear: "IDIR" alone is a shorter, less specific alias than e.g. `prod approval, production approval` or `custom realm, custom SSO, ...`, so it lost the cut in this particular (deliberately overloaded) question.

**Example 5 — nothing matches**

```text
question: "Is this expected behaviour or a bug on your end?"
```
```
(empty — no <vocabulary> lines are sent at all)
```
A question with no SSO-specific wording sends an empty `<vocabulary>` block; the rewriter still runs (it isn't a whole-message greeting, so Stage 0 doesn't short-circuit it either) — it just gets no vocabulary hints for this particular call.

Only the matching lines are ever sent — not the ~90-line full table — which keeps each call small and lets the model see only the mappings relevant to this question.

## Stage 2 — Building the user message

`user_message(question, history)` assembles three XML-ish blocks, all explicitly treated as *data* by the system prompt (never as instructions):

```text
<vocabulary>
- app, application, project, system, my project -> CSS project / CSS SSO integration
- frontend, SPA, single-page app, browser-only, mobile app, React, Angular, Vue -> public client, Authorization Code Flow with PKCE, no client secret
- live, production, prod -> PROD
- callback, return URL, login callback, redirect URI, redirect_uri -> redirect URI (redirect_uri), valid redirect URIs
</vocabulary>
<conversation>
user: How do I set up my React app with CSS?
assistant: You'll need a public client with PKCE. Which environment are you testing in?
</conversation>
<question>
Why does my callback fail with invalid redirect_uri in prod?
</question>
```
Note that `<vocabulary>` here picks up `app`/`React` from the **earlier** user turn too (`relevant_terminology()` is given the whole conversation's user text, not just the current question — same mechanism as Stage 1's Example 2), on top of the current question's `prod`/`callback` matches.

Assistant turns are truncated to 200 characters, user turns to 500; `<grounding>` blocks are stripped from assistant turns first (`split_grounding`). History length is `QUERY_REWRITER_HISTORY_TURNS` (default 3 exchanges).

## Stage 3 — The model call

One `bedrock-runtime` `converse` call with `REWRITE_SYSTEM` as the (cached) system prompt and the Stage 2 message as the single user turn:

```python
inferenceConfig={"temperature": 0, "topP": 0.9, "maxTokens": max_tokens}
```

The system prompt asks for exactly one JSON object:

```json
{"intent": "...", "query": "...", "bcgov_query": "...", "step_back": "...", "sub_queries": []}
```

### Why `temperature=0`

The rewriter's job is to produce a **deterministic, structured** artifact that other code parses and searches with — not prose a user reads — so there is nothing to gain from sampling variety and real cost to it:

- **Reproducibility.** The same question (with the same history) should rewrite to the same queries every time; `temperature=0` makes the model always pick the highest-probability token (greedy decoding) instead of sampling, so retrieval behaviour for a given input is stable across calls and between prod and local testing — important for debugging a bad retrieval ("why did this question find nothing?") and for the eval suite ([evals/](../../evals)) comparing runs.
- **Format discipline.** A single JSON object with specific field names is exactly the kind of rigid, narrow output where sampling only adds risk (stray prose before/after the JSON, a dropped quote, an invented extra field) without adding value; greedy decoding keeps the model on the most-reinforced (closest-to-the-examples) continuation.
- **This is the opposite goal from the answering agent.** The agent itself (`app/agents/rag_agent.py`'s `BedrockModel`) does *not* pin `temperature`, because its output is the free-form answer a person reads — some natural variation in phrasing is fine there. The rewriter's output is consumed by `parse_rewrite()` and `multi_search()`, which have zero tolerance for creative variation.

`topP=0.9` is also set, but with `temperature=0` it's effectively inert: nucleus sampling only restricts which tokens are *eligible* to be sampled, and greedy decoding (temperature 0) doesn't sample at all — it always takes the single most likely next token regardless of `topP`. The code has no comment explaining why both are set; most plausibly it's just Bedrock's conventional `inferenceConfig` pairing carried over unconditionally, harmless here, but it is not doing anything with `temperature=0`.

### Why `maxTokens=400` (and `300` for retry)

`REWRITE_MAX_TOKENS = 400` and `RETRY_MAX_TOKENS = 300` ([query_rewriter.py](../../app/services/query_rewriter.py)) are **output budgets**, not stylistic choices — a ceiling the reply is cut off at if exceeded:

- **Sized to the expected reply, not the model's default.** The rewrite reply is one JSON object with five short string/array fields (`intent`, `query`, `bcgov_query`, `step_back`, up to 4 `sub_queries`), each capped at 300 characters by `MAX_QUERY_CHARS` — comfortably inside 400 tokens even with JSON punctuation overhead. The retry reply is just a `retry_queries` array (one short string per failed query, at most a handful), so 300 tokens is enough. Compare this to the answering agent's `model_max_tokens` (default 4096, [config.py](../../app/common/config.py)) — the rewriter's budget is kept deliberately small because its output is tiny structured data, not a prose answer.
- **A safety valve against runaway output**, not a quality control. If the model ever rambled (extra prose, repeated text, a malformed loop) the reply is truncated at the token limit rather than the call hanging or the response growing unbounded. A reply truncated mid-JSON becomes invalid JSON.
- **What happens if the budget is hit.** A truncated/invalid JSON reply fails `_parse_json()`/`parse_rewrite()`, which returns `None`, `_transform()` then logs `status="parse_error"` and fails open to `RewrittenQuery.raw(prompt, history)` (Stage 9) — the same fallback used for a timeout or a Bedrock error. It's not a crash, just a less-targeted search for that turn.
- **Latency, not just cost.** The rewrite call is a *blocking* step the tool waits on (bounded by `QUERY_REWRITER_TIMEOUT_SECONDS`, default 3s) before it can search at all, so a smaller `maxTokens` also means less time generating tokens the caller doesn't need.

### Worked examples by intent

**`sso_question` — a follow-up resolved from the conversation**

```text
conversation: user said "BCeID in TEST", assistant asked "which environment should I check prod approval for?"
question:     "what about prod?"
```
```json
{"intent": "sso_question", "query": "How do I request PROD approval for a BCeID integration in the CSS app?",
 "bcgov_query": "", "step_back": "identity provider production approval process", "sub_queries": []}
```
The pronoun ("prod?") is resolved using the conversation's topic (BCeID integration) — the rewriter never asks the agent to re-ask the environment.

**`sso_question` — an error message kept verbatim**

```text
question: "Why does my BCeID login in TEST say invalid redirect_uri?"
```
```json
{"intent": "sso_question", "query": "Why does BCeID login in TEST fail with invalid parameter: redirect_uri?",
 "bcgov_query": "", "step_back": "How are redirect URIs configured and validated for CSS app integrations?",
 "sub_queries": []}
```
Literal values (error text here) are copied exactly, per `REWRITING_RULES`; a `step_back` query is added because the question needs background on redirect URI configuration beyond the one error.

**`general_auth` — a non-BC-Gov question gets a BC Gov facet**

```text
question: "How does refresh token rotation work in Auth0 for a React app?"
```
```json
{"intent": "general_auth", "query": "refresh token rotation for a single-page app",
 "bcgov_query": "refresh token and SSO session lifespan for a public client with PKCE in CSS",
 "step_back": "", "sub_queries": []}
```
`bcgov_query` is how the assistant steers towards BC Gov SSO: the CSS-specific facet is searched as the *main* query (see Stage 5) and reranked against, so CSS pages survive even though the question never mentioned CSS; `query` is still searched too, tagged `origin="general"`, purely for background.

**`sso_question` — a multi-part question decomposed into sub-queries**

```text
question: "How do I get BCeID approved for prod and give my service account client roles?"
```
```json
{"intent": "sso_question", "query": "Business BCeID PROD approval and client roles for a service account",
 "bcgov_query": "", "step_back": "",
 "sub_queries": ["Business BCeID PROD approval request", "assign client roles to service account"]}
```
Two independent asks ("get BCeID approved" and "give roles") become two short, self-contained sub-queries, each searched and reranked separately (see Stage 5-6) so neither part crowds out the other in a single combined query.

**`conversational` / `off_topic` — nothing to search**

```json
{"intent": "conversational", "query": "", "bcgov_query": "", "step_back": "", "sub_queries": []}
```
```json
{"intent": "off_topic", "query": "", "bcgov_query": "", "step_back": "", "sub_queries": []}
```
(for `"thanks!"` and `"write me a poem about the ocean"` respectively — both skip `multi_search` entirely; see [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py)'s `_skip_search`).

## Stage 4 — Parsing and cleanup

`parse_rewrite()` is deliberately tolerant — only `intent` and `query` are required:

| Raw model output | Parsed result |
|---|---|
| ` ```json\n{"intent": "sso_question", "query": "How do I  add a redirect URI? "}\n``` ` | code fence stripped, whitespace collapsed → `RewrittenQuery(intent="sso_question", rewritten_query="How do I add a redirect URI?")` |
| `{"intent": "sso_question", "query": "x", "step_back": "x"}` (step-back identical to query) | `step_back_query=None` (adds nothing new, so it's dropped) |
| `{"intent": "sso_question", "query": "x", "sub_queries": ["only one"]}` | `sub_queries=[]` (fewer than 2 parts isn't a real decomposition) |
| `{"intent": "sso_question", "query": "x", "sub_queries": ["q1","q2","Q1","q3","q4","q5"]}` | `sub_queries=["q1","q2","q3","q4"]` (case-insensitive de-dupe, capped at 4) |
| `"not valid json at all"` | `None` → caller falls back (Stage 9) |
| `{"intent": "unknown_value", "query": "x"}` | treated as `sso_question` — unsure defaults towards searching |

Query text is also capped at 300 characters (`MAX_QUERY_CHARS`) after whitespace collapsing.

## Stage 5 — Turning a `RewrittenQuery` into searches

`build_queries()` (in [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py)) orders the facets, main query first:

```python
# sso_question
[SearchQuery("Why does BCeID login in TEST fail with invalid parameter: redirect_uri?", "rewrite"),
 SearchQuery("How are redirect URIs configured and validated for CSS app integrations?", "step_back")]

# general_auth (bcgov_query present)
[SearchQuery("refresh token and SSO session lifespan for a public client with PKCE in CSS", "rewrite"),
 SearchQuery("refresh token rotation for a single-page app", "general")]

# sso_question with sub-queries
[SearchQuery("Business BCeID PROD approval and client roles for a service account", "rewrite"),
 SearchQuery("Business BCeID PROD approval request", "sub"),
 SearchQuery("assign client roles to service account", "sub")]
```

`rerank_question()` picks what every hit is scored against: the **BC Gov** facet for `general_auth` (so CSS results outrank generic ones), otherwise the rewritten query.

### Types of queries (origins)

Every `SearchQuery` the rewriter can produce carries an `origin` tag. The origin decides three things: whether it widens `top_n` in `multi_search` (a **facet**, `FACET_ORIGINS` in [knowledge_base.py](../../app/retrieval/knowledge_base.py)), whether a miss triggers a retry (`RETRYABLE_ORIGINS` in [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py)), and whether an unfixed miss is surfaced to the agent as a `NOTE:`.

| Origin | Comes from | When it exists | Facet (widens `top_n`)? | Retried on a miss? | Reported uncovered in `NOTE:`? |
|---|---|---|---|---|---|
| `rewrite` | `RewrittenQuery.rewritten_query` (or `bcgov_query` when `general_auth` has one) | Always — every call produces one | Yes | Yes | No — a `rewrite` miss (with nothing else surviving) results in `NO_MATCH` instead |
| `general` | `RewrittenQuery.rewritten_query` | Only `general_auth` *and* a `bcgov_query` was returned | No | No — a generic, non-BC-Gov query is *expected* to miss the BC Gov docs | No |
| `sub` | `RewrittenQuery.sub_queries` | Only when the question has 2+ independent parts or is a comparison | Yes | Yes | Yes — named individually in the `NOTE:` line if still missing after retry |
| `step_back` | `RewrittenQuery.step_back_query` | Whenever the main query isn't already broad enough for background (dropped if identical to `query`) | No — it only adds context | No — background is optional, not a requirement | No |
| `retry` | `QueryRewriter.retry()`'s reworded text, one per failed `rewrite`/`sub` query | Only after Stage 6 reports a retryable miss (see Stage 7) | Yes | N/A (it *is* the retry) | N/A |

Two queries are always searched for `general_auth` with a `bcgov_query` (`rewrite`=CSS facet, `general`=the generic facet); every other intent only ever searches one `rewrite` query plus whatever `sub`/`step_back`/`retry` queries exist. `sso_question` never produces a `general` query, and `general_auth` never produces a `bcgov_query`-less `rewrite`-only pair unless `bcgov_query` was empty/dropped (then it falls back to a single `rewrite`-origin query, the generic one).

## Stage 6 — `multi_search`

For the BCeID/redirect_uri example above (2 queries: `rewrite`, `step_back`):

1. **Normalise & dedupe** — both queries kept (distinct text), capped at `MAX_QUERIES=7`.
2. **Budget** — `per_query = max(5, 25 // 2) = 12` vector results per query.
3. **Acronym expansion** (`expand_query`) — e.g. a query containing "OIDC" becomes `"... OIDC (OpenID Connect)"` for the vector search only (`ACRONYMS` in [constants.py](../../app/common/constants.py)); neither example here has an acronym to expand.
4. **Parallel retrieve** — one Bedrock KB `Retrieve` call per query, run concurrently.
5. **Merge** — chunks are keyed by a SHA-1 of their text; a chunk found by both queries keeps its best score and both queries are listed in `metadata["_queries"]`.
6. **Rerank** against the rerank question — only `rewrite`/`sub`/`retry` origins count as facets (`step_back` only adds background), so `facets=1` here, giving `top_n = min(12, 6 + 2*0) = 6`.
7. **Filter** — hits scoring below `MIN_RERANK_SCORE` (default 0.05) are dropped.
8. **Stats** returned:

```json
{"queries": ["Why does BCeID login in TEST fail with invalid parameter: redirect_uri?",
             "How are redirect URIs configured and validated for CSS app integrations?"],
 "origins": ["rewrite", "step_back"],
 "expanded": [],
 "candidates": 17,
 "reranked": true,
 "scores": [0.81, 0.63, 0.44, 0.22, 0.11, 0.07],
 "missing": []}
```

`missing` lists any query whose hits were all filtered out or that found nothing at all — these feed Stage 7.

## Stage 7 — Retry on a miss

### When does retry happen?

Retry is not automatic for every miss — it is gated by several conditions, all checked in `_search()` ([knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py)):

1. **At least one retryable query is missing.** After Stage 6, `stats["missing"]` lists every query with no surviving hit. Only the ones whose `origin` is `rewrite` or `sub` (`RETRYABLE_ORIGINS`) count as `failed`; a missing `step_back` or `general` query is *expected* (background, or a deliberately non-BC-Gov query) and never retried. If `failed` is empty, Stage 7 is skipped entirely — no retry call is made.
2. **`QUERY_REWRITER_RETRY_ENABLED` is true** (default). `QueryRewriter.retry()` returns `[]` immediately otherwise.
3. **The retry call itself succeeds and returns usable queries.** One Converse call (`RETRY_SYSTEM`, `maxTokens=300`) is made with *all* failed queries at once — not one call per miss — and returns one reworded query per failed query, aligned by position (`parse_retry` pads/truncates to `len(failed)`). A model error, timeout or unparseable reply yields `[]`.
4. **The reworded query isn't a duplicate of one already searched.** Any `retry_queries` entry that matches (case-insensitively) a query already in `stats["queries"]` is dropped before searching again — it would just repeat the same miss.

If all of the above hold, the surviving `(failed, retry)` pairs are searched as a **second** `multi_search` call, each tagged `origin="retry"`, and the two hit lists are combined with `merge_hits()`: same chunk text ⇒ same hit, best score and the union of finding queries kept, re-sorted and capped at 12 total.

Retry runs **once per turn** — a retry query that itself finds nothing is not retried again.

### Example

The sub-query `"assign client roles to service account"` finds nothing in Stage 6:

```text
failed = ["assign client roles to service account"]
```

`QueryRewriter.retry()` calls `RETRY_SYSTEM` with `retry_message()`:

```text
<vocabulary>...</vocabulary>
<question>
How do I get BCeID approved for prod and give my service account client roles?
</question>
<failed_queries>
- assign client roles to service account
</failed_queries>
```

A possible reply:

```json
{"retry_queries": ["client role mapping for a service account in Keycloak"]}
```

The reworded query is searched (`origin="retry"`) and its hits are merged into the original set with `merge_hits()` (best score per chunk kept, result capped at 12). If it still finds nothing, the sub-query is reported to the agent as uncovered (Stage 8's `NOTE:` line).

## Stage 8 — Assembling the tool's output

The tool concatenates a header, one block per surviving hit, and an optional `NOTE:` line, separated by `---`:

```text
TYPE: sso_question
QUESTION: How do I get BCeID approved for prod and give my service account client roles?

---

[1] Requesting Production Approval
source: https://developer.gov.bc.ca/docs/default/component/css-docs/.../prod-approval
Once your integration has been tested in TEST, request PROD approval by...

---

[2] Client Roles and Service Accounts
source: https://developer.gov.bc.ca/docs/default/component/css-docs/.../client-roles
To assign a client role to a service account, open the client's Service Account Roles tab...

---

NOTE: the documentation returned nothing for: "assign client roles to service account". Say this part is not covered (PARTIALLY_SUPPORTED).
```

If `hits` is empty after Stage 6-7, the tool instead returns `NO_MATCH` (or `NO_MATCH_GENERAL` for `general_auth`, which tells the agent it may still answer from general knowledge without a "For BC Gov teams" part).

## Stage 9 — Fail-open (when any stage above breaks)

Every failure mode degrades to searching the raw prompt rather than blocking or erroring out:

| Where it fails | What runs instead |
|---|---|
| The Converse call errors, times out (`QUERY_REWRITER_TIMEOUT_SECONDS`, default 3s), or `parse_rewrite` returns `None` | `RewrittenQuery.raw(prompt, history)` → `intent="sso_question"`, `fallback=True` |
| The tool's wait on the rewriter's `Future` times out or the future failed | same: `RewrittenQuery.raw(...)` |
| The retry call fails or returns unusable JSON | `[]` — the hits found so far are kept, nothing is merged in |

`fallback_query()` builds the searched text by prepending up to 2 previous user messages (most recent first) to the prompt, trimmed to fit `MAX_QUERY_CHARS=300`:

```text
history (user turns): ["How do I get BCeID in TEST?"]
prompt:               "what about prod?"
fallback_query():      "How do I get BCeID in TEST? what about prod?"
```

This is also what the reranker scores results against in a fallback, so retrieval still understands "prod" refers to BCeID approval even without a successful rewrite.

## End-to-end example

Putting every stage together for one real exchange:

```text
history:  user: "I'm building a React SPA, how do I set it up with CSS?"
          assistant: "You'll need a public client with PKCE. Which environment, TEST or PROD?"
question: "TEST — and why does Auth0's silent refresh pattern not apply here?"
```

1. **Stage 0** — not a greeting.
2. **Stage 1** — vocabulary lines for "TEST", "SPA"/"public client"/"PKCE", "refresh token" are selected.
3. **Stage 2-3** — the model sees the conversation and replies:
   ```json
   {"intent": "general_auth",
    "query": "Why doesn't Auth0's silent refresh pattern apply to a different OIDC provider?",
    "bcgov_query": "refresh token and SSO session behaviour for a public client with PKCE in CSS TEST",
    "step_back": "",
    "sub_queries": []}
   ```
4. **Stage 4** — parses cleanly; `step_back` dropped (empty), `bcgov_query` kept (differs from `query`).
5. **Stage 5** — `build_queries` → `[rewrite=bcgov_query, general=query]`; `rerank_question` = the `bcgov_query` (CSS stays the measuring stick).
6. **Stage 6** — `multi_search` retrieves for both, reranks against the CSS facet; the CSS-specific chunks about refresh tokens and SSO session timeouts in TEST outrank the generic-OIDC chunk (if any) because the rerank question is CSS-flavoured.
7. **Stage 7** — nothing missing, no retry.
8. **Stage 8** — `TYPE: general_auth` header, result blocks about CSS refresh tokens/session behaviour.
9. The agent answers the general Auth0 question from its own knowledge and adds a "For BC Gov teams" section grounded in the returned CSS chunks.

## See also

- [query-decomposition.md](./query-decomposition.md) — full architecture, configuration table, logging fields and known limits.
- [grounding-status.md](./grounding-status.md) — how the agent's final answer is checked against the chunks these stages returned.
- [query_rewriter_prompt.py](../../app/prompts/query_rewriter_prompt.py) — the exact prompt text, including the model-facing examples quoted above.
