# Plan: SSO Buddy Teams Bot

Status: proposed · Scope: MS Teams integration · Replaces: flow 4 "MS Teams channel via MCP" in [architecture.md](../architecture/architecture.md)

## 1. Summary

Build **SSO Buddy** (`ssobuddy`), a Microsoft Teams bot that answers SSO
questions in Teams channels, group chats and 1:1 chats. Which of these
are active is set in configuration. In channels, the bot is active only
in the channel IDs on an allow-list.

The bot receives every message in the places it is installed. It answers:

- **Messages that start with `@ssobuddy`.** This can be a real Teams
  @mention of the bot or the literal text `@ssobuddy`.
- **Replies in an engaged channel thread, with or without the prefix.** A
  thread becomes *engaged* when its root post **or any reply in it** starts
  with `@ssobuddy`. From then on, the bot answers every later reply in that
  thread.
- **Every message in a 1:1 chat**, when 1:1 chats are enabled.

Everything else is ignored silently.

For each message it answers, the bot:

1. strips the prefix,
2. sends the question to the existing SSO RAG API (`POST /chat`, SSE),
3. collects the whole answer and its citations, and
4. posts the answer as a reply in the same thread or chat.

History is kept per thread, and per chat for chats, so follow-up questions
have context.

The bot is a new, separately deployable Python service in
[ssobuddy/](../../ssobuddy). It is built on the **Microsoft Teams SDK for
Python** (`microsoft-teams-apps`), hosted on FastAPI through
`FastAPIAdapter`. It runs as its own ECS service behind its **own API
Gateway**, which has its own default `execute-api` hostname.

### 1.1 Decisions (from review)

| # | Decision |
|---|---|
| D1 | `@ssobuddy` in **any** thread reply engages that whole thread, not only a prefixed root post |
| D2 | Channels, group chats and 1:1 chats are all supported. Each one is switched on or off in configuration (`ENABLED_SCOPES`) |
| D3 | Channels are allow-listed by channel ID (`ALLOWED_CHANNEL_IDS`). The bot ignores every channel that isn't on the list |
| D4 | BC Gov M365 admins approve every install, and they grant the RSC permissions as part of that approval |
| D5 | The bot gets its own hostname: the **default AWS hostname** of a dedicated API Gateway REST API, `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/api/messages`. A custom DNS name is out of scope for now |
| D6 | There is **no test M365 tenant**. All Teams testing happens in the BC Gov tenant, using a separate `ssobuddy-dev` app that admins approve once for a private test team (§7.2). Most iteration happens locally first, in the Agents Playground and unit tests |
| D7 | The group-chat follow-up window (M2) is **on by default**, for 5 minutes |

## 2. Why a bot rather than the MCP / Graph poller

The proposed flow 4 polls Graph and runs into a problem it already notes.
App-only Graph **cannot post** channel messages, so posting needs a service
account or delegated token. A Teams bot avoids this:

| Concern | Graph poller (flow 4) | Teams bot (this plan) |
|---|---|---|
| Reading every message | `ChannelMessage.Read.All` (tenant-wide) or RSC | RSC `ChannelMessage.Read.Group` / `ChatMessage.Read.Chat`, granted per install |
| Posting replies | Needs delegated `ChannelMessage.Send` (a service account) | Native: the bot replies as itself |
| Chats | Separate Graph APIs and permissions | The same handler, the same code path |
| Latency | Polling interval | Push. Teams POSTs each message to `/api/messages` |
| Thread context | Built by hand from `messages/delta` | `conversation.id` already identifies the thread |
| Coupling to the API | In-process `ChatService` | HTTP call to the RAG API, as the user asked |

## 3. Key Teams platform facts the design relies on

1. **Without RSC, a bot in a channel or group chat only receives messages
   that @mention it.**
   - A plain-text `@ssobuddy` and an un-prefixed thread reply would never
     reach the bot. So the manifest requests these RSC permissions:
     - `ChannelMessage.Read.Group` for channels,
     - `ChatMessage.Read.Chat` for group chats.
   - With them, Teams delivers **all** messages in that team or chat to the
     bot. The bot then filters by scope and allow-list itself (§4.1).
   - 1:1 chats always deliver every message.
2. **Conversation identity.**
   - **Channels:** `activity.conversation.id` is
     `19:<channel>@thread.tacv2;messageid=<rootMessageId>`.
     - For a top-level post, `rootMessageId == activity.id`.
     - For a reply, `rootMessageId` is the thread root.

     The base channel ID is `activity.channel_data.channel.id`, which is
     `19:<channel>@thread.tacv2`.
   - **Group chats** (`conversation.conversation_type == "groupChat"`) have
     no threads. The conversation ID is the chat ID, `19:…@thread.v2`.
   - **1:1 chats** have `conversation_type == "personal"`. The conversation
     ID is unique per user and bot.
3. **Mentions.** A real @mention arrives as `<at>ssobuddy</at> …` in
   `activity.text`, plus a `mention` entity whose `mentioned.id ==
   activity.recipient.id`. The bot's short name in the manifest is
   **`ssobuddy`**, so the mention a user picks matches the prefix.
4. **Response timeout.** Teams expects the webhook to return within about
   15 s, and it retries on timeouts. A RAG answer, including grounding, can
   take longer. So the handler acknowledges at once (typing indicator),
   processes the message in a background task, and replies proactively:
   - in channels, with `app.reply(channel_conv_id, root_message_id, …)`;
   - in chats, with `app.send(conversation_id, …)`.
