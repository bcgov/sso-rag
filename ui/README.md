# SSO RAG – Chat UI

A minimalist streaming chat interface for the [SSO RAG FastAPI backend](../poc-02/README.md). Queries are sent to the API and responses are rendered token-by-token as they arrive over a server-sent events (SSE) stream.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | [React 18](https://react.dev/) |
| Language | [TypeScript 5](https://www.typescriptlang.org/) |
| Build tool | [Vite 6](https://vitejs.dev/) |
| Styling | [Tailwind CSS 3](https://tailwindcss.com/) |
| Streaming | Fetch API + `ReadableStream` reader |

---

## Project Structure

```
ui/
├── index.html                  # HTML entry point
├── vite.config.ts              # Vite config + dev proxy to API
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
└── src/
    ├── main.tsx                # React root mount
    ├── App.tsx                 # Root layout: header, message list, input
    ├── index.css               # Tailwind directives
    ├── types.ts                # All shared TypeScript types
    ├── components/
    │   ├── ChatMessageBubble.tsx   # User / assistant message bubbles + citations
    │   └── ChatInput.tsx           # Auto-growing textarea, Enter to send
    └── hooks/
        └── useChat.ts              # Streaming fetch, SSE parser, message state
```

### Key files

**`src/types.ts`**  
Defines all types used across the app:
- `ChatCompletionChunk` / `StreamChoice` / `DeltaContent` — mirrors the OpenAI-compatible SSE format emitted by the API
- `Citation` / `CitationLocation` — the custom citation payload attached to retrieval results
- `ChatMessage` — the UI-level message model with `streaming` flag for the blinking cursor

**`src/hooks/useChat.ts`**  
Core logic hook:
- Sends `POST /query` with `{ query: string }`
- Reads the response body as a `ReadableStream`, decodes SSE lines, and parses each `data: …` chunk
- Uses `flushSync` to force React to re-render on every token so text appears progressively
- Detects `data: [DONE]` and `finish_reason: "stop"` to cancel the reader and close the connection cleanly
- Accumulates `citations` from retrieval result chunks and attaches them to the completed message

**`src/components/ChatMessageBubble.tsx`**  
Renders a single message. User messages are right-aligned (blue), assistant messages left-aligned (gray). Shows a blinking cursor while streaming. Renders citation sources with links once streaming is complete.

**`src/components/ChatInput.tsx`**  
Auto-growing textarea capped at 10 lines. Submits on `Enter` (new line on `Shift+Enter`). Disabled while the assistant is streaming.

**`vite.config.ts`**  
Dev server runs on port `5173` and proxies `/query` and `/health` to `http://localhost:8000`, avoiding CORS issues during local development.

---

## Running Locally

### Prerequisites

- Node.js ≥ 18 (≥ 20 recommended)
- The FastAPI backend running at `http://localhost:8000` (see [`poc-02/README.md`](../poc-02/README.md))

### Steps

```bash
# 1. Install dependencies
cd ui
npm install        # or: yarn install

# 2. Start the dev server
npm run dev        # or: yarn dev
```

Open [http://localhost:5173](http://localhost:5173) in your browser.

### Other scripts

| Command | Description |
|---|---|
| `npm run dev` | Start Vite dev server with HMR and API proxy |
| `npm run build` | Type-check and produce a production build in `dist/` |
| `npm run preview` | Serve the production build locally for final testing |

---

## Running in Docker

### Build and run

```bash
# From the ui/ directory
docker build -t sso-rag-ui .

docker run --rm -p 8080:8080 \
  -e API_URL=http://your-api-host:8000 \
  sso-rag-ui
```

The app will be available at [http://localhost:8080](http://localhost:8080).

### Environment variables (build-time)

| Variable | Default | Description |
|---|---|---|
| `API_URL` | `http://localhost:8000` | Base URL of the FastAPI backend (used by the nginx proxy) |

> **Note:** The Docker image serves the pre-built static files via nginx. The `API_URL` variable is injected at build time via `--build-arg` to configure the nginx reverse proxy for the `/query` and `/health` routes.

```bash
docker build --build-arg API_URL=https://api.example.com -t sso-rag-ui .
```

### Docker Compose (with the API)

```yaml
services:
  api:
    build:
      context: ../poc-02
    environment:
      - KNOWLEDGE_BASE_ID=${KNOWLEDGE_BASE_ID}
      - MODEL_ARN=${MODEL_ARN}
      - AWS_REGION=${AWS_REGION}
    ports:
      - "8000:8000"

  ui:
    build:
      context: .
      args:
        API_URL: http://api:8000
    ports:
      - "8080:80"
    depends_on:
      - api
```
