# Agentic RAG Platform

An Agentic Retrieval-Augmented Generation (RAG) application built with:

- Python 3.14
- FastAPI
- Strands Agents
- AWS Bedrock
- AWS Bedrock Knowledge Bases
- Amazon S3
- UV Package Manager

The application uses AWS Bedrock Knowledge Bases as the retrieval layer and Strands Agents as the orchestration layer, allowing the LLM to dynamically invoke tools and perform retrieval before generating responses.

---

# Architecture

An interactive, click-through diagram of all components and data flows (chat,
cookie sessions, cleanup, ingestion/deploy, and the proposed MS Teams
integration via MCP) is in
[docs/architecture/architecture.html](docs/architecture/architecture.html), with
a text version in [docs/architecture/architecture.md](docs/architecture/architecture.md).

```text
                   ┌──────────────────────┐
                   │ Source Documents     │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Document Processor   │
                   │ Chunking             │
                   │ Metadata Extraction  │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Amazon S3            │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Bedrock Knowledge    │
                   │ Base (S3 Vectors)    │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Retrieval Tool       │
                   │ (Strands Tool)       │
                   │ multi-query search + │
                   │ Bedrock reranker     │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Strands Agent        │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ FastAPI API          │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Client Applications  │
                   └──────────────────────┘
```