5. **SDK, not Bot Framework.** Use `App`, `@app.on_message`, `ctx.reply`,
   `app.reply`, `app.send`, and
   `MessageActivityInput().add_ai_generated().add_citation(...)`. Do not use
   `TurnContext` or `send_activity`.
6. **The manifest is fixed at approval time.** Because admins approve every
   install (D4), changing scopes or RSC permissions means a new app version
   and re-approval. So the manifest declares **all** scopes and RSC
   permissions from the start, and the bot switches behaviour at runtime
   through configuration (D2, D3).

## 4. Trigger rules

### 4.1 Gate: scope and allow-list (evaluated first)

| # | Condition | Action |
|---|---|---|
| G1 | The sender is a bot, including SSO Buddy itself | Ignore |
| G2 | The activity isn't a new `message` (edits, deletes, reactions, `conversationUpdate`) | Ignore. Installs are only logged (§6.6) |
| G3 | The conversation's scope (`channel`, `groupChat` or `personal`) isn't in `ENABLED_SCOPES` | Ignore |
| G4 | Channel, and `channel_data.channel.id` isn't in `ALLOWED_CHANNEL_IDS` | Ignore. This fails closed: an empty list means no channels |
| G5 | Group chat, and `ALLOWED_GROUP_CHAT_IDS` isn't `*` and doesn't contain the chat ID | Ignore |
| G6 | `ALLOWED_TENANT_IDS` is set and `channel_data.tenant.id` isn't in it | Ignore |

Ignored messages are dropped **before** anything else happens: no S3
access, no RAG call, and no message text in the logs.

### 4.2 Channel rules

Inputs:

- `is_root` means `root_id == activity.id`.
- `has_prefix` means one of these is true:
  - after stripping HTML and whitespace, the text starts with `@ssobuddy`
    (case-insensitive);
  - the first mention entity points at the bot.
- `thread_engaged` means the root conversation ID is in the engaged-thread
  registry (§6.3).

| # | Message | Condition | Action |
|---|---|---|---|
| C1 | Top-level post | `has_prefix` | Mark the thread engaged, then answer |
| C2 | Top-level post | not `has_prefix` | Ignore |
| C3 | Thread reply | `thread_engaged` | Answer. The prefix is optional and stripped if present |
| C4 | Thread reply | not `thread_engaged` **and** `has_prefix` | **Mark the thread engaged (D1)**, then answer |
| C5 | Thread reply | not `thread_engaged` and not `has_prefix` | Ignore |

### 4.3 Chat rules

| # | Scope | Condition | Action |
|---|---|---|---|
| P1 | 1:1 chat | Any text | Answer. The prefix is optional, unless `PERSONAL_REQUIRE_PREFIX=true` |
| M1 | Group chat | `has_prefix` | Answer, and open a follow-up window for this chat (see M2) |
| M2 | Group chat | not `has_prefix`, but a message from the **same user** arrives within `GROUP_CHAT_FOLLOWUP_SECONDS` (default `300`) of the bot's last answer to them | Answer, and reset that user's window (D7) |
| M3 | Group chat | Otherwise | Ignore |

Group chats have no threads, so "engage the whole conversation" would make
the bot answer everyone indefinitely. Instead, M2 gives the person who
asked a time-boxed follow-up window. Each answer resets the window. Other
members still need the prefix. Set `GROUP_CHAT_FOLLOWUP_SECONDS=0` to
require the prefix on every message.

### 4.4 Common rules

| # | Condition | Action |
|---|---|---|
| X1 | The prefix with no question after it | Reply with a short usage hint (once per conversation per 10 minutes) |
| X2 | Text longer than `MAX_QUESTION_CHARS` (default 4000) | Reply asking for a shorter question |

### 4.5 Prefix normalisation

Before the text is sent to the RAG API:

1. Remove `<at>…</at>` segments that belong to the bot's mention entities.
2. Remove a leading literal `@ssobuddy` (case-insensitive) plus any
   punctuation and whitespace after it (`:`, `,`, `-`).
3. Strip the remaining HTML. Teams sends `textFormat: "xml"` or `"plain"`.
   Unescape entities and collapse whitespace.

## 5. Architecture

```
Teams (channel / group chat / 1:1)
   │ POST https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/api/messages   (Bot Framework JWT)
   ▼
API Gateway (dedicated REST API, default execute-api hostname)
   │ VPC Link → Host: ssobuddy.internal
   ▼
Internal ALB ── rule: host=ssobuddy.internal AND path=/api/messages ──► sso-rag-ssobuddy (ECS, :3978)
                                                                         │ FastAPI + Teams SDK App
                                                                         │ 1. gate (§4.1) + classify (§4.2–4.4)
                                                                         │ 2. typing indicator
                                                                         │ 3. background task:
                                                                         │    POST http://<internal-alb>/chat (SSE)
                                                                         │    Cookie: strands_session=session_<uuid5(conversation key)>
                                                                         │ 4. app.reply / app.send (answer + citations)
                                                                         ▼
                                                     S3 (engaged-thread + dedup markers)
                                                     SSM Parameter Store (allow-lists, scopes)
```

The RAG API is called over the **internal** ALB from inside the VPC. It
never goes back out through the public gateway.

### 5.1 Components (new)

