# SSO RAG – FastAPI Backend

A production-ready streaming RAG (Retrieval-Augmented Generation) API backed by **AWS Bedrock Knowledge Base** and served over **Server-Sent Events** in an OpenAI-compatible format.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.14 |
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) |
| ASGI server | [Uvicorn](https://www.uvicorn.org/) + [Gunicorn](https://gunicorn.org/) |
| AWS SDK | [boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) |
| Settings | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| Package manager | [uv](https://docs.astral.sh/uv/) |

---

## How It Works

1. Client sends `POST /query` with `{ "query": "…" }`.
2. The API calls **AWS Bedrock `RetrieveAndGenerateStream`**, which performs:
   - **Vector search** over the configured Knowledge Base
   - **Bedrock native reranking** to select the top-N most relevant chunks
   - **Text generation** via the configured foundation model (e.g. Meta Llama 3 8B)
3. Each token is streamed back to the client as an **OpenAI-compatible SSE chunk**:
   ```
   data: {"id":"chatcmpl-…","object":"chat.completion.chunk","choices":[{"delta":{"content":"…"}}]}
   data: {"id":"chatcmpl-…","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}
   data: [DONE]
   ```
4. Retrieval citations are emitted as a separate SSE chunk with a custom `citations` field on the delta.
5. Simple greetings are detected with a regex and answered directly without calling Bedrock.

---

## Project Structure

```
poc-02/
├── api.py            # FastAPI application — settings, routes, streaming generator
├── pyproject.toml    # Project metadata and dependencies (uv)
├── uv.lock           # Locked dependency versions
├── Dockerfile        # Multi-stage container build
└── .env.example      # Example environment variable file
```

---

## Configuration

All settings are loaded from environment variables (or a `.env` file at the project root).

| Variable | Required | Default | Description |
|---|---|---|---|
| `KNOWLEDGE_BASE_ID` | ✅ | — | AWS Bedrock Knowledge Base ID |
| `MODEL_ARN` | ✅ | — | Foundation model ARN |
| `RERANKER_MODEL_ARN` | ✅ | — | Bedrock native reranker model ARN |
| `AWS_REGION` | | `ca-central-1` | AWS region |
| `NUMBER_OF_RESULTS` | | `5` | Number of chunks retrieved from the Knowledge Base |
| `NUMBER_OF_RERANKED_RESULTS` | | `3` | Number of chunks kept after reranking |
| `CORS_ORIGINS` | | `["*"]` | Allowed CORS origins (JSON list, e.g. `["https://app.example.com"]`) |

### AWS credentials

The API uses the **boto3 default credential provider chain** — no credentials are ever stored in code:

1. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`
2. AWS config file: `~/.aws/credentials`
3. IAM instance role / ECS task role / EKS IRSA (recommended for production)

---

## Running Locally

### Prerequisites

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/) installed
- AWS credentials configured with access to the Bedrock Knowledge Base

### Steps

```bash
# 1. Clone and enter the directory
cd poc-02

# 2. Install dependencies
uv sync

# 3. Create a .env file
cp .env.example .env
# Edit .env and fill in KNOWLEDGE_BASE_ID, MODEL_ARN, RERANKER_MODEL_ARN

# 4. Start the development server
uv run uvicorn api:app --reload --port 8000
```

The API will be available at [http://localhost:8000](http://localhost:8000).

### Interactive docs

| URL | Description |
|---|---|
| [http://localhost:8000/docs](http://localhost:8000/docs) | Swagger UI |
| [http://localhost:8000/redoc](http://localhost:8000/redoc) | ReDoc |

### API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness / readiness probe |
| `POST` | `/query` | Stream an answer from the Knowledge Base |

#### `POST /query`

**Request body:**
```json
{ "query": "What is SSO?" }
```

**Response:** `text/event-stream` — OpenAI-compatible SSE chunks (see [How It Works](#how-it-works)).

---

## Running in Docker

### Build and run

```bash
cd poc-02

docker build -t sso-rag-api .

docker run --rm -p 8000:8000 \
  -e KNOWLEDGE_BASE_ID=your-kb-id \
  -e MODEL_ARN=arn:aws:bedrock:ca-central-1::foundation-model/meta.llama3-8b-instruct-v1:0 \
  -e RERANKER_MODEL_ARN=arn:aws:bedrock:ca-central-1::foundation-model/amazon.rerank-v1:0 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  -e AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN \
  sso-rag-api
```

> In production, prefer an IAM role (ECS task role / EKS IRSA) over passing credentials as environment variables.

### Docker Compose (with the UI)

```yaml
services:
  api:
    build:
      context: .
    environment:
      - KNOWLEDGE_BASE_ID=${KNOWLEDGE_BASE_ID}
      - MODEL_ARN=${MODEL_ARN}
      - RERANKER_MODEL_ARN=${RERANKER_MODEL_ARN}
      - AWS_REGION=${AWS_REGION:-ca-central-1}
      # AWS credentials (omit if using an IAM role)
      - AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
      - AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
      - AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN}
    ports:
      - "8000:8000"

  ui:
    build:
      context: ../ui
      args:
        API_URL: http://api:8000
    ports:
      - "8080:8080"
    depends_on:
      - api
```
