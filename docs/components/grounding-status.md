# Grounding Statuses

Every answer is checked against the documentation retrieved in the same turn **before** anything is sent to the user. The check lives in [grounding.py](../../app/services/grounding.py) and is invoked from [chat_service.py](../../app/services/chat_service.py).

## Statuses

| Status | Meaning | User sees |
|---|---|---|
| `SUPPORTED` | Every fact is stated in this turn's search results | The model's answer, with citations |
| `PARTIALLY_SUPPORTED` | Results cover only part of the question; the answer says which part is not covered | The model's answer, with citations |
| `NOT_SUPPORTED` | Results don't answer the question, **or** the answer failed a check | `NO_DOCUMENTATION_RESPONSE` (Teams / email contacts) |

`grounding_status` is omitted for `clarification` and `conversational` replies (greetings, follow-up questions), which make no factual claims. They are still checked for URLs and `code` values, so the prompt asks for clarifying questions in plain text: `` `invalid_grant` `` in a clarification is rejected if the results don't contain it, while plain-text options pass.

## Where the status comes from

1. **The model self-reports.** The system prompt requires every reply to end with a hidden block:

   ```text
   <grounding>{"response_type": "answer", "grounding_status": "SUPPORTED", "citations": ["https://bcgov.github.io/sso-docs/..."]}</grounding>
   ```

   - `response_type`: `answer`, `clarification` or `conversational`.
   - `grounding_status`: required for answers.
   - `citations`: the `source:` URLs of results relied on, copied exactly.

2. **The server verifies.** `split_grounding()` strips the block (the last one wins; unterminated or code-fenced blocks are also removed) and parses it. `verify()` then compares the reply with what `search_knowledge_base` returned this turn.

3. **The verdict decides.** A `Verdict` carries the final text, status, citations and rejection reasons.

## Verification rules

The reply is replaced with `NO_DOCUMENTATION_RESPONSE` and the status becomes `NOT_SUPPORTED` when:

| Check | Reason logged |
|---|---|
| Block missing or malformed | `missing or malformed grounding block` |
| `answer` without a valid status | `missing or invalid grounding_status` |
| Model reports `NOT_SUPPORTED` | (replaced, not counted as a rejection) |
| `SUPPORTED` with no citations | `grounding_status is SUPPORTED but citations is empty` |
| A citation URL not returned by retrieval | `citations not returned by retrieval` |
| No retrieval results but the reply is an answer or has factual markers (URLs, code, paths) | `retrieval returned no results but the answer contains factual guidance` |
| A URL in the reply not in retrieved text/URLs, the user's message, or the support contacts (email and Teams link from `NO_DOCUMENTATION_RESPONSE`) | `undocumented URLs` |
| A code value, role/claim, config key/value or endpoint path not found in the evidence | `undocumented values` |
| Empty body after stripping | `empty answer` |

### How values are extracted and matched

- URLs: `http(s)://` and `mailto:`. Compared by key (no `#fragment`, trailing slash or case).
- Values checked: inline `` `code` ``, quoted strings and paths inside fenced code blocks, and endpoint-like paths with 2+ segments (e.g. `/auth/realms/standard`).
- Evidence corpus: retrieved chunk text and URIs, the user's prompt, `NO_DOCUMENTATION_RESPONSE`, and contact URLs; normalised (lowercased, whitespace collapsed, backticks removed).
- Placeholders (`<name>`, `{{x}}`, `${X}`, `YOUR_…`, `...`) are ignored; only the literal fragments around them must be documented.
- A URL whose whole host is a placeholder (`https://<your-app-host>/callback`) is a user-supplied value and is not checked. A host that only starts with one (`https://<env>.loginproxy.gov.bc.ca/...`) is still checked.
- Example URLs on hosts reserved for documentation and testing (RFC 2606 / 6761: `example.com`, `example.net`, `example.org` and their subdomains, `*.example`, `*.test`, `*.invalid`, `*.localhost`) and loopback hosts (`localhost`, `127.0.0.1`, `::1`, `0.0.0.0`) are allowed. They can never be real BC Gov services. The host is parsed, so lookalikes such as `example.com.attacker.ca`, `notexample.com` or `localhost.gov.bc.ca` are still rejected.
- URLs are found by `_URL_RE` (`http(s)://` or `mailto:` up to whitespace or `<>()[]"'` and backticks). The model writes them; nothing generates them. Realistic-looking invented examples (`https://your-app.gov.bc.ca/callback`) and addresses from the model's memory (`https://sso.gov.bc.ca/`) are rejected; reserved example and loopback hosts are not.
- Matching is exact string containment, **not** a semantic judge. Plain-prose claims are not verified, which is why the prompt asks the model to write every role, claim, config value and endpoint as `code`.

