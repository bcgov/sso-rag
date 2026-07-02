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

## Bedrock Knowledge Base Setup

> **The knowledge base is created and managed manually in the AWS Console. It is not provisioned by the Terraform in this repository.**
> After creation, copy the Knowledge Base ID into the GitHub repository secret `AWS_BEDROCK_KNOWLEDGE_BASE_ID`. The CI/CD pipeline passes it to Terraform as `TF_VAR_knowledge_base_id`.

### Overview

The knowledge base is configured with:

| Setting | Value |
|---|---|
| **Vector store** | Amazon S3 (native Bedrock vector index — no external OpenSearch/Aurora required) |
| **Data source** | Amazon S3 bucket containing the documents to index |
| **Embeddings model** | Amazon Titan Embeddings (configured in the knowledge base) |
| **Chunking** | Default semantic chunking |

### Step-by-step: Create the knowledge base in the AWS Console

1. **Create an S3 bucket** to hold your source documents (e.g. PDFs, plain text, Word files).  
   Upload the documents you want the RAG to answer questions about.

2. Open the **Amazon Bedrock console** → **Knowledge bases** → **Create knowledge base**.

3. **Knowledge base details**
   - Name: e.g. `sso-rag-kb`
   - IAM permissions: let the console create a new service role, or attach an existing one that has `s3:GetObject` on your bucket and `bedrock:InvokeModel` on the embeddings model.

4. **Set up data source** → choose **Amazon S3**
   - S3 URI: `s3://<your-bucket-name>/` (or a prefix path)
   - Leave parsing and chunking at defaults, or enable advanced parsing if your documents are PDFs with complex layouts.

5. **Select embeddings model**
   - Recommended: **Amazon Titan Embeddings G1 – Text** (`amazon.titan-embed-text-v1`)

6. **Configure vector store** → choose **Amazon S3** (Bedrock-managed vector store)
   - Bedrock creates and manages the vector index in S3 automatically — no additional infrastructure needed.

7. Review and **Create knowledge base**. Initial sync (ingestion) runs automatically.

8. Once created, copy the **Knowledge Base ID** (e.g. `NVDUCAWMJW`) shown on the knowledge base detail page.

### Wire the Knowledge Base ID into CI/CD

Go to your GitHub repository → **Settings → Secrets and variables → Actions** and create (or update) the secret:

| Secret name | Value |
|---|---|
| `AWS_BEDROCK_KNOWLEDGE_BASE_ID` | The Knowledge Base ID from the console |

The workflow file passes it to Terraform automatically:

```yaml
env:
  TF_VAR_knowledge_base_id: ${{ secrets.AWS_BEDROCK_KNOWLEDGE_BASE_ID }}
```

### Re-syncing the data source

Whenever you upload new documents to S3, trigger a sync from the console:

**Bedrock console → Knowledge bases → `<your kb>` → Data sources → Sync**

Or via the CLI:

```bash
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KNOWLEDGE_BASE_ID> \
  --data-source-id <DATA_SOURCE_ID> \
  --region ca-central-1
```

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
