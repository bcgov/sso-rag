# Query Decomposition

Query decomposition splits a multi-part user question into several focused search queries, runs them in parallel against the Bedrock Knowledge Base, and merges the results into one reranked set. There is no separate decomposition service: the LLM decides how to split the question (guided by the system prompt) and the retrieval layer executes the split.

| Concern | Where |
|---|---|
| When and how to split (LLM behaviour) | [system_prompt.py](../../app/prompts/system_prompt.py) (`SEARCHING`, `TERMINOLOGY`) |
| Tool contract the LLM calls | [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) (`search_knowledge_base`) |
| Parallel search, merge, rerank | [knowledge_base.py](../../app/retrieval/knowledge_base.py) (`multi_search`) |
| Acronym expansion | [constants.py](../../app/common/constants.py) (`ACRONYMS`) |
| Tunables | [config.py](../../app/common/config.py) |

## Flow

```text
User question (+ conversation history)
  ↓
Agent (LLM) calls search_knowledge_base(question, queries)
  - question: one standalone sentence, pronouns resolved
  - queries:  1–4 search strings in the docs' vocabulary
  ↓
multi_search()
  1. dedupe / trim queries, cap at MAX_QUERIES (4)
  2. expand_query() – append spelled-out acronyms
  3. retrieve() each query in parallel (ThreadPoolExecutor)
  4. merge by SHA-1 of chunk text, tag chunks with the queries that found them
  5. rerank all candidates against `question`
  6. drop hits below MIN_RERANK_SCORE
  ↓
Numbered result blocks (+ NOTE for queries with no hits) → LLM → answer
```

## 1. Decomposition by the LLM

The agent has one tool, `search_knowledge_base(question, queries)`. The decomposition is the `queries` list:

- **Single-topic question** → one query (the default; "most questions are single-topic").
- **Independent parts or a comparison** → one query per part, all in the same tool call, max 4. Example: "BCeID PROD approval" and "service account client roles".
- **Troubleshooting** → the exact error text, plus the related feature.
- **Rewrite into the docs' vocabulary** before searching (App → Integration, M2M → client credentials, …), including any known identity provider, environment, realm and client type.

`question` is a separate argument: the user's need as one standalone sentence ("what about prod?" → "How do I get PROD approval for my BCeID integration?"). It is **not** decomposed; it is what the reranker scores all results against.

### Retrying

If the tool returns `NO_MATCH`, or a `NOTE: no relevant results for: "<query>"` line, the prompt tells the model to retry only those queries once with broader or different wording. No more than one retry per part.

## 2. Execution in `multi_search`

1. **Normalise queries.** Strip, drop blanks, dedupe (order preserved), keep the first `MAX_QUERIES = 4`. If nothing is left, fall back to `[question]`.
2. **Per-query budget.** `per_query = max(10, NUMBER_OF_RESULTS // len(queries))` (default `NUMBER_OF_RESULTS = 25`).
3. **Acronym expansion.** `expand_query()` appends the spelled-out form of known acronyms (e.g. `oidc` → `OpenID Connect`) when the long form is not already in the query. Only genuine abbreviations are listed in `ACRONYMS`; conceptual mapping stays in the prompt.
4. **Parallel retrieval.** One Bedrock `retrieve` call per query, search type `SEARCH_TYPE` (default `SEMANTIC`).
5. **Merge.** Chunks are keyed by SHA-1 of their text. For a duplicate, the higher vector score wins and the query is appended to the chunk's `metadata["_queries"]`.
6. **Rerank.** Candidates are reranked against `question` (not the sub-queries) with the Bedrock reranker when `RERANKER_MODEL_ARN` is set. `top_n = min(12, NUMBER_OF_RERANKED_RESULTS + 2 * (len(queries) - 1))` (default 6), so each extra facet gets some extra room and one facet cannot crowd out the others.
7. **Filter.** Reranked hits below `MIN_RERANK_SCORE` (default `0.05`) are dropped.
8. **Fallbacks.** With no reranker configured, or if Bedrock rejects the rerank request (`ValidationException`), the top `top_n` candidates by vector score are used.

## 3. Output to the LLM

`search_knowledge_base` returns numbered blocks (`[n] title`, `source:`, `score:`, text) separated by `---`. Using `_queries`, it computes which queries produced no surviving hit and appends a `NOTE` naming them so the model can retry. If nothing survives at all it returns `NO_MATCH: …`.

Every returned chunk is also appended to the `citations` list passed through `invocation_state` (`CITATIONS_KEY`). That list is the evidence used by the [grounding checks](./grounding-status.md).

## Configuration

| Setting (env) | Default | Effect |
|---|---|---|
| `NUMBER_OF_RESULTS` | 25 | Total vector results budget, split across queries (min 10 each) |
| `NUMBER_OF_RERANKED_RESULTS` | 6 | Base number of results kept after rerank |
| `RERANKER_MODEL_ARN` | unset | Enables reranking |
| `MIN_RERANK_SCORE` | 0.05 | Minimum rerank score to keep a hit |
| `SEARCH_TYPE` | `SEMANTIC` | Bedrock `overrideSearchType` |
| `MAX_QUERIES` (code constant) | 4 | Hard cap on sub-queries |

## Logging

Each tool call logs a JSON line: `question`, `kept`, and stats — `queries`, `expanded`, `candidates`, `reranked`, `scores`.

## Notes and limits

- Decomposition quality depends on the LLM following the prompt; the code only enforces the cap of 4, dedupe and the empty-list fallback.
- Because reranking uses the single standalone `question`, sub-queries influence what is *retrieved*, not how it is *ranked*.
