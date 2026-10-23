# End-to-End Request Flow

How a user question travels from the browser to a grounded, streamed answer: request parsing, session handling, agent and model invocation, retrieval, answer verification and the response stream.

| Stage | Code |
|---|---|
| App wiring, CORS, cleanup task | [main.py](../../main.py) |
| Route, SSE streaming, cookies | [chat.py](../../app/api/routes/chat.py) |
| Request schema | [schemas/chat.py](../../app/schemas/chat.py) |
| Agent, model, session storage | [rag_agent.py](../../app/agents/rag_agent.py) |
| Orchestration, `<thinking>` filter, status events | [chat_service.py](../../app/services/chat_service.py) |
| System prompt | [system_prompt.py](../../app/prompts/system_prompt.py) |
| Query rewriter (Llama 3 8B) | [query_rewriter.py](../../app/services/query_rewriter.py), [query_rewriter_prompt.py](../../app/prompts/query_rewriter_prompt.py) |
| Retrieval tool | [knowledge_base_tool.py](../../app/tools/knowledge_base_tool.py) |
| Search, merge, rerank | [knowledge_base.py](../../app/retrieval/knowledge_base.py) |
| Answer verification | [grounding.py](../../app/services/grounding.py) |

## Overview

```text
Browser (UI)
  POST /chat  {"prompt": "..."}   Cookie: strands_session   X-Tab-Id: <uuid>
    ↓
FastAPI route chat()            validate body + cookie + tab id → conversation id
    ↓
StreamingResponse(_event_stream)   Set-Cookie, SSE headers
    ↓
ChatService.stream()            restore agent from stored snapshot
    ├─► Query rewriter (background)   intent + rewrite, step-back, sub-queries (Llama 3 8B)
    ↓
Strands Agent → Bedrock model   system prompt + history + prompt
    ↓  (tool call, only for SSO questions)
search_knowledge_base()         wait for rewriter → multi_search → KB retrieve → rerank → retry misses
    ↓  (tool result)
Bedrock model                   writes answer + <grounding> block
    ↓
ChatService                     strip <thinking>, buffer reply
    ↓
grounding.verify()              accept, or replace with NO_DOCUMENTATION_RESPONSE
    ↓
SSE: status → content → grounding_status → citations → stop → [DONE]
```

## 1. Application startup

[main.py](../../main.py) builds the FastAPI app and:

- adds `CORSMiddleware` (allowed headers include `X-Tab-Id`; credentials are allowed only when `CORS_ORIGINS` doesn't contain `*`);
- mounts the health, chat and documents routers;
- in the lifespan hook, starts a background loop that deletes stored conversations older than `SESSION_RETENTION_DAYS` every `SESSION_CLEANUP_INTERVAL_HOURS` (0 disables it).

At import time [rag_agent.py](../../app/agents/rag_agent.py) creates the shared pieces once: the `BedrockModel` (model id parsed from the last segment of `MODEL_ARN`, region `AWS_REGION`) and the session storage (`LocalFileStorage` when `ENVIRONMENT=local`, otherwise `S3Storage` on `AGENT_STORAGE_S3_BUCKET`). The Bedrock Agent Runtime client for retrieval is a lazy singleton with adaptive retries (3 attempts).

## 2. Request arrives: `POST /chat`

The UI ([useChat.ts](../../ui/src/hooks/useChat.ts)) posts `{"prompt": "<text>"}` with `credentials: "same-origin"` and an `X-Tab-Id` header, then reads the response as a stream.

In `chat()`:

1. **Body parsing.** FastAPI validates the body against `ChatRequest` (`prompt: str`). A missing or non-string prompt returns 422 before any agent work.
2. **Session cookie.** The `strands_session` cookie (name configurable) is read. Because it becomes a storage key, `is_valid_session_id` only accepts `session_<uuid>`. A missing or malformed value gets a fresh `new_session_id()`.
3. **Tab id.** The optional `X-Tab-Id` header is used only if it is a valid UUID.
4. **Conversation id.** `conversation_id(session_id, tab_id)` is `"<session>__<tab>"` with a valid tab id, otherwise just the session id. This keeps each tab's history separate while sharing one HttpOnly cookie.
5. **Response.** A `StreamingResponse` over `_event_stream(prompt, conversation_id)` is returned with `text/event-stream`, `Cache-Control: no-cache` and `X-Accel-Buffering: no`. The cookie is set on every response (HttpOnly, SameSite=Lax, `Secure` per config, sliding `max_age`, default 24 h). Headers go out before the first chunk, so the cookie is set even if the client disconnects mid-answer.

## 3. Event stream setup

`_event_stream` (details in [event-streams.md](./event-streams.md)) creates a `chatcmpl-<uuid>` completion id and a `threading.Event` cancel signal, sends the opening `{"role": "assistant"}` chunk, and iterates `chat_service.stream(prompt, conversation_id, cancel_signal=cancel)`. If the client disconnects, the generator is closed and `finally` sets the cancel signal so the agent stops.

## 4. Agent creation

`ChatService.stream` calls `create_agent(session_id)` via `asyncio.to_thread`, because restoring the snapshot is synchronous storage I/O. A **new `Agent` per request** is built with:

- `model`: the shared `BedrockModel`;
- `tools`: `[search_knowledge_base]`;
- `system_prompt`: `SYSTEM_PROMPT` (when to call the no-argument search tool, clarifying-question policy, answering rules, escalation text and the grounding-report contract);
- `context_manager="auto"`: Strands trims the conversation when it grows too large;
- `session_manager`: `SnapshotSessionManager` on the storage above, which restores previous messages and saves a snapshot after the turn;
- `callback_handler=None`: nothing is printed to stdout; output is consumed from the stream.

A fresh agent per request prevents conversation history from leaking between users.

## 5. Query rewriting

Right after the agent is restored, `ChatService` reads the last few turns from `agent.messages` (before the new prompt is appended) and calls `query_rewriter.submit(prompt, history)`. It wraps the returned future in a `SearchTurn`.

- **Greetings.** Whole-message greetings, thanks and capability questions match a regex and resolve at once as conversational, with no model call.
- **Everything else.** Three Llama 3 8B calls (Bedrock `Converse`) run in the background, in parallel:
  - rewrite + intent;
  - step-back query;
  - sub-query decomposition.
- **Overlap.** These calls overlap the agent's first model call.
- **Fail-open.** Any failure falls back to searching the raw prompt.
- **Cleanup.** The future is cancelled when the stream ends, in case it is still queued.

Details: [query-decomposition.md](./query-decomposition.md).

## 6. Model invocation and tool loop

`agent.stream_async(question, invocation_state={CITATIONS_KEY: retrieved, SEARCH_TURN_KEY: turn}, cancel_signal=cancel)` runs the Strands agent loop against Bedrock. `retrieved` is an empty list shared with the tool so it can collect every result for later verification.

The loop:

1. The model receives the system prompt, restored history and the new prompt.
2. For SSO questions it calls `search_knowledge_base()`, which has **no arguments**: the agent never writes queries. For greetings, thanks and "what can you do" questions it replies directly.
3. The tool does the following:
   - Waits for the rewriter, up to the timeout. A conversational intent returns `NO_SEARCH_NEEDED`.
   - Runs `multi_search` over the rewritten query, any sub-queries and the step-back query: acronym expansion, parallel Bedrock KB `retrieve`, dedupe, rerank against the rewritten query, score filter.
   - Asks the rewriter to reword queries that found nothing, once, and merges those results.
   - Appends each hit `{title, uri, score, text}` to `retrieved` and returns numbered text blocks, or `NO_MATCH`, plus a `NOTE` for sub-queries the docs don't cover.
4. The output is cached for the turn, so a repeated tool call returns the same result.
5. The final reply ends with a `<grounding>{...}</grounding>` block.

A clarifying question is a valid final reply too (`response_type: "clarification"`); the prompt prefers searching first and asking only when the docs give different answers depending on the missing detail.

## 7. Streaming events consumed by `ChatService`

For every event from `stream_async` that is a dict:

| Event | Effect |
|---|---|
| `current_tool_use` | phase `searching` |
| `data` (text delta) | passed through `ThinkingFilter`; any visible text is appended to the reply buffer, phase `writing` |
| anything else | ignored |

- A `Status` is yielded **only when the phase changes**, so the UI sees `searching` and `writing`.
- If 10 s (`HEARTBEAT_SECONDS`) pass without sending anything, a `Heartbeat` is yielded; the route turns it into the SSE comment `: keepalive`.
- `ThinkingFilter` removes `<thinking>…</thinking>` blocks that some models (e.g. Amazon Nova) emit. It handles tags split across deltas, an unclosed tag (everything after is dropped), stray closing tags, leading whitespace and runs of 3+ newlines.

The reply is **buffered, not streamed token by token**, because it must be verified first.

## 8. Verification

After the stream ends, `ThinkingFilter.flush()` is appended and `verify(reply, retrieved, question)` runs (full rules in [grounding-status.md](./grounding-status.md)). It strips the `<grounding>` block and rejects the answer if citations, URLs or code/config values aren't backed by this turn's retrieved text.

- Rejections are logged as `answer_rejected` with reasons.
- If the answer was replaced with `NO_DOCUMENTATION_RESPONSE`, `replace_last_answer()` overwrites the last assistant message and re-saves the snapshot, so the rejected text isn't used as context next turn.
- Otherwise the snapshot saved by the session manager already holds the delivered reply.

Result: a single `Answer(text, grounding_status, citations)`.

## 9. Response chunks

Back in `_event_stream`, items are encoded as OpenAI-style `chat.completion.chunk` events (`model` is the model id):

```text
data: {"choices":[{"delta":{"role":"assistant"}}]}
data: {"choices":[{"delta":{"status":"searching"}}]}
: keepalive
data: {"choices":[{"delta":{"status":"writing"}}]}
data: {"choices":[{"delta":{"content":"<full answer>"}}]}
data: {"choices":[{"delta":{"grounding_status":"SUPPORTED"}}]}
data: {"choices":[{"delta":{"citations":[{"title":"…","location":{"uri":"…"},"score":0.81}]}}]}
data: {"choices":[{"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

- `grounding_status` is omitted for clarifications and greetings; `citations` is omitted when empty.
- If an exception occurs after streaming began, `data: {"error": "The assistant failed to respond. Please try again."}` is sent, followed by `[DONE]`.
- On client disconnect, no `[DONE]` is sent.

The UI parses each `data:` line, updates the status indicator, appends content, stores `grounding_status` and `citations`, and stops on `[DONE]` (an `error` chunk is raised as an error and the placeholder message removed).

## 10. Related endpoints

- `DELETE /chat`: drops the stored history of the current conversation. With a valid `X-Tab-Id` only that tab's history is removed and the cookie is kept; without it the browser-level conversation is removed and the cookie cleared.
- `DELETE /chat/history`: deletes every stored conversation of this browser (all tabs) and clears the cookie; returns 500 and keeps the cookie if some deletions fail so the user can retry.

## Failure and edge cases

| Situation | Behaviour |
|---|---|
| Invalid body | 422 from FastAPI |
| Invalid/forged session cookie | Ignored; new session issued |
| Greeting / thanks | No rewriter call (regex) or `intent=conversational`; no search; reply without a grounding status |
| Query rewriter fails or times out | Raw prompt searched as the single query |
| A query finds nothing | Reworded once by the rewriter and searched again |
| No relevant docs found | Model replies with the no-documentation text, `NOT_SUPPORTED` |
| Answer fails verification | Replaced with `NO_DOCUMENTATION_RESPONSE`, `NOT_SUPPORTED`; stored history corrected |
| Rerank request rejected | Falls back to vector-score order |
| Exception mid-stream | In-band `error` event, then `[DONE]` |
| Client disconnects | Cancel signal stops the agent |
| Idle upstream | `: keepalive` comments every 10 s |