## Citations

For accepted answers, the sources are the cited URLs plus any retrieved page the answer links to, deduplicated by URL key. Each maps to its best-scoring retrieved result (`title`, `uri`, `score`).

## Runtime behaviour

In `ChatService.stream`:

1. The agent's output is buffered (with `<thinking>` blocks removed by `ThinkingFilter`); the client only sees `status` progress chunks (`searching`, `writing`) and heartbeats meanwhile.
2. `verify()` runs on the full reply.
3. **Repair attempt.** If the reply was rejected (or cut off at `MODEL_MAX_TOKENS`, which raises Strands' `MaxTokensReachedException` and is treated as a rejection asking for a shorter reply), the search returned results, the client is still connected and `GROUNDING_REPAIR_ENABLED` is on, the agent is run once more:
   - It receives `repair_request(reasons)` ([system_prompt.py](../../app/prompts/system_prompt.py)), which lists the rejection reasons and asks for a complete rewrite using only this turn's results.
   - The search tool won't search again in the same turn (it returns `ALREADY_SEARCHED`), so the evidence is unchanged; the results are already in the conversation.
   - `verify()` runs again on the rewrite.
   - `drop_repair_exchange()` removes the repair request and the rejected reply from the history.
   - Not repaired: a model-reported `NOT_SUPPORTED`, or a rejection with no retrieval results. Neither has evidence to fix it from.
4. Rejections are logged as `{"event": "answer_rejected", "attempt": "first" | "repair", "reasons": [...], "retrieved": N}`, and repair outcomes as `{"event": "answer_repair", "repaired": bool}`.
5. If the final text was replaced, `replace_last_answer()` rewrites the stored assistant message (including a `NOT_SUPPORTED` grounding block), so the rejected answer is not reused as context next turn. After a successful repair the cleaned history is saved with `save_session()`.
6. One `Answer(text, grounding_status, citations)` is yielded.

Example: for "how to request an integration for my app using css", a reply that wrote the CSS app URL from memory (`https://css.pathfinder.gov.bc.ca`, not in the docs) and added `token_endpoint` / `userinfo_endpoint` details from another page was rejected. The repair request names those values, and the agent rewrites the reply using the documented `https://sso-requests.apps.gold.devops.gov.bc.ca/` from the retrieved result.

## Wire format

The `/chat` route (see [event-streams.md](./event-streams.md)) sends the answer as one `content` chunk, then:

```text
data: {... "delta": {"grounding_status": "SUPPORTED"} ...}
data: {... "delta": {"citations": [{"title": "...", "location": {"uri": "..."}, "score": 0.81}]} ...}
data: {... "delta": {}, "finish_reason": "stop"}
data: [DONE]
```

`grounding_status` is omitted when `None`; `citations` is omitted when empty.

## Related

- Evidence comes from [query decomposition](./query-decomposition.md) (`CITATIONS_KEY` collection in the tool).
- Prompt rules: `GROUNDING REPORT` section of [system_prompt.py](../../app/prompts/system_prompt.py).
- Reply text: `NO_DOCUMENTATION_RESPONSE` in [constants.py](../../app/common/constants.py).
