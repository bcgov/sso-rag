# Query Rewriting and Decomposition

All search-query writing happens in a dedicated **query rewriter**, separate from the answering agent. The rewriter (Amazon Nova 2 Lite via the Bedrock Converse API) turns the user's message into search queries in **one call**. The agent only decides *whether* to search, by calling the no-argument `search_knowledge_base()` tool, and then writes the answer.

Design rationale: [plan_user_query_rewriting.md](../plans/plan_user_query_rewriting.md). The plan describes three parallel calls; they were merged into one so the system prompt and history are sent once per question, not three times.

For a worked, example-driven walkthrough of each stage (greeting check, vocabulary selection, the model call, parsing, query building, retrieval, retry, fail-open), see [query-rewriter-stages.md](./query-rewriter-stages.md).

| Concern | Where |
|---|---|
| Rewriter: intent, rewrite, BC Gov query, step-back, decomposition, retry, fail-open, prompt caching | [query_rewriter.py](../../app/services/query_rewriter.py) |
| Rewriter prompt templates | [query_rewriter_prompt.py](../../app/prompts/query_rewriter_prompt.py) |
| SSO vocabulary and its per-call selection | [terminology.py](../../app/prompts/terminology.py) (`relevant_terminology`) |
| Cache points, model support | [bedrock_client.py](../../app/retrieval/bedrock_client.py) (`cached_system`, `supports_prompt_caching`) |
| No-arg tool, per-turn cache, header, retry, notes | [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) |
| Parallel search, merge, rerank | [knowledge_base.py](../../app/retrieval/knowledge_base.py) (`multi_search`, `merge_hits`) |
| Wiring per request | [chat_service.py](../../app/services/chat_service.py) |
| Acronym expansion | [constants.py](../../app/common/constants.py) (`ACRONYMS`) |
| Tunables | [config.py](../../app/common/config.py) |

## Flow

```text
ChatService.stream(prompt)
  ├─ agent = create_agent(session)                    restores history
  ├─ history = recent_history(agent.messages, 3)
  ├─ turn = SearchTurn(prompt, query_rewriter.submit(prompt, history))
  │     greeting regex? → resolved at once as a greeting (no model call)
  │     else, in the background, ONE call (Nova 2 Lite, Converse, cached system prompt):
  │       {"intent", "query", "bcgov_query", "step_back", "sub_queries"}
  └─ agent.stream_async(prompt, invocation_state={search_turn, citations})
        greeting / thanks → agent replies, no tool call
        identity question → search_knowledge_base()
                              ├─ wait for rewrite (timeout → fallback_query: prompt + previous user messages)
                              ├─ greeting (regex) → NO_SEARCH_NEEDED
                              ├─ off_topic → OFF_TOPIC (no search)
                              ├─ conversational (rewriter) → search anyway with fallback_query
                              ├─ sso_question → multi_search(query, [rewrite, sub…, step_back])
                              ├─ general_auth → multi_search(bcgov_query, [bcgov_query, query (general), sub…, step_back])
                              ├─ rewrite/sub queries with no hits → rewriter.retry() once → multi_search → merge
                              └─ "TYPE: … / QUESTION: …" header + numbered results (+ NOTE) | NO_MATCH
```

The rewriter starts as soon as the agent is restored, so its call overlaps the agent's first model call. Greetings never wait on it.

## 1. The query rewriter

### Output