Visit [link](https://htmlpreview.github.io/?https://github.com/bcgov/sso-rag/blob/feat/agentic-rag-init/docs/architecture/architecture.html) to view the architecture diagram

---

# Key Features

## Agentic RAG

Unlike traditional RAG, retrieval is exposed as a tool.

The agent decides:

- When to retrieve
- Whether a question is single-topic (one query) or has independent parts (one query per part)
- Which parts came back empty and need a retry with different wording
- How to formulate the final response

## AWS Bedrock Knowledge Base Integration

Document retrieval leverages:

- Semantic (vector) search over an S3 Vectors store
- Managed embedding generation
- Bedrock reranking of the merged results

> **Note:** Bedrock KB hybrid search (`HYBRID`) is not supported on S3 Vectors,
> so `SEARCH_TYPE` defaults to `SEMANTIC`. Keyword matching for exact strings
> (error messages, client IDs) would need either a store that supports hybrid
> search (e.g. OpenSearch Serverless) or an in-app BM25 step.

## Retrieval Pipeline

Each `search_knowledge_base` call runs the strategy set by `QUERY_STRATEGY`
(`app/services/retrieval_pipeline.py`). The default, `planner_iterative`:

1. **Conversation-aware input** – the tool reads the user's verbatim message
   and the last `PLANNER_HISTORY_TURNS` turns from the agent's messages
   (tool calls, `<thinking>` and grounding blocks removed). The agent's own
   `question` and `queries` are passed along as a draft.
2. **Fast path** – self-contained, single-topic questions that are short or
   anchored by an exact literal (error text, identifier, version) skip the
   planner LLM call and are searched as written plus the agent's queries
   (`query_guards.is_simple_query`).
3. **Query plan** – otherwise the planner (same model as `MODEL_ARN`, temperature
   0, structured output `QueryPlan`) produces a standalone `canonical_query`, 3–4
   complementary `query_variants`, `sub_questions` for multi-part questions, a
   `complexity` label, and optional metadata `filters`. If it fails, the fast
   path is used.
4. **Deterministic guards** (`app/services/query_guards.py`) – the original
   question is kept on the plan; identifiers, quoted error messages, versions,
   URLs and numbers dropped by the rewrite are put back; variants that lost a
   negation are discarded. Filters are kept only for allowed fields
   (`doc_type`, `category`, `tags`, `release_version`) and operators, and only
   when the question itself justifies them; they become a Bedrock
   `RetrievalFilter`. A filtered query that finds nothing is retried unfiltered.
5. **Round 1** – up to 6 queries (canonical, sub-questions, variants, agent
   drafts), each with acronym expansion, retrieved in parallel with
   `CANDIDATES_PER_QUERY` candidates each, merged, deduped and reranked
   together against the canonical query.
6. **Gap check + follow-ups** – for multi-facet/ambiguous plans, or when a
   sub-question found nothing, an LLM `GapAssessment` names missing facets
   (e.g. "registering a second application" when only SSO session docs came
   back) and up to 3 targeted follow-up queries. They are searched, merged into
   the pool, and the union is reranked.
7. **Stop** – after `MAX_RETRIEVAL_ROUNDS` rounds, when
   `RETRIEVAL_TOKEN_BUDGET` or `RETRIEVAL_LATENCY_BUDGET_MS` is spent, when the
   gap check says the evidence is sufficient, or when a round adds fewer than
   `MIN_NEW_EVIDENCE` new chunks to the final reranked set.
8. **Loop guards** – every planner, gap-check and HyDE call has a hard
   timeout and at most 2 attempts; the agent may search at most
   `MAX_SEARCHES_PER_TURN` times and run `AGENT_MAX_TURNS` turns per message.
9. **Coverage note** – sub-questions with no surviving results are named in a
   `NOTE`; if nothing was found and the planner flagged an essential ambiguity,
   the tool returns `CLARIFY: <question>`.

`QUERY_STRATEGY=baseline` restores the previous behaviour (the agent's queries,
one round); `planner` plans but runs a single round. `HYDE_ENABLED=true` adds a
hypothetical documentation passage as an extra retrieval query (never as
evidence); keep it off unless the evals show it helps.

If no reranker is configured, or the reranker request is rejected, results are
ordered by vector score instead.

## Evaluation

`evals/golden.jsonl` is a seed set of 40 questions (tags: `simple`,
`troubleshooting`, `multi_facet`, `follow_up`, `negation`, `version`), each
with conversation `history` and the page URLs that answer it. Review and extend
it as the docs change.

```bash
# Recall@1/5/10, MRR, latency, planner tokens and rounds per strategy and tag
uv run python evals/run_retrieval.py
uv run python evals/run_retrieval.py --strategies baseline planner_iterative --tags multi_facet

# Full agent: LLM-judged faithfulness, grounding rejection rate, cited recall
uv run python evals/run_answers.py --strategies baseline planner_iterative --limit 20
```

Reports and per-question JSONL go to `evals/results/` (git-ignored). Both
need AWS credentials and the usual `.env`. In `run_retrieval.py` the baseline
searches the question verbatim, so it is a lower bound for the production
baseline (where the agent writes the queries).

Unit tests (Bedrock and the LLM are stubbed):

```bash
uv run pytest
```

## Tool-Based Architecture

Current tools:

- `search_knowledge_base`

Future tools can include:

- SharePoint search
- ServiceNow integration
- Jira integration
- Microsoft Graph lookup
- Ticket creation
- User profile lookup

## FastAPI REST API

Provides:

- Streaming chat endpoint (SSE) with per-browser-tab conversation history and cited sources
- Health endpoint
- Document retrieval endpoint

## Configuration-Driven

Application configuration is controlled via environment variables.

---

# Project Structure

```text
sso-rag/
│
├── app/
│   ├── agents/
│   │   ├── bedrock_model.py     # shared Bedrock models (agent + planner)
│   │   ├── planner_agent.py     # planner, gap-check and HyDE agents
│   │   └── rag_agent.py
│   │
│   ├── api/
│   │   └── routes/
│   │       ├── chat.py
│   │       ├── health.py
│   │       └── documents.py
│   │
│   ├── common/
│   │   ├── config.py
│   │   ├── constants.py      # acronym expansions
│   │   └── logging.py
│   │
│   ├── jobs/
│   │   └── cleanup_sessions.py  # one-off session cleanup CLI
│   │
│   ├── prompts/
│   │   └── system_prompt.py
│   │
│   ├── retrieval/
│   │   ├── bedrock_client.py
│   │   ├── iterative.py         # multi-round retrieval, gap checks, budgets
│   │   ├── knowledge_base.py
│   │   └── models.py
│   │
│   ├── services/
│   │   ├── chat_service.py
│   │   ├── grounding.py
│   │   ├── query_guards.py      # literal/negation preservation, filter whitelist, fast path
│   │   ├── query_planner.py     # QueryPlan, GapAssessment and HyDE LLM calls
│   │   ├── retrieval_pipeline.py  # strategy orchestration for search_knowledge_base
│   │   └── session_cleanup.py   # deletes inactive sessions
│   │
│   ├── tools/
│   │   └── knowledge_base_tool.py
│   │
│   └── schemas/
│       ├── chat.py
│       ├── health.py
│       └── query_plan.py        # QueryPlan, GapAssessment
│
├── evals/                    # golden set + retrieval/answer evaluation scripts
├── tests/                    # unit tests (uv run pytest)
├── chunker/                  # docs preprocessor (chunks for the KB)
├── legacy/                   # earlier POCs
├── terraform/                # AWS infrastructure (ECS, ALB, API Gateway, IAM, ECR)
├── ui/                       # chat UI (React + nginx)
├── .github/workflows/
│   └── build-push-deploy.yml # build images, terraform apply, roll out
├── Dockerfile                # API image
├── .dockerignore
├── main.py
├── pyproject.toml
├── uv.lock
└── .env                      # local only, never committed or baked into images
```

---

# Request Flow

## User Query

```text
"How do I get PROD approval for BCeID, and how do I give my service account roles?"
```

## Agent Execution

A fresh agent is created for each request and restored from the
conversation's stored history (session cookie + browser tab id; see
[Agent Configuration](#agent-configuration)).

```text
Agent
  ↓  search_knowledge_base(
  ↓    question="How do I get PROD approval for BCeID and assign roles to a service account?",
  ↓    queries=["BCeID PROD approval", "service account client roles"])
Knowledge Base Tool
  ↓  parallel retrieve → dedupe → rerank against question
Bedrock Knowledge Base + Reranker
  ↓
Ranked chunks with source URLs (+ NOTE for any query with no results)
  ↓
Agent (retries empty queries once, then answers + grounding block)
  ↓
Grounding checks (citations, URLs, code/config values vs retrieved text)
  ↓
Checked answer (SSE) + grounding status + cited sources; snapshot saved
```

## Response

The answer is checked against the retrieved documentation, then sent as
server-sent events with its grounding status and sources (see
[Chat](#chat) for the full format):

```text
data: {... "delta": {"status": "searching"} ...}
data: {... "delta": {"status": "writing"} ...}
data: {... "delta": {"content": "To get PROD approval for BCeID... ([Requesting BCeID in PROD](https://bcgov.github.io/sso-docs/...))"} ...}
data: {... "delta": {"grounding_status": "SUPPORTED"} ...}
data: {... "delta": {"citations": [{"title": "Requesting BCeID in PROD", "location": {"uri": "https://bcgov.github.io/sso-docs/..."}, "score": 0.81}]} ...}
data: {... "delta": {}, "finish_reason": "stop" ...}
data: [DONE]
```

---

# Installation

## Prerequisites

- Python 3.14+
- UV
- AWS Account
- AWS Bedrock Access
- AWS Bedrock Knowledge Base

---

## Install UV

Mac/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify:

```bash
uv --version
```

---

## Clone Repository

```bash
git clone <repository>
cd sso-rag
```

---

## Install Dependencies

```bash
uv sync
```

---

# Environment Variables

Create a `.env` file in the project root.

```env
ENVIRONMENT=local
SESSION_COOKIE_SECURE=false

AWS_REGION=ca-central-1

KNOWLEDGE_BASE_ID=<knowledge-base-id>

MODEL_ARN=arn:aws:bedrock:ca-central-1::foundation-model/amazon.nova-lite-v1:0

RERANKER_MODEL_ARN=<reranker-model-arn>
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `sandbox` | `local` stores conversations in `./.agent-store/`; anything else uses `AGENT_STORAGE_S3_BUCKET` |
| `AWS_REGION` | `ca-central-1` | Region for Bedrock calls |
| `KNOWLEDGE_BASE_ID` | – (required) | Bedrock Knowledge Base to search |
| `MODEL_ARN` | – | Foundation model the agent uses |
| `RERANKER_MODEL_ARN` | unset | Bedrock reranker model; unset = order by vector score |
| `NUMBER_OF_RESULTS` | `25` | Total candidates retrieved per tool call, split across queries (min 10 per query) |
| `NUMBER_OF_RERANKED_RESULTS` | `6` | Results kept after reranking for a single query (+2 per extra query, max 12) |
| `MIN_RERANK_SCORE` | `0.05` | Drop reranked results below this relevance score |
| `SEARCH_TYPE` | `SEMANTIC` | Bedrock `overrideSearchType`; keep `SEMANTIC` on S3 Vectors |
| `QUERY_STRATEGY` | `planner_iterative` | `baseline` (agent's queries, one round), `planner` (planned queries, one round) or `planner_iterative` (planner + gap check + follow-up rounds) |
| `CANDIDATES_PER_QUERY` | `15` | Candidates retrieved per planned query before merging and reranking |
| `MAX_RETRIEVAL_ROUNDS` | `2` | Hard limit on retrieval rounds per tool call |
| `RETRIEVAL_TOKEN_BUDGET` | `6000` | Max tokens for planner, gap-check and HyDE calls per tool call |
| `RETRIEVAL_LATENCY_BUDGET_MS` | `12000` | No new round starts after this much time |
| `MIN_NEW_EVIDENCE` | `1` | Stop when a round adds fewer new chunks than this to the reranked results |
| `PLANNER_HISTORY_TURNS` | `6` | Prior conversation messages given to the planner |
| `HYDE_ENABLED` | `false` | Add a hypothetical-answer passage as an extra retrieval query |
| `AGENT_MAX_TURNS` | `6` | Max agent loop turns (model call + its tool calls) per chat message; a reply cut off by this fails grounding and the no-documentation reply is sent |
| `MAX_SEARCHES_PER_TURN` | `3` | Max `search_knowledge_base` calls per chat message; further calls return `SEARCH_LIMIT` |
| `PLANNER_CALL_TIMEOUT_MS` | `8000` | Hard timeout per planner / gap-check / HyDE call (also bounded by the remaining latency budget); on timeout the search falls back |
| `MODEL_MAX_ATTEMPTS` | `3` | Attempts for the agent's model calls on throttling (backoff 2–10s) |
| `BEDROCK_READ_TIMEOUT_S` | `60` | Read timeout for the agent's Bedrock model client |
| `CORS_ORIGINS` | `["*"]` | Allowed origins for the UI (answers `OPTIONS` preflight). Cookies are only allowed cross-origin when this lists explicit origins, e.g. `["https://ui.example.gov.bc.ca"]` |
| `AGENT_STORAGE_S3_BUCKET` | `sandbox-sso-rag-agent-context-store` | Bucket for conversation history when `ENVIRONMENT` is not `local` |
| `SESSION_COOKIE_NAME` | `strands_session` | Name of the session cookie |
| `SESSION_COOKIE_MAX_AGE` | `86400` | Session cookie lifetime in seconds (refreshed on every chat request) |
| `SESSION_COOKIE_SECURE` | `true` | Send the cookie over HTTPS only; set `false` for plain-HTTP localhost |
| `SESSION_RETENTION_DAYS` | `3` | Delete stored conversations with no activity for longer than this many days |
| `SESSION_CLEANUP_INTERVAL_HOURS` | `24` | How often the API runs the session cleanup; `0` disables the in-process job |

---

# AWS Authentication

The application uses the standard AWS credential provider chain.

Supported authentication methods:

## AWS CLI Profile

```bash
aws configure
```

## Environment Variables

```bash
export AWS_ACCESS_KEY_ID=xxxx
export AWS_SECRET_ACCESS_KEY=xxxx
```

## IAM Roles

Recommended for:

- ECS
- EKS
- EC2
- Lambda

No credentials should ever be hardcoded.

---

# Available Endpoints

## Health Check

```http
GET /health
```

Response:

```json
{
  "status": "ok"
}
```

---

## Chat

```http
POST /chat
```

Request:

```json
{
  "prompt": "What is the MFA policy?"
}
```

The response is a `text/event-stream` of OpenAI-compatible
`chat.completion.chunk` events, ending with `data: [DONE]`:

```text
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"status": "searching"}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"status": "writing"}, "finish_reason": null}], …}
: keepalive
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "MFA is…"}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"grounding_status": "SUPPORTED"}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"citations": [{"title": "…", "location": {"uri": "https://…"}, "score": 0.82}]}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], …}
data: [DONE]
```

- `status` chunks report progress while the answer is prepared: `searching`
  (a knowledge base search is running) and `writing` (the model is writing
  the answer). `: keepalive` comments are sent every 10s of silence.
- The answer is buffered and checked before it is sent (see
  [Grounding checks](#grounding-checks)), so it arrives as a single
  `content` chunk.
- `grounding_status` is `SUPPORTED`, `PARTIALLY_SUPPORTED` or
  `NOT_SUPPORTED`; it is omitted for greetings and clarifying questions.
- `citations` lists the sources the answer cites or links, deduplicated by
  URL; it is omitted when there are none.

### Grounding checks

The system prompt makes the model end every reply with a block the user
never sees:

```text
<grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", "citations": ["https://bcgov.github.io/sso-docs/..."]}</grounding>
```

`response_type` is `answer`, `clarification` or `conversational`.
`app/services/grounding.py` strips the block and compares the reply with the
results `search_knowledge_base` returned in this turn. The reply is replaced
with `NO_DOCUMENTATION_RESPONSE` (`app/common/constants.py`: the "documentation
does not appear to cover this scenario" text with the Teams and email
contacts) when:

- `grounding_status` is `NOT_SUPPORTED`;
- `grounding_status` is `SUPPORTED` but `citations` is empty;
- a citation URL was not returned by the retrieval tool;
- retrieval returned no results but the reply contains factual guidance (an
  `answer`, or any URL, code or endpoint path);
- the reply contains a URL, endpoint path, inline code (roles, claims,
  configuration keys and values) or quoted string/path in a code block that
  appears neither in the retrieved text nor in the user's message
  (`<placeholders>` are ignored);
- the grounding block is missing or malformed.

Every replaced reply is logged as a warning with its cause:

- `answer_rejected` – the checks above rejected it; `reasons` says why.
- `answer_not_supported` – the model itself labelled it `NOT_SUPPORTED`.

Both include `model_status`, `model_citations`, the agent's `stop_reason`
(e.g. `limit_turns`), how many results retrieval returned with their
`top_score` and `sources`, and a `reply_preview` of the model's text. A
high `top_score` on `answer_not_supported` usually means the model was too
cautious rather than the docs lacking the answer. The stored conversation
keeps the delivered reply rather than the rejected one.

The checks are exact string matches, not a semantic judge: plain-prose
statements aren't verified. The prompt asks the model to write every role,
claim, configuration value and endpoint as `code`, so those are checked.

If the agent fails after streaming has started, a `data: {"error": "…"}`
event is sent before `[DONE]`.

Conversation history is kept per session. The response sets an `HttpOnly`
`strands_session` cookie (name, lifetime and `Secure` flag via
`SESSION_COOKIE_NAME`, `SESSION_COOKIE_MAX_AGE`, `SESSION_COOKIE_SECURE`);
send it back to continue the conversation. A missing or malformed cookie
starts a new session.

The cookie is shared by every tab in a browser, so the UI also sends an
`X-Tab-Id: <uuid>` header (kept in the tab's `sessionStorage`). History is
stored per cookie + tab id, so each tab has its own conversation; without the
header, the cookie alone is used.

```http
DELETE /chat
```

With `X-Tab-Id`, deletes that tab's stored history and keeps the cookie (other
tabs still use it). Without it, deletes the cookie-level history and clears
the cookie.

```http
DELETE /chat/history
```

Deletes all of the browser's stored history — the cookie-level conversation
and every tab's — and clears the cookie, so the next `/chat` call starts a new
session. Returns `204` (also when nothing is stored). If some conversations
can't be deleted it returns `500` with `{"detail": "..."}` and keeps the
cookie so the request can be retried. The UI's **Delete history** button calls
it after a confirmation dialog.

Conversations are kept in the agent storage (`.agent-store/` locally, the
`AGENT_STORAGE_S3_BUCKET` bucket otherwise) under `session/<id>/` and
`context/<id>/`. A background job in the API deletes every conversation whose
newest object is older than `SESSION_RETENTION_DAYS` (default 3), running
shortly after startup and then every `SESSION_CLEANUP_INTERVAL_HOURS`
(default 24). Each API worker runs it; deletes are idempotent. Only ids in
the format the API issues are touched.

To run it from an external scheduler instead, set
`SESSION_CLEANUP_INTERVAL_HOURS=0` and run:

```bash
python -m app.jobs.cleanup_sessions            # uses SESSION_RETENTION_DAYS
python -m app.jobs.cleanup_sessions --days 30
```

---

## Direct Document Retrieval

Useful for testing retrieval independently of the agent. Returns raw vector
search results for a single query, without acronym expansion or reranking.

```http
GET /documents/search?query=mfa
```

---

# Agent Configuration

The primary agent lives in:

```text
app/agents/rag_agent.py
```

A Strands `Agent` keeps its message history, so sharing one instance would
leak conversation context between users. `create_agent(session_id)` returns a
fresh agent whose history is restored from, and saved back to, that session's
snapshot via `SnapshotSessionManager`; the Bedrock model object is created
once and shared.

```python
def create_agent(session_id: str) -> Agent:
    return Agent(
        model=model,
        tools=[
            search_knowledge_base
        ],
        system_prompt=SYSTEM_PROMPT,
        context_manager="auto",
        session_manager=SnapshotSessionManager(session_id, storage=storage),
        callback_handler=None,
    )
