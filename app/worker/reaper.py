"""Expire PROCESSING jobs whose worker died before writing a terminal status.

A job turns into a zombie when the worker disappears while the job is in
flight — container restart, OOM-killer, deploy, or ARQ hitting ``job_timeout``.
``process_video`` never reaches its ``except``/``finally`` handlers in that
case, so the row stays ``PROCESSING`` forever: no error, no user message, and
the job is never retried.

Staleness is judged by ``started_at``, not ``created_at``. With ``max_jobs=2``
and a 12-20 min pipeline a job can legitimately sit in QUEUED for an hour
behind other jobs; a created_at-based reaper would kill a job that started
30 seconds ago. Rows created before ``started_at`` existed (all legacy
zombies) have NULL and fall back to created_at — new jobs always set it, so
the NULL branch only ever sees old rows.
"""

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from app.db.database import AsyncSessionLocal
from app.db.models import Job, JobStatus

logger = logging.getLogger(__name__)

# Must stay comfortably above WorkerSettings.job_timeout (1800s), otherwise the
# reaper races ARQ and kills jobs that are still legitimately running.
DEFAULT_MAX_AGE_SECONDS = float(os.getenv("REAPER_MAX_AGE_SECONDS", "3600"))


def stale_reason(max_age_seconds: float) -> str:
    return (
        "Worker died without writing a terminal status (container restart, OOM "
        f"or ARQ job_timeout). Job stayed in PROCESSING for more than "
        f"{int(max_age_seconds)}s."
    )


def stale_processing_condition(cutoff: datetime):
    """Rows considered zombies: PROCESSING and started before `cutoff`.

    NULL started_at means the row predates the column (every current zombie),
    so it falls back to created_at. New jobs always set started_at, which keeps
    a job that waited in QUEUED for hours from being killed the moment it starts.
    """
    return and_(
        Job.status == JobStatus.PROCESSING,
        or_(
            (Job.started_at.is_not(None)) & (Job.started_at < cutoff),
            (Job.started_at.is_(None)) & (Job.created_at < cutoff),
        ),
    )


async def expire_stale_processing(
    max_age_seconds: float | None = None,
    session_factory=AsyncSessionLocal,
    now: datetime | None = None,
) -> int:
    """Mark stale PROCESSING jobs as ERROR. Returns how many were expired."""
    max_age = DEFAULT_MAX_AGE_SECONDS if max_age_seconds is None else max_age_seconds
    now = now if now is not None else datetime.utcnow()
    cutoff = now - timedelta(seconds=max_age)
    reason = stale_reason(max_age)

    async with session_factory() as session:
        rows = (
            await session.execute(select(Job).where(stale_processing_condition(cutoff)))
        ).scalars().all()

        for job in rows:
            logger.warning(
                "REAPER: expiring stale job %s (created=%s, started=%s)",
                job.id,
                job.created_at,
                job.started_at,
            )
            job.status = JobStatus.ERROR
            job.error_text = reason

        if rows:
            await session.commit()
            logger.warning("REAPER: expired %d stale job(s)", len(rows))

    return len(rows)


async def reap_stale_jobs(ctx) -> int:
    """ARQ cron entrypoint. ``ctx`` is supplied by arq and is unused."""
    return await expire_stale_processing()
