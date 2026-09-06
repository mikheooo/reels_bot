"""Reaper and error-reporting tests.

Two failures this file exists to prevent from coming back:
  * jobs frozen in PROCESSING forever because the worker died mid-flight
    (the reaper's job), and
  * jobs in ERROR with a NULL/empty error_text, i.e. a status with no cause
    (format_error_text's job).
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite as sqlite_dialect

from app.db.models import Job
from app.worker import reaper
from app.worker.reaper import (
    expire_stale_processing,
    stale_processing_condition,
    stale_reason,
)
from app.worker.tasks import format_error_text

# --------------------------------------------------------------------------
# format_error_text — nothing may ever land in ERROR without a cause
# --------------------------------------------------------------------------

def test_format_error_text_keeps_type_and_message():
    out = format_error_text(ValueError("boom"))
    assert out == "ValueError: boom"


def test_format_error_text_never_empty_for_messageless_exception():
    # `raise ValueError()` stringifies to "". Before this fix that produced a
    # row with status=ERROR and error_text='' — nothing to debug.
    try:
        raise ValueError()
    except ValueError as e:
        out = format_error_text(e)
    assert out.startswith("ValueError")
    assert len(out) > len("ValueError"), "traceback must be attached when the message is empty"


def test_format_error_text_is_truncated():
    out = format_error_text(RuntimeError("x" * 5000), limit=100)
    assert len(out) == 100


# --------------------------------------------------------------------------
# stale_processing_condition — the predicate decides what gets killed, so it
# is verified for real by running the compiled SQL against sqlite3 in memory.
# --------------------------------------------------------------------------

def _ids_matching(jobs, cutoff):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (id TEXT, status TEXT, error_text TEXT, "
        "created_at TEXT, started_at TEXT)"
    )
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", jobs)
    stmt = select(Job.id).where(stale_processing_condition(cutoff))
    sql = str(stmt.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}))
    rows = {r[0] for r in conn.execute(sql)}
    conn.close()
    return rows


def _job(job_id, status, created_at, started_at=None):
    return (job_id, status, None, created_at, started_at)


def test_predicate_expires_old_processing_and_keeps_the_rest():
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.%f")
    fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")

    jobs = [
        _job("zombie", "PROCESSING", created_at=old, started_at=old),
        _job("fresh", "PROCESSING", created_at=fresh, started_at=fresh),
        _job("legacy_null_started", "PROCESSING", created_at=old),
        _job("legacy_fresh", "PROCESSING", created_at=fresh),
        _job("done_old", "DONE", created_at=old, started_at=old),
        _job("queued_old", "QUEUED", created_at=old),
    ]
    cutoff = now - timedelta(seconds=3600)
    assert _ids_matching(jobs, cutoff) == {"zombie", "legacy_null_started"}


def test_predicate_ignores_created_at_when_started_at_is_known():
    """A job queued for hours must not be killed the moment it starts."""
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    created = (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
    started = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S.%f")

    jobs = [_job("just_started", "PROCESSING", created_at=created, started_at=started)]
    cutoff = now - timedelta(seconds=3600)
    assert _ids_matching(jobs, cutoff) == set()


# --------------------------------------------------------------------------
# expire_stale_processing — mutation + commit, without touching a real DB
# --------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _query):
        return _FakeResult(self.rows)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_expire_stale_processing_marks_rows_and_reports_count():
    stale = Job(id="a", status="PROCESSING")
    session = _FakeSession([stale])

    n = await expire_stale_processing(max_age_seconds=3600, session_factory=lambda: session)

    assert n == 1
    assert stale.status == "ERROR"
    assert "Worker died" in stale.error_text
    assert session.commits == 1


@pytest.mark.asyncio
async def test_expire_stale_processing_no_rows_no_commit():
    session = _FakeSession([])
    n = await expire_stale_processing(max_age_seconds=3600, session_factory=lambda: session)
    assert n == 0
    assert session.commits == 0


def test_stale_reason_mentions_the_threshold():
    assert "3600" in stale_reason(3600)


# --------------------------------------------------------------------------
# End-to-end against a real database. Needs Postgres on localhost:5432
# (docker-compose publishes it). Skips instead of failing when unavailable.
# --------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_expire_stale_processing_against_postgres():
    from sqlalchemy import text

    from app.db.database import AsyncSessionLocal

    now = datetime.utcnow()
    ids = [f"reaper_test_{uuid.uuid4().hex}" for _ in range(2)]
    try:
        async with AsyncSessionLocal() as s:
            try:
                await s.execute(text("SELECT 1"))
            except Exception as e:
                pytest.skip(f"Postgres unavailable: {e}")

            s.add(Job(
                id=ids[0], user_id=0, original_url="https://example.com/zombie",
                url_hash=f"zh_{uuid.uuid4().hex}", status="PROCESSING",
                created_at=now - timedelta(hours=5), started_at=now - timedelta(hours=5),
            ))
            s.add(Job(
                id=ids[1], user_id=0, original_url="https://example.com/fresh",
                url_hash=f"fr_{uuid.uuid4().hex}", status="PROCESSING",
                created_at=now, started_at=now,
            ))
            await s.commit()

        expired = await expire_stale_processing(max_age_seconds=3600)
        assert expired >= 1

        async with AsyncSessionLocal() as s:
            zombie = await s.get(Job, ids[0])
            fresh = await s.get(Job, ids[1])
            assert zombie.status == "ERROR"
            assert zombie.error_text
            assert fresh.status == "PROCESSING"
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": ids})
            await s.commit()