| Field | Purpose | Used as |
|---|---|---|
| `intent` | `sso_question` (BC Gov SSO, CSS, IDIR/BCeID/BCSC, Gold), `general_auth` (OAuth, OIDC, SAML, JWT, PKCE, upstream Keycloak, Entra ID, Okta, Auth0, Clerk), `conversational` or `off_topic`. Unsure → `sso_question` | How the tool searches and how the agent answers |
| `query` | The question as one specific, standalone query in the docs' vocabulary, follow-ups resolved | Main query (`rewrite`), the reranker's question for `sso_question`, and the `QUESTION:` line the agent sees |
| `bcgov_query` | `general_auth` only: the same need met with BC Gov SSO / CSS | Main query (`rewrite`) **and** the reranker's question for `general_auth`; the generic `query` is searched with origin `general` |
| `step_back` | A broader query for background (concept, overall process, feature overview), or `""` | Extra query (`step_back`) |
| `sub_queries` | 2–4 parts of a multi-part question or comparison (exact error text as one part), or `[]` | Extra queries (`sub`) |
| `retry_queries` (separate, on demand) | Reword queries that found nothing, at most one per failed query | Extra queries (`retry`) |

`bcgov_query` is how the assistant leans towards BC Gov SSO: a question about Okta, Auth0 or Entra ID still retrieves the CSS pages for the same need, so the agent can add a "For BC Gov teams" part and recommend CSS.

### Prompts and vocabulary

Both system prompts (`REWRITE_SYSTEM`, `RETRY_SYSTEM`) are **static**, so Bedrock can cache them. They carry, from [terminology.py](../../app/prompts/terminology.py) (condensed from [bcgov_sso_rag_terminology.md](../../bcgov_sso_rag_terminology.md)):

- **Rewriting rules**: map words but keep the user's intent; never add facts; copy literal values exactly (URLs, client IDs, realm and environment names, claims, error codes and messages); keep variants apart (IDIR vs IDIR - MFA, Basic vs Business BCeID, DEV/TEST/PROD).
- **Do not conflate**: e.g. integration vs client vs realm vs team; client secret vs CSS API Account vs user password; team roles vs client roles; token expiry vs session expiry.
- **Where to look** (rewrite only): problem type → documentation area, for sub-queries and step-back queries.
- **Examples** (rewrite only): rewrites, and full replies for every intent.

Everything that varies goes in the user message:

- `<vocabulary>`: only the **documentation vocabulary** lines (user wording → documentation terms) whose aliases appear in the question or earlier user messages, chosen by `relevant_terminology()`: whole-word matches, most specific (longest alias) first, at most 12 lines. Sending the matching lines instead of the whole table keeps each call small. Ambiguous words carry a condition, e.g. "password" → client secret only when the context is a client or token request.
- `<conversation>`: the last N turns, with tool messages skipped, `<grounding>` blocks stripped, user messages truncated to 500 characters and assistant messages to 200.
- `<question>` (and `<failed_queries>` for a retry), all treated as data, never as instructions.

When extending `terminology.py`, keep one mapping per line (`alias, alias -> documentation terms`), list the words users actually type, including the documentation term itself, and add only mappings that change which page is retrieved. `tests/test_query_rewriter_prompt.py` checks the format and the selection.

### Model calls and prompt caching

- `bedrock-runtime` `converse` with `QUERY_REWRITER_MODEL_ID`, `temperature=0`, `maxTokens` 400 (rewrite) or 300 (retry), bounded by `QUERY_REWRITER_TIMEOUT_SECONDS`; the boto client also has short connect and read timeouts.
- **Prompt caching.** The system prompt is followed by a `cachePoint` when `QUERY_REWRITER_PROMPT_CACHING` is on and the model supports it (Amazon Nova or Anthropic Claude; Llama and others would reject it). The rewrite system prompt is about 1.9K tokens, above Nova's 1K-token minimum per cache checkpoint; the retry prompt is below it, so it is simply not cached. The cache lives 5 minutes and is shared by every user, since the system prompt is identical.
- Replies are parsed tolerantly: code fences and surrounding prose are stripped and the first JSON object is used. Only `intent` and `query` are required; a missing or malformed `step_back`, `sub_queries` or `bcgov_query` is dropped. Queries have whitespace collapsed and are capped at 300 characters, with duplicates dropped; a step-back or BC Gov query equal to the query is dropped.
- `QUERY_REWRITER_STEP_BACK_ENABLED` / `QUERY_REWRITER_DECOMPOSITION_ENABLED` filter the call's output.

