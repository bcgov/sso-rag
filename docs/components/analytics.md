# Turn Analytics and Feedback

Every `/chat` turn is stored for analysis, and users can rate each answer with 👍 or 👎. The records live in one DynamoDB table, separate from the conversation history, so they outlive the 3-day session cleanup and **Delete history**. They are kept for 180 days.

This page covers what is stored, how a turn's records change over their lifetime, the data model, and how to query it. The design decisions are in [plan_feedback_analytics.md](../plans/plan_feedback_analytics.md).

| Part | Code |
|---|---|
| Building and storing records, credential masking, rating queries | [interaction_store.py](../../app/services/interaction_store.py) |
| Setting a rating, listing rated turns | [feedback_service.py](../../app/services/feedback_service.py) |
| Recording a turn, `POST /chat/feedback` | [chat.py](../../app/api/routes/chat.py) |
| Pipeline values collected during a turn | [turn_report.py](../../app/common/turn_report.py) |
| CloudWatch metrics | [metrics.py](../../app/common/metrics.py) |
| Listing rated answers from the command line | [feedback_report.py](../../app/jobs/feedback_report.py) |
| Thumbs in the UI | [useChat.ts](../../ui/src/hooks/useChat.ts), [ChatMessageBubble.tsx](../../ui/src/components/ChatMessageBubble.tsx) |
| Table, index and IAM | [dynamodb.tf](../../terraform/dynamodb.tf), [iam.tf](../../terraform/iam.tf) |
| Local DynamoDB | [docker-compose.yml](../../docker-compose.yml) |

## What is recorded