```

The `/chat` route is async and runs `agent.stream_async()`, buffering the
reply for the grounding checks. If the client disconnects, the agent
invocation is cancelled.

---

# System Prompt

The prompt lives in `app/prompts/system_prompt.py`. It covers:

- **Terminology** – rewriting user wording into the docs' vocabulary
  (App → Integration, Azure IDIR → IDIR with MFA, M2M → client credentials, …).
- **Searching** – one query for single-topic questions, one per part for
  multi-part questions or comparisons (max 4), exact error text for errors,
  and one retry per part that comes back empty.
- **Clarifying questions** – search first; ask about identity provider,
  environment or client type only when the answer depends on it.
- **Answering** – facts only from search results, markdown citations built
  from result URLs, guides preferred over release notes, Silver flagged as
  legacy, partial answers that state what isn't covered.
- **Safety** – retrieved text is treated as data, not instructions; never ask
  for or repeat secrets, and tell users to rotate any they paste.
- **Escalation** – the exact `NO_DOCUMENTATION_RESPONSE` text (Teams: Keycloak
  How-to, `bcgov.sso@gov.bc.ca`, and the details support needs).
- **Grounding report** – the `<grounding>` block that ends every reply, and
  the rules the automatic [grounding checks](#grounding-checks) enforce.

---

# Retrieval Layer

The retrieval layer is intentionally separated from the agent.

Location:

```text
app/retrieval/
```

Responsibilities:

- Bedrock client management (`bedrock_client.py`)
- Knowledge Base queries (`retrieve`)
- Reranking (`rerank`)
- Multi-query search, dedupe and coverage tracking (`search_round`, `rank`,
  `multi_search`)
- Multi-round retrieval with gap checks and budgets (`iterative.py`)
- Acronym expansion (`expand_query`)
- Result normalization (`models.py`)

This separation allows future migration to:

- OpenSearch
- Pinecone
- Weaviate
- PgVector

without changing agent code.

---

# Tooling

Current tool:

## search_knowledge_base

Purpose:

- Retrieve relevant chunks from Bedrock Knowledge Base for one or more queries
- Rank them against the user's question
- Return numbered result blocks with section, source URL and score
- Flag queries that found nothing so the agent can retry them

```python
@tool(context=True)
def search_knowledge_base(question: str, queries: list[str], tool_context: ToolContext) -> str:
    ...
