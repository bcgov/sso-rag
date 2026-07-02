# sso-rag

Retrieval-Augmented Generation (RAG) system for SSO documentation — ingests Markdown files, stores vector embeddings in PostgreSQL, and answers natural-language questions using a locally-hosted Ollama LLM.

---

## Architecture

```mermaid
graph TD
    User -->|"HTTP POST /query"| API["API<br/>(FastAPI - port 8000)"]
    User -->|"HTTP POST /documents"| API
    API -->|"embed query - nomic-embed-text"| Ollama["Ollama<br/>(nomic-embed-text + gemma3)"]
    API -->|"vector search - cosine similarity"| PG["PostgreSQL 18 + pgvector<br/>(HNSW index - 768 dims)"]
    PG -->|top-5 chunks| API
    API -->|"prompt + context - gemma3"| Ollama
    Ollama -->|generated answer| API
    API -->|answer + sources| User

    Parser["parser.py<br/>(LangChain MarkdownHeaderTextSplitter)"] -->|"read .md files from docs/"| Chunks["Chunked Documents"]
    Chunks -->|"embed - nomic-embed-text"| Ollama
    Ollama -->|768-dim vectors| PG
```

---

## RAG System Overview

| Component | Technology |
|-----------|-----------|
| Embedding model | `nomic-embed-text` via Ollama (768 dimensions) |
| Generation model | `gemma3:1b` via Ollama |
| Vector store | PostgreSQL 18 + pgvector |
| Vector index | HNSW (`vector_cosine_ops`) |
| Document parsing | LangChain `MarkdownHeaderTextSplitter` |

**How it works:**

1. **Ingestion** — Markdown files are split by `#`, `##`, and `###` headers using LangChain's `MarkdownHeaderTextSplitter`, which preserves the full H1 → H2 → H3 hierarchy in chunk metadata.
2. **Embedding** — Before embedding, the header breadcrumb (e.g. `"Authentication > OIDC > Token exchange"`) is prepended to the chunk text so the vector captures structural context, not just body text.
3. **Storage** — Each chunk, its header metadata, source filename, and 768-dimensional embedding are stored in `document_embeddings`. An HNSW index enables fast approximate nearest-neighbour search.
4. **Query** — At query time the question is embedded with the same model, the top-5 closest chunks are retrieved by cosine distance, injected into a strict system prompt, and sent to `gemma3:1b` for grounded answer generation.
5. **Deduplication** — Ingestion is idempotent: a file is skipped if any chunk from it already exists in the database.

---

## Local Development

### Prerequisites

- Docker + Docker Compose
- Python 3.12+

### Setup

```bash
# 1. Start PostgreSQL (Spilo/Patroni) and Ollama
docker-compose up -d

# 2. Install Python dependencies
pip install -r requirements.txt

# 4. Add markdown files to docs/ then ingest
python3 parser.py

# 5. Query from the command line (uses parser.py directly)
python3 -c "
from parser import query_rag
print(query_rag('What is OIDC?'))
"
# OR query via api:
python3 api.py
# → http://localhost:8000
# → Swagger UI: http://localhost:8000/docs
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | PostgreSQL DSN |

---

## Document Ingestion

1. Drop `.md` files into `docs/` (subdirectories are supported).
2. For bulk ingestion run `python3 parser.py` — already-ingested files are skipped automatically (idempotent check by `source_file`).
3. For single files, can use the api to POST `http://localhost:8000/docuements`. See below for example curl command.
4. To re-ingest a file, delete it first:
   ```bash
   curl -X DELETE http://localhost:8000/documents/my-doc.md
   # or directly in SQL:
   DELETE FROM document_embeddings WHERE source_file = 'docs/my-doc.md';
   ```

---

## API Endpoints

Interactive docs available at **`http://localhost:8000/docs`** (Swagger UI).

### `GET /health`

Check that PostgreSQL and Ollama are reachable.

**Response `200`**
```json
{ "status": "ok", "postgres": true, "ollama": true }
```

**Response `503`** — one or more dependencies unavailable.

---

### `POST /documents`

