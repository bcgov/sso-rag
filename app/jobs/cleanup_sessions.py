"""One-off session cleanup, for running from cron or an ECS scheduled task:

    python -m app.jobs.cleanup_sessions [--days N]
"""

import argparse
import asyncio

import app.common.logging  # noqa: F401  (configures log output)
from app.common.config import settings
from app.services.session_cleanup import cleanup_expired_sessions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=float,
        default=settings.session_retention_days,
        help="delete sessions inactive for longer than this "
             "(default: SESSION_RETENTION_DAYS=%(default)s)"
    )
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be greater than 0")

    asyncio.run(cleanup_expired_sessions(args.days))


if __name__ == "__main__":
    main()
