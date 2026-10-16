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
                   │ Base                 │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ Retrieval Tool       │
                   │ (Strands Tool)       │
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

---

# Key Features

## Agentic RAG

Unlike traditional RAG, retrieval is exposed as a tool.

The agent decides:

- When to retrieve
- What information to retrieve
- How many retrieval iterations to perform
- Whether additional context is needed
- How to formulate the final response

## AWS Bedrock Knowledge Base Integration

Document retrieval leverages:

- Semantic search
- Metadata filtering
- Vector search
- Managed embedding generation

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

- Chat endpoint
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
│   │   └── logging.py
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
│   │   └── chat_service.py
│   │
│   ├── tools/
│   │   └── knowledge_base_tool.py
│   │
│   └── schemas/
│       ├── chat.py
│       └── health.py
│
├── tests/
│
├── main.py
├── pyproject.toml
├── uv.lock
└── .env
```

---

# Request Flow

## User Query

```text
"What is the MFA policy?"
```

## Agent Execution

```text
Agent
  ↓
Knowledge Base Tool
  ↓
Bedrock Knowledge Base
  ↓
Retrieved Chunks
  ↓
Agent
  ↓
Response
```

## Response

```json
{
  "answer": "According to the MFA policy..."
}
```

---

# Installation

## Prerequisites

- Python 3.13+
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
AWS_REGION=ca-central-1

KNOWLEDGE_BASE_ID=<knowledge-base-id>

NUMBER_OF_RESULTS=5

MODEL_ARN=arn:aws:bedrock:ca-central-1::foundation-model/amazon.nova-lite-v1:0
```

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

Response:

```json
{
  "answer": "..."
}
```

---

## Direct Document Retrieval

Useful for testing retrieval independently of the agent.

```http
GET /documents/search?query=mfa
```

---

# Agent Configuration

The primary agent lives in:

```text
app/agents/rag_agent.py
```

Example:

```python
agent = Agent(
    model=model,
    tools=[
        search_knowledge_base
    ]
)
```

---

# Retrieval Layer

The retrieval layer is intentionally separated from the agent.

Location:

```text
app/retrieval/
```

Responsibilities:

- Bedrock client management
- Knowledge Base queries
- Result normalization
- Citation extraction

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

- Retrieve relevant chunks from Bedrock Knowledge Base
- Return structured data to the LLM
- Provide source metadata

Example:

```python
@tool
def search_knowledge_base(question: str):
    ...
```

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

Run all tests:

```bash
uv run pytest
```

Run a single test:

```bash
uv run pytest tests/test_chat.py
```

---

# Deployment

Recommended deployment options:

## ECS Fargate

Recommended for production.

```text
FastAPI
 ↓
Docker
 ↓
ECR
 ↓
ECS Fargate
```

## EKS

Suitable for larger environments.

## Lambda

Possible but not currently optimized.

---

# Security Considerations

## IAM

Follow least-privilege principles.

Required permissions:

```text
bedrock:Retrieve
bedrock:RetrieveAndGenerate
bedrock:InvokeModel
```

Knowledge Base permissions should be scoped to the specific KB.

## Secrets

Store secrets in:

- AWS Secrets Manager
- AWS Parameter Store

Avoid storing secrets in source control.

---

# Future Roadmap

## Memory

- Session memory
- User memory
- Conversation history

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
| Storage | Amazon S3 |
| Dependency Management | UV |
| Validation | Pydantic |
| Deployment | ECS Fargate |
| Language | Python 3.13+ |