Upload a Markdown file and ingest it into the vector store. Skips if already ingested.

**Request** — `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `file` | `UploadFile` | `.md` file to ingest |

**Response `200`**
```json
{ "status": "ingested", "filename": "oidc-guide.md", "chunks": 14 }
```
or
```json
{ "status": "skipped", "filename": "oidc-guide.md" }
```

**Example**
```bash
curl -X POST http://localhost:8000/documents \
  -F "file=@docs/oidc-guide.md"
```

---

### `POST /query`

Run a question through the RAG pipeline and return an answer with source citations.

**Request body**
```json
{ "question": "What is OIDC?" }
```

**Response `200`**
```json
{
  "answer": "OIDC (OpenID Connect) is an identity layer on top of OAuth 2.0...",
  "sources": [
    { "file": "docs/oidc-guide.md", "headers": "Authentication > OIDC", "distance": 0.123456 }
  ]
}
```

**Example**
```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is OIDC?"}'
```

---

### `DELETE /documents/{filename}`

Delete all embeddings for a document by filename. Matches exact filename or trailing path segment.

**Response `200`**
```json
{ "status": "deleted", "filename": "oidc-guide.md", "chunks_removed": 14 }
```

**Response `404`** — document not found.

**Example**
```bash
curl -X DELETE http://localhost:8000/documents/oidc-guide.md
```

---

## Helm Deployment (OpenShift)

The chart deploys the FastAPI API, an Ollama inference server, and (optionally) a Patroni HA PostgreSQL cluster.

### Prerequisites

- `helm` 3.x
- An OpenShift/Kubernetes cluster
- Patroni already deployed, or use the bundled sub-chart (`patroni.enabled=true`)

### Setup

```bash
# Create the database credentials secret
kubectl create secret generic sso-rag-db-credentials \
  --from-literal=DATABASE_URL="postgresql://user:pass@patroni-service:5432/postgres"

# Pass the created username/password credentials to patroni helm chart to use automatically. Alternatively can set the patroni
# created credentials into the sso-rag-db-credentials secrect after initial install and upgrade

# Fetch sub-chart dependencies
helm dependency update ./helm

# Install
helm install sso-rag ./helm

# Upgrade
helm upgrade sso-rag ./helm
```

### Key `values.yaml` Options

| Key | Default | Description |
|-----|---------|-------------|
| `api.replicas` | `1` | Number of API pod replicas (stateless, safe to scale) |
| `api.image.tag` | `v1.0.0-beta2` | API container image tag |
| `ollama.replicas` | `1` | Ollama pods (keep at 1; no multi-replica model sharding) |
| `ollama.resources.requests.memory` | `8Gi` | Memory request for Ollama (model weights live in RAM) |
| `ollama.resources.limits.memory` | `16Gi` | Memory limit for Ollama |
| `database.secretName` | `sso-rag-db-credentials` | Name of the Secret containing `DATABASE_URL` |
| `database.urlKey` | `DATABASE_URL` | Key within the secret |
| `patroni.enabled` | `true` | Deploy bundled Patroni HA PostgreSQL |
| `patroni.replicaCount` | `1` | Number of Patroni replicas |

---

## Project Structure

```
.
├── api.py                # FastAPI application — HTTP endpoints for query and document ingestion
├── parser.py             # Core RAG engine — parsing, embedding, storage, retrieval, generation
├── docker-compose.yaml   # Local dev stack: Spilo/Patroni PostgreSQL + Ollama
├── init.sql              # Idempotent DB init: pgvector extension, tables, HNSW index
├── Dockerfile.api        # Container image for the FastAPI service (python:3.12-slim)
├── Dockerfile            # Container image for the Ollama inference server
├── requirements.txt      # Python dependencies
├── docs/                 # Drop .md files here for ingestion via parser.py
├── helm/
│   ├── Chart.yaml        # Helm chart metadata and sub-chart dependencies
│   ├── values.yaml       # Default configuration values
│   ├── charts/           # Downloaded sub-charts (patroni, ollama)
│   └── templates/        # Kubernetes manifest templates
└── test.md               # Sample markdown file for testing ingestion
```
