"""Lists rated answers, with their question and answer:

    python -m app.jobs.feedback_report [--rating up|down|all]
        [--date YYYY-MM-DD | --days N | --from YYYY-MM-DD --to YYYY-MM-DD] [--json]

Dates are the day the question was asked, in BC (America/Vancouver). The
default is today's 👍 and 👎. Runs with your own AWS credentials (it needs
dynamodb:Query on the table's gsi1 index and dynamodb:BatchGetItem on the
table; see terraform/iam.tf) against ANALYTICS_TABLE_NAME, or DynamoDB Local
when AWS_ENDPOINT_URL_DYNAMODB is set. --json prints one JSON object per
line, e.g. as candidate cases for the evals dataset.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta

import app.common.logging  # noqa: F401  (configures log output)
from app.services.feedback_service import DOWN, UP, RatedTurn, list_rated_turns
from app.services.interaction_store import LOCAL_TZ

RATING_CHOICES = {UP: (UP,), DOWN: (DOWN,), "all": (UP, DOWN)}


def date_range(
    today: date,
    on: date | None = None,
    days: int | None = None,
    first: date | None = None,
    last: date | None = None,
) -> tuple[date, date]:
    """The inclusive range the options select; today when none is given."""
    if on:
        return on, on
    if days:
        return today - timedelta(days=days - 1), today
    if first or last:
        return first or last, last or today
    return today, today


def _summary(rated: RatedTurn) -> dict:
    return {**rated.turn, "up": rated.up, "down": rated.down}


def _print_text(rated: list[RatedTurn], first: date, last: date) -> None:
    span = first.isoformat() if first == last else f"{first} to {last}"
    print(f"{len(rated)} rated answer(s), {span}\n")
    for r in rated:
        turn = r.turn
        asked = datetime.fromisoformat(turn["created_at"]).astimezone(LOCAL_TZ)
        votes = ", ".join(f"{n} {label}" for n, label in ((r.up, "👍"), (r.down, "👎")) if n)
        print(f"── {asked:%Y-%m-%d %H:%M} · {votes} · {turn.get('grounding_status') or '-'} · {turn['turn_id']}")
        print(f"Q: {turn.get('question') or ''}")
        print(f"A: {turn.get('answer') or '(no answer: ' + str(turn.get('outcome')) + ')'}")
        sources = [c.get("uri") for c in turn.get("citations") or [] if c.get("uri")]
        if sources:
            print("Sources: " + ", ".join(sources))
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rating", choices=RATING_CHOICES, default="all")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--date", type=date.fromisoformat, help="one day (YYYY-MM-DD)")
    when.add_argument("--days", type=int, help="the last N days, today included")
    when.add_argument("--from", dest="first", type=date.fromisoformat, help="first day of a range")
    parser.add_argument("--to", dest="last", type=date.fromisoformat, help="last day of a range (default: today)")
    parser.add_argument("--json", action="store_true", help="one JSON object per line")
    args = parser.parse_args(argv)

    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    if args.last and (args.date or args.days):
        parser.error("--to goes with --from")

    today = datetime.now(LOCAL_TZ).date()
    first, last = date_range(today, args.date, args.days, args.first, args.last)
    if first > last:
        parser.error("the first day is after the last")

    rated = list_rated_turns(RATING_CHOICES[args.rating], first, last)

    if args.json:
        for r in rated:
            sys.stdout.write(json.dumps(_summary(r), ensure_ascii=False) + "\n")
    else:
        _print_text(rated, first, last)


if __name__ == "__main__":
    main()
