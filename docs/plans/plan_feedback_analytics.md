# Storing chat turns and thumbs up/down feedback

Status: implemented (see [Implementation notes](#implementation-notes)).
Decisions confirmed (see [Decisions](#decisions)). How the feature works and
how to query it: [analytics.md](../components/analytics.md).

## Problem and approach

There is no record of what users ask or how good the answers are once a
conversation is deleted. Stored conversations live only 3 days and users can
delete them (`DELETE /chat/history`). So they can't be used for analysis.

Store every `/chat` turn (question, answer, pipeline metadata) in a separate
DynamoDB table for 6 months. Let users rate each answer with an optional 👍/👎.
Build the web UI only for now, but shape the data and the feedback service so
Microsoft Teams ratings can be added later without changing the table.

For local development, a Docker Compose file runs the UI, the API and DynamoDB
Local together.

## Decisions

| # | Decision |
| --- | --- |
| 1 | Turns and feedback are kept for **6 months** (DynamoDB TTL, 180 days). Point-in-time recovery stays at its **35-day** maximum, so a deleted item can still be restored from backup for up to 35 days after TTL removes it. |
| 2 | **Delete history does not delete analytics records.** It still deletes the conversation history and clears the cookie, as it does today. |
| 3 | **Question and answer text are stored** in full for analysis. Only credential-shaped strings are masked (see [Credential masking](#credential-masking)). |
| 4 | **Thumbs only.** No reason chips or free-text comments. |
| 5 | **No Teams-specific code now.** The data model and feedback service leave room for it (see [Room for Teams](#room-for-microsoft-teams-later)). |
| 6 | **Docker Compose** for local development: UI, API and `amazon/dynamodb-local`. |
| 7 | **The rating sort key is `FB#<rater_hash>`**, without the source. The source is only an attribute. |
| 8 | **Abandoned turns are not recorded.** When the client disconnects, the turn is cancelled and nothing is written. |
| 9 | **DynamoDB, not Postgres.** Rated turns (👍, 👎 or both) for a day or a date range such as the last 30 days are read straight from DynamoDB through one index (see [Reviewing ratings](#reviewing-ratings)). There is no S3 export or Athena. |

## Starting points in the current code

- [app/api/routes/chat.py](../../app/api/routes/chat.py) already creates a
  `completion_id` for each turn and sends it as every chunk's `id`. Reuse it as
  the **turn id** that feedback refers to.
- [app/common/turn_report.py](../../app/common/turn_report.py) already records
  the rewriter output, the searches, token usage and the grounding verdict, but
  only during evals. Attaching it in production gives most of the analytics
  data for free.
- The API is called with a Keycloak service account, so there is **no end-user
  identity**. On the web, the session cookie is the only link between a person
  and a turn.
- The UI's nginx already proxies `^/(chat|health)` to the API, so
  `/chat/feedback` needs no proxy change.

## Architecture

```text
/chat turn ──► ChatService ──► TurnReport (also attached in production)
                    │
                    └─ before the final "stop" chunk: PutItem ──► DynamoDB "sso-rag-interactions"
                                                                   │  (TTL 180 days, KMS, PITR, gsi1)
Web UI 👍/👎 ──► POST /chat/feedback ──► FeedbackService ──────────┤
(later: Teams bridge ──► FeedbackService)                          │
feedback_report CLI ──► Query gsi1 + BatchGetItem ─────────────────┘
```

### Why DynamoDB

Feedback has to be checked when it arrives (does the turn exist, and does it
belong to the caller's session?). It also has to be idempotent: a user can
switch from up to down or clear the vote. Both need a key/value lookup.
Reviewing rated turns by day or date range is served by one index on the
same table (see [Reviewing ratings](#reviewing-ratings)), so no second store
is needed, and DynamoDB Local gives the same API for local development.

Alternatives considered:

- **Firehose to S3, append-only:** cheaper, but feedback can't be checked
  against its turn, and votes that are changed have to be resolved at query
  time.
- **RDS/Aurora Postgres:** gives live SQL, but more to operate than this needs
  (instance, credentials, migrations, a delete job instead of TTL). It is also
  a fixed cost: about $15–30 a month for `db.t4g.micro`, about $45 for Aurora
  Serverless v2, against about $1–2 for DynamoDB at ~500 turns a day.
- **CloudWatch Logs only:** short retention, and joining feedback to turns is
  awkward.

## Data model (single table)

| PK | SK | Contents |
| --- | --- | --- |
| `TURN#<turn_id>` | `META` | `created_at` (UTC), `created_date` (`YYYY-MM-DD` in `America/Vancouver`), `channel` (`web`), `session_hash`, `conversation_hash`, question, final delivered answer, `grounding_status`, replaced/repaired flags, rewriter intent + rewritten query + sub-queries + step-back, retrieved doc URIs + rerank scores, citations, model ids, app version (git SHA), token usage, latencies (rewrite, first model call, total), outcome (`ok`/`timeout`/`failed`), `ttl` |
| `TURN#<turn_id>` | `FB#<rater_hash>` | `rating` (`up`/`down`), `source` (`web`), `created_at`, `updated_at`, `ttl`, `gsi1pk`, `gsi1sk` (see [Reviewing ratings](#reviewing-ratings)) |

- **Feedback is stored per rater, not as one flag on the turn.** On the web
  the rater is the session (one person per turn). The same layout fits a Teams
  channel, where many people can rate one reply. Totals per turn are a sum over
  these items.
- `session_hash` is the SHA-256 of the session id. Session ids are random
  UUIDs, so the hash can't be reversed, and the analytics data can't be used to
  take over a session. Because Delete history keeps analytics records, the hash
  still groups a session's turns after the conversation itself is gone.
- `ttl` is set to the turn's time + 180 days on both the turn and its feedback
  items. Changing a vote doesn't extend it, so feedback is never kept longer
  than its turn.
- Failed and timed-out turns (with no answer) are recorded too. They are often
  the most useful ones to analyse. Turns abandoned by a client disconnect are
  not.

### Reviewing ratings

The main question asked of the data is "show me the 👍 and/or 👎 turns, with
their question and answer, for a day or for the last 30 days". One global
secondary index, `gsi1`, answers it without a scan. Only rating items carry
its keys, so turn records stay out of the index.

| Index key | Value | Example |
| --- | --- | --- |
| `gsi1pk` | `RATING#<rating>#<month of the turn>` | `RATING#down#2026-11` |
| `gsi1sk` | `<created_date of the turn>#<created_at of the turn>` | `2026-11-16#2026-11-16T17:07:49.812Z` |

Projection: `KEYS_ONLY` (the table's `pk` is the turn id, which is all the
second read needs).

- **Dates are the turn's, in `America/Vancouver`**, so a turn is listed under
  the day it was asked, and a BC working day isn't split across two UTC dates.
  `created_at` stays in UTC.
- **Month partitions** keep a 30-day range to at most two queries per rating;
  a day is one query. Volume per partition is tiny.
- **Changing a vote moves the item in the index:** the feedback write sets
  `gsi1pk` for the new rating, and DynamoDB moves the index entry. Clearing a
  vote deletes the rating item, which removes it from the index.

Reading rated turns:

1. `Query` `gsi1` for each rating asked for (`up`, `down` or both) and each
   month the range touches: `gsi1pk = RATING#down#2026-11` with
   `begins_with(gsi1sk, "2026-11-16#")` for a day, or `gsi1sk BETWEEN
   "<from>" AND "<to>~"` for a range.
2. Remove duplicate turn ids (several raters can rate one turn later, in
   Teams), then `BatchGetItem` the turn records (100 per call) with a
   `ProjectionExpression` of `question`, `answer`, `grounding_status`,
   `citations`, `created_at`.

At ~500 turns a day with 10% rated, a 30-day review reads about 1,500
items, which costs a fraction of a cent.

### Credential masking

Question and answer text are stored as written, with one exception: people
asking about SSO sometimes paste credentials. Before storing, a regex pass
replaces JWTs, `Bearer …` tokens, `client_secret` / `password` values
(including prefixed names such as `KEYCLOAK_CLIENT_SECRET=`) and the
`"secret"` key of Keycloak's installation JSON with `[REDACTED]`. Nothing
else (names, emails, client ids) is changed.

## Backend changes

1. **`app/services/interaction_store.py`** (new): `record_turn()` and
   `record_feedback()` over DynamoDB, used both deployed and locally (DynamoDB
   Local, see [Local development](#local-development-docker-compose)). Turned
   off for evals and when `ANALYTICS_ENABLED=false`.
2. **`app/services/feedback_service.py`** (new): `set_rating(turn_id,
   rater_hash, rating, source)`. It has no HTTP or cookie logic, so a future
   Teams bridge can call it directly.
   - It first reads the turn record with `GetItem`, `ConsistentRead=True`
     (the turn was written moments before), projecting `session_hash`, `ttl`,
     `created_at` and `created_date`.
   - `up`/`down`: `UpdateItem` on the rating item setting `rating`, `source`,
     `updated_at`, `ttl` (copied from the turn), `gsi1pk`, `gsi1sk`, and
     `created_at = if_not_exists(created_at, :now)`.
   - `null`: `DeleteItem` on the rating item.
3. **Capture the turn**: attach `capture()` in `_event_stream` for every
   request. After the `Answer`, or on a timeout or error, build the record and
   write it **before** the `finish_reason: "stop"` chunk, with a short timeout
   and inside try/except. The UI only shows the thumbs after `stop`, so feedback
   can't arrive before its turn exists. A failed write only logs a warning and
   never breaks the chat.
4. **`POST /chat/feedback`** with body `{turn_id, rating: "up" | "down" | null}`,
   returns 204.
   - The cookie's session hash must match the turn's `session_hash`; otherwise
     404 (which also avoids revealing whether a turn id exists). After Delete
     history the cookie is gone, so earlier turns can no longer be rated.
   - `null` clears the vote. Repeated calls overwrite, so the endpoint is
     idempotent.
5. **Configuration**:

   | Variable | Proposed default | Purpose |
   | --- | --- | --- |
   | `ANALYTICS_ENABLED` | `true` | Store each turn |
   | `ANALYTICS_TABLE_NAME` | `sso-rag-interactions` | DynamoDB table |
   | `ANALYTICS_RETENTION_DAYS` | `180` | TTL on turn and feedback items |
   | `FEEDBACK_ENABLED` | `true` | Enables `POST /chat/feedback`; exposed as `feedback_enabled` in `GET /chat/config` so the UI can hide the buttons |
   | `AWS_ENDPOINT_URL_DYNAMODB` | unset | Standard boto3 setting; points only the DynamoDB client at DynamoDB Local. Bedrock and S3 calls are unaffected |

6. **Local table creation**: with `ENVIRONMENT=local`, the API creates the
   table at startup if it is missing, retrying briefly while DynamoDB Local
   starts. Deployed tables come from Terraform only.
7. **CloudWatch**: log `feedback` events in Embedded Metric Format, so the
   👍/👎 rate and grounding status get a dashboard.
8. **`app/jobs/feedback_report.py`** (new): a CLI that runs the
   [Reviewing ratings](#reviewing-ratings) reads and prints each turn's
   date, rating, question and answer (or JSON, for the evals dataset). It runs
   with the caller's AWS credentials, not the API's:

   ```bash
   uv run python -m app.jobs.feedback_report --rating down --date 2026-11-16
   uv run python -m app.jobs.feedback_report --rating all --days 30 --json
   ```

## UI changes

In [useChat.ts](../../ui/src/hooks/useChat.ts) and
[ChatMessageBubble.tsx](../../ui/src/components/ChatMessageBubble.tsx):

- Keep the chunk `id` as `turnId` on each assistant message.
- Show 👍/👎 after `finish_reason: "stop"`, but not on errors or greetings (no
  `grounding_status`). Update the button state immediately and roll back if the
  request fails. Clicking the selected thumb again clears the vote.
- A short note near the input saying that questions and answers are kept for
  6 months to improve the service. This is likely needed for a BC Gov privacy
  notice; confirm the wording with the privacy officer, including whether it
  must mention the up to 35 extra days in backups.

## Local development (Docker Compose)

A new `docker-compose.yml` at the repo root:

| Service | Image / build | Ports | Notes |
| --- | --- | --- | --- |
| `dynamodb` | `amazon/dynamodb-local` (pinned version) | `8001:8000` | `-jar DynamoDBLocal.jar -sharedDb -dbPath /home/dynamodblocal/data`, named volume `dynamodb-data`. `-sharedDb` makes the table visible whatever credentials or region the client uses. If the volume isn't writable, run the service as `user: root`. |
| `api` | `build: .` (existing Dockerfile) | `8000:8000` | `env_file: .env`, overridden with `ENVIRONMENT=local`, `AUTH_LOCAL_BYPASS=true`, `SESSION_COOKIE_SECURE=false`, `AGENT_STORE_DIR=/data/agent-store` (named volume), `AWS_ENDPOINT_URL_DYNAMODB=http://dynamodb:8000`. `depends_on: dynamodb`. |
| `ui` | `build: ./ui`, `args: API_URL=http://api:8000` | `8080:8080` | nginx already proxies `/chat` and `/health` to the API. |
| `dynamodb-admin` (optional, `profiles: [tools]`) | `aaronshaf/dynamodb-admin` | `8002:8001` | Browse stored turns and feedback locally. |

Things to handle:

- **Bedrock credentials:** the API still calls real Bedrock. Mounting `~/.aws`
  doesn't work reliably because the image runs as uid 1001 and the host files
  are usually mode 600. Instead, pass `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` through from the shell, for
  example after `eval "$(aws configure export-credentials --profile <p> --format env)"`.
  SSO session credentials expire, so this has to be redone after they do.
- **Agent store:** use a named volume, not a bind mount of `.agent-store/`,
  for the same uid reason.
- **Turn timeout:** the UI's nginx `proxy_read_timeout` is 120s and
  `TURN_TIMEOUT_SECONDS` is 240s, so very slow turns are cut off by nginx
  locally. Optional: raise it to 250s in `nginx.conf`.
- **Running the API on the host instead:** `docker compose up dynamodb` and set
  `AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8001` in `.env`.
- Document `docker compose up --build` (and `--profile tools`) in the README.

## Terraform

- `terraform/dynamodb.tf`: `PAY_PER_REQUEST`, KMS encryption, point-in-time
  recovery with `recovery_period_in_days = 35` (to recover from bad writes;
  deleted items stay restorable for those 35 days), TTL on `ttl`,
  `ca-central-1`. One GSI,
  `gsi1` (`gsi1pk` / `gsi1sk`, both strings, projection `KEYS_ONLY`); see
  [Reviewing ratings](#reviewing-ratings).
- `terraform/iam.tf`: a policy for `api_task` allowing only `PutItem`,
  `UpdateItem`, `GetItem`, `DeleteItem` and `Query` on the table
  (`DeleteItem` is for clearing a vote; `Query` reads all rating items of one
  turn, `pk = TURN#<turn_id> AND begins_with(sk, "FB#")`, for per-turn totals).
  The index needs no API permission; DynamoDB maintains it.
- A read-only policy for whoever runs `feedback_report`: `dynamodb:Query` on
  `table/sso-rag-interactions/index/gsi1` and `dynamodb:BatchGetItem` on the
  table (and `dynamodb:Scan` on the table for ad hoc analysis). Not attached
  to the API task. No KMS permission is needed: the table uses the AWS
  managed key (`aws/dynamodb`).
- ECS task definition: `ANALYTICS_TABLE_NAME` and `ANALYTICS_RETENTION_DAYS`.

## Room for Microsoft Teams later

Nothing Teams-specific is built now. These choices keep it a small addition:

- `channel` on turns and `source` on feedback, so Teams records sit beside web
  records.
- Per-rater feedback items, which already handle many people rating one reply.
- `FeedbackService` takes a rater hash and a source, not a cookie, so the
  Teams bridge can call it in-process.
- What Teams will need then: a `channel_message_id` attribute on turns and a
  GSI on it (DynamoDB adds GSIs to an existing table online), the rater hash
  from the Entra user id, and a mapping from Teams feedback to `up`/`down`.
  The recommended route is a Bot Framework / Teams SDK bot with the built-in
  feedback loop (`feedbackLoopEnabled`). Its `message/submitAction` invoke
  carries `like`/`dislike` and the `replyToId` of the rated message. A
  Graph-polling bridge could read message reactions instead, but Teams has no
  native thumbs-down reaction.

## Using the data

Rated turns are reviewed with `feedback_report`; rates over time come from
the CloudWatch metrics. Other analyses below are ad hoc: a script that scans
the table (a few cents per full scan at this size), run with the same
read-only role plus `dynamodb:Scan`.

- **Documentation gaps:** questions answered `NOT_SUPPORTED`, or with the
  no-documentation reply, grouped by topic.
- **Answer quality:** 👎 rate by intent, grounding status, repaired vs. first
  attempt, model or prompt version (git SHA), and cited document.
- **Retrieval health:** turns with low rerank scores, or whose sub-queries
  found nothing.
- **Feeding the evals:** periodically export 👎 turns (and 👍 ones as positive
  examples) as candidate cases for the deepeval dataset in
  [evals/](../../evals/), so real-world failures become regression tests.

## Tests

- Interaction store and feedback service with moto.
- Feedback endpoint: wrong session returns 404; setting, changing and
  clearing a vote; input validation; 404 when `FEEDBACK_ENABLED=false`.
- Reviewing ratings: changing a vote moves the turn from the 👍 to the 👎
  listing and clearing it removes it; a range across a month boundary; a turn
  near midnight is listed under its `America/Vancouver` date.
- The SSE stream still completes when the store raises an error.
- Delete history leaves analytics records in place.
- Credential masking patterns.

## Rollout

1. Docker Compose with DynamoDB Local, then turn storage plus CloudWatch
   metrics.
2. Feedback endpoint, UI thumbs and `feedback_report`.
3. Terraform table and IAM, deploy.
4. Teams feedback, built together with the bridge.

## Implementation notes

Where the build differs from, or adds to, the plan above:

- **Local storage guard:** with `ENVIRONMENT=local`, turns are only stored when
  `AWS_ENDPOINT_URL_DYNAMODB` is set (`settings.analytics_active`), so a
  developer's AWS credentials never create or write a real table. The setting
  is read by `Settings` and passed to boto3, so it also works from `.env`.
- **Local table creation** runs as a background task at startup, so the API
  serves while DynamoDB Local starts.
- **Encryption:** the AWS managed KMS key; readers need no KMS permission.
- **Latency:** `latency_ms.first_output` is the time until the model's first
  output (a tool call or answer text), measured in the route; `rewrite` and
  `total` as planned.
- **Rejected replies:** attempts that failed the grounding checks keep their
  raw reply, so a no-documentation answer can be compared with what the model
  wrote.
- **Masking** applies to every string in the turn item (rewritten and search
  queries can repeat what the user pasted), not just question and answer.
- **CloudWatch metrics** (namespace `SsoRag`): `Turns` by `Outcome` and
  `GroundingStatus`, and `Feedback` by `Rating` (and `GroundingStatus`), so a
  👍/👎 rate has a denominator.
- **Docker Compose:** the agent store volume is mounted at `/app/.agent-store`
  (the Dockerfile creates it owned by the app user, so a new named volume is
  writable); the UI gets `API_URL` as a runtime environment variable. The
  UI's nginx `proxy_read_timeout` is 250s.
- **App version:** `APP_VERSION` build arg, set to the commit SHA by the
  deploy workflow.