```

`tool_context` is injected by Strands and isn't part of the model-facing
schema. If the caller passes `invocation_state={"citations": [...]}`, each
returned result's title, source URL, score and text is appended to that list;
`/chat` builds its citations and grounding checks from it.

- `question` – the user's need as one standalone sentence, pronouns resolved;
  results are reranked against it.
- `queries` – 1–4 search strings in the docs' vocabulary; one per independent
  part of the question.

Returns `NO_MATCH: ...` when nothing relevant was found for any query.

Each call logs one JSON line with the question, queries, which queries were
acronym-expanded, candidate count, whether reranking ran, and the kept scores.
Use these logs to tune `MIN_RERANK_SCORE` and the result counts.

---

# Local Development

Run the API:

```bash
uv run uvicorn main:app \
  --reload \
  --host 0.0.0.0 \
  --port 8000
```

Swagger:

```text
http://localhost:8000/docs
```

Redoc:

```text
http://localhost:8000/redoc
```

---

# Testing

There is no automated test suite yet. Before changing the prompt or retrieval
settings, run a fixed set of 15–20 real user questions (including multi-part
ones) and compare answers and the tool's log lines.

Once tests are added, run them with:

```bash
uv run pytest
```

---

# Deployment

The API and UI run as two ECS Fargate services behind an internal ALB, exposed
publicly through an API Gateway REST API. Infrastructure is in `terraform/`
and deployed by GitHub Actions.

API Gateway is a **REST API** (not an HTTP API) because only REST APIs can
stream integration responses (`response_transfer_mode = "STREAM"`). An HTTP
API buffers the whole `/chat` SSE response, so the answer would appear all at
once. Streaming needs AWS provider ≥ 6.26.

## AWS Topology

```text
Client
  ↓