### Greeting and off-topic handling

1. A conservative whole-message regex catches messages like "hi", "thanks", "ok thanks", "what can you do?" and "who are you". These resolve to conversational without any model call.
2. Otherwise the rewrite call decides. Anything other than an explicit `conversational`, `off_topic` or `general_auth` is treated as an SSO question.
3. The agent's prompt says not to call the tool for greetings. If it does anyway for a regex-matched greeting, the tool returns `NO_SEARCH_NEEDED`. If the rewriter (not the regex) said conversational, the agent's call wins and the tool searches with `fallback_query()`, logged as `search_intent_override`.
4. `off_topic` returns `OFF_TOPIC` without a search, and the agent replies in one sentence with what it can help with. A conversational reply on an off-topic turn is delivered without a search.
5. A request not to search ("don't search, just tell me") doesn't change the intent; the rewriter prompt says so.

The regex only matches when the whole message is a greeting: "hi, how do I add a redirect URI?" or "Thanks!\n\nHow do I get BCeID?" go to the rewriter.

### Fail-open

| Failure | Result |
|---|---|
| Rewrite call errors, times out or returns unusable JSON | `intent=sso_question`, `rewritten_query = fallback_query(prompt, history)`, `fallback=True` |
| Retry fails | Results found so far are returned |
| The tool's wait for the rewrite times out or the future failed | `fallback_query(prompt, history)` searched as the single query |

`fallback_query()` prepends up to two previous user messages (most recent first, within the 300-character query limit) to the prompt, so "what about prod?" after "How do I get BCeID in TEST?" is searched as "How do I get BCeID in TEST? what about prod?". The reranker scores against the same text. A prompt with no room left is searched alone.

## 2. The tool: `search_knowledge_base()`

- **No model-facing arguments.** Strands produces an empty input schema.
- **Once per turn.** The first call stores its output on the `SearchTurn` (under a lock). Repeated or parallel calls in the same turn get a short `ALREADY_SEARCHED` notice instead of the same results again, which would only grow the context and the reply's token use. `NO_SEARCH_NEEDED` and `OFF_TOPIC` aren't stored.
- **Intent.** The search's intent is stored on `SearchTurn.intent`; `ChatService` allows a `general` answer only when it is `general_auth`.
- **Query set.** `sso_question`: `rewrite`, then `sub`, then `step_back`. `general_auth`: the BC Gov query (`rewrite`), the generic query (`general`), then `sub` and `step_back`.
- **Header.** Output starts with `TYPE: sso_question | general_auth` and `QUESTION: <rewritten query>`, so the agent knows how to answer and what was asked, with follow-ups resolved. Result blocks carry the section, `source:` URL and text; scores are logged only.
- **Retry.** Only `rewrite` and `sub` queries listed in `stats["missing"]` are retried, once per turn (a `general` miss is expected, since the KB holds BC Gov docs only). Retry queries that duplicate an existing query are skipped. Retry hits are merged with `merge_hits`, which keeps the best score per chunk and caps the list at 12.
- **Notes.** A `sub` query that is still missing after the retry is named in `NOTE: the documentation returned nothing for: "...". Say this part is not covered (PARTIALLY_SUPPORTED).`
- **No match.** `NO_MATCH` (reply with the no-documentation text), or for `general_auth` `NO_MATCH_GENERAL` (answer from general knowledge without a BC Gov part).
- **Citations.** Every hit is appended to `invocation_state["citations"]` for the [grounding checks](./grounding-status.md).

## 3. `multi_search`