| Path | Purpose |
|---|---|
| `ssobuddy/pyproject.toml` | Standalone uv project. Deps: `microsoft-teams-apps`, `fastapi`, `uvicorn`, `httpx`, `pydantic-settings`, `boto3`. Dev deps: `pytest`, `pytest-asyncio`, `respx`, `moto` |
| `ssobuddy/src/main.py` | FastAPI app, `App(...)` with `FastAPIAdapter`, `/health`, and startup and shutdown (drains in-flight answers) |
| `ssobuddy/src/config.py` | `Settings` (§5.2) and `RuntimeConfig`, which refreshes the allow-lists and scopes from SSM |
| `ssobuddy/src/handlers.py` | `@app.on_message`, which gates, classifies and dispatches. Install and conversation-update handlers log the IDs (§6.6) |
| `ssobuddy/src/triggers.py` | Pure functions: `scope_of()`, `parse_root_id()`, `has_prefix()`, `strip_prefix()`, `gate()`, `classify()` → `Answer` / `Ignore` / `Hint`. Unit-tested heavily |
| `ssobuddy/src/rag_client.py` | `httpx.AsyncClient` SSE consumer for `/chat`. Returns `RagAnswer(text, citations, grounding_status)`. Raises on an in-band `{"error": …}` |
| `ssobuddy/src/thread_store.py` | `ThreadStore` protocol, with `InMemoryThreadStore` (TTL dict) and `S3ThreadStore` (marker objects) |
| `ssobuddy/src/dedup.py` | Activity-ID dedup, in memory and with an S3 conditional put |
| `ssobuddy/src/formatting.py` | Converts the RAG Markdown to a Teams message. Adds citations, the AI-generated label and a "Sources" list |
| `ssobuddy/appPackage/manifest.json` (+ icons) | Teams manifest (§7) |
| `ssobuddy/Dockerfile` | Slim Python image, `uv sync --frozen`, `uvicorn src.main:api --host 0.0.0.0 --port 3978` |
| `ssobuddy/tests/` | pytest: triggers, gate, rag_client (respx-mocked SSE), thread_store and dedup (moto), handler flow with a fake `ctx` |
| `ssobuddy/README.md` | Setup, local dev (devtunnel), configuration, the admin approval process, and how to find a channel ID |

> The bot is a **separate deployable**. It does not import `app.*`. It only
> talks to the RAG API over HTTP, so either side can be released on its own.

### 5.2 Configuration

| Setting | Default | Notes |
|---|---|---|
| `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` | – | The bot identity, from Secrets Manager |
| `RAG_API_URL` | – | The internal ALB URL, e.g. `http://internal-…elb.amazonaws.com` |
| `RAG_API_TIMEOUT_S` | `120` | Read timeout for the SSE stream |
| `RAG_MAX_CONCURRENCY` | `8` | Concurrent RAG calls per task |
| `BOT_PREFIX` | `@ssobuddy` | Case-insensitive |
| `ENABLED_SCOPES` | `channel` | Comma list of `channel`, `groupChat` and `personal` (D2) |
| `ALLOWED_CHANNEL_IDS` | *(empty, so no channels)* | Comma list of `19:…@thread.tacv2`, or `*` (D3) |
| `ALLOWED_GROUP_CHAT_IDS` | `*` | Comma list of `19:…@thread.v2`, or `*`. Admins already approve each install (D4) |
| `ALLOWED_TENANT_IDS` | `TENANT_ID` | Defence in depth |
| `PERSONAL_REQUIRE_PREFIX` | `false` | |
| `GROUP_CHAT_FOLLOWUP_SECONDS` | `300` | The per-user follow-up window in group chats (M2). `0` turns it off |
| `THREAD_STORE` | `memory` | `s3` in AWS |
| `STATE_S3_BUCKET`, `STATE_S3_PREFIX` | –, `ssobuddy/` | Engaged-thread and dedup markers |
| `THREAD_TTL_DAYS` | `14` | Matches the S3 lifecycle rule |
| `RUNTIME_CONFIG_SSM_PATH` | *(unset)* | e.g. `/sso-rag/<env>/ssobuddy/`. When set, `ENABLED_SCOPES` and the `ALLOWED_*` values are read from SSM and refreshed every `RUNTIME_CONFIG_REFRESH_S` (300 s). Channels can then be added or removed **without a redeploy**. Environment variables are the fallback |

**How to find a channel ID:**

- In Teams, use **⋯ → Get link to channel**. The URL contains
  `19%3a…%40thread.tacv2`; URL-decode it.
- Alternatively, the bot logs the team, channel and chat IDs (with team
  and channel names) when it is installed (§6.6).

## 6. Detailed behaviour

### 6.1 Message handler flow

```python
@app.on_message
async def on_message(ctx):
    a = ctx.activity
    if not gate(a, runtime_config.current()):              # §4.1
        return
    decision = await classify(a, bot_id=a.recipient.id, store=thread_store)   # §4.2–4.4
    if decision.kind is Kind.IGNORE:
        return
    if not await dedup.first_time(a.id):                   # Teams retries on timeout
        return
    if decision.kind is Kind.HINT:
        await ctx.reply(decision.hint); return
    if decision.mark_engaged:                              # C1, C4
        await thread_store.mark(decision.conversation_key)
    await ctx.reply(TypingActivityInput())
    tasks.spawn(answer(decision))                          # tracked; drained on shutdown
```

`decision.conversation_key` is set as follows:

- **Channel:** the root conversation ID, `19:…@thread.tacv2;messageid=<root>`.
- **Group chat:** the chat ID.
- **1:1 chat:** the conversation ID.

`answer()`:

1. Calls `rag_client.ask(question, session_id=session_id_for(conversation_key))`.
2. Builds the reply: `MessageActivityInput(text=md).add_ai_generated()`,
   plus a citation for each source the answer actually uses.
3. Posts the reply:
   - **Channel:** `app.reply(channel_id, root_message_id, reply)`. This keeps
     the answer in the thread, even when the trigger was a reply.
   - **Group chat:** `app.send(conversation_id, reply)` with a quote or
     mention of the asker, so it's clear which question it answers.
   - **1:1 chat:** `app.send(conversation_id, reply)`.
4. On failure, posts *"Sorry, I couldn't get an answer right now. Please
   try again or contact the SSO team."* and logs it with the activity ID.

Concurrency:

- A semaphore limits concurrent RAG calls per task
  (`RAG_MAX_CONCURRENCY`).
- A per-conversation `asyncio.Lock` serialises messages in the same
  thread or chat, so history is applied in order.

### 6.2 Calling the SSO RAG API

- `POST {RAG_API_URL}/chat` with `{"prompt": question}`, and
  `Accept: text/event-stream`.
- **History per conversation, without a server change.**
  - The API keys history on the `strands_session` cookie and accepts only
    `session_<uuid>` ([rag_agent.py](../../app/agents/rag_agent.py)).
  - The bot sends
    `Cookie: strands_session=session_<uuid5(NS_SSOBUDDY, conversation_key)>`.
  - The ID is deterministic, so no cookie store is needed, it survives
    restarts, and it passes `is_valid_session_id`.
  - The existing retention job (`SESSION_RETENTION_DAYS`) cleans these
    sessions up.
- SSE parsing keeps:
  - `delta.content`, appended to the answer text
  - `delta.citations` (`title`, `location.uri`, `score`)
  - `delta.grounding_status`
  - a top-level `error`, which raises `RagError`

  It ignores `delta.status`, keep-alive comments and `[DONE]`.
- Timeouts: connect 5 s, read `RAG_API_TIMEOUT_S`. Retry once only on
  connect errors or 502/503/504 before the first byte.
- `NO_DOCUMENTATION_RESPONSE` is posted unchanged. It already contains the
  escalation text.

### 6.3 Engaged-thread registry (channels)

- The key is the root conversation ID, which includes the channel, so it
  is unique.
- `mark()` is called on C1 and C4. `is_engaged()` is checked on every
  channel reply that passes the gate.
- The **S3 store** is the default in AWS:
  - one marker per thread at `ssobuddy/threads/<sha256(key)>`;
  - it sits behind an in-process cache (positive results only), so most
    replies cost no S3 call;
  - expiry uses an S3 lifecycle rule (`THREAD_TTL_DAYS`).
- The **memory store** is for local development and tests.
- The group-chat follow-up window (M2) is held in memory only. It is short
  and best-effort.
- **Graph fallback (optional, phase 4):** on a cache and S3 miss for a
  reply, the bot can read the root post and earlier replies through Graph
  (`GET /teams/{teamId}/channels/{channelId}/messages/{rootId}` and
  `/replies`), using an app-only token. RSC `ChannelMessage.Read.Group`
  allows this for the approved team. If any of them starts with the prefix,
  the bot marks the thread and answers. This covers threads engaged before
  a marker expired or before the bot was installed.

### 6.4 Idempotency

Teams can redeliver an activity. `dedup.first_time(activity.id)` uses:

- an in-memory TTL set for 10 minutes, and
- an S3 conditional put (`If-None-Match: *` on
  `ssobuddy/dedup/<activity.id>`) with a one-day lifecycle rule, which
  covers more than one task.

### 6.5 Formatting

- The RAG returns Markdown, and Teams renders a subset of it in message
  text. The bot keeps headings, lists, code and links, and converts
  anything Teams doesn't render well, such as wide tables, to lists.
- Citations use the SDK's `add_citation(position, Appearance(...))`, with
  `[n]` markers where the RAG doesn't add them. A plain "Sources:" list is
  also appended so mobile clients render it.
- When `grounding_status == PARTIALLY_SUPPORTED`, the bot adds a short
  note in italics.
- `add_feedback()` is optional (phase 4). It enables thumbs up/down, which
  are logged for evals.

### 6.6 Install and onboarding events

On `installationUpdate` / `conversationUpdate` (bot added), the bot logs at
INFO:

- tenant ID, team ID, team name and channel IDs and names (for teams), or
- the chat ID (for group chats).

This lets operators copy IDs into the allow-list. **The bot posts no
welcome message**, so it stays silent in channels that aren't enabled.

## 7. Teams app manifest essentials

```jsonc
{
  "manifestVersion": "1.19",
  "version": "1.0.0",
  "name": { "short": "ssobuddy", "full": "SSO Buddy" },
  "bots": [{
    "botId": "${{CLIENT_ID}}",
    "scopes": ["team", "groupChat", "personal"],   // all declared up front; runtime-gated (D2)
    "supportsFiles": false,
    "isNotificationOnly": false,
    "commandLists": [{ "scopes": ["team", "groupChat", "personal"],
                       "commands": [{ "title": "ask", "description": "@ssobuddy <your SSO question>" }] }]
  }],
  "webApplicationInfo": { "id": "${{CLIENT_ID}}", "resource": "api://botid-${{CLIENT_ID}}" },
  "authorization": {
    "permissions": {
      "resourceSpecific": [
        { "name": "ChannelMessage.Read.Group", "type": "Application" },
        { "name": "ChatMessage.Read.Chat",    "type": "Application" }
      ]
    }
  },
  "validDomains": []
}
```