| Recorded | Not recorded |
|---|---|
| Every `/chat` turn that ends with an answer (`outcome: ok`), a timeout (`timeout`) or an error (`failed`) | Messages rejected before the turn starts (blank or too long: `422`) |
| Greetings, clarifying questions and the links-only reply (`outcome: ok`, no `grounding_status`) | Turns abandoned by a client disconnect (closed tab, **New chat** or **Delete history** while an answer is streaming) |
| One rating per rater per turn (the browser session on the web) | Evaluation runs ([evals/collect.py](../../evals/collect.py) sets `ANALYTICS_ENABLED=false`) |
| | Local runs without `AWS_ENDPOINT_URL_DYNAMODB` (see [Local development](#local-development)) |

The API is called with a Keycloak service account, so there is no end-user identity. On the web, the session cookie is the only link between a person and their turns, and it is stored only as a hash.

## Lifecycle of a turn

```text
POST /chat ─► route creates turn id (chatcmpl-<uuid>) + TurnReport
                │
                ▼
          ChatService runs the turn; rewriter, search tool and grounding
          check record their values in the TurnReport
                │
                ▼
          answer, timeout or error
                │
                ▼
          PutItem  TURN#<id> / META      (5s limit; failure only logged)
                │
                ▼
          "stop" chunk (or in-band error) ─► UI shows 👍/👎 (answers only)
                │
   user rates ──┤
                ▼
          POST /chat/feedback ─► GetItem (owner check) ─► UpdateItem TURN#<id> / FB#<rater>
                │                                         (index entry RATING#<rating>#<month>)
   changes vote ─► same item updated; index entry moves to the other rating
   clears vote  ─► DeleteItem; index entry removed
                │
          day 3: session cleanup deletes the conversation history (not these records)
                │
          day 180: TTL expires the turn and its ratings (deleted within a few days)
                │
          +35 days: no longer restorable from point-in-time recovery
```

| When | What happens | Notes |
|---|---|---|
| The turn starts | The route creates the turn id, the same `id` every stream chunk carries. It attaches a `TurnReport` to the turn's task. | Only when turns are stored (`settings.analytics_active`) |
| During the turn | The rewriter, the search tool and the grounding check add events: `rewrite`, `search`, `attempt`, `model_call` | The same mechanism the evaluations use |
| The turn ends | The turn item is written **before** the `stop` chunk (or the in-band error), with `attribute_not_exists(pk)`, so it is never overwritten | The UI shows the thumbs only after `stop`, so a rating can't arrive before its turn exists. A failed or slow write (over 5s) is logged as `turn_record_failed`, and the chat carries on. |
| The user rates the answer | `POST /chat/feedback` reads the turn with a consistent read and checks that its `session_hash` matches the caller's cookie. It then sets the rating item. | Another session's turn, an unknown id, or no cookie returns 404 |
| The user changes or clears the vote | The rating item is overwritten, or deleted when the rating is `null` | Calls can be repeated safely. The rating keeps the turn's `ttl`, so changing the vote doesn't extend it. |
| **New chat** or **Delete history** | Deletes the conversation history and, for Delete history, the cookie. The analytics records stay. | Without the cookie, earlier answers can't be rated any more |
| Day 3 | The session cleanup deletes the stored conversation (`SESSION_RETENTION_DAYS`) | Analytics records are unaffected |
| Day 180 | TTL expires the turn and every rating of it (`ANALYTICS_RETENTION_DAYS`) | DynamoDB deletes expired items in the background, usually within a few days. Until then they can still be read; filter on `ttl` if the cut-off must be exact. |
| Up to 35 days later | The deleted items can be restored from point-in-time recovery | Restoring creates a new table; the live table never gets them back |

## Data model

One table, `sso-rag-interactions`, on-demand billing, encrypted with the AWS managed KMS key. All items of a turn share its partition key:

| `pk` | `sk` | Item | Count per turn |
|---|---|---|---|
| `TURN#<turn id>` | `META` | The turn | 1 |
| `TURN#<turn id>` | `FB#<rater hash>` | One rater's rating | 0 or more (one per rater) |

The turn id is `chatcmpl-<uuid>`. On the web the rater hash is the session hash, so a turn has at most one rating; a future Teams bridge can add one per person.

### Turn item

Attributes with no value (for example `answer` on a timeout, or `error` on an `ok` turn) are left out of the item.

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `TURN#<turn id>`, `META` |
| `entity`, `schema_version` | S, N | `turn`, `1` |
| `turn_id` | S | `chatcmpl-<uuid>` |
| `created_at` | S | Start of the turn, UTC, e.g. `2026-11-16T17:07:49.812Z` |
| `created_date` | S | The day in BC (`America/Vancouver`), `YYYY-MM-DD` |
| `ttl` | N | Expiry: `created_at` + 180 days, seconds since 1970 |
| `channel` | S | `web` |
| `session_hash` | S | SHA-256 of the session cookie value. Groups a browser's turns, and is used for the owner check when rating. |
| `conversation_hash` | S | SHA-256 of the conversation id (session + tab) |
| `app_version` | S | Git SHA of the deployed image |
| `question` | S | The user's message, trimmed and masked |
| `answer` | S | The delivered answer, masked. It is the no-documentation reply when the model's answer was rejected. |
| `outcome` | S | `ok`, `timeout` or `failed` |
| `error` | S | Exception class name, for `failed` turns |
| `grounding_status` | S | `SUPPORTED`, `PARTIALLY_SUPPORTED`, `NOT_SUPPORTED` or `GENERAL`; absent for greetings and clarifications |
| `response_type` | S | `answer`, `general`, `clarification` or `conversational` (from the last attempt) |
| `answer_replaced` | BOOL | The user got the no-documentation reply instead of the model's answer |
| `repair_attempted`, `repaired` | BOOL | The answer got a repair attempt; the repair succeeded |
| `link_only` | BOOL | The message was only links and got the fixed clarifying question |
| `attempts` | L | Per reply attempt (`first`, `repair`): `response_type`, `grounding_status`, `reasons`, `replaced`, `truncated`, cited URLs, and `raw_reply` **only when it was rejected** |
| `rewrite` | M | The query rewriter's `intent`, `query`, `bcgov_query`, `sub_queries`, `step_back_query`, `fallback`, `short_circuit`, `status`, `latency_ms` |
| `search_skipped` | S | Why the search tool didn't search (`off_topic`, `conversational`) |
| `searches` | L | Per search: `queries` (`text`, `origin`), `missing` (queries that found nothing), `retry_queries`, `candidates`, `reranked`, `hits` (`uri`, `title`, `score`, `found_by`), `waited_ms`, `fallback`, `intent_override`. Hits carry no chunk text. |
| `citations` | L | Sources shown to the user: `uri`, `title`, `score` |
| `models` | M | `agent`, `rewriter`, `reranker` model ids |
| `model_calls` | L | Per agent model call: `attempt`, `stop_reason`, `input_tokens`, `output_tokens` |
| `usage` | M | Token totals: `agent_calls`, `agent_input_tokens`, `agent_output_tokens`, `agent_cache_read_tokens`, `agent_cache_write_tokens`, `rewriter_calls`, `rewriter_input_tokens`, `rewriter_output_tokens` |
| `latency_ms` | M | `rewrite`, `first_output` (until the model's first tool call or text), `total` |
| `truncated` | BOOL | Present when a text field was cut at 50,000 characters |

A typical turn item is 5–25 KB, far below DynamoDB's 400 KB item limit.

### Rating item

| Attribute | Type | Meaning |
|---|---|---|
| `pk`, `sk` | S | `TURN#<turn id>`, `FB#<rater hash>` |
| `entity`, `schema_version` | S, N | `feedback`, `1` |
| `turn_id` | S | The rated turn |
| `rating` | S | `up` or `down` |
| `source` | S | `web` (later `teams`) |
| `rater_hash` | S | Who rated: the session hash on the web |
| `created_at`, `updated_at` | S | First vote and last change, UTC |
| `turn_created_date` | S | The turn's BC date |
| `ttl` | N | Copied from the turn |
| `gsi1pk` | S | `RATING#<rating>#<YYYY-MM of the turn>` |
| `gsi1sk` | S | `<turn's BC date>#<turn's created_at>` |

### Index `gsi1`

Only rating items carry `gsi1pk` / `gsi1sk`, so the index holds ratings and nothing else. Its projection is keys only: an entry gives the turn id (from `pk`), which is enough to read the turns afterwards.

| Question | Read |
|---|---|
| 👎 on one day | `gsi1pk = RATING#down#2026-11 AND begins_with(gsi1sk, "2026-11-16#")` |
| 👎 in a range | One query per month the range touches: `gsi1sk BETWEEN "2026-10-18" AND "2026-11-16~"` (`~` sorts after `#`, so the whole last day is included) |
| 👍 and 👎 | The same queries for both ratings |

Then `BatchGetItem` reads the turns (up to 100 per call). Dates are the day the question was asked in BC, not the day of the vote.

### Example items

```json
{
  "pk": "TURN#chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f", "sk": "META",
  "entity": "turn", "schema_version": 1, "turn_id": "chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f",
  "created_at": "2026-11-16T17:07:49.812Z", "created_date": "2026-11-16", "ttl": 1810400869,
  "channel": "web", "session_hash": "9f2c…", "conversation_hash": "41ab…", "app_version": "3e1d2c4…",
  "question": "How do I add a redirect URI to my integration?",
  "answer": "In the CSS app, open your integration … [Redirect URIs](https://bcgov.github.io/sso-docs/…)",
  "outcome": "ok", "grounding_status": "SUPPORTED", "response_type": "answer",
  "answer_replaced": false, "repair_attempted": false, "repaired": false, "link_only": false,
  "rewrite": {"intent": "sso_question", "query": "…", "sub_queries": [], "step_back_query": "…", "latency_ms": 410},
  "searches": [{"queries": [{"text": "…", "origin": "rewrite"}], "missing": [],
                "hits": [{"uri": "https://bcgov.github.io/sso-docs/…", "title": "Redirect URIs", "score": 0.83}]}],
  "citations": [{"uri": "https://bcgov.github.io/sso-docs/…", "title": "Redirect URIs", "score": 0.83}],
  "models": {"agent": "global.amazon.nova-2-lite-v1:0", "rewriter": "global.amazon.nova-2-lite-v1:0", "reranker": "amazon.rerank-v1:0"},
  "usage": {"agent_calls": 2, "agent_input_tokens": 9120, "agent_output_tokens": 640, "rewriter_calls": 1},
  "latency_ms": {"rewrite": 410, "first_output": 2100, "total": 6800}
}
{
  "pk": "TURN#chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f", "sk": "FB#9f2c…",
  "entity": "feedback", "schema_version": 1, "turn_id": "chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f",
  "rating": "down", "source": "web", "rater_hash": "9f2c…",
  "created_at": "2026-11-16T17:08:30.120Z", "updated_at": "2026-11-16T17:09:02.554Z",
  "turn_created_date": "2026-11-16", "ttl": 1810400869,
  "gsi1pk": "RATING#down#2026-11", "gsi1sk": "2026-11-16#2026-11-16T17:07:49.812Z"
}
```

## Privacy

- **No raw ids.** Session and conversation ids are stored only as SHA-256 hashes. The ids are random UUIDs, so a hash can't be turned back into a usable cookie.
- **Credential masking.** Before storing, every string in the turn item is masked, including the rewritten and search queries, which can repeat what the user pasted. JWTs, `Bearer …` tokens, and `client_secret` / `password` values (also prefixed names such as `KEYCLOAK_CLIENT_SECRET=`) become `[REDACTED]`, as does the `"secret"` key of Keycloak's installation JSON. Names, emails and client ids are kept.
- **Retention.** 180 days by TTL, plus up to 35 days in point-in-time recovery backups. The UI says questions and answers are kept for 6 months; confirm that wording with the privacy officer.
- **Delete history** removes the conversation, not these records (plan decision 2).

## Querying the data

Deployed, use your own AWS credentials with the `sso-rag-feedback-reader` policy (Terraform output `feedback_reader_policy_arn`). It allows `Query` on `gsi1`, plus `BatchGetItem` and `Scan` on the table. Locally, add `--endpoint-url http://localhost:8001` to the AWS CLI commands, or set `AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8001` for the scripts.

### Rated answers: `feedback_report`

The main way to review feedback. It runs the index queries and the batch read described in [Index gsi1](#index-gsi1).

```bash
# Today's 👍 and 👎
uv run python -m app.jobs.feedback_report

# 👎 on one day
uv run python -m app.jobs.feedback_report --rating down --date 2026-11-16

# Both ratings over the last 30 days, today included
uv run python -m app.jobs.feedback_report --rating all --days 30

# A range, as JSON lines (e.g. candidate cases for evals/datasets)
uv run python -m app.jobs.feedback_report --rating down --from 2026-11-01 --to 2026-11-15 --json > thumbs-down.jsonl
```

Text output, oldest first:

```text
2 rated answer(s), 2026-11-16

── 2026-11-16 10:07 · 1 👎 · SUPPORTED · chatcmpl-1b6f0c2e-…
Q: How do I add a redirect URI to my integration?
A: In the CSS app, open your integration …
Sources: https://bcgov.github.io/sso-docs/…
```

Each JSON line holds `turn_id`, `created_at`, `created_date`, `channel`, `question`, `answer`, `outcome`, `grounding_status`, `response_type`, `answer_replaced`, `citations`, `up` and `down`.

### AWS CLI

One turn, with selected attributes. Expression attribute names avoid clashes with DynamoDB reserved words.

```bash
aws dynamodb get-item --table-name sso-rag-interactions \
  --key '{"pk": {"S": "TURN#chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f"}, "sk": {"S": "META"}}' \
  --projection-expression "#q, #a, #g, #at" \
  --expression-attribute-names '{"#q": "question", "#a": "answer", "#g": "grounding_status", "#at": "attempts"}'
```

A turn and all its ratings:

```bash
aws dynamodb query --table-name sso-rag-interactions \
  --key-condition-expression "pk = :pk" \
  --expression-attribute-values '{":pk": {"S": "TURN#chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f"}}'
```

Turn ids rated 👎 on one day (the index, as `feedback_report` reads it):

```bash
aws dynamodb query --table-name sso-rag-interactions --index-name gsi1 \
  --key-condition-expression "gsi1pk = :pk AND begins_with(gsi1sk, :day)" \
  --expression-attribute-values '{":pk": {"S": "RATING#down#2026-11"}, ":day": {"S": "2026-11-16#"}}' \
  --query "Items[].pk.S" --output text
```

👍 between two dates in the same month:

```bash
aws dynamodb query --table-name sso-rag-interactions --index-name gsi1 \
  --key-condition-expression "gsi1pk = :pk AND gsi1sk BETWEEN :first AND :last" \
  --expression-attribute-values '{":pk": {"S": "RATING#up#2026-11"}, ":first": {"S": "2026-11-01"}, ":last": {"S": "2026-11-15~"}}' \
  --select COUNT
```

### PartiQL (DynamoDB console or `aws dynamodb execute-statement`)

Handy for spot checks in the console's **PartiQL editor**. A statement with the partition key reads one turn; anything else scans the whole table.

```sql
-- One turn and its ratings
SELECT * FROM "sso-rag-interactions"
WHERE pk = 'TURN#chatcmpl-1b6f0c2e-5d1a-4c3e-9f7a-2a1d3c4b5e6f'

-- Ratings in the index for one day
SELECT pk, gsi1pk FROM "sso-rag-interactions"."gsi1"
WHERE gsi1pk = 'RATING#down#2026-11' AND begins_with(gsi1sk, '2026-11-16')

-- Questions the documentation didn't answer on one day (full scan)
SELECT created_at, question FROM "sso-rag-interactions"
WHERE entity = 'turn' AND created_date = '2026-11-16' AND grounding_status = 'NOT_SUPPORTED'

-- Timed-out and failed turns (full scan)
SELECT created_at, outcome, "error", question FROM "sso-rag-interactions"
WHERE entity = 'turn' AND outcome IN ['timeout', 'failed']
```

### Python, for ad hoc analysis

Questions that span all turns, such as rates, groupings or trends, need a scan. At about 2 GB, a full scan costs a few cents. This script prints the 👎 share by grounding status for the last 30 days:

```python
from collections import Counter
from datetime import date, timedelta

import boto3
from boto3.dynamodb.types import TypeDeserializer

TABLE = "sso-rag-interactions"
since = (date.today() - timedelta(days=29)).isoformat()
client = boto3.client("dynamodb", region_name="ca-central-1")  # endpoint_url="http://localhost:8001" locally
plain = TypeDeserializer().deserialize


def scan(entity: str, date_attribute: str):
    pages = client.get_paginator("scan").paginate(
        TableName=TABLE,
        FilterExpression="#e = :e AND #d >= :since",
        ExpressionAttributeNames={"#e": "entity", "#d": date_attribute},
        ExpressionAttributeValues={":e": {"S": entity}, ":since": {"S": since}},
    )
    for page in pages:
        for item in page["Items"]:
            yield {k: plain(v) for k, v in item.items()}


status = {t["turn_id"]: t.get("grounding_status", "NONE") for t in scan("turn", "created_date")}
votes = Counter((status.get(r["turn_id"], "?"), r["rating"]) for r in scan("feedback", "turn_created_date"))
answers = Counter(status.values())

for s in sorted(answers):
    up, down = votes[(s, "up")], votes[(s, "down")]
    share = f"{down / (up + down):.0%}" if up + down else "-"
    print(f"{s:20} turns={answers[s]:5}  👍={up:4}  👎={down:4}  👎 share={share}")
```

Other useful groupings come from the same scan of turn items:

| Question | Group or filter turns by |
|---|---|
| Documentation gaps | `grounding_status = NOT_SUPPORTED` or `answer_replaced`, grouped by `rewrite.intent` or by words in `question` |
| Answer quality by version | 👎 share by `app_version` or `models.agent` |
| Repairs | `repair_attempted` vs. `repaired`, and the `reasons` of rejected `attempts` |
| Retrieval health | Searches whose `missing` is not empty, or whose best `hits[0].score` is low |
| Cost and speed | `usage.*_tokens` and `latency_ms.total` by day or intent |

## CloudWatch metrics

Each stored turn and each vote logs an Embedded Metric Format line to stdout. CloudWatch turns these into metrics in namespace `SsoRag`, with no extra setup.

| Metric | Dimensions | Counts |
|---|---|---|
| `Turns` | `Environment` + `Outcome`; `Environment` + `GroundingStatus` | Stored turns |
| `Feedback` | `Environment` + `Rating`; `Environment` + `Rating` + `GroundingStatus` | Votes cast. A changed vote counts once for each rating; clearing a vote isn't counted. |

`GroundingStatus` is `NONE` for greetings, clarifications, timeouts and failures. For a 👎 rate on a dashboard, use metric math such as `down / turns`, with `down` = `Feedback` (Rating = `down`) and `turns` = `Turns` summed over all Outcome values.

The metric lines are plain JSON in the `/ecs/sso-rag/api` log group, so CloudWatch Logs Insights can query them too:

```text
fields @timestamp, Rating, GroundingStatus
| filter ispresent(Feedback)
| stats count(*) as votes by Rating, GroundingStatus
```

```text
fields @timestamp, Outcome
| filter ispresent(Turns)
| stats count(*) as turns by Outcome, bin(1d)
```

## Local development

Local runs use DynamoDB Local, never the AWS table:

- `docker compose up --build` starts DynamoDB Local (port 8001) and points the API at it (`AWS_ENDPOINT_URL_DYNAMODB=http://dynamodb:8000`). The API creates the table at startup. `docker compose --profile tools up` adds **dynamodb-admin** on <http://localhost:8002> for browsing items.
- With the API on the host, run `docker compose up dynamodb` and set `AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8001` in `.env`.
- With `ENVIRONMENT=local` and no `AWS_ENDPOINT_URL_DYNAMODB`, turns aren't stored, and the UI shows no thumbs. A developer's AWS credentials therefore never write to a real table.

The data stays in the `dynamodb-data` volume until `docker compose down -v`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANALYTICS_ENABLED` | `true` | Store turns |
| `ANALYTICS_TABLE_NAME` | `sso-rag-interactions` | The table |
| `ANALYTICS_RETENTION_DAYS` | `180` | TTL of turns and their ratings |
| `FEEDBACK_ENABLED` | `true` | `POST /chat/feedback` and the UI's thumbs; needs stored turns |
| `AWS_ENDPOINT_URL_DYNAMODB` | unset | DynamoDB Local endpoint |
| `APP_VERSION` | unset | Git SHA stored on turns (image build arg) |

`GET /chat/config` tells the UI whether to show the thumbs (`feedback_enabled`) and how long data is kept (`analytics_retention_days`, `null` when turns aren't stored).

## Troubleshooting

| Symptom | Check |
|---|---|
| No thumbs in the UI | `GET /chat/config` returns `feedback_enabled: false`: locally, `AWS_ENDPOINT_URL_DYNAMODB` isn't set; otherwise `FEEDBACK_ENABLED` or `ANALYTICS_ENABLED` is `false`. Thumbs never show on greetings, clarifications or errors. |
| `turn_record_failed` warnings in the API log | DynamoDB unreachable or denied (task role, table name), or the write took longer than 5s. The chat isn't affected, but the turn is missing. |
| A rating returns 404 | The cookie changed (Delete history, expired cookie, another browser), or the turn wasn't stored |
| `feedback_report` lists nothing | Dates are the day the question was asked, in BC time. Locally, check the endpoint setting. |
| `AccessDeniedException` from `feedback_report` | Attach the `sso-rag-feedback-reader` policy to your role |
