"""Chat turns and their thumbs up/down ratings, kept in one DynamoDB table
for analysis (docs/plans/plan_feedback_analytics.md).

Every item of a turn shares `pk = TURN#<turn id>`:

- `sk = META`: the turn (question, delivered answer, pipeline values).
- `sk = FB#<rater hash>`: one rater's rating. Rating items also carry the
  `gsi1` index keys (`RATING#<rating>#<YYYY-MM>`, `<date>#<created_at>`), so
  rated turns for a day or a date range are read without a scan.

Dates are the turn's, in America/Vancouver; `created_at` is UTC. Every item
expires (TTL) ANALYTICS_RETENTION_DAYS after its turn. Locally the table is
DynamoDB Local (AWS_ENDPOINT_URL_DYNAMODB); deployed, it comes from
terraform/dynamodb.tf.
"""

import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from app.common.config import settings

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Vancouver")
SCHEMA_VERSION = 1
TURN_PK_PREFIX = "TURN#"
TURN_SK = "META"
FEEDBACK_SK_PREFIX = "FB#"
RATING_INDEX = "gsi1"
RATINGS = ("up", "down")
WEB = "web"

# Longest text kept per field, far below DynamoDB's 400 KB item limit.
MAX_TEXT_CHARS = 50_000
BATCH_GET_LIMIT = 100
BATCH_GET_RETRIES = 5

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