- `webApplicationInfo` is **required** for RSC permissions to apply.
- Both RSC permissions are declared even if a scope starts disabled. This
  avoids re-approval when a scope is switched on later (§3.6).
- **Where the endpoint lives.** The messaging endpoint is stored in the
  **bot registration** (§8.5), not in the manifest. `validDomains` is only
  needed for tabs and link unfurling, so it stays empty.
  - Changing the endpoint, for example after the `execute-api` ID changes,
    is a `teams app update --endpoint …` or Azure Bot change. It needs no
    new package.
  - If the CLI adds the domain to `validDomains` on an endpoint update,
    remove it before packaging, or accept a version bump.
  - The REST API also gets `lifecycle { prevent_destroy = true }`, so its
    ID, and with it the URL, stays stable.

### 7.1 Admin approval process (D4)

1. **Package.** CI builds `ssobuddy-<version>.zip` (manifest and icons) for
   each environment.
2. **Submit.** The SSO team submits the package to the BC Gov Teams admins
   to publish it in the org app catalog. The request explains the RSC
   permissions, what the bot reads (all messages, so it can see the
   prefix), what it keeps (only triggered questions, under the existing
   RAG retention), and the allow-list controls.
3. **Approve and install.** For each target team or group chat, the admins
   (or a team owner the admins have permitted) install the app. That
   grants the RSC consent. Admins can use app permission and setup
   policies to limit who can install it.
4. **Enable.** The operator adds the channel IDs, or group-chat IDs, to the
   SSM allow-list. The bot starts answering within
   `RUNTIME_CONFIG_REFRESH_S`, with no redeploy.
5. **Version bumps.** A new package version is needed **only** for
   manifest changes, such as name, icons, permissions or scopes. Bot code,
   configuration and endpoint changes don't need re-approval.

### 7.2 Testing without a test tenant (D6)

There is no separate M365 tenant, so testing is layered to keep approvals
to a minimum:

1. **Local, no tenant.** Most behaviour is covered by unit tests built on
   recorded activity payloads (§10).
   - Run the bot with the **Microsoft 365 Agents Playground**:

     ```bash
     npm i -g @microsoft/m365agentsplayground
     agentsplayground -e http://localhost:3978/api/messages -c emulator
     ```

   - Set `dangerously_allow_unauthenticated_requests` on `App` in a
     `LOCAL_DEV=true` mode **only**. This is never set in AWS.
   - This exercises prefix handling, the RAG call, formatting and
     citations. It does **not** reproduce channel threads or RSC delivery
     faithfully, so those rely on unit tests and step 2.
2. **`ssobuddy-dev` in the BC Gov tenant.** This is a second app, with its
   own bot identity (§8.5) and app ID.
   - Admins approve it **once** and install it in:
     - a private "SSO Buddy – Dev" team with one or two test channels;
     - a test group chat.
   - Its endpoint points at the sandbox bot API Gateway. While actively
     debugging, it can point temporarily at a devtunnel, **if devtunnels
     are permitted on BC Gov devices**.
   - Endpoint changes don't need re-approval (§7).
   - Ask admins whether a **custom app upload (sideloading) policy** can be
     granted to the SSO developers for that team only. This would make
     manifest iterations self-service.
3. **`ssobuddy` (production) in the BC Gov tenant.** Admins approve it and
   install it in the real support teams (§7.1). It only reaches channels
   once their IDs are on the allow-list.

## 8. Infrastructure and deployment

### 8.1 Bot identity creation (once per app: `ssobuddy-dev`, `ssobuddy`)

Use either option A or option B. The choice is explained in §8.5.

```bash
# Option A – Teams-managed bot (needs app-registration rights in the BC Gov tenant)
teams login
teams app create --name "ssobuddy-dev" \
  --endpoint "https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/api/messages" \
  --env ssobuddy/.env --json
# Later, if the endpoint changes:
teams app update <teamsAppId> --endpoint "https://<new-api-id>.execute-api.<region>.amazonaws.com/<stage>/api/messages"
```

- SSO isn't needed, because the bot calls the RAG API, not Graph on behalf
  of users.
- **Option B:** an Azure contributor creates an *Azure Bot* resource
  (single-tenant, with the existing app ID from Entra). They set its
  messaging endpoint to the same `execute-api` URL and enable the
  Microsoft Teams channel. Alternatively, create it with option A and run
  `teams app bot migrate <teamsAppId> --resource-group <rg>`.
- Next, replace the generated manifest with the one from §7 and send the
  package for approval (§7.1).
- `CLIENT_SECRET` goes in AWS Secrets Manager, never in git. `ssobuddy/.env`
  is git-ignored.

### 8.2 Dedicated API Gateway with its default hostname (D5)

Today, the API Gateway REST API's `{proxy+}` sends everything to the
internal ALB with `Host` set to the ALB DNS name
([apigateway.tf](../../terraform/apigateway.tf)). The bot gets its **own**
REST API, which has a different `execute-api` hostname from the RAG API.
There is no custom domain, ACM certificate or DNS record:

- **New REST API** `${name_prefix}-ssobuddy-api-gw`:
  - a single resource, `/api/messages`, method `POST`, `authorization =
    NONE` (the Teams SDK validates the Bot Framework JWT);
  - an `HTTP_PROXY` integration over the existing VPC link to the internal
    ALB, with `uri = "http://ssobuddy.internal/api/messages"`. With VPC
    link v2 the URI sets the `Host` header, so the ALB can tell bot
    traffic apart;
  - stage throttling (e.g. 50 rps burst / 20 rps steady) and access logs;
  - `lifecycle { prevent_destroy = true }` on the REST API, so its ID and
    URL stay stable.
- **Endpoint URL** (Terraform output `ssobuddy_endpoint`):
  `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/api/messages`.
  The stage name is part of the path, and that's fine for Teams. Paste this
  URL into the bot registration (§8.1).
- A custom domain can be added later, through
  `aws_api_gateway_domain_name` and a base path mapping. This only changes
  the bot registration's endpoint and needs no re-approval.
- **ALB rule:** `host_header = ["ssobuddy.internal"]` **and**
  `path_pattern = ["/api/messages"]` → the bot target group.
  - Requests to `/api/messages` through the main RAG gateway carry the ALB
    host, so they never match and fall through to the UI default.
  - The public `/health` of the bot isn't exposed. Only the ALB health
    check uses it.

### 8.3 AWS (Terraform, in [terraform/](../../terraform))

| File | Change |
|---|---|
| `ecr.tf` | New repo `sso-rag-ssobuddy` |
| `ecs.tf` | New log group, task definition and service `sso-rag-ssobuddy`. `desired_count = 1` to start (see §11). Port 3978. Env: `RAG_API_URL=http://${aws_lb.main.dns_name}`, `THREAD_STORE=s3`, `STATE_S3_BUCKET`, `RUNTIME_CONFIG_SSM_PATH`. Secrets: `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` |
| `alb.tf` | Target group `ssobuddy` (health check `/health`) and a listener rule (host and path, §8.2) |
| `apigateway_ssobuddy.tf` *(new)* | REST API (`prevent_destroy`), `/api` and `/api/messages` resources, `POST` method, VPC link integration, deployment, stage and throttling (§8.2) |
| `iam.tf` | Bot task role: `s3:GetObject`/`PutObject` on `ssobuddy/*`, `ssm:GetParametersByPath` on the runtime config path, and `secretsmanager:GetSecretValue` on the bot secret (execution role) |
| `s3.tf` | Lifecycle rules: `ssobuddy/threads/` expire after `THREAD_TTL_DAYS`, `ssobuddy/dedup/` after 1 day |
| `ssm.tf` *(new)* | `/sso-rag/<env>/ssobuddy/enabled_scopes`, `allowed_channel_ids`, `allowed_group_chat_ids` (`lifecycle { ignore_changes = [value] }`, so operators can edit them) |
| `security_groups.tf` | Bot task SG: ingress 3978 from the ALB SG. Egress 443 to the internet (Bot Connector `smba.trafficmanager.net`, `login.microsoftonline.com`, `graph.microsoft.com`) and 80 to the ALB SG for the RAG API |
| `variables.tf` / `terraform.tfvars.example` | `ssobuddy_image_tag`, `ssobuddy_desired_count`, `ssobuddy_bot_secret_arn` |
| `outputs.tf` | `ssobuddy_endpoint` (the full `execute-api` messaging URL) |

### 8.4 Local development

```bash
cd ssobuddy && uv sync
LOCAL_DEV=true RAG_API_URL=http://localhost:8000 ENABLED_SCOPES=channel,groupChat,personal ALLOWED_CHANNEL_IDS='*' \
  uv run uvicorn src.main:api --port 3978 --reload
agentsplayground -e http://localhost:3978/api/messages -c emulator     # no tenant needed
```

Run the RAG API locally (`uv run python main.py`) or point `RAG_API_URL`
at the sandbox.

For an optional live debug against the `ssobuddy-dev` app in Teams, if
devtunnels are allowed:

```bash
devtunnel create ssobuddy --allow-anonymous
devtunnel port create ssobuddy -p 3978 --protocol auto
devtunnel host ssobuddy
teams app update <devTeamsAppId> --endpoint "https://<tunnel-id>.devtunnels.ms/api/messages"
# afterwards, point it back at the sandbox execute-api URL
```

### 8.5 Bot identity options (Q3)

**What a "bot identity" is.** Three linked pieces:

1. **An Entra ID app registration** in the BC Gov tenant: an app (client)
   ID plus a credential (a client secret). The bot uses it to get tokens
   from `login.microsoftonline.com` when it posts replies. Teams signs
   every request it sends to `/api/messages` with a JWT for this app ID,
   which the SDK validates.
2. **A bot registration.** This links the app ID to the messaging endpoint
   (the `execute-api` URL) and enables the Teams channel. It lives either
   in the Teams Developer Portal (option A) or as an Azure resource
   (option B).
3. **The Teams app package** (manifest). `bots[].botId` and
   `webApplicationInfo.id` are set to the same app ID. This is what admins
   approve (§7.1).

The bot runs in **AWS**, so the Azure-only passwordless options (managed
identity and federated credentials through a managed identity) aren't
available. The SDK will use **client-secret** auth: `CLIENT_ID`,
`CLIENT_SECRET` and `TENANT_ID`. The app registration should be
**single-tenant** (BC Gov tenant only).

