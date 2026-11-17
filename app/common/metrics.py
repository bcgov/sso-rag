"""CloudWatch metrics written as Embedded Metric Format (EMF) log lines.

The ECS awslogs driver sends each stdout line to CloudWatch Logs as one
event, and CloudWatch turns events in EMF into metrics. An EMF event must be
the JSON object alone, so these lines go to stdout through their own handler,
without the timestamp/level prefix of the other log lines.
"""

import json
import logging
import sys
import time

from app.common.config import settings

NAMESPACE = "SsoRag"


class _StdoutHandler(logging.StreamHandler):
    """Writes to the current sys.stdout (which tests and tools may swap)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


_logger = logging.getLogger("app.metrics")
_logger.propagate = False
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _handler = _StdoutHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)


def count(metric: str, dimensions: dict[str, str], dimension_sets: list[list[str]]) -> None:
    """Adds 1 to `metric`, once per dimension set. Every set also gets the
    Environment dimension, so sandbox and prod don't mix."""
    _logger.info(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["Environment", *names] for names in dimension_sets],
                "Metrics": [{"Name": metric, "Unit": "Count"}],
            }],
        },
        "Environment": settings.environment,
        **dimensions,
        metric: 1,
    }))