@lru_cache(maxsize=1)
def dynamodb_client():
    # Short timeouts: a turn is written while the user waits for the stream
    # to finish.
    return boto3.client(
        "dynamodb",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url_dynamodb or None,
        config=Config(
            connect_timeout=2,
            read_timeout=5,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def hash_id(value: str) -> str:
    """SHA-256 of a session or conversation id. The ids are random UUIDs, so
    the hash can't be reversed into a usable session cookie."""
    return hashlib.sha256(value.encode()).hexdigest()


# ── Credential masking ────────────────────────────────────────────────────────

REDACTED = "[REDACTED]"

# Linear time on any input: a match can only start where a keyword (or a JWT
# not preceded by a token character) starts, and no two parts of a pattern can
# match the same characters, so a failed attempt never backtracks.
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++\.[A-Za-z0-9_-]*+")
_BEARER = re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/-]{8,}=*", re.IGNORECASE)
# Only the keyword is matched, so prefixed names such as KEYCLOAK_CLIENT_SECRET
# or DB_PASSWORD are masked too, their prefix left as it is.
_SECRET_VALUE = re.compile(
    r"""(client[_-]?secret|password|passwd)(["']?\s*[:=]\s*["']?)[^\s"'&,;]+""",
    re.IGNORECASE,
)
# Keycloak's installation JSON: "credentials": {"secret": "..."}. Only the
# quoted key, so prose like "the secret: ..." is left alone.
_SECRET_JSON = re.compile(r"""("secret"\s*:\s*")[^"]+""", re.IGNORECASE)


def mask_credentials(text: str) -> str:
    """Replaces JWTs, bearer tokens and client secret / password values with
    [REDACTED]. Nothing else (names, emails, client ids) is changed."""
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_JSON.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _SECRET_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def _masked(value):
    if isinstance(value, str):
        return mask_credentials(value)
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


# ── Keys and dates ────────────────────────────────────────────────────────────

def turn_pk(turn_id: str) -> str:
    return f"{TURN_PK_PREFIX}{turn_id}"


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_date(moment: datetime) -> str:
    """The day a moment falls on in BC, as YYYY-MM-DD."""
    return moment.astimezone(LOCAL_TZ).date().isoformat()


def expiry(created_at: datetime) -> int:
    """The TTL of a turn's items: seconds since 1970."""
    return int(created_at.timestamp()) + settings.analytics_retention_days * 86400


def rating_partition(rating: str, created_date: str) -> str:
    return f"RATING#{rating}#{created_date[:7]}"


def _model_id(arn: str | None) -> str | None:
    return arn.split("/")[-1] if arn else None


# ── Turn records ──────────────────────────────────────────────────────────────

def _usage_sum(events: list[dict], key: str) -> int:
    return sum((e.get("usage") or {}).get(key) or 0 for e in events)


def build_turn_record(
    *,
    turn_id: str,
    session_id: str,
    conversation_id: str,
    question: str,
    outcome: str,
    events: list[dict],
    created_at: datetime,
    latency_ms: int,
    first_output_ms: int | None = None,
    answer: str | None = None,
    grounding_status: str | None = None,
    citations: list[dict] | None = None,
    error: str | None = None,
    channel: str = WEB,
) -> dict:
    """The turn item: what was asked and delivered, plus the pipeline values
    from the turn's TurnReport `events`. `outcome` is ok, timeout or failed."""

    def of(name: str) -> list[dict]:
        return [e for e in events if e.get("event") == name]

    truncated = False

    def text(value: str | None) -> str | None:
        nonlocal truncated
        if value is not None and len(value) > MAX_TEXT_CHARS:
            truncated = True
            return value[:MAX_TEXT_CHARS]
        return value

    rewrites = of("rewrite")
    rewrite = rewrites[-1] if rewrites else None
    rewriter_calls = [*rewrites, *of("rewrite_retry")]
    model_calls = of("model_call")
    attempts = of("attempt")
    last = attempts[-1] if attempts else {}

    item = {
        "pk": turn_pk(turn_id),
        "sk": TURN_SK,
        "entity": "turn",
        "schema_version": SCHEMA_VERSION,
        "turn_id": turn_id,
        "created_at": iso_utc(created_at),
        "created_date": local_date(created_at),
        "ttl": expiry(created_at),
        "channel": channel,
        "session_hash": hash_id(session_id),
        "conversation_hash": hash_id(conversation_id),
        "app_version": settings.app_version,
        "question": text(question),
        "answer": text(answer),
        "outcome": outcome,
        "error": error,
        "grounding_status": grounding_status,
        "response_type": last.get("response_type"),
        "answer_replaced": bool(last.get("replaced")),
        "repair_attempted": len(attempts) > 1,
        "repaired": len(attempts) > 1 and not last.get("replaced"),
        "link_only": bool(of("link_only")),
        # The rejected replies are kept: when the user got the
        # no-documentation reply, they show what the model wrote.
        "attempts": [
            {
                "attempt": a.get("attempt"),
                "response_type": a.get("response_type"),
                "grounding_status": a.get("grounding_status"),
                "reasons": a.get("reasons") or [],
                "replaced": bool(a.get("replaced")),
                "truncated": bool(a.get("truncated")),
                "citations": a.get("citations") or [],
                "raw_reply": text(a.get("raw_reply")) if a.get("reasons") else None,
            }
            for a in attempts
        ],
        "rewrite": {
            k: rewrite.get(k)
            for k in (
                "intent", "query", "bcgov_query", "sub_queries", "step_back_query",
                "fallback", "short_circuit", "status", "latency_ms",
            )
        } if rewrite else None,
        "search_skipped": next((e.get("reason") for e in of("search_skipped")), None),
        "searches": [{k: v for k, v in e.items() if k != "event"} for e in of("search")],
        "citations": [
            {"uri": c.get("uri"), "title": c.get("title"), "score": c.get("score")}
            for c in citations or []
        ],
        "models": {
            "agent": _model_id(settings.model_arn),
            "rewriter": settings.query_rewriter_model_id,
            "reranker": _model_id(settings.reranker_model_arn),
        },
        "model_calls": [
            {
                "attempt": e.get("attempt"),
                "stop_reason": e.get("stop_reason"),
                "input_tokens": (e.get("usage") or {}).get("inputTokens"),
                "output_tokens": (e.get("usage") or {}).get("outputTokens"),
            }
            for e in model_calls
        ],
        "usage": {
            "agent_calls": len(model_calls),
            "agent_input_tokens": _usage_sum(model_calls, "inputTokens"),
            "agent_output_tokens": _usage_sum(model_calls, "outputTokens"),
            "agent_cache_read_tokens": _usage_sum(model_calls, "cacheReadInputTokens"),
            "agent_cache_write_tokens": _usage_sum(model_calls, "cacheWriteInputTokens"),
            "rewriter_calls": len([e for e in rewriter_calls if e.get("usage")]),
            "rewriter_input_tokens": _usage_sum(rewriter_calls, "inputTokens"),
            "rewriter_output_tokens": _usage_sum(rewriter_calls, "outputTokens"),
        },
        "latency_ms": {
            "rewrite": rewrite.get("latency_ms") if rewrite else None,
            # Until the model's first output (a tool call or answer text).
            "first_output": first_output_ms,
            "total": latency_ms,
        },
    }
    if truncated:
        item["truncated"] = True
    # Rewritten queries can repeat what the user pasted, so every string is
    # masked, not just the question and answer.
    return _masked(item)


def record_turn(item: dict) -> None:
    """Stores a turn built by build_turn_record. Never overwrites a turn."""
    dynamodb_client().put_item(
        TableName=settings.analytics_table_name,
        Item=_to_item(item),
        ConditionExpression="attribute_not_exists(pk)",
    )


def get_turn(turn_id: str, attributes: Iterable[str]) -> dict | None:
    """The turn's `attributes`, read consistently (a rating can arrive
    moments after the turn was written); None if it doesn't exist."""
    names = _names(attributes)
    response = dynamodb_client().get_item(
        TableName=settings.analytics_table_name,
        Key=_key(turn_pk(turn_id), TURN_SK),
        ConsistentRead=True,
        ProjectionExpression=", ".join(names),
        ExpressionAttributeNames=names,
    )
    item = response.get("Item")
    return _from_item(item) if item else None


# ── Ratings ───────────────────────────────────────────────────────────────────

def record_feedback(
    turn_id: str,
    turn: dict,
    rater_hash: str,
    rating: str,
    source: str,
    now: datetime,
) -> None:
    """Sets one rater's rating of a turn. `turn` holds the turn's
    `created_at`, `created_date` and `ttl`: the rating expires with its turn,
    and is listed in the index under the turn's date."""
    values = {
        "entity": "feedback",
        "schema_version": SCHEMA_VERSION,
        "turn_id": turn_id,
        "rating": rating,
        "source": source,
        "rater_hash": rater_hash,
        "updated_at": iso_utc(now),
        "ttl": turn["ttl"],
        "turn_created_date": turn["created_date"],
        "gsi1pk": rating_partition(rating, turn["created_date"]),
        "gsi1sk": f"{turn['created_date']}#{turn['created_at']}",
    }
    names = {f"#a{i}": name for i, name in enumerate(values)}
    assignments = [f"#a{i} = :v{i}" for i in range(len(values))]
    names["#created"] = "created_at"
    assignments.append("#created = if_not_exists(#created, :now)")
    dynamodb_client().update_item(
        TableName=settings.analytics_table_name,
        Key=_key(turn_pk(turn_id), f"{FEEDBACK_SK_PREFIX}{rater_hash}"),
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={
            **{f":v{i}": _value(v) for i, v in enumerate(values.values())},
            ":now": _value(iso_utc(now)),
        },
    )


def delete_feedback(turn_id: str, rater_hash: str) -> None:
    """Clears one rater's rating; succeeds when there is none."""
    dynamodb_client().delete_item(
        TableName=settings.analytics_table_name,
        Key=_key(turn_pk(turn_id), f"{FEEDBACK_SK_PREFIX}{rater_hash}"),
    )


def query_rated_turn_ids(rating: str, month: str, first_date: str, last_date: str) -> Iterator[str]:
    """Turn ids with a `rating` from one rater, for turns asked from
    `first_date` to `last_date` (YYYY-MM-DD, inclusive) within `month`
    (YYYY-MM). A turn rated by several raters appears once per rater."""
    paginator = dynamodb_client().get_paginator("query")
    pages = paginator.paginate(
        TableName=settings.analytics_table_name,
        IndexName=RATING_INDEX,
        KeyConditionExpression="gsi1pk = :pk AND gsi1sk BETWEEN :first AND :last",
        ExpressionAttributeValues={
            ":pk": _value(f"RATING#{rating}#{month}"),
            ":first": _value(first_date),
            # "~" sorts after the "#<created_at>" that follows the date.
            ":last": _value(f"{last_date}~"),
        },
    )
    for page in pages:
        for item in page.get("Items", []):
            yield item["pk"]["S"][len(TURN_PK_PREFIX):]


def batch_get_turns(turn_ids: Iterable[str], attributes: Iterable[str]) -> dict[str, dict]:
    """The `attributes` of each turn that still exists, by turn id."""
    names = _names(["pk", *attributes])
    projection = ", ".join(names)
    ids = list(dict.fromkeys(turn_ids))
    found: dict[str, dict] = {}
    for start in range(0, len(ids), BATCH_GET_LIMIT):
        request = {
            settings.analytics_table_name: {
                "Keys": [_key(turn_pk(i), TURN_SK) for i in ids[start:start + BATCH_GET_LIMIT]],
                "ProjectionExpression": projection,
                "ExpressionAttributeNames": names,
            }
        }
        for attempt in range(BATCH_GET_RETRIES + 1):
            response = dynamodb_client().batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(settings.analytics_table_name, []):
                turn = _from_item(item)
                found[turn.pop("pk")[len(TURN_PK_PREFIX):]] = turn
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
            if attempt == BATCH_GET_RETRIES:
                raise RuntimeError("DynamoDB kept returning unprocessed keys; try again later")
            time.sleep(0.1 * 2 ** attempt)
    return found


# ── Local table ───────────────────────────────────────────────────────────────

def ensure_table() -> None:
    """Creates the table with the same keys, index and TTL as
    terraform/dynamodb.tf if it doesn't exist. Only for DynamoDB Local."""
    client = dynamodb_client()
    name = settings.analytics_table_name
    try:
        client.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": a, "AttributeType": "S"}
                for a in ("pk", "sk", "gsi1pk", "gsi1sk")
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": RATING_INDEX,
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }],
        )
    except ClientError as e:
        # Already there, or being created by another API worker.
        if e.response["Error"]["Code"] == "ResourceInUseException":
            return
        raise
    client.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
    client.update_time_to_live(
        TableName=name,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
    )
    logger.info(json.dumps({"event": "analytics_table_created", "table": name}))


# ── DynamoDB types ────────────────────────────────────────────────────────────

def _names(attributes: Iterable[str]) -> dict[str, str]:
    return {f"#p{i}": name for i, name in enumerate(attributes)}


def _key(pk: str, sk: str) -> dict:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _prepare(value):
    """Python values DynamoDB accepts: floats become Decimals."""
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _prepare(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_prepare(v) for v in value]
    return value


def _value(value) -> dict:
    return _serializer.serialize(_prepare(value))


def _to_item(record: dict) -> dict:
    return {k: _value(v) for k, v in record.items() if v is not None}


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _from_item(item: dict) -> dict:
    return {k: _plain(_deserializer.deserialize(v)) for k, v in item.items()}
