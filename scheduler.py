"""Persistent scheduler for delayed Meta posts.

Wraps APScheduler's BackgroundScheduler with a SQLite jobstore so scheduled
jobs survive container restarts. Jobs are dispatched into a fresh asyncio loop
via `asyncio.run()` because BackgroundScheduler fires in worker threads while
the post helpers in `server.py` are coroutines.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import pathlib
import re
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger("instagram-mcp")

TZ = ZoneInfo("Europe/Berlin")

_scheduler: BackgroundScheduler | None = None


def init(db_path: pathlib.Path) -> BackgroundScheduler:
    """Create and start the scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    db_path.parent.mkdir(parents=True, exist_ok=True)
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}
    _scheduler = BackgroundScheduler(jobstores=jobstores, timezone=TZ)
    _scheduler.start()
    log.info("scheduler started, jobstore=%s pending_jobs=%d",
             db_path, len(_scheduler.get_jobs()))
    return _scheduler


def get() -> BackgroundScheduler:
    if _scheduler is None:
        raise RuntimeError("scheduler not initialized — call scheduler.init() first")
    return _scheduler


_REL_RE = re.compile(r"^in\s+(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)$", re.I)

_REL_UNITS = {
    "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60, "mins": 60,
    "h": 3600, "hr": 3600, "hrs": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_ISO_FMTS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_when(when: str) -> dt.datetime:
    """Parse 'in 2h' / 'in 30m' / ISO timestamps into an aware datetime (Europe/Berlin).

    Returns a timezone-aware datetime. Raises ValueError if the input is junk
    or already in the past.
    """
    s = when.strip()
    now = dt.datetime.now(tz=TZ)

    m = _REL_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = _REL_UNITS[unit] * n
        run_at = now + dt.timedelta(seconds=seconds)
    else:
        run_at = None
        for fmt in _ISO_FMTS:
            try:
                naive = dt.datetime.strptime(s, fmt)
                run_at = naive.replace(tzinfo=TZ)
                break
            except ValueError:
                continue
        if run_at is None:
            try:
                run_at = dt.datetime.fromisoformat(s)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=TZ)
            except ValueError as e:
                raise ValueError(
                    f"cannot parse 'when'={s!r}; try 'in 2h', "
                    "'2026-06-14 18:00', or an ISO 8601 timestamp"
                ) from e

    if run_at <= now:
        raise ValueError(
            f"scheduled time {run_at.isoformat()} is not in the future "
            f"(now={now.isoformat()})"
        )
    return run_at


def run_async_job(target: str, kwargs: dict[str, Any]) -> None:
    """Top-level entrypoint APScheduler resolves by import path.

    Must be a module-level function so APScheduler can pickle a stable
    reference into the jobstore. Imports server lazily to avoid a circular
    import at module load.
    """
    import server  # noqa: PLC0415 — lazy on purpose

    targets = {
        "facebook": server.post_facebook_local_photo,
        "instagram": server.post_instagram_local_photo,
        "facebook_carousel": server.post_facebook_local_carousel,
        "instagram_carousel": server.post_instagram_local_carousel,
        "instagram_reel": server.post_instagram_local_reel,
        "facebook_video": server.post_facebook_local_video,
        "facebook_reel": server.post_facebook_local_reel,
    }
    fn = targets.get(target)
    if fn is None:
        log.error("scheduled job: unknown target %r", target)
        return

    try:
        result = asyncio.run(fn(**kwargs))
    except Exception:
        log.exception("scheduled %s job raised", target)
        return

    log.info(
        "scheduled %s job done ok=%s kwargs=%s",
        target, result.get("ok"), {k: v for k, v in kwargs.items() if k != "caption"},
    )
