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

Each `search_knowledge_base` call:

1. **Acronym expansion** – queries using an acronym from `app/common/constants.py`
   (`BCSC`, `PKCE`, `M2M`, …) get its spelled-out form appended, so the
   embedding also matches the docs' wording. Only genuine abbreviations are
   expanded; general words are left alone.
2. **Parallel multi-query retrieval** – the agent passes 1–4 queries; duplicates
   are dropped and each query is searched in parallel.
3. **Dedupe** – chunks found by more than one query are merged.
4. **Rerank** – the merged set is reranked against the user's question (not the
   individual queries) with the Bedrock `Rerank` API, and results below
   `MIN_RERANK_SCORE` are dropped. Extra facets get a little extra room
   (up to 12 results) so one facet can't crowd out the others.
5. **Coverage note** – queries with no surviving results are named in a `NOTE`
   so the agent can retry just those.

If no reranker is configured, or the reranker request is rejected, results are
ordered by vector score instead.

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
│   │   ├── knowledge_base.py
│   │   └── models.py
│   │
│   ├── services/
│   │   ├── chat_service.py
│   │   └── session_cleanup.py   # deletes inactive sessions
│   │
│   ├── tools/
│   │   └── knowledge_base_tool.py
│   │
│   └── schemas/
│       ├── chat.py
│       └── health.py
│
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
Agent (retries empty queries once, then answers)
  ↓
Streamed answer (SSE) + cited sources; conversation snapshot saved
```

## Response

The answer is streamed as server-sent events, followed by the sources it
links to (see [Chat](#chat) for the full format):

```text
data: {... "delta": {"content": "To get PROD approval for BCeID... ([Requesting BCeID in PROD](https://bcgov.github.io/sso-docs/...))"} ...}
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
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "MFA is…"}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"citations": [{"title": "…", "location": {"uri": "https://…"}, "score": 0.82}]}, "finish_reason": null}], …}
data: {"id": "chatcmpl-…", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], …}
data: [DONE]
```

The `citations` chunk is sent once, after the answer text, when the knowledge
base was searched. It lists the retrieved sources the answer links to, or all
retrieved sources if it links none, deduplicated by URL.

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

### Session cleanup

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

The `/chat` route is async and streams `agent.stream_async()` output. If the
client disconnects, the agent invocation is cancelled.

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
- **Escalation** – Teams (Keycloak How-to) and `bcgov.sso@gov.bc.ca`, with the
  details support needs.

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
- Multi-query search, dedupe and coverage tracking (`multi_search`)
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
returned result's title, source URL and score is appended to that list; this
is how `/chat` builds its citations.

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
publicly through an API Gateway HTTP API. Infrastructure is in `terraform/`
and deployed by GitHub Actions.

## AWS Topology

```text
Client
  ↓
API Gateway (HTTP API, $default route)
  ↓  VPC Link
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
| API Gateway + VPC Link | `sso-rag-api-gw` | `apigateway.tf` |
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
URL=$(terraform -chdir=terraform output -raw api_gateway_url)

curl "$URL/health"
curl -N -c cookies.txt -b cookies.txt -X POST "$URL/chat" -H "Content-Type: application/json" \
  -d '{"prompt": "How do I request a new integration?"}'
```

Logs: CloudWatch `/ecs/sso-rag/api` (app and tool logs) and
`/aws/apigateway/sso-rag` (access logs).

## Known Limitations

- **30-second request limit.** API Gateway HTTP APIs have a fixed 30s
  integration timeout. Agent answers that need several searches can exceed
  it and return 504. If that becomes common, front the ALB with CloudFront or
  a public ALB instead of API Gateway.
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
s3:DeleteObject, s3:ListBucket         # DELETE /chat and session cleanup
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