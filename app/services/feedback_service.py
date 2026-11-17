"""Thumbs up/down ratings of stored turns.

Callers pass a rater hash and a source rather than a cookie, so the web route
and a future Teams bridge can share it (docs/plans/plan_feedback_analytics.md).
"""

import json
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.common import metrics
from app.services import interaction_store

logger = logging.getLogger(__name__)

UP = "up"
DOWN = "down"

# What a rating needs from its turn: the owner check, the expiry and date it
# inherits, and the grounding status for the metric.
_RATING_FIELDS = ("session_hash", "created_at", "created_date", "ttl", "grounding_status")

REVIEW_FIELDS = (
    "turn_id", "created_at", "created_date", "channel", "question", "answer",
    "outcome", "grounding_status", "response_type", "answer_replaced", "citations",
)


def set_rating(
    turn_id: str,
    rater_hash: str,
    rating: str | None,
    source: str,
    *,
    session_hash: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Sets (`up`, `down`) or clears (None) one rater's rating of a turn.
    Repeated calls overwrite, so it is idempotent.

    Returns False, changing nothing, if the turn doesn't exist, or if
    `session_hash` is given and the turn belongs to another session."""

    turn = interaction_store.get_turn(turn_id, _RATING_FIELDS)
    if turn is None or (session_hash is not None and turn.get("session_hash") != session_hash):
        return False

    if rating is None:
        interaction_store.delete_feedback(turn_id, rater_hash)
    else:
        interaction_store.record_feedback(turn_id, turn, rater_hash, rating, source, now or datetime.now(UTC))
        # Votes cast, so a changed vote counts once for each rating.
        metrics.count(
            "Feedback",
            {"Rating": rating, "GroundingStatus": turn.get("grounding_status") or "NONE"},
            [["Rating"], ["Rating", "GroundingStatus"]],
        )

    logger.info(json.dumps({"event": "feedback", "rating": rating, "source": source}))
    return True


@dataclass(frozen=True)
class RatedTurn:
    turn: dict
    up: int
    down: int


def months(first: date, last: date) -> Iterator[str]:
    """Each month (YYYY-MM) from `first` to `last`, inclusive."""
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def list_rated_turns(ratings: Iterable[str], first: date, last: date) -> list[RatedTurn]:
    """Turns asked from `first` to `last` (BC dates, inclusive) that have
    any of `ratings`, oldest first, with their counts of those ratings."""

    votes: dict[str, Counter] = {}
    for rating in ratings:
        for month in months(first, last):
            for turn_id in interaction_store.query_rated_turn_ids(
                rating, month, first.isoformat(), last.isoformat()
            ):
                votes.setdefault(turn_id, Counter())[rating] += 1

    turns = interaction_store.batch_get_turns(votes, REVIEW_FIELDS)
    rated = [
        RatedTurn(turns[turn_id], up=count[UP], down=count[DOWN])
        for turn_id, count in votes.items()
        if turn_id in turns
    ]
    return sorted(rated, key=lambda r: r.turn.get("created_at") or "")