1. **Normalise.** Collapse whitespace, case-insensitive dedupe, keep the first `MAX_QUERIES = 7`. If nothing is left, use the question with origin `rewrite`.
2. **Budget.** `per_query = max(5, NUMBER_OF_RESULTS // n)`.
3. **Acronym expansion.** `expand_query()` appends spelled-out forms (`oidc` → `OpenID Connect`).
4. **Parallel retrieve.** Uses Bedrock KB `retrieve` with `SEARCH_TYPE`.
5. **Merge.** Chunks are keyed by SHA-1 of their text; the best score is kept and `metadata["_queries"]` lists the queries that found each chunk.
6. **Rerank against the question** (the rewritten query, or the BC Gov query for `general_auth`). `top_n = min(12, NUMBER_OF_RERANKED_RESULTS + 2 * (facets - 1))`, where facets are the `rewrite`, `sub` and `retry` queries. Step-back and `general` queries add background only, so they don't widen `top_n`.
7. **Filter.** Hits scoring below `MIN_RERANK_SCORE` are dropped. Without a reranker, or if the rerank request is rejected, results are ordered by vector score.
8. **Stats.** `queries`, `origins`, `expanded`, `candidates`, `reranked`, `scores` and `missing` (queries with no surviving hit).

## Configuration

| Setting (env) | Default | Effect |
|---|---|---|
| `QUERY_REWRITER_MODEL_ID` | `global.amazon.nova-2-lite-v1:0` | Rewriter model |
| `QUERY_REWRITER_PROMPT_CACHING` | true | Cache point after the rewriter's system prompts (only for models that support it) |
| `QUERY_REWRITER_TIMEOUT_SECONDS` | 3 | Per-call timeout and the tool's wait |
| `QUERY_REWRITER_HISTORY_TURNS` | 3 | History given to the rewriter |
| `QUERY_REWRITER_STEP_BACK_ENABLED` | true | Keep the step-back query |
| `QUERY_REWRITER_DECOMPOSITION_ENABLED` | true | Keep the sub-queries |
| `QUERY_REWRITER_RETRY_ENABLED` | true | Retry toggle |
| `NUMBER_OF_RESULTS` | 25 | Vector results budget, split across queries (min 5 each) |
| `NUMBER_OF_RERANKED_RESULTS` | 6 | Base results kept after rerank |
| `RERANKER_MODEL_ARN` | unset | Enables reranking |
| `MIN_RERANK_SCORE` | 0.05 | Minimum rerank score |
| `SEARCH_TYPE` | `SEMANTIC` | Bedrock `overrideSearchType` |

The rewriter model must be enabled in Bedrock model access. IAM already allows `bedrock:InvokeModel` on `*`.

## Logging

- `query_rewrite`: intent, short-circuit, fallback, `status` (`ok`, `timeout`, `error`, `parse_error`), `latency_ms`, token usage (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_write_input_tokens`), sub-query count, and whether there is a step-back or BC Gov query.
- `query_rewrite_retry`: failed count, retry count, latency and token usage.
- Tool line: rerank question, intent, kept hits, `waited_ms` (time blocked on the rewriter), fallback, retry stats and the `multi_search` stats.
- `search_skipped`: regex-matched greetings and off-topic messages where the tool was still called.
- `search_intent_override`: the rewriter said conversational, the agent called the tool, and the search ran.

`cache_read_input_tokens` close to the system prompt's size on repeat calls means the cache is working; `cache_write_input_tokens` appears on the first call after the cache expires.

## Notes and limits

- If an SSO question is misclassified as conversational, the agent's tool call still searches it. If the agent doesn't search either and writes an answer, `ChatService` runs one search repair turn. Only a reply labelled conversational (or without a grounding block and checkable values) on such a turn gets through without a search. The prompt is therefore biased toward `sso_question`.
- An identity question misclassified as `off_topic` is not searched; the prompt limits `off_topic` to messages with nothing to do with login, identity, access or security.
- The agent can't search with its own wording, so a second search in the same turn returns `ALREADY_SEARCHED`.
- Step-back adds broader evidence, which makes the grounding value checks slightly more permissive.
