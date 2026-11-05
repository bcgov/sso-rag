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
- `context_manager=False` and a `SlidingWindowConversationManager(window_size=CONVERSATION_WINDOW_MESSAGES)` (default 20 messages, trimmed after each request). After the restore, `drop_tool_exchanges()` removes earlier turns' search calls and results, so history is questions and delivered answers. Strands' `"auto"` context manager is not used: it truncated each search result to a ~750-token preview before the model read it and added a `retrieve_context` tool;
- `session_manager`: `SnapshotSessionManager(save_latest_on="trigger")` on the storage above, which restores previous messages but never saves on its own; `ChatService` saves once, when the turn completes;
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
- **Fail-open.** A failed or timed-out rewrite falls back to `fallback_query()`: the prompt with up to two previous user messages prepended (300 characters max), so a follow-up such as "what about prod?" keeps its topic. Step-back and sub-queries that succeeded are kept.
- **Cleanup.** The future is cancelled when the stream ends, in case it is still queued.

Details: [query-decomposition.md](./query-decomposition.md).

## 6. Model invocation and tool loop

`agent.stream_async(question, invocation_state={CITATIONS_KEY: retrieved, SEARCH_TURN_KEY: turn}, cancel_signal=cancel)` runs the Strands agent loop against Bedrock. `retrieved` is an empty list shared with the tool so it can collect every result for later verification.

The loop:

1. The model receives the system prompt, restored history and the new prompt.
2. For SSO questions it calls `search_knowledge_base()`, which has **no arguments**: the agent never writes queries. For greetings, thanks and "what can you do" questions it replies directly.
3. The tool does the following:
   - Waits for the rewriter, up to the timeout. A regex-matched greeting returns `NO_SEARCH_NEEDED`. A message the rewriter classified as conversational is still searched (with the fallback query): the agent's call wins, since a search is cheap and an ungrounded answer isn't.
   - Runs `multi_search` over the rewritten query, any sub-queries and the step-back query: acronym expansion, parallel Bedrock KB `retrieve`, dedupe, rerank against the rewritten query, score filter.
   - Asks the rewriter to reword queries that found nothing, once, and merges those results.
   - Appends each hit `{title, uri, score, text}` to `retrieved` and returns numbered text blocks, or `NO_MATCH`, plus a `NOTE` for sub-queries the docs don't cover.
4. The tool searches once per turn; a repeated call returns `ALREADY_SEARCHED`. Model output is limited to `MODEL_MAX_TOKENS` (4096, reasoning tokens included) per call; each call logs `model_call` with its token usage and stop reason.
5. The final reply ends with a `<grounding>{...}</grounding>` block.

A clarifying question is a valid final reply too (`response_type: "clarification"`). After searching, the agent asks one to three plain-text questions instead of answering in two cases. The first is a request too vague to act on, such as a problem without its error or a request without its subject. The second is results that differ by a detail the user hasn't given (IdP, environment, realm, client type, protocol). It prefers this to the no-documentation reply for vague requests. On the next turn, the query rewriter folds the user's reply into the rewritten query, and treats it as an SSO question even when it is short ("IDIR, in PROD").

## 7. Streaming events consumed by `ChatService`

For every event from `stream_async` that is a dict:

| Event | Effect |
|---|---|
| `current_tool_use` | phase `searching` |
| `data` (text delta) | passed through `ThinkingFilter`; any visible text is appended to the reply buffer, phase `writing` |
| anything else | ignored |

- A `Status` is yielded **only when the phase changes**, so the UI sees `searching` and `writing`.
- `ChatService.stream()` runs the turn in a background task and yields a `Heartbeat` whenever nothing was sent for 10 s (`HEARTBEAT_SECONDS`), on a timer, so keepalives also flow during a search, storage I/O or a reasoning model's silence; the route turns it into the SSE comment `: keepalive`.
- Past `TURN_TIMEOUT_SECONDS` (240 s, under API Gateway's 300 s stream limit) the turn is cancelled and `TurnTimeout` is raised; the route sends an in-band "took too long" error. Each agent run is capped at 4 loop iterations (`limits={"turns": 4}`, logged as `agent_limit_reached`).
- `ThinkingFilter` removes `<thinking>…</thinking>` blocks that some models (e.g. Amazon Nova) emit. It handles tags split across deltas, an unclosed tag (everything after is dropped), stray closing tags, leading whitespace and runs of 3+ newlines.

The reply is **buffered, not streamed token by token**, because it must be verified first.

## 8. Verification

After the stream ends, `ThinkingFilter.flush()` is appended and `verify(reply, retrieved, question)` runs (full rules in [grounding-status.md](./grounding-status.md)). It strips the `<grounding>` block and rejects the answer if citations, URLs or code/config values aren't backed by this turn's retrieved text.

- **Skipped search.** If no search ran this turn, `ChatService` waits for the rewrite (up to the timeout) to learn the intent. Unless the intent is conversational (failed or timed-out rewrites count as needing a search), only a clarifying question is delivered without a search. On a conversational turn, a reply labelled `conversational` (or one with no grounding block and no checkable values) is delivered too. Anything else is rejected, logged as `search_skipped_by_model`, and gets one repair turn with `search_request()`: search now, even if the user asked not to, and answer from the results. If it still doesn't search, the user gets `NO_DOCUMENTATION_RESPONSE`.
- **Repair attempt.** If the answer is rejected and the search returned results, the agent is run once more with the rejection reasons (`repair_request`). It rewrites the reply from the same results, without a new search, and the rewrite is verified again. The repair request and rejected reply are then removed from the history. The client only sees the `writing` status and heartbeats meanwhile. Set `GROUNDING_REPAIR_ENABLED=false` to turn this off.
- Only one repair runs per turn (search or grounding). Rejections are logged as `answer_rejected` (`attempt`: `first` or `repair`) with reasons; repair outcomes as `answer_repair` (`kind`: `skipped_search`, `truncated` or `grounding`). `answer_rejected` carries the same `cause`, so truncation is counted apart from grounding failures.
- If the final answer was replaced with `NO_DOCUMENTATION_RESPONSE`, `replace_last_answer()` overwrites the last assistant message and saves the snapshot, so the rejected text isn't used as context next turn. Otherwise `save_session()` saves it. This is the turn's only save, shielded from cancellation; a turn that fails, times out or is abandoned saves nothing, so stored history only holds delivered replies.

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
| Blank prompt, or over `MAX_PROMPT_TOKENS` (estimated at ~4 chars/token) | 422 with a user-facing `detail`; the UI reads the limit from `GET /chat/config` and blocks sending over it |
| Message that is only links | `LINK_ONLY_RESPONSE` clarifying question, no model call; saved to history |
| Non-English message | Reply in English; rewriter queries in English |
| Invalid/forged session cookie | Ignored; new session issued |
| Greeting / thanks | No rewriter call (regex) or `intent=conversational`; no search; reply without a grounding status |
| Rewriter says conversational, agent calls the tool | Searched anyway with the fallback query (`search_intent_override`) |
| Agent replies without searching on a turn that needs one | Clarifying questions delivered; otherwise one search repair turn, then `NO_DOCUMENTATION_RESPONSE` |
| User asks the assistant not to search | Ignored: the system prompt and the search repair still require a search |
| Query rewriter fails or times out | Prompt plus up to two previous user messages searched (`fallback_query`); successful step-back/sub-queries kept |
| A query finds nothing | Reworded once by the rewriter and searched again |
| No relevant docs found | Model replies with the no-documentation text, `NOT_SUPPORTED` |
| Answer fails verification | One repair attempt with the reasons, if the search returned results; if that also fails, replaced with `NO_DOCUMENTATION_RESPONSE`, `NOT_SUPPORTED`; stored history corrected |
| Rerank request rejected | Falls back to vector-score order |
| Reply cut off at `MODEL_MAX_TOKENS` | Logged as `max_tokens_reached` and `cause: truncated`; one repair attempt (`shorten_request()`) asking for a shorter reply, otherwise the no-documentation reply; partial message removed from history |
| Long conversation | History capped at `CONVERSATION_WINDOW_MESSAGES`; earlier turns' search results dropped on restore |
| Exception mid-stream | In-band `error` event, then `[DONE]` |
| Client disconnects | Agent loop cancelled at once; nothing of the turn is saved (a model call in flight may finish in Strands' worker thread) |
| Idle upstream | `: keepalive` whenever nothing was sent for 10 s (timer-based) |
| Turn over `TURN_TIMEOUT_SECONDS` | Turn cancelled, in-band "took too long" error, nothing saved |
| Stream cut by a proxy (no `[DONE]`) | UI shows "The response was interrupted" instead of an empty reply |