| | **A. Teams-managed bot** (Teams Developer Portal / `teams app create`) | **B. Azure Bot Service resource** |
|---|---|---|
| Where the bot registration lives | Microsoft-hosted, under the Teams Developer Portal's Bot management | An `Azure Bot` resource in a BC Gov Azure subscription and resource group |
| Needs an Azure subscription | No | Yes (Contributor on a resource group) |
| Who can create it | A tenant user allowed to register Entra apps: either the tenant allows user app registrations, or the user has the *Application Developer* role. The CLI creates the app registration and secret for you | An Azure contributor. The app registration can be created by Entra admins and **passed in as an existing app ID** |
| Governance and visibility | Owned by the user(s) who created it in the Developer Portal. Add co-owners so it isn't tied to one person. Not visible in Azure policy, RBAC or activity logs | Full Azure governance: RBAC, Azure Policy, tags, activity logs, diagnostics |
| Cost | None | None for Teams (Teams is a standard channel) |
| Features we need (messaging, RSC, proactive replies) | Yes | Yes |
| Extra features | – | OAuth connection settings (needed only if the bot later signs users in, e.g. Graph on their behalf), other channels, Azure monitoring |
| Changing later | `teams app bot migrate <teamsAppId> --resource-group <rg>` moves it to option B without changing the app ID or the manifest | – |
| Secret handling | Same for both: the secret lives in AWS Secrets Manager (the ECS task gets it as a secret env var). Entra caps secret lifetime (up to 24 months, and BC Gov policy may be shorter). **Rotation runbook:** add a new secret in Entra → update Secrets Manager → force a new ECS deployment → delete the old secret. An alarm fires on bot auth failures |

**Split of responsibilities (either option):**

| Who | Does |
|---|---|
| BC Gov Entra / M365 admins | Allow or create the app registrations (`ssobuddy-dev` and `ssobuddy`). Approve the packages in the org catalog. Install them and grant RSC per team or chat. Set the app permission and setup policies |
| BC Gov Azure admins *(option B only)* | Provide a subscription and resource group, and Contributor access, for the Azure Bot resources |
| SSO team | Bot code and AWS infrastructure. Endpoint configuration. Storing and rotating secrets in Secrets Manager. Allow-lists in SSM. Owning or co-owning the bot registrations |

**Recommendation.**

- Use **option A** if BC Gov admins will either:
  - grant a named SSO team member the *Application Developer* role
    (time-boxed is fine), or
  - let the SSO team run `teams app create` with an admin watching.

  This is the fastest route, needs no Azure footprint, and can be migrated
  to option B later without re-approval.
- Use **option B** if BC Gov policy requires every app identity to be
  Azure-governed, or doesn't allow Developer Portal–created registrations.
- Either way, create **two identities** (`ssobuddy-dev` and `ssobuddy`),
  each with at least two SSO team owners.

**Questions to take to the BC Gov admins:**

1. Can SSO team members create app registrations, or will admins create
   them? If admins create them, how is the secret handed over? Preferably
   it goes straight into AWS Secrets Manager, never by email or chat.
2. Is a Teams Developer Portal (Teams-managed) bot registration
   acceptable, or must it be an Azure Bot resource in a BC Gov
   subscription?
3. What is the maximum client-secret lifetime, and is there a rotation
   standard?
4. Can the `ssobuddy-dev` app be approved for a private dev team, ideally
   with custom-app upload allowed for SSO developers in that team only
   (§7.2)?
5. What lead time should we expect for each approval?

## 9. Implementation phases

| Phase | Deliverable | Done when |
|---|---|---|
| **0. Admin engagement** (starts day 1, runs in parallel) | Take the §8.5 questions to the BC Gov admins. Agree option A or B, the dev team, and the approval lead times | Decision on the identity option. `ssobuddy-dev` identity created |
| **1. Skeleton (local)** | `teams project new py ssobuddy -t echo` scaffold in `ssobuddy/`, switched to `FastAPIAdapter`, with the full manifest (§7), `LOCAL_DEV` mode, Agents Playground | The echo bot works in the Agents Playground |
| **2. Gate and triggers (local)** | `triggers.py` (G1–G6, C1–C5, P1, M1–M3, X1–X2), static config, in-memory thread store, recorded Teams activity fixtures for channel, thread, group chat and 1:1 | Unit tests cover every rule |
| **3. RAG integration (local)** | `rag_client.py` (SSE), per-conversation session cookie, background answer with `app.reply` / `app.send`, typing indicator, error reply, concurrency and ordering | Questions in the Agents Playground get grounded answers, and follow-ups use history |
| **4. AWS sandbox** | Dockerfile, Terraform (§8.2, §8.3), S3 thread and dedup stores, the SSM runtime config with refresh, Secrets Manager, CloudWatch alarms (5xx, RAG latency, error replies, auth failures) | `ssobuddy_endpoint` (`execute-api`) returns 401 for unauthenticated POSTs and the ALB health check is green |
| **5. Dev app in Teams** | `ssobuddy-dev` package approved and installed in the private dev team and a test group chat. Endpoint set to the sandbox `execute-api` URL | The manual acceptance checklist (§10) passes, including RSC delivery and threads |
| **6. Polish** | Citations, the AI-generated label, the usage hint, install-event ID logging, optional feedback buttons, optional Graph fallback | UX reviewed with the SSO team in the dev team |
| **7. Production approval and rollout** | `ssobuddy` identity and package, admin submission (§7.1), production stack, install in the first support team, channel added to the allow-list | The bot answers in the approved channel |
| **8. Docs** | `ssobuddy/README.md`; [architecture.md](../architecture/architecture.md) flow 4 rewritten as "Teams bot"; [README.md](../../README.md) updated | Docs reviewed |

