"""Deletes stored conversations: those inactive for longer than
SESSION_RETENTION_DAYS (scheduled cleanup), or all of one browser's
conversations on request (DELETE /chat/history).

Strands keeps each conversation under two prefixes of the agent storage:
``session/<id>/`` (agent snapshots, rewritten after every invocation) and
``context/<id>/`` (context-manager stash). A conversation's last activity is
the newest modification time of any object under either prefix. The Strands
``Storage`` interface doesn't expose timestamps, so they are read directly
from the bucket (S3 ``LastModified``) or the local directory (file mtime).
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from app.agents.rag_agent import (
    LOCAL_STORAGE_DIR,
    delete_session,
    is_valid_session_id,
    is_valid_tab_id
)
from app.common.config import settings

logger = logging.getLogger(__name__)

# Top-level storage prefixes Strands writes per-session data under.
SESSION_PREFIXES = ("session", "context")


def _is_managed_id(conversation_id: str) -> bool:
    """Only touch ids in the format this app issues
    (`session_<uuid>` or `session_<uuid>__<tab uuid>`)."""
    session_id, sep, tab_id = conversation_id.partition("__")
    return is_valid_session_id(session_id) and (not sep or is_valid_tab_id(tab_id))


def _record(activity: dict[str, datetime], key: str, modified: datetime) -> None:
    parts = key.split("/")
    if len(parts) < 3 or parts[0] not in SESSION_PREFIXES:
        return
    conversation_id = parts[1]
    if not _is_managed_id(conversation_id):
        return
    if conversation_id not in activity or modified > activity[conversation_id]:
        activity[conversation_id] = modified


def _local_activity(base_dir: str) -> dict[str, datetime]:
    activity: dict[str, datetime] = {}
    for prefix in SESSION_PREFIXES:
        root = os.path.join(base_dir, prefix)
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                try:
                    mtime = os.path.getmtime(path)
                except FileNotFoundError:
                    continue
                key = os.path.relpath(path, base_dir).replace(os.sep, "/")
                _record(activity, key, datetime.fromtimestamp(mtime, timezone.utc))
    return activity


def _s3_activity(bucket: str) -> dict[str, datetime]:
    import boto3

    client = boto3.client("s3", region_name=settings.aws_region)
    paginator = client.get_paginator("list_objects_v2")
    activity: dict[str, datetime] = {}
    for prefix in SESSION_PREFIXES:
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents", []):
                _record(activity, obj["Key"], obj["LastModified"])
    return activity


def list_session_activity() -> dict[str, datetime]:
    """Conversation id -> time of its most recent write (UTC)."""
    if settings.environment == "local":
        return _local_activity(LOCAL_STORAGE_DIR)
    return _s3_activity(settings.agent_storage_s3_bucket)


def _belongs_to(conversation_id: str, session_id: str) -> bool:
    return conversation_id == session_id or conversation_id.startswith(f"{session_id}__")


def list_browser_conversations(session_id: str) -> set[str]:
    """Every stored conversation of one browser session: the cookie-level one
    (`session_<uuid>`) and each tab's (`session_<uuid>__<tab uuid>`). Listing
    is scoped to the session's key prefix rather than the whole bucket."""

    if not is_valid_session_id(session_id):
        return set()

    found: set[str] = set()
    if settings.environment == "local":
        for prefix in SESSION_PREFIXES:
            root = os.path.join(LOCAL_STORAGE_DIR, prefix)
            try:
                names = os.listdir(root)
            except FileNotFoundError:
                continue
            # Strands deletes files but leaves their directories behind.
            found.update(
                name for name in names
                if any(files for _, _, files in os.walk(os.path.join(root, name)))
            )
    else:
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        paginator = client.get_paginator("list_objects_v2")
        for prefix in SESSION_PREFIXES:
            pages = paginator.paginate(
                Bucket=settings.agent_storage_s3_bucket,
                Prefix=f"{prefix}/{session_id}"
            )
            for page in pages:
                for obj in page.get("Contents", []):
                    parts = obj["Key"].split("/")
                    if len(parts) >= 2:
                        found.add(parts[1])

    return {
        cid for cid in found
        if _is_managed_id(cid) and _belongs_to(cid, session_id)
    }


async def delete_browser_history(session_id: str) -> list[str]:
    """Deletes every stored conversation of one browser session (all of its
    tabs). Returns the deleted conversation ids; raises if any delete fails
    so the caller can report that history may remain."""

    conversations = await asyncio.to_thread(list_browser_conversations, session_id)

    failed = []
    for cid in sorted(conversations):
        try:
            await delete_session(cid)
        except Exception:
            logger.exception("failed to delete conversation %s", cid)
            failed.append(cid)

    if failed:
        raise RuntimeError(f"{len(failed)} of {len(conversations)} conversations could not be deleted")
    return sorted(conversations)


async def cleanup_expired_sessions(
    retention_days: float | None = None,
    now: datetime | None = None
) -> list[str]:
    """Deletes every conversation with no writes in the last
    `retention_days` (default SESSION_RETENTION_DAYS). Returns deleted ids."""

    if retention_days is None:
        retention_days = settings.session_retention_days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)

    activity = await asyncio.to_thread(list_session_activity)
    expired = sorted(cid for cid, last in activity.items() if last < cutoff)

    deleted = []
    for conversation_id in expired:
        try:
            await delete_session(conversation_id)
            deleted.append(conversation_id)
        except Exception:
            logger.exception("failed to delete expired session %s", conversation_id)

    logger.info(
        "session cleanup: %d of %d sessions older than %s days deleted",
        len(deleted), len(activity), retention_days
    )
    return deleted


async def run_session_cleanup_loop(interval_hours: float) -> None:
    """Runs `cleanup_expired_sessions` every `interval_hours` until cancelled.

    Every API worker/task runs this loop; deletes are idempotent, and the
    random start delay spreads the runs out."""

    interval = interval_hours * 3600
    await asyncio.sleep(random.uniform(0, min(interval, 300)))

    while True:
        try:
            await cleanup_expired_sessions()
        except Exception:
            logger.exception("session cleanup failed")
        await asyncio.sleep(interval)
