# SSO RAG – Chat UI

A minimalist chat interface for the [SSO RAG FastAPI backend](../poc-02/README.md). Queries are sent to the API over a server-sent events (SSE) stream; progress ("Searching the documentation…", "Checking sources…") is shown while the API checks the answer against the documentation, then the answer is revealed smoothly.

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
- `ChatRequest` — the `/chat` request body
- `ChatCompletionChunk` / `StreamChoice` / `DeltaContent` — mirrors the OpenAI-compatible SSE format streamed by `/chat`
- `StreamError` — in-band error event sent if the stream fails after it starts
- `Citation` / `CitationLocation` — the custom citation payload attached to retrieval results
- `ChatMessage` — the UI-level message model with `streaming` flag for the blinking cursor

**`src/hooks/useChat.ts`**  
Core logic hook:
- Sends `POST /chat` with `{ prompt: string }`
- Reads the response body as a `ReadableStream`, decodes SSE lines, and parses each `data: …` chunk
- Feeds received text into a typewriter (`src/lib/typewriter.ts`) instead of painting each chunk: text is revealed every animation frame at a speed that follows the smoothed arrival rate (speeding up if more than ~1.2s behind), so bursty network chunks render as a steady flow; citations appear once the text has finished revealing
- Until the answer text arrives, `delta.status` events set the bubble's progress label (`searching` → "Searching the documentation…", `writing` → "Checking sources…"; "Thinking…" before either), announced via `role="status"`. The API checks the whole answer against the retrieved docs before sending it, so it typically arrives as one chunk
- `delta.grounding_status` is stored on the message; `PARTIALLY_SUPPORTED` answers show a "The documentation covers only part of this question." note
- Detects `data: [DONE]` and `finish_reason: "stop"` to cancel the reader and close the connection cleanly; a `{ "error": … }` event is shown as an error
- Conversation history is kept server-side, keyed by an `HttpOnly` session cookie the API sets on the first response plus an `X-Tab-Id` header; the browser sends the cookie back automatically because the UI is served from the same origin
- The tab id is a UUID in `sessionStorage`, so each tab has its own conversation and keeps it across reloads (a duplicated tab copies it and continues the same conversation until cleared)
- Collects the `citations` chunk sent after the answer and shows the sources' titles as links
- **New chat** stops any in-flight answer, calls `DELETE /chat` for this tab's history and then switches to a new tab id
- **Delete history** opens a confirmation dialog (`src/components/DeleteHistoryDialog.tsx`); confirming calls `DELETE /chat/history`, which erases every tab's stored conversation and the session cookie, then clears the screen and announces "Your chat history was deleted." If the server fails, the error is shown in the dialog with a **Try again** button and the conversation is kept

**`src/components/DeleteHistoryDialog.tsx`**  
Confirmation for the destructive delete, built on the native `<dialog>` (`showModal()` traps focus, makes the page inert, closes on `Escape` and returns focus to the opener). Focus starts on **Cancel**; the dialog can't be dismissed while the delete is in progress.

**`src/components/ChatMessageBubble.tsx`**  
Renders a single message. User messages are right-aligned (blue), assistant messages left-aligned (gray). Assistant messages are rendered as Markdown (GitHub-flavoured: lists, tables, code, links) with `react-markdown` + `remark-gfm` and styled with `@tailwindcss/typography`; raw HTML in answers is not rendered, and links open in a new tab. User messages are shown as plain text. While streaming, the text goes through `stabilizeStreamingMarkdown` (`src/lib/markdown.ts`), which shows an unfinished link as plain label text and drops or temporarily closes open `**`/`*`/`` ` ``/code-fence markers, so raw Markdown syntax never flashes on screen. The component is memoized so only the streaming bubble re-renders each frame. Shows a blinking cursor while streaming. Renders citation sources with links once streaming is complete.

**`src/components/ChatInput.tsx`**  
Auto-growing textarea capped at 10 lines. Submits on `Enter` (new line on `Shift+Enter`). Disabled while the assistant is streaming.

**`vite.config.ts`**  
Dev server runs on port `5173` and proxies `/chat` and `/health` to `http://localhost:8000`, avoiding CORS issues during local development.

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

> **Note:** The Docker image serves the pre-built static files via nginx. The `API_URL` variable is injected at build time via `--build-arg` to configure the nginx reverse proxy for the `/chat` and `/health` routes.

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