## 10. Testing

- **Unit tests** (pytest, no network):
  - `scope_of` and `parse_root_id` for a channel root, a channel reply, a
    group chat and a 1:1 chat.
  - `has_prefix` and `strip_prefix` with `<at>` mentions, plain text,
    mixed case, punctuation, HTML entities, another user mentioned first,
    and the prefix mid-text (no trigger).
  - `gate`:
    - a disabled scope;
    - a channel that isn't allow-listed, an empty list and `*`;
    - a group-chat allow-list;
    - a foreign tenant;
    - a bot sender.
  - `classify`:
    - every rule in C, P, M and X;
    - **C4 followed by C3**: a prefixed reply engages the thread, and the
      next un-prefixed reply is answered;
    - the M2 window opens and expires.
  - `rag_client` against recorded SSE streams: a normal answer, an answer
    with citations, the in-band error, a timeout and a 503 retry.
  - `session_id_for` is deterministic and passes the API's
    `is_valid_session_id`.
  - `ThreadStore` and `dedup` against moto/S3 and the memory
    implementations.
  - `RuntimeConfig` refreshes from SSM (moto) and falls back to
    environment variables.
- **Handler tests** with a fake `ctx` and fake `app.reply` / `app.send`,
  which check:
  - the reply goes to the thread root in channels;
  - ignored messages produce no calls and no logged text.
- **Manual acceptance** (the `ssobuddy-dev` app in the private dev team, against the sandbox stack):
  1. A channel that isn't allow-listed: a prefixed post gets no reply.
  2. An allow-listed channel, top-level post without the prefix: no reply.
  3. `@ssobuddy how do I add a redirect URI?`, as a real mention and as
     plain text: an answer in the thread.
  4. A follow-up in that thread without the prefix: an answer that uses
     context.
  5. An unrelated thread, reply without the prefix: ignored.
  6. An unrelated thread, reply **with** the prefix: an answer, after
     which a reply without the prefix in that thread also gets an answer
     (D1).
  7. A group chat:
     - a prefixed message gets an answer;
     - the **same user's** un-prefixed follow-up within 5 minutes gets an
       answer;
     - **another user's** un-prefixed message is ignored;
     - after the window expires, an un-prefixed message is ignored.
  8. A 1:1 chat: any message gets an answer.
  9. Remove `groupChat` from `ENABLED_SCOPES` in SSM: within 5 minutes,
     group chats are ignored.
  10. Only `@ssobuddy`: the usage hint.
  11. With the RAG API down: the apology message, and the error is logged.
  12. Two quick questions in the same thread are answered in order.

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Admin approval lead time delays rollout (there is no test tenant) | Do most of the work locally (Agents Playground and unit tests). Ask for one-time approval of `ssobuddy-dev` early. Declare all scopes and RSC permissions up front, so later changes are configuration-only |
| Testing in the production tenant leaks into real teams | `ssobuddy-dev` is a separate app, installed only in a private dev team, with channel allow-lists that fail closed |
| The `execute-api` URL changes (the REST API is recreated) | `prevent_destroy` on the REST API. The endpoint is updated in the bot registration only, with no re-approval |
| Admins decline RSC | Fallback mode: only real @mentions are delivered. C3 then needs a real mention in each reply. Document this in the approval request |
| RAG latency over 15 s causes Teams retries and duplicate answers | Ack fast, process in the background, dedup on `activity.id` |
| Volume: the bot receives every message in installed teams and chats | The gate is O(1) and in memory. Only triggered messages touch S3 or the RAG API |
| Privacy: the bot can read all messages where it is installed | Ignored messages are never logged or stored. Triggered questions follow the RAG retention. Allow-lists fail closed for channels |
| Group-chat noise | The follow-up window covers only the user who asked, lasts 5 minutes and is configurable. Set it to `0` to turn it off |
| Multiple bot replicas | Dedup covers retries. Per-conversation ordering needs a single consumer, so start with `desired_count = 1`. If scaling out, route by conversation (e.g. SQS FIFO with `MessageGroupId = conversation_key`) |
| Public endpoint abuse | The JWT is validated by the SDK, plus API Gateway throttling. Optionally attach AWS WAF to the bot stage |
| Client secret expiry | A Secrets Manager rotation runbook, plus an alarm on bot auth failures |

## 12. Open questions

- ~~Q1. Custom DNS~~: resolved as D5. Use the default `execute-api`
  hostname for now.
- ~~Q2. Test tenant~~: resolved as D6. There is none. Use local tooling and
  the `ssobuddy-dev` app in a private dev team.
- **Q3. Bot identity.** Choose option A (Teams-managed) or option B (Azure
  Bot Service), and settle who creates the app registrations and secrets.
  This depends on the BC Gov admins' answers to the questions in §8.5. The
  recommendation is option A, with two owners and the secret in AWS
  Secrets Manager.
- ~~Q4. Group-chat follow-up~~: resolved as D7. It is on by default, for 5
  minutes, for the same user.
