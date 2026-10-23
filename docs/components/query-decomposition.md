# Query Rewriting and Decomposition

All search-query writing happens in a dedicated **query rewriter**, separate from the answering agent. The rewriter (Meta Llama 3 8B Instruct via the Bedrock Converse API) turns the user's message into search queries. The agent only decides *whether* to search, by calling the no-argument `search_knowledge_base()` tool, and then writes the answer.

Design rationale: [plan_user_query_rewriting.md](../plans/plan_user_query_rewriting.md).

| Concern | Where |
|---|---|
| Rewriter: intent, rewrite, step-back, decomposition, retry, fail-open | [query_rewriter.py](../../app/services/query_rewriter.py) |
| Rewriter prompt templates | [query_rewriter_prompt.py](../../app/prompts/query_rewriter_prompt.py) |
| SSO vocabulary used by the templates | [terminology.py](../../app/prompts/terminology.py) |
| No-arg tool, per-turn cache, retry, notes | [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) |
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
  │     greeting regex? → resolved at once as conversational (no model call)
  │     else, in the background, in parallel (Llama 3 8B, Converse):
  │       rewrite + intent → {"intent", "rewritten_query"}
  │       step-back        → {"step_back_query"}
  │       decomposition    → {"sub_queries": [] | 2–4}
  └─ agent.stream_async(prompt, invocation_state={search_turn, citations})
        greeting / thanks → agent replies, no tool call
        SSO question      → search_knowledge_base()
                              ├─ wait for rewrite (timeout → raw prompt)
                              ├─ conversational → NO_SEARCH_NEEDED
                              ├─ multi_search(rewritten_query, [rewrite, sub…, step_back])
                              ├─ rewrite/sub queries with no hits → rewriter.retry() once → multi_search → merge
                              └─ numbered results (+ NOTE for uncovered sub-queries) | NO_MATCH
