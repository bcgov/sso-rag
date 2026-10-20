# SSO RAG Assistant — Architecture

Interactive version: open [architecture.html](./architecture.html) in a browser
(`open docs/architecture/architecture.html`). Pick a flow tab, then press Play
or step with ←/→. The **AWS / Local** toggle switches the deployment shape.

Dashed nodes and flow 4 are **proposed** (the Microsoft Teams integration is
not implemented yet). Everything else reflects the current code.

---

## Components

| Component | Role | Tech | Where it runs |
| --- | --- | --- | --- |
| Browser · Chat UI | User | React SPA, `useChat` hook, Markdown rendering | Browser |
| UI service | Edge | nginx serving the built SPA; proxies `/chat`, `/health` to the ALB | ECS Fargate `:8080` (AWS) |
| Vite dev server | Edge | Dev server proxying `/chat`, `/health` to uvicorn | `localhost:5173` (Local) |
| API Gateway | Edge | REST API, stage `/<environment>/`, `ANY /` + `ANY /{proxy+}` HTTP_PROXY integrations with `response_transfer_mode = STREAM`, VPC Link v2 to the ALB | AWS |
| Internal ALB | Edge | Path rules: `/chat`, `/documents/*`, `/health` → API; rest → UI; idle 120s | AWS |
| FastAPI API | API | `POST/DELETE /chat` (SSE), `/health`, `/documents/search`; session cookie + `X-Tab-Id` | ECS Fargate `:8000`, 2 workers / `uvicorn :8000` |
| Strands Agent | Agent | New `Agent` per request, `SnapshotSessionManager`, `ThinkingFilter` | In the API process |
| Bedrock model | LLM | Amazon Nova Lite via `ConverseStream` | Bedrock |
| KB search tool | Tool | `search_knowledge_base` → `multi_search` (acronym expansion, parallel retrieve, dedupe, rerank) | In the API process |
| Bedrock Knowledge Base | Datastore | S3 Vectors index (`SEMANTIC`), Rerank API | Bedrock |
| Agent storage | Datastore | Conversation snapshots under `session/<id>/` and `context/<id>/` | S3 `<env>-sso-rag-agent-context-store` / `./.agent-store/` |
| Session cleanup | Job | FastAPI lifespan loop, or `python -m app.jobs.cleanup_sessions` | Each API worker |
| Docs chunker | Pipeline | `chunker/preprocess.py` → chunks + `.metadata.json` → S3 → KB sync | Manual |
| GitHub Actions | Pipeline | Build/push images to ECR, `terraform apply`, ECS force-new-deployment | On push to `main` |
| Teams bridge *(proposed)* | Job | MCP client; polls the channel, calls `ChatService` in-process, posts replies | Dedicated ECS service `sso-rag-teams-bridge` (desired count 1), API image with a different command |
| Teams MCP server *(proposed)* | Integration | MCP server exposing Teams channel tools over Microsoft Graph | Sidecar container in the bridge task, `localhost:8090` |
| MS Teams channel *(proposed)* | Channel | SSO support channel | Microsoft 365 |

## Modes

| | AWS | Local |
| --- | --- | --- |
| Request path | Browser → API Gateway REST API (`/<stage>/…`) → ALB → API | Browser → Vite dev server → uvicorn |
| UI | nginx UI service on Fargate | Vite dev server |
| Agent storage | `S3Storage` (`AGENT_STORAGE_S3_BUCKET`) | `LocalFileStorage("./.agent-store/")` (`ENVIRONMENT=local`) |
| Cookie | `Secure` | `SESSION_COOKIE_SECURE=false` (plain HTTP) |
| Deploy | GitHub Actions → ECR → ECS | — |

The UI is always same-origin with the API (nginx/Vite proxy), so the browser
sends the session cookie automatically.

---

## Flows

### 1. New chat → session cookie (`POST /chat`)