API Gateway (REST API, stage /<environment>/, ANY / and /{proxy+}, STREAM)
  ↓  VPC Link v2 → ALB (integration_target)
Internal ALB (port 80, idle timeout 120s)
  ├── /chat, /documents/*, /health  →  API service (Fargate, :8000)
  └── everything else              →  UI service  (Fargate, :8080)
                                         nginx proxies /chat, /health back to the ALB
API service
  ├── Bedrock Knowledge Base (Retrieve)
  ├── Bedrock Rerank
  ├── Bedrock agent model (ConverseStream)
  └── S3 agent storage bucket (conversation history)
```

| Resource | Name | File |
| --- | --- | --- |
| ECS cluster | `sso-rag-cluster` | `ecs.tf` |
| ECS services | `sso-rag-api`, `sso-rag-ui` | `ecs.tf` |
| ECR repositories | `sso-rag-api`, `sso-rag-ui` | `ecr.tf` |
| Internal ALB + target groups | `sso-rag-alb` | `alb.tf` |
| API Gateway REST API + stage + VPC Link v2 | `sso-rag-api-gw`, stage `<environment>` | `apigateway.tf` |
| API Gateway CloudWatch role (account setting, for access logs) | `sso-rag-apigw-cloudwatch` | `apigateway.tf` |
| Agent storage bucket | `<environment>-sso-rag-agent-context-store` | `s3.tf` |
| Task roles | `sso-rag-api-task`, `sso-rag-ui-task` | `iam.tf` |
| Log groups | `/ecs/sso-rag/api`, `/ecs/sso-rag/ui`, `/aws/apigateway/sso-rag` | `ecs.tf`, `apigateway.tf` |

Networking uses an existing VPC: subnets are looked up by their `Name` tag
(`Dev-App-A/B` for the ALB and VPC Link, `Dev-Web-MainTgwAttach-A/B` for the
tasks; see `variables.tf`). The tasks need outbound access to Bedrock, ECR
and CloudWatch.

## API Image

The root `Dockerfile` builds the API image:

- Build stage installs dependencies from `uv.lock` with a pinned uv version.
- Runtime stage is `python:3.14-slim`, runs as a non-root user, and contains
  only the virtualenv, `main.py` and `app/`.
- Starts `uvicorn main:app` with 2 workers on port 8000, with keep-alive (125s)
  longer than the ALB idle timeout to avoid 502s.

`.dockerignore` keeps `.env`, `.venv`, `.git`, `legacy/`, `ui/`, `terraform/`
and `chunker/` out of the build context. Configuration comes from ECS task
environment variables, never from a baked-in `.env`.

Build and run locally:

```bash
docker build --platform linux/amd64 -t sso-rag-api:local .
docker run --rm -p 8000:8000 --env-file .env \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  sso-rag-api:local
```

## CI/CD (GitHub Actions)

`.github/workflows/build-push-deploy.yml` runs on pushes to `main` that touch
`app/`, `main.py`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore`,
`ui/`, `terraform/` or the workflow itself, and on manual dispatch.

Steps:

1. Assume the deploy role via GitHub OIDC.
2. Build and push the API image (repo root) and UI image (`ui/`) to ECR as `:latest`.
3. `terraform init` with the S3 backend, then `plan` and `apply`.
4. Force a new deployment of `sso-rag-api` and `sso-rag-ui` so ECS pulls the
   new `:latest` images, and wait until both services are stable.

Required repository secrets:

| Secret | Purpose |
| --- | --- |
| `TERRAFORM_DEPLOY_ROLE_ARN_SANDBOX` | IAM role assumed by the workflow (Terraform, ECR push, ECS update) |
| `AWS_BEDROCK_KNOWLEDGE_BASE_ID` | Passed to Terraform as `knowledge_base_id` |
| `AWS_ECR_SSO_RAG_API_REPO_URI` | ECR repository URI for the API image |
| `AWS_ECR_SSO_RAG_UI_REPO_URI` | ECR repository URI for the UI image |

Terraform state is stored in the `sso-rag-tfstate` S3 bucket (key `tfstate`,
region `ca-central-1`).

## Rollout and Rollback

- Changes to task configuration (env vars, CPU/memory) roll out on
  `terraform apply`, since the API service tracks its task definition.
- New images roll out through the workflow's force-new-deployment step.
- The API service has the ECS deployment circuit breaker enabled: if new
  tasks fail the `/health` check, ECS rolls back to the last working
  deployment automatically.

## Terraform Variables

Set in `terraform.auto.tfvars` (git-ignored) or as `TF_VAR_*` environment
variables. See `terraform/terraform.tfvars.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `knowledge_base_id` | – (required) | Bedrock Knowledge Base ID |
| `model_arn` | Nova Lite `ca.` inference profile | Agent model |
| `reranker_model_arn` | `amazon.rerank-v1:0` | Reranker model |
| `number_of_results` | `25` | → `NUMBER_OF_RESULTS` |
| `number_of_reranked_results` | `6` | → `NUMBER_OF_RERANKED_RESULTS` |
| `min_rerank_score` | `0.05` | → `MIN_RERANK_SCORE` |
| `search_type` | `SEMANTIC` | → `SEARCH_TYPE` |
| `api_cpu` / `api_memory` | `512` / `1024` | API task size |
| `ui_cpu` / `ui_memory` | `256` / `512` | UI task size |
| `api_desired_count` / `ui_desired_count` | `1` / `1` | Task counts (ignored after creation, so manual scaling sticks) |
| `api_image` / `ui_image` | ECR `:latest` | Override image URIs |

## Manual Deployment

With AWS credentials for the target account:

```bash
# 1. Build and push images
aws ecr get-login-password --region ca-central-1 \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.ca-central-1.amazonaws.com

docker build --platform linux/amd64 -t <api-repo-uri>:latest .
docker push <api-repo-uri>:latest

docker build --platform linux/amd64 -t <ui-repo-uri>:latest ui
docker push <ui-repo-uri>:latest

# 2. Apply infrastructure
cd terraform
terraform init -backend-config="bucket=sso-rag-tfstate" \
  -backend-config="key=tfstate" -backend-config="region=ca-central-1"
terraform apply -var="knowledge_base_id=<kb-id>"

# 3. Roll out the new images
aws ecs update-service --cluster sso-rag-cluster --service sso-rag-api --force-new-deployment
aws ecs update-service --cluster sso-rag-cluster --service sso-rag-ui --force-new-deployment
aws ecs wait services-stable --cluster sso-rag-cluster --services sso-rag-api sso-rag-ui
```

## Verifying a Deployment

```bash
URL=$(terraform -chdir=terraform output -raw api_gateway_url)   # https://<id>.execute-api.ca-central-1.amazonaws.com/<environment>/

curl "${URL}health"
curl -N -c cookies.txt -b cookies.txt -X POST "${URL}chat" -H "Content-Type: application/json" \
  -d '{"prompt": "How do I request a new integration?"}'
```

Logs: CloudWatch `/ecs/sso-rag/api` (app and tool logs) and
`/aws/apigateway/sso-rag` (access logs).

## Known Limitations

- **Stage path in the URL.** The REST API invoke URL ends in `/<environment>/`.
  The UI is built with a relative base (`base: "./"`) and calls `chat`
  relatively, and `index.html` adds a missing trailing slash. A custom domain
  with a base path mapping would remove the stage segment.
- **Streaming limits.** A streamed response can last up to 5 minutes here
  (`timeout_milliseconds = 300000`; the API Gateway maximum is 15). The idle gap
  between chunks is limited by the ALB idle timeout (120s) and API Gateway's
  5-minute idle timeout for Regional endpoints.
- **No CORS at API Gateway.** `OPTIONS` requests are proxied to FastAPI's
  `CORSMiddleware` (configured via `CORS_ORIGINS`).
- **Account-wide setting.** REST API access logs need a CloudWatch role in the
  account's API Gateway settings; `aws_api_gateway_account` sets it for the
  whole account/region, and destroying it resets that setting.
---

# Security Considerations

## IAM

Follow least-privilege principles.

Required permissions:

```text
bedrock:Retrieve                       # Knowledge Base search (scoped to the KB ARN)
bedrock:InvokeModel                    # agent model and reranker model
bedrock:InvokeModelWithResponseStream  # agent model (ConverseStream)
bedrock:GetInferenceProfile            # cross-region inference profile
bedrock:Rerank                         # reranking merged results
s3:GetObject, s3:PutObject             # conversation history (agent storage bucket)
s3:DeleteObject, s3:ListBucket         # DELETE /chat, DELETE /chat/history, session cleanup
```

These are defined for the API task role in `terraform/iam.tf`. Model
permissions are not pinned to one region because cross-region inference
profiles route to models in other regions; `Rerank` does not support
resource-level permissions.

## Secrets

Store secrets in:

- AWS Secrets Manager
- AWS Parameter Store

Avoid storing secrets in source control.

---

# Future Roadmap

## Memory

- User memory across conversations
- Restoring the visible chat history in the UI after a page reload (history is kept server-side)

## Retrieval

- Keyword (BM25) search fused with vector results for exact-string queries
- Optional `doc_type` / `category` metadata filters in the search tool

## Multi-Agent Workflows

Potential agents:

```text
Router Agent
Policy Agent
RAG Agent
Summarization Agent
Ticket Agent
```

## Additional Tools

```text
SharePoint Tool
Graph API Tool
ServiceNow Tool
Jira Tool
Email Tool
```

## Streaming Responses

Support token streaming from Strands to client applications.

---

# Design Principles

1. Agent orchestration should be separated from retrieval.
2. Retrieval should be independent of the underlying vector store.
3. Tools expose capabilities to the agent.
4. Business logic belongs in services.
5. Routes should remain thin.
6. Configuration should be environment-driven.
7. AWS credentials should never be hard-coded.

---

# Technology Stack

| Component | Technology |
|------------|------------|
| API | FastAPI |
| Agent Framework | Strands Agents |
| LLM | AWS Bedrock |
| RAG | Bedrock Knowledge Base |
| Vector Store | Amazon S3 Vectors |
| Reranking | Bedrock Rerank API |
| Storage | Amazon S3 |
| Dependency Management | UV |
| Validation | Pydantic |
| Deployment | ECS Fargate, ALB, API Gateway (Terraform, GitHub Actions) |
| Language | Python 3.14+ |