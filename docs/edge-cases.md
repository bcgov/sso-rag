# Edge Cases: Routing, Generation Limits and Request Handling

This page records the edge cases found in the chat pipeline, how each one was
confirmed, what was changed, and which tests cover it. Each fix has unit tests
that stub Bedrock (no live calls); the reproduction tests failed before the
fix and pass after it.

Severity follows: user-facing wrong answer > data integrity > cost/latency >
cosmetic.

## Summary

| ID | Edge case | Severity | Outcome |
|---|---|---|---|
| [A1](#a1-model-skips-the-search-on-an-sso-question) | Model answers an SSO question without searching | 🔴 Wrong answer | Fixed: one search repair turn, then the no-documentation reply |
| [A2](#a2-rewriter-says-conversational-model-calls-the-tool) | Rewriter says "conversational", the model calls the tool and is blocked | 🔴 Wrong answer | Fixed: the agent's tool call wins, except for regex greetings |
| [A3](#a3-greeting-regex-short-circuit) | Greeting regex matches a greeting followed by a question | – | Not a bug; regression tests added |
| [A4](#a4-rewriter-failure-on-follow-ups) | Rewriter failure loses a follow-up's context | 🟠 Retrieval quality | Fixed: history-aware fallback query |
| [C1](#c1-truncated-replies) | Replies cut off at the output-token limit | ⚪ Observability | Fixed: distinct logging and a "shorter reply" repair prompt; limit unchanged |
| [C2a](#c2a-search-results-truncated-before-the-model-read-them) | Strands `"auto"` context manager truncated the current search result | 🔴 Wrong answer | Fixed: context manager off |
| [C2b](#c2b-unbounded-conversation-history) | Conversation history grew without bound | 🟠 Cost/latency | Fixed: sliding window plus dropping earlier search results |
| [D1](#d1-input-validation) | Blank, very long, link-only and non-English messages | 🟡 Cost/cosmetic | Fixed: 422 with a readable reason, configurable limit, fixed link-only reply, English replies |
| [D2](#d2-client-disconnects-mid-stream) | Disconnect mid-turn persisted partial or unverified turns | 🟠 Data integrity | Fixed: one explicit save per completed turn |
| [D3](#d3-idle-timeouts-and-the-5-minute-stream-cap) | Silent stretches got no keepalive; turns had no time bound | 🟠 Dropped stream | Fixed: timer-based heartbeats, turn deadline, loop and client limits |

---

## A. Routing and grounding

### A1. Model skips the search on an SSO question

**Problem.** The model (Nova or Kimi) could answer an SSO question without
calling `search_knowledge_base`:

- A reply labelled `conversational` or `clarification`, written in plain prose
  with no `code`, URLs or paths, passed `verify()` and was delivered without
  any search behind it.
- A reply labelled `answer`, or one with no grounding block, was rejected. But
  the repair attempt only ran when the search had returned results, so the
  user got the no-documentation reply even when the docs cover the question.
- "Don't search, just tell me": nothing told the model that searching is
  required, so it could comply.

**Decision.** Option (b) plus (c): one repair turn telling the agent to
search, then the no-documentation reply if it still doesn't. Without a
search, only clarifying questions are delivered. Prefetching retrieval
(option a) was rejected: it adds rewriter and retrieval time to every first
token and makes the tool redundant.

**Fix** ([chat_service.py](../app/services/chat_service.py)):

- A turn needs a search unless the query rewriter classified it as
  conversational. A failed or timed-out rewrite counts as needing one. If the
  rewrite is still running when the model replies, `ChatService` waits for it,
  up to the rewriter timeout.
- When no search ran, `_ok_without_search()` accepts only a clarifying
  question, or a conversational reply on a conversational turn. Anything else
  is rejected with the reason "the reply was written without searching the
  documentation", logged as `search_skipped_by_model`.
- The repair turn sends `search_request()`
  ([system_prompt.py](../app/prompts/system_prompt.py)). If the model still
  doesn't search, the no-documentation reply is sent.
- The system prompt says the user can't turn searching off. The rewriter
  prompt ([query_rewriter_prompt.py](../app/prompts/query_rewriter_prompt.py))
  says that asking not to search doesn't change the intent.
- `Verdict.response_type` ([grounding.py](../app/services/grounding.py))
  carries the reply type for this decision.

**Tests:** `test_skipped_search_gets_a_search_repair`,
`test_still_unsearched_after_repair_falls_back_to_no_documentation`,
`test_skipped_search_without_repair_is_rejected`,
`test_user_asking_not_to_search_still_gets_a_search`,
`test_unsearched_clarification_is_delivered`,
`test_failed_rewrite_counts_as_needing_a_search`,
`test_rewrite_timeout_counts_as_needing_a_search`,
`test_waits_for_a_rewrite_still_running`,
`test_searching_cannot_be_turned_off_by_the_user`.

### A2. Rewriter says conversational, model calls the tool

**Problem.** If the rewriter misclassified a short SSO question (e.g. "what's
CSS?") as conversational, the tool returned `NO_SEARCH_NEEDED`. The model was
then told to reply conversationally and answered from memory. A reply with no
grounding block and no checkable values was delivered without any search.

**Decision.** When in doubt, search: a search is cheap, an ungrounded answer
is costly.

**Fix:**

- The tool ([knowledge_base_tool.py](../app/tools/knowledge_base_tool.py))
  searches anyway with the history-aware fallback query (A4), logged as
  `search_intent_override`. Only greetings matched by the regex
  (`RewrittenQuery.short_circuit`) still get `NO_SEARCH_NEEDED`, and that
  result isn't cached, so a later call in the same turn isn't told
  `ALREADY_SEARCHED`.
- The reverse case: the rewriter says conversational, but the model writes an
  `answer` without searching. The signals disagree, so this gets the A1
  search repair.

**Tests:** `test_agent_call_overrides_conversational_classification`,
`test_greeting_skips_search`,
`test_conversational_intent_with_unsearched_answer_gets_a_search`,
`test_conversational_intent_with_conversational_reply_is_delivered`.

### A3. Greeting regex short-circuit

**Finding: not a bug.** The regex in
[query_rewriter.py](../app/services/query_rewriter.py) is anchored to the
whole message. "hi, how do I add a redirect URI?", "hello\nhow do I…" and
"Thanks!\n\nHow do I get BCeID?" all go to the rewriter. Regression cases were
added to `test_questions_are_not_greetings`.

### A4. Rewriter failure on follow-ups

**Problem.** When the rewriter failed or timed out, the raw prompt was
searched. A follow-up such as "what about prod?" then retrieved poorly.

**Fix:** `fallback_query()` in
[query_rewriter.py](../app/services/query_rewriter.py) puts up to two
previous user messages, most recent kept first, in front of the prompt,
within the 300-character query limit. "what about prod?" after "How do I get
BCeID in TEST?" is searched as "How do I get BCeID in TEST? what about
prod?". It is used on rewriter errors, unusable replies, timeouts, and the
tool's own wait timeout (`SearchTurn.history`).

**Partial failures:** if only the rewrite call fails, the step-back and
sub-queries that succeeded are kept; they were written with the history. If
step-back or decomposition fails, only that technique is dropped.

**Tests:** `test_fallback_query_*`, `test_transform_rewrite_error_on_follow_up_uses_history`,
`test_transform_all_techniques_fail_uses_history`,
`test_transform_rewrite_timeout_keeps_other_techniques`,
`test_transform_unexpected_error_uses_history`,
`test_rewrite_failure_on_follow_up_uses_history`,
`test_rewrite_timeout_on_follow_up_uses_history`.

---

## C. Generation limits

### C1. Truncated replies

**Findings:**

- **Frequency.** Visible replies observed locally were 82–632 tokens against
  `MODEL_MAX_TOKENS=4096`. The deployed model (Kimi K3) emits *encrypted*
  reasoning, which almost certainly counts toward the limit but can't be
  measured from snapshots.
- **Logging.** Truncation was logged under the same `answer_rejected` and
  `answer_repair` events as grounding failures, told apart only by free text.
- **Repair prompt.** A cut-off reply was told it "failed the automatic
  grounding check".

**Decision.** Keep `MODEL_MAX_TOKENS=4096` and measure before lowering it. The
system prompt (about 300 words) is unchanged.

**Fix** ([chat_service.py](../app/services/chat_service.py)):

- `answer_rejected` has a `cause`: `truncated`, `skipped_search` or
  `grounding`. `answer_repair` uses the same values as `kind`.
  `max_tokens_reached` has an `attempt`.
- A new `model_call` log line per model call records `attempt`,
  `stop_reason`, `input_tokens`, `output_tokens` and `max_tokens`. Use it to
  see the real output-token distribution, reasoning included, before changing
  the limit.
- Cut-off replies get `shorten_request()` instead of the grounding repair
  prompt.

**Tests:** `test_truncation_is_logged_apart_from_grounding_failures`,
`test_grounding_failure_is_logged_as_grounding`,
`test_truncation_repair_asks_for_a_shorter_reply_not_a_grounding_fix`,
`test_each_model_call_logs_its_token_usage`.

### C2a. Search results truncated before the model read them

**Found while checking C2.** The agent used Strands'
`context_manager="auto"`. Its truncation strategy has `preserve_recent=0`, so
it hooks every added message: any tool result over about 1,500 estimated
tokens was cut to a roughly 750-token preview, about 60% from the start and
40% from the end. That happened **before the model read it in the same
turn**. A typical result of 6 hits is about 1,900 tokens, so the model saw
results 1, 2 and the end of 6 only.

The context manager also added a hidden `retrieve_context` tool. Local
sessions showed the model calling it on every turn to grep the elided text
with its own regex: one extra model call per question, and an answer written
from that grep instead of the reranked results.

**Fix** ([rag_agent.py](../app/agents/rag_agent.py)): `context_manager=False`.
History management moved to C2b.

**Tests:** `test_model_sees_the_whole_search_result_this_turn`,
`test_model_is_offered_only_the_search_tool`.

### C2b. Unbounded conversation history

**Problem.** `"auto"` summarizes only at 85% of an assumed 200K-token window
(`context_window_limit` isn't set), about 170K tokens. After 30 turns the
model was sent 119 messages. Locally, 4 turns came to about 28K tokens per
model call (≈7K per turn), and every turn re-sent all earlier search results,
though only the current turn's results count as evidence.

**Decision.** Sliding window of 20 messages, with earlier search exchanges
dropped. Summarization was rejected: it adds a model call when it triggers,
and puts unverified model-written text into history.

**Fix** ([rag_agent.py](../app/agents/rag_agent.py)):

- `SlidingWindowConversationManager(window_size=CONVERSATION_WINDOW_MESSAGES)`,
  default 20, trimmed after each request.
- `drop_tool_exchanges()` runs on restore. It removes earlier turns' tool
  calls and results, merges an interrupted turn so roles still alternate, and
  makes sure history starts with a user message. Stored history is then just
  questions and delivered answers: about the last 10 turns.
- Old snapshots restore fine. Leftover `context/<id>/` stash objects are
  removed by the retention cleanup.

**Tests:** `test_history_sent_to_the_model_is_bounded`,
`test_earlier_turns_search_results_are_not_resent`,
`test_window_setting_caps_history`,
`test_restored_history_alternates_and_keeps_answers`,
`test_drops_tool_calls_and_results`,
`test_interrupted_turn_is_merged_to_keep_roles_alternating`,
`test_history_starts_with_a_user_message`.

---

## D. Request handling

### D1. Input validation

**Problem.** The UI trimmed input and blocked empty sends, but the API
accepted any string:

- A blank prompt reached Bedrock, which rejects empty text, so the user saw
  the generic failure.
- There was no length limit: a huge paste was sent to Bedrock and to the
  rewriter, whose Llama model has an 8K context.
- Any 4xx response appeared in the UI as a raw `HTTP 422`.

**Decisions:**

- **Length:** `MAX_PROMPT_TOKENS=200`, configurable. Tokens are estimated at
  about 4 characters each, since the deployed model's tokenizer isn't
  available, so the default allows about 800 characters.
- **Link-only messages:** a fixed clarifying question, with no model calls,
  saved to history.
- **Language:** always reply in English.

**Fix:**

- **Checks.** [prompt_policy.py](../app/services/prompt_policy.py) trims the
  prompt and rejects blank or too-long messages with a 422 whose `detail` is
  shown to the user (e.g. "Please enter a question.").
- **UI limit.** `GET /chat/config` returns `max_prompt_tokens` and
  `max_prompt_chars`. It is covered by the existing `/chat/*` routing, so no
  infrastructure change was needed. The UI
  ([ChatInput.tsx](../ui/src/components/ChatInput.tsx)) shows a counter from
  80% of the limit and disables Send over it.
  ([useChat.ts](../ui/src/hooks/useChat.ts)) shows the server's `detail` for
  4xx responses.
- **Link-only messages.** `LINK_ONLY_RESPONSE`
  ([constants.py](../app/common/constants.py)) is sent and saved with a
  `clarification` grounding block, so a follow-up such as "how do I log in
  there?" still resolves.
- **English.** The system prompt says "Always reply in English". The rewriter
  writes every query in English, translating if needed and keeping literal
  values such as error messages unchanged.

**Tests:** `test_blank_prompt_is_rejected_with_a_friendly_message`,
`test_very_long_prompt_is_rejected_with_a_friendly_message`,
`test_too_long_message_names_the_limit`, `test_config_reports_the_limit`,
`test_prompt_is_passed_on_stripped`,
`test_limit_is_estimated_tokens_and_configurable`, `test_links_only`,
`test_not_links_only`,
`test_link_only_message_gets_the_fixed_reply_without_model_calls`,
`test_replies_are_english_and_queries_are_english`.

### D2. Client disconnects mid-stream

**Findings:**

- Uvicorn advertises ASGI 2.3, so Starlette watches for `http.disconnect` and
  cancels the response task immediately. The agent loop stops at once, so no
  further model or tool calls run.
- **Inconsistent state (the bug).** Strands saved a snapshot in a `finally`
  that also ran on cancellation. A disconnect could store a dangling question,
  or an unverified or rejected answer plus the repair request, as context for
  the next turn. It also raced "Delete history": the UI deleted first and
  aborted the stream afterwards, so the save on cancellation could write the
  deleted conversation back.
- **Cost.** The Bedrock worker thread watches the agent's internal cancel
  event, which the agent clears as the cancellation unwinds. The route's
  `cancel` event is set after that, so the call already in flight keeps
  streaming. Strands 1.61.1 can't reliably abort it.

**Decision.** Keep cancelling on disconnect and save nothing. Accept that at
most one in-flight model call's tokens are wasted.

**Fix:**

- `SnapshotSessionManager(..., save_latest_on="trigger")`
  ([rag_agent.py](../app/agents/rag_agent.py)) turns off Strands' own saves.
- `ChatService` saves once, after the grounding checks, only when the turn
  completes: `save_session()`, or `replace_last_answer()` when the reply was
  replaced. The save is shielded from cancellation (`_persist`). A cancelled,
  failed or timed-out turn saves nothing. This also covers most of B1:
  stored history only ever holds delivered replies.
- The UI now stops an answer in progress before calling
  `DELETE /chat/history`.

**Tests:** `test_disconnect_during_search_leaves_history_unchanged`,
`test_disconnect_during_repair_does_not_keep_the_rejected_answer`
([test_turn_persistence.py](../tests/test_turn_persistence.py), using the real
`create_agent()` with a scripted model and local storage),
`test_failed_turn_saves_nothing`.

### D3. Idle timeouts and the 5-minute stream cap

**Findings:**

- **Heartbeats.** They were sent only when an agent event arrived, not on a
  timer. Silent stretches got no keepalive: the search tool, a reasoning model
  before its first token, or a stalled Bedrock read. Strands' Bedrock read
  timeout is 120s, the same as the ALB idle limit. Knowledge-base calls used
  botocore defaults (60s read × 3 attempts).
- **The 5-minute cap.** Nothing bounded a turn. Agent loop cycles were
  unlimited (the model could keep calling the tool and getting
  `ALREADY_SEARCHED`), and a turn could make up to 4 model calls of 4,096
  output tokens each, with retries. When API Gateway cut the stream, the UI
  treated the missing `[DONE]` as success and showed an empty reply with no
  error. Typical turns take about 10–35s.

**Fix:**

- **Heartbeats.** `ChatService.stream()` runs the turn (`_turn()`) in one
  background task, so contextvars and tracing spans stay consistent.
  `_with_heartbeats()` sends a `Heartbeat` (`: keepalive`) whenever nothing
  was sent for 10s, covering the whole turn, including storage I/O.
- **Turn deadline.** `TURN_TIMEOUT_SECONDS` (default 240, under the 300s cap)
  cancels the turn and raises `TurnTimeout`. The route sends an in-band "took
  too long" error, and nothing is saved.
- **Loop limit.** `limits={"turns": 4}` per agent run (`AGENT_MAX_TURNS`),
  logged as `agent_limit_reached`.
- **Knowledge-base client.** 5s connect / 20s read timeouts
  ([bedrock_client.py](../app/retrieval/bedrock_client.py)).
- **UI.** A stream that ends without `[DONE]` is reported as "The response
  was interrupted. Please try again."

**Tests:** `test_heartbeats_flow_while_the_agent_is_silent`,
`test_turn_stops_at_the_deadline`, `test_turn_timeout_is_reported_in_band`,
`test_failure_is_reported_in_band`, `test_agent_runs_are_limited_in_turns`,
`test_knowledge_base_client_has_short_timeouts`.

---

## Configuration added or relevant

| Env var | Default | Edge case |
|---|---|---|
| `GROUNDING_REPAIR_ENABLED` | `true` | A1, C1: also switches off the search repair and the shorten repair |
| `MODEL_MAX_TOKENS` | `4096` | C1: reasoning tokens count; check `model_call` logs before lowering |
| `CONVERSATION_WINDOW_MESSAGES` | `20` | C2b |
| `MAX_PROMPT_TOKENS` | `200` | D1: estimated at about 4 characters per token |
| `TURN_TIMEOUT_SECONDS` | `240` | D3: keep under API Gateway's 300s |

## Log events

| Event | Meaning |
|---|---|
| `search_skipped_by_model` | A1: a reply was written without the required search |
| `search_intent_override` | A2: the rewriter said conversational, the agent called the tool, and the search ran |
| `answer_rejected` (`cause`) | `grounding`, `truncated` or `skipped_search` |
| `answer_repair` (`kind`, `searched`, `repaired`) | Outcome of the single repair attempt |
| `max_tokens_reached` (`attempt`) | C1: a reply hit `MODEL_MAX_TOKENS` |
| `model_call` | C1/C2: per-call `stop_reason`, `input_tokens`, `output_tokens`, `max_tokens` |
| `link_only_prompt` | D1: fixed link-only reply sent |
| `agent_limit_reached` | D3: an agent run hit `limits={"turns": 4}` |
| `turn_timeout` | D3: the turn ran past `TURN_TIMEOUT_SECONDS` |

## Known limitations and follow-ups

- **Prose claims after a search.** A reply labelled `conversational` *after* a
  search is still delivered without citations. The grounding checks can't
  judge plain prose.
- **Search repair cost.** If the rewriter is down, small talk the greeting
  regex misses (e.g. "great, that worked") may cost an extra model call for a
  search repair.
- **Short message limit.** At 200 tokens (about 800 characters), users can't
  paste long error logs; raise `MAX_PROMPT_TOKENS` if that comes up often.
- **In-flight Bedrock calls.** A disconnect stops the agent loop, but the
  model call already in progress may run to completion in Strands' worker
  thread.
- **Not addressed yet:**
  - B2: concurrent requests on one conversation (last write wins).
  - B3: a full review of errors mid-turn. A failed turn now saves nothing (see
    D2), but the error messages and retry behaviour haven't been reviewed.
  - E: the grounding-policy design note for adding more agents.