1. **Browser → API Gateway** (Local: **→ Vite**): `POST /chat` with `{"prompt"}` and `X-Tab-Id: <uuid>` from the tab's `sessionStorage`. There's no cookie yet.
2. **API Gateway → ALB** over VPC Link v2. The REST API strips the stage prefix (`/sandbox/chat` → `/chat`), and its integration streams the response (`STREAM`). `OPTIONS` is proxied to FastAPI's CORS middleware.
3. **ALB → API** via the `/chat` path rule (Local: **Vite → uvicorn**).
4. **API → API**: `strands_session` is missing or invalid, so a new `session_<uuid4>` is created. With a valid tab id, the conversation id becomes `session_<uuid>__<tab uuid>`.
5. **API → Agent**: `create_agent(conversation_id)` builds a fresh agent with `SnapshotSessionManager`.
6. **Agent → Storage**: the agent tries to restore `snapshot_latest.json`. None exists, so history starts empty.
7. **Agent → Bedrock model**: the model receives the system prompt and the user message, and responds with a `search_knowledge_base` tool call.
8. **Agent → KB tool**: the tool runs with `tool_context`. `invocation_state["citations"]` collects sources.
9. **KB tool → Knowledge Base**: acronyms are expanded, queries are retrieved in parallel, results are deduped, then reranked against the question.
10. **Knowledge Base → KB tool**: ranked chunks come back with `section` and `source_url`.
11. **KB tool → Agent**: the model gets numbered result blocks, and `{title, uri, score}` is recorded for citations.
12. **Bedrock model → Agent**: the Markdown answer streams back. `ThinkingFilter` strips `<thinking>` blocks.
13. **Agent → Storage**: the snapshot is saved on `AfterInvocationEvent`.
14. **Agent → API**: text is yielded as it arrives, then the cited sources (`select_citations`).
15. **API → ALB → API Gateway → Browser** (Local: **API → Vite → Browser**): `200 text/event-stream` with `Set-Cookie: strands_session=…; HttpOnly; SameSite=lax; Secure`. The UI renders the Markdown and a Sources list.

### 2. Follow-up in the same tab (cookie + `X-Tab-Id`)

1. **Browser → … → API**: the cookie is sent automatically, with the same `X-Tab-Id`.
2. **API → API**: both ids pass UUID validation and map to the same conversation id. Another tab sends a different id and gets its own history; with no tab id, the cookie-level id is used.
3. **API → Agent**: a fresh agent object again.
4. **Storage → Agent**: the previous turns are restored.
5. **Agent → Bedrock model**: history plus the follow-up. The model turns "what about…" into a standalone search.
6. **Agent → Storage**: the updated snapshot is saved, which also refreshes the conversation's last-modified time used by cleanup.
7. **API → … → Browser**: the answer streams back and the cookie is re-set (sliding 24h expiry).

### 3. Clear chat (`DELETE /chat`)

1. **Browser → … → API**: `DELETE /chat` with the cookie and `X-Tab-Id`.
2. **API → Storage**: `delete_session(conversation_id)` removes `session/<id>/…` and `context/<id>/…`.
3. **API → … → Browser**: `204`. The cookie is kept with `X-Tab-Id`; without it, the cookie is cleared with `Max-Age=0`.
4. **Browser → Browser**: the UI stores a new tab UUID, so the next message starts a new conversation.

### 4. MS Teams channel via MCP *(proposed)*

1. **Teams → Teams**: a member posts a question in the SSO support channel.
2. **Bridge → MCP server**: `tools/call list_channel_messages {team_id, channel_id, since}`, on an interval or triggered by a Graph change notification.
3. **MCP server → Teams (Graph)**: `GET /teams/{team}/channels/{channel}/messages/delta`.
4. **Teams → MCP server**: new messages and replies.
5. **MCP server → Bridge**: structured tool result. The bridge skips its own posts.
6. **Bridge → Bridge**: maps the thread to a conversation id, `session_<uuid5(team:channel:thread)>`. This gives one conversation per thread, and it passes the existing validation without a cookie.
7. **Bridge → Agent**: `ChatService.stream(text, conversation_id)` is called in-process, the same code path as the web UI but without going through API Gateway.
8. **Storage → Agent**: the thread's history is restored.
9. **Agent → KB tool → Knowledge Base → KB tool → Agent**: search, as in flow 1.
10. **Agent → Bedrock model**: the answer is generated.
11. **Agent → Bridge**: the full text and citations are collected, because Teams posts whole messages.
12. **Bridge → MCP server**: `tools/call post_channel_reply {thread_id, html}`, with Markdown converted to Teams HTML plus a Sources list.
13. **MCP server → Teams**: `POST …/messages/{id}/replies`.

Design notes for the proposal:

- **Who calls MCP.** The bridge calls the MCP tools deterministically; the agent is not given a "post to Teams" tool. Reading and posting stay under application control, and the agent stays the same for both channels. An alternative is to give the agent the MCP tools through Strands' `MCPClient`. That's more flexible, but the model would then decide when to post.
- **Graph permissions.** Reading channel messages app-only needs `ChannelMessage.Read.All`, or resource-specific consent per team.
- **Posting needs a different auth approach.** Graph only allows posting channel messages with **delegated** `ChannelMessage.Send`; app-only posting is limited to migration. So the MCP server needs a service account / delegated token, or replies could go through a Teams bot (Bot Framework) or a Workflows webhook instead.
- **Secrets.** Entra app credentials go in AWS Secrets Manager and are read by the MCP server only.
- **Deployment.** Run the bridge as its own ECS Fargate service, for example `sso-rag-teams-bridge`, in the existing cluster and private subnets, with `desired_count = 1`:
  - **Image:** the same API image, so it imports `ChatService` in-process, with a different command, e.g. `python -m app.jobs.teams_bridge`.
  - **MCP server:** a second container in the same task definition (a sidecar), reached on `localhost:8090` over streamable HTTP, so it's never exposed outside the task.
  - **Networking:** no ALB rule or API Gateway route. The task only makes outbound calls to Graph, Bedrock and S3.
  - **IAM:** its task role needs the same Bedrock and agent-storage S3 permissions as the API, plus `secretsmanager:GetSecretValue` for the Entra app secret, which only the MCP container receives.
  - **Why not inside the API service:** every uvicorn worker (2 per task) on every API task would poll and reply, so each message could be answered several times. API scaling and the Teams poller also shouldn't be coupled. If it ever needs more than one task, add a lease (e.g. a conditional write on an S3 or DynamoDB lock object) or switch from polling to Graph change notifications behind a queue.
- **Limits and retention.** Teams replies aren't subject to API Gateway limits, because the bridge calls the agent in-process. Teams conversations are cleaned up by the same retention job, since their ids use the same format.

### 5. Session cleanup (background job)

1. **Cleanup → Cleanup**: the timer fires. It starts with the app after up to 5 minutes of random delay, then runs every `SESSION_CLEANUP_INTERVAL_HOURS` (24).
2. **Cleanup → Storage**: lists `session/` and `context/` with timestamps (S3 `LastModified`, or file mtime locally).
3. **Storage → Cleanup**: the newest write per conversation. Only ids in the format the API issues are considered.
4. **Cleanup → Cleanup**: a conversation expires when its newest write is older than `SESSION_RETENTION_DAYS` (3).
5. **Cleanup → Storage**: `delete_session(id)` for each expired id. Deletes are idempotent, and failures are logged and skipped.

To run it externally instead, set `SESSION_CLEANUP_INTERVAL_HOURS=0` and schedule `python -m app.jobs.cleanup_sessions [--days N]`.

### 6. Docs ingestion & deploy (platform)

1. **Chunker → Chunker**: `python3 preprocess.py --repo ./sso-docs --out ./chunks` writes heading-aware chunks with metadata sidecars.
2. **Chunker → Knowledge Base**: `aws s3 sync` to the docs bucket, then a KB data-source sync (chunking strategy NONE) embeds the chunks into S3 Vectors.
3. **GitHub Actions → GitHub Actions** (AWS only): OIDC role, build and push both images to ECR as `:latest`, then `terraform apply`.
4. **GitHub Actions → API**: force a new deployment. The circuit breaker rolls back if `/health` fails.
5. **GitHub Actions → UI service**: force a new deployment, then wait until both services are stable.

---

## Sessions at a glance

| Piece | Value |
| --- | --- |
| Cookie | `strands_session=session_<uuid4>`; `HttpOnly`, `SameSite=Lax`, `Secure` (configurable), `Max-Age=86400`, re-set on every `/chat` |
| Tab id | `X-Tab-Id: <uuid4>` from `sessionStorage` (per tab, survives reload, rotated on Clear) |
| Storage key | `session_<uuid>__<tab uuid>` (or `session_<uuid>` without a tab id; proposed Teams: `session_<uuid5(thread)>`) |
| Validation | Both ids must be strict UUIDs in the issued format before they touch storage |
| Retention | Deleted after `SESSION_RETENTION_DAYS` (3) of inactivity |

## Workshop scenarios

- **"Why does a second tab start a new conversation?"** Play flow 2, step "Valid cookie → same conversation". The tab id is part of the storage key.
- **"Can JavaScript steal the session?"** Flow 1, last steps: the cookie is `HttpOnly`, and the tab id alone can't open another browser's history.
- **"What happens after a page reload?"** Same cookie and the same `sessionStorage` tab id, so the history is still on the server. The UI doesn't re-display old messages yet.
- **"How would Teams users get the same answers?"** Flow 4: the bridge reuses `ChatService`, so retrieval, citations and storage are shared.
- **"Why does the answer stream in AWS?"** Flow 1, "API Gateway → ALB": the REST API integration uses `STREAM` mode (an HTTP API would buffer the whole answer). Streams are capped at 5 minutes, with the ALB's 120s idle timeout between chunks.