```

The rewriter starts as soon as the agent is restored, so its calls overlap the agent's first model call. Greetings never wait on it.

## 1. The query rewriter

### Techniques

| Technique | Purpose | Output | Used as |
|---|---|---|---|
| **Rewrite + intent** | Classify the message, and make the query specific, detailed and standalone in the docs' vocabulary, with follow-ups resolved | `{"intent": "sso_question" \| "conversational", "rewritten_query": "..."}` | Main query (`rewrite`) **and** the question the reranker scores against |
| **Step-back** | A broader query for background (concept, overall process, feature overview) | `{"step_back_query": "..."}` | Extra query (`step_back`) |
| **Sub-query decomposition** | Split multi-part questions or comparisons; exact error text as one part | `{"sub_queries": [...]}`: 2–4 items, or `[]` for single-topic | Extra queries (`sub`) |
| **Retry** (on demand) | Reword queries that found nothing | `{"retry_queries": [...]}`, at most one per failed query | Extra queries (`retry`) |

Every template shares a preamble built from [terminology.py](../../app/prompts/terminology.py). That module is condensed from the full guide, [bcgov_sso_rag_terminology.md](../../bcgov_sso_rag_terminology.md), to fit Llama 3 8B's 8K context. The preamble contains:

- **Rewriting rules**: map words but keep the user's intent; never add facts; copy literal values exactly (URLs, client IDs, realm and environment names, claims, error codes and messages); keep variants apart (IDIR vs IDIR - MFA, Basic vs Business BCeID, DEV/TEST/PROD).
- **Documentation vocabulary**: user wording → documentation terms, grouped by area (integrations, clients and credentials, realms, identity providers, environments, redirects and login flow, protocols and tokens, service accounts and APIs, roles and teams). Ambiguous words carry a condition, e.g. "password" → client secret only when the context is a client or token request.
- **Do not conflate**: e.g. integration vs client vs realm vs team; client secret vs CSS API Account vs user password; team roles vs client roles; token expiry vs session expiry.
- **Where to look**: problem type → documentation area, used for sub-queries and step-back queries.

The rewrite template also carries worked rewrite examples, and each template adds:

- the last N conversation turns in `<conversation>`, with tool messages skipped, `<grounding>` blocks stripped and each message truncated to 500 characters;
- the user's message in `<question>`;
- an instruction to treat the tagged content as data, never as instructions.

`tests/test_query_rewriter_prompt.py` keeps each system prompt under 12,000 characters (about 3K tokens) so history and the reply still fit. When extending `terminology.py`, add only wording users actually type that changes which page is retrieved.

### Model calls

- `bedrock-runtime` `converse` with `QUERY_REWRITER_MODEL_ID`, `temperature=0` and `maxTokens` of 200 or 300.
- The three techniques run in parallel. Each is bounded by `QUERY_REWRITER_TIMEOUT_SECONDS`; the boto client also has short connect and read timeouts.
- Replies are parsed tolerantly: code fences and surrounding prose are stripped and the first JSON object is used. Queries have whitespace collapsed and are capped at 300 characters, with duplicates dropped.

### Greeting handling

1. A conservative whole-message regex catches messages like "hi", "thanks", "ok thanks", "what can you do?" and "who are you". These resolve to conversational without any model call.
2. Otherwise the rewrite call decides. Anything other than an explicit `"conversational"` is treated as an SSO question.
3. The agent's prompt says not to call the tool for greetings. If it does anyway, the tool returns `NO_SEARCH_NEEDED`.

### Fail-open

| Failure | Result |
|---|---|
| Rewrite call errors, times out or returns unusable JSON | `intent=sso_question`, `rewritten_query = raw prompt`, `fallback=True` (step-back and sub-queries still used if they succeeded) |
| Step-back or decomposition fails | That technique is dropped |
| Retry fails | Results found so far are returned |
| The tool's wait for the rewrite times out or the future failed | Raw prompt searched as the single query |

## 2. The tool: `search_knowledge_base()`

- **No model-facing arguments.** Strands produces an empty input schema.
- **Per-turn cache.** The first call stores its output on the `SearchTurn` (under a lock), and repeated or parallel calls in the same turn return it.
- **Query set.** `rewrite`, then `sub` queries, then `step_back`.
- **Retry.** Only `rewrite` and `sub` queries listed in `stats["missing"]` are retried, once per turn. Retry queries that duplicate an existing query are skipped. Retry hits are merged with `merge_hits`, which keeps the best score per chunk and caps the list at 12.
- **Notes.** A `sub` query that is still missing after the retry is named in `NOTE: the documentation returned nothing for: "...". Say this part is not covered (PARTIALLY_SUPPORTED).`
- **Citations.** Every hit is appended to `invocation_state["citations"]` for the [grounding checks](./grounding-status.md).

## 3. `multi_search`

1. **Normalise.** Collapse whitespace, case-insensitive dedupe, keep the first `MAX_QUERIES = 6`. If nothing is left, use the question with origin `rewrite`.
2. **Budget.** `per_query = max(5, NUMBER_OF_RESULTS // n)`.
3. **Acronym expansion.** `expand_query()` appends spelled-out forms (`oidc` → `OpenID Connect`).
4. **Parallel retrieve.** Uses Bedrock KB `retrieve` with `SEARCH_TYPE`.
5. **Merge.** Chunks are keyed by SHA-1 of their text; the best score is kept and `metadata["_queries"]` lists the queries that found each chunk.
6. **Rerank against the rewritten question.** `top_n = min(12, NUMBER_OF_RERANKED_RESULTS + 2 * (facets - 1))`, where facets are the `rewrite`, `sub` and `retry` queries. Step-back adds background only, so it doesn't widen `top_n`.
7. **Filter.** Hits scoring below `MIN_RERANK_SCORE` are dropped. Without a reranker, or if the rerank request is rejected, results are ordered by vector score.
8. **Stats.** `queries`, `origins`, `expanded`, `candidates`, `reranked`, `scores` and `missing` (queries with no surviving hit).

## Configuration

| Setting (env) | Default | Effect |
|---|---|---|
| `QUERY_REWRITER_MODEL_ID` | `meta.llama3-8b-instruct-v1:0` | Rewriter model |
| `QUERY_REWRITER_TIMEOUT_SECONDS` | 3 | Per-call timeout and the tool's wait |
| `QUERY_REWRITER_HISTORY_TURNS` | 3 | History given to the rewriter |
| `QUERY_REWRITER_STEP_BACK_ENABLED` | true | Step-back toggle |
| `QUERY_REWRITER_DECOMPOSITION_ENABLED` | true | Decomposition toggle |
| `QUERY_REWRITER_RETRY_ENABLED` | true | Retry toggle |
| `NUMBER_OF_RESULTS` | 25 | Vector results budget, split across queries (min 5 each) |
| `NUMBER_OF_RERANKED_RESULTS` | 6 | Base results kept after rerank |
| `RERANKER_MODEL_ARN` | unset | Enables reranking |
| `MIN_RERANK_SCORE` | 0.05 | Minimum rerank score |
| `SEARCH_TYPE` | `SEMANTIC` | Bedrock `overrideSearchType` |

The rewriter model must be enabled in Bedrock model access. IAM already allows `bedrock:InvokeModel` on `*`.

## Logging

- `query_rewrite`: intent, short-circuit, fallback, per-technique `status` (`ok`, `timeout`, `error`, `parse_error`) and `latency_ms`, sub-query count, and whether there is a step-back query.
- `query_rewrite_retry`: failed count, retry count and latency.
- Tool line: rewritten question, kept hits, `waited_ms` (time blocked on the rewriter), fallback, retry stats and the `multi_search` stats.
- `search_skipped`: conversational turns where the tool was still called.

## Notes and limits

- If an SSO question is misclassified as conversational, there is no retrieval and the grounding checks force the no-documentation reply. The prompt is therefore biased toward `sso_question`.
- The agent can't search with its own wording, so a follow-up search within the same turn returns the cached result.
- Step-back adds broader evidence, which makes the grounding value checks slightly more permissive.
