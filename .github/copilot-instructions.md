# Copilot Instructions — sso-rag

An agentic RAG assistant for BC Gov SSO (Keycloak / Common Hosted Single
Sign-On "CSS" app) documentation. A Strands agent answers questions by calling a
`search_knowledge_base` tool backed by an AWS Bedrock Knowledge Base, and
streams the answer to a React chat UI.

## Tech stack

- **API:** Python 3.14, FastAPI, Strands Agents (`strands-agents` ≥ 1.61), boto3,
  pydantic-settings, uvicorn. Dependencies are managed with **uv** (`pyproject.toml`, `uv.lock`).
- **LLM / retrieval:** Bedrock model via `BedrockModel` (default Amazon Nova Lite),
  Bedrock Knowledge Base `Retrieve` on an S3 Vectors store, Bedrock `Rerank`.
- **UI:** React 18 + TypeScript, Vite, Tailwind CSS (+ `@tailwindcss/typography`),
  `react-markdown` + `remark-gfm`. Uses **yarn** (`ui/yarn.lock`); served by nginx in Docker.
- **Infra:** Terraform (`terraform/`) — ECS Fargate (API + UI), internal ALB,
  API Gateway REST API (response streaming) via VPC Link v2, ECR, S3, IAM. Deployed by GitHub Actions.
- **Chunker:** standalone script (`chunker/preprocess.py`) that turns the
  `bcgov/sso-docs` Markdown into Bedrock-KB chunks with `.metadata.json` sidecars.

Ignore the `legacy/` folder; it is not part of the application.

## Repository layout

```text
main.py                          FastAPI app: CORS middleware, lifespan (session cleanup loop), routers
app/
  agents/rag_agent.py            BedrockModel, agent storage, create_agent(), session-id helpers, delete_session()
  api/routes/chat.py             POST /chat (SSE stream), DELETE /chat (reset), DELETE /chat/history; session cookie + X-Tab-Id
  api/routes/documents.py        GET /documents/search — raw single-query KB retrieval (debugging)
  api/routes/health.py           GET /health
  common/config.py               Settings (pydantic-settings, env vars / .env)
  common/constants.py            ACRONYMS used for query expansion
  common/logging.py              logging.basicConfig (import for side effect in CLIs)
  jobs/cleanup_sessions.py       CLI: python -m app.jobs.cleanup_sessions [--days N]
  prompts/system_prompt.py       SYSTEM_PROMPT
  retrieval/bedrock_client.py    cached bedrock-agent-runtime client
  retrieval/knowledge_base.py    expand_query, retrieve, rerank, multi_search
  retrieval/models.py            RetrievedChunk
  schemas/                       Pydantic request/response models
  services/chat_service.py       ChatService.stream(), ThinkingFilter, select_citations
  services/session_cleanup.py    deletes sessions inactive > SESSION_RETENTION_DAYS; delete_browser_history()
  tools/knowledge_base_tool.py   @tool search_knowledge_base
ui/src/
  hooks/useChat.ts               fetch + SSE parsing, tab id, new chat / delete history, stream abort
  components/DeleteHistoryDialog.tsx  native <dialog> confirmation for DELETE /chat/history
  components/ChatMessageBubble.tsx  Markdown rendering, citation list
  components/ChatInput.tsx       auto-growing input
  types.ts                       stream chunk / citation / message types
terraform/                       AWS infrastructure
chunker/                         docs preprocessor for the Knowledge Base
.github/workflows/build-push-deploy.yml  build images → terraform apply → ECS rollout
.github/skills/                  fastapi and react skills — follow them for API / UI work
```

Python packages under `app/` are **namespace packages** (no `__init__.py`); don't add them.

## Request flow

1. UI sends `POST /chat` with `{"prompt": "..."}`, header `X-Tab-Id: <uuid>`, and
   the `strands_session` cookie (same-origin, sent automatically).
2. `chat.py` reads the cookie; if missing or not `session_<uuid>`, it creates a new
   id. Storage key = `conversation_id(session_id, tab_id)` →
   `session_<uuid>__<tab uuid>` (or just the session id if no valid tab id).
3. `ChatService.stream()` builds a fresh agent with `create_agent(conversation_id)`
   (in a thread — restoring the snapshot does blocking I/O) and iterates
   `agent.stream_async(question, invocation_state={"citations": [...]}, cancel_signal=...)`.
4. Text deltas pass through `ThinkingFilter` (removes `<thinking>…</thinking>`,
   including tags split across chunks) and are emitted as SSE.
5. After the text, `select_citations()` keeps retrieved sources whose URL appears
   in the answer (whole-URL match, fragment optional), or all retrieved sources if
   none are linked, deduped by URL.
6. The response sets the session cookie on every call (sliding expiry).

### SSE contract (`text/event-stream`)

OpenAI-compatible `chat.completion.chunk` events, in this order:

```text
data: {..."choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}
data: {..."choices":[{"index":0,"delta":{"content":"..."},"finish_reason":null}]}      (repeated)
data: {..."choices":[{"index":0,"delta":{"citations":[{"title","location":{"uri"},"score"}]},"finish_reason":null}]}   (optional)
data: {..."choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

If the agent fails mid-stream, `data: {"error": "..."}` is sent before `[DONE]`
(status is already 200). The UI (`useChat.ts`) depends on this exact shape —
change `chat.py`, `ui/src/types.ts` and `useChat.ts` together.

### `DELETE /chat`

With `X-Tab-Id`: deletes that tab's stored conversation, keeps the cookie.
Without it: deletes the cookie-level conversation and clears the cookie. The UI
calls it on "New chat" and then rotates its tab id.

### `DELETE /chat/history`

Deletes every conversation of the browser session (`session_<uuid>` and all
`session_<uuid>__<tab>` ids, found by listing the `session/` and `context/`
prefixes for that id) and clears the cookie; 204 even when nothing is stored.
On a partial failure: 500 `{"detail"}` and the cookie is kept for a retry. The
ALB listener rule must route `/chat/*` to the API for this path. The UI calls it
from "Delete history" after a confirmation dialog.

## Sessions and storage

- One `Agent` per request — **never share an Agent across requests/users**; it
  holds message history. The `BedrockModel` is module-level and shared.
- History persists via `SnapshotSessionManager(conversation_id, storage=storage)`.
  `storage` is `LocalFileStorage("./.agent-store/")` when `ENVIRONMENT=local`,
  otherwise `S3Storage(AGENT_STORAGE_S3_BUCKET)`. Keys live under
  `session/<id>/…` (snapshots) and `context/<id>/…` (context-manager stash).
- Cookie: `strands_session`, `HttpOnly`, `SameSite=Lax`, `Secure` per
  `SESSION_COOKIE_SECURE` (set `false` for plain-HTTP localhost or the browser drops it).
- Cookie and tab ids are client-controlled and become storage keys: always
  validate with `is_valid_session_id` / `is_valid_tab_id` (strict UUID format)
  before use. Only ids in the format we issue are ever read or deleted.
- Tab id lives in the tab's `sessionStorage` (survives reload, unique per tab;
  duplicated tabs share it until cleared).
- `app/services/session_cleanup.py` deletes conversations whose newest object is
  older than `SESSION_RETENTION_DAYS`. It runs in every API worker via the
  FastAPI lifespan every `SESSION_CLEANUP_INTERVAL_HOURS` (0 disables it; then
  schedule `python -m app.jobs.cleanup_sessions`). Deletes are idempotent.

## Retrieval and the tool

`search_knowledge_base(question, queries, tool_context)` is declared with
`@tool(context=True)`; `tool_context` is injected by Strands and is not visible
to the model. Its docstring **is** the model-facing tool description — edit it
deliberately.

`multi_search` pipeline: acronym expansion (`constants.ACRONYMS`, genuine
abbreviations only) → up to 4 queries retrieved in parallel → dedupe → Bedrock
rerank against the **question** → drop below `MIN_RERANK_SCORE` → note queries
that found nothing. Falls back to vector-score order without a reranker. Returns
numbered text blocks (`[n] title / source: / score: / text`) or a string starting
with `NO_MATCH`. Each call logs one JSON line; use it to tune retrieval settings.

When the tool runs through the agent, it appends `{title, uri, score}` for each
hit to `invocation_state["citations"]` if that list is present.

S3 Vectors does not support `HYBRID` search; keep `SEARCH_TYPE=SEMANTIC`.

## System prompt

`app/prompts/system_prompt.py`: the knowledge base is the only source of truth
for SSO facts; maps user wording to docs vocabulary; asks for markdown links
built from a result's `source:` line (citation selection relies on this). Some
models (Nova) emit `<thinking>` text — handled by `ThinkingFilter`, don't strip
it elsewhere.

## Configuration (`app/common/config.py`)

| Env var | Default | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `sandbox` | `local` → local file storage instead of S3 |
| `AWS_REGION` | `ca-central-1` | |
| `KNOWLEDGE_BASE_ID` | required | |
| `MODEL_ARN` | – | model id = last path segment |
| `RERANKER_MODEL_ARN` | unset | unset → no rerank |
| `NUMBER_OF_RESULTS` | `25` | candidates per tool call |
| `NUMBER_OF_RERANKED_RESULTS` | `6` | kept after rerank (+2 per extra query, max 12) |
| `MIN_RERANK_SCORE` | `0.05` | |
| `SEARCH_TYPE` | `SEMANTIC` | |
| `CORS_ORIGINS` | `["*"]` | credentials allowed only for explicit origins |
| `AGENT_STORAGE_S3_BUCKET` | `sandbox-sso-rag-agent-context-store` | |
| `SESSION_COOKIE_NAME` | `strands_session` | |
| `SESSION_COOKIE_MAX_AGE` | `86400` | seconds |
| `SESSION_COOKIE_SECURE` | `true` | |
| `SESSION_RETENTION_DAYS` | `3` | must be > 0 |
| `SESSION_CLEANUP_INTERVAL_HOURS` | `24` | `0` disables in-process cleanup |

Read settings via `from app.common.config import settings`; never `os.environ`
directly. Secrets never go in source or images — `.env` is local only and is
excluded by `.dockerignore`.

## Coding conventions

### Python / FastAPI
- Follow `.github/skills/fastapi`. Keep routes thin; logic lives in `services/`,
  retrieval in `retrieval/`, tools in `tools/`.
- Don't block the event loop: async routes must push blocking work (boto3,
  storage I/O, agent construction) to `asyncio.to_thread`, or use a sync `def` route.
- Use `logging.getLogger(__name__)`; no `print`. Agents are created with
  `callback_handler=None` so nothing is printed to stdout.
- Type hints everywhere (`str | None`, built-in generics). Comment only non-obvious "why".
- Errors after streaming starts must be reported in-band, not as exceptions.

### UI
- Follow `.github/skills/react`. Function components and hooks; Tailwind classes, no CSS files beyond `index.css`.
- All API calls are same-origin, page-relative paths (`chat`, not `/chat`) with
  `credentials: "same-origin"`; never store the session id in JS (it is HttpOnly).
- Assistant messages render as Markdown (`react-markdown`, no raw HTML); user
  messages stay plain text. Links open in a new tab.
- Type-check/build with `yarn build` (`tsc && vite build`).

## Keep these in sync

- **New API path:** `ui/vite.config.ts` proxy, `ui/nginx.conf` location regex,
  and the ALB listener rule in `terraform/alb.tf` (paths are matched exactly —
  `/chat` does not cover `/chat/x`).
- **New request header or HTTP method from the UI:** CORS in `main.py`
  (`terraform/apigateway.tf` needs no change — the REST API proxies `OPTIONS` to FastAPI).
- **New env var:** `config.py`, the README env table, and the API task
  `environment` list in `terraform/ecs.tf` if it must differ from the default.
- **New AWS call:** API task role in `terraform/iam.tf`.
- **Stream format change:** `chat.py`, `ui/src/types.ts`, `ui/src/hooks/useChat.ts`, README.

## Commands

```bash
# API (from repo root)
uv sync
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000   # docs at /docs
ENVIRONMENT=local SESSION_COOKIE_SECURE=false ...             # local storage + plain-HTTP cookie
uv run python -m app.jobs.cleanup_sessions --days 3

# UI (from ui/) — dev server on :5173 proxies /chat and /health to :8000
yarn install
yarn dev
yarn build

# Docker
docker build --platform linux/amd64 -t sso-rag-api:local .
docker build --platform linux/amd64 -t sso-rag-ui:local ui

# Terraform (from terraform/)
terraform fmt && terraform validate && terraform plan

# Chunker (from chunker/)
python3 preprocess.py --repo ./sso-docs --out ./chunks
aws s3 sync ./chunks s3://<bucket>/sso-docs/ --delete
```

## Testing

There is no automated test suite yet. When adding tests use `pytest`
(`uv run pytest`) with FastAPI's `TestClient`, and mock the agent
(`app.services.chat_service.create_agent`) and boto3 so tests need no AWS
access. For local storage tests, run from a temp directory — `./.agent-store/`
is relative to the working directory and holds real dev sessions. For prompt or
retrieval changes, compare answers and tool log lines on a fixed set of real
questions (including multi-part ones).

## Deployment notes

- Push to `main` (paths: `app/**`, `main.py`, `pyproject.toml`, `uv.lock`,
  `Dockerfile`, `.dockerignore`, `ui/**`, `terraform/**`, the workflow) builds
  both images as `:latest`, runs `terraform apply`, and forces new ECS deployments.
- API image: `python:3.14-slim`, non-root, `uvicorn main:app --workers 2
  --timeout-keep-alive 125` (longer than the 120s ALB idle timeout).
- UI image: nginx on :8080; `API_URL` is substituted into `nginx.conf` at
  container start; proxy buffering is off for SSE.
- API Gateway is a **REST API** with `response_transfer_mode = "STREAM"` on its
  `HTTP_PROXY` integrations — don't switch back to an HTTP API (it buffers SSE).
  Streams can run up to `timeout_milliseconds` (300s). The invoke URL ends in
  `/<environment>/`, so UI assets and API calls must stay relative (`base: "./"`,
  `fetch("chat")`); never use root-absolute paths in the UI.
- Terraform state: S3 backend, region `ca-central-1`. `*.tfvars` are git-ignored.
