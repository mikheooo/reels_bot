"""Deterministic tests for canonical full-transcript persistence & delivery.

No network, no Telegram, no production DB: sqlite in-memory round-trips
plus fake Bot/callback objects.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.bot.transcript_view import (
    DOC_THRESHOLD,
    LEGACY_TEXT,
    send_full_transcript,
    should_send_document,
    split_transcript,
)
from app.db.database import Base
from app.db.models import Job

LONG_TEXT = ("Слово flows here. " * 400).strip()  # >> 1000 chars
assert len(LONG_TEXT) > 5000


def _sqlite_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _words(t):
    return t.split()


def test_persistence_round_trip_exact():
    s = _sqlite_session()
    s.add(Job(id="j1", user_id=1, original_url="u", url_hash="h",
              status="DONE", full_transcript=LONG_TEXT))
    s.commit()
    back = s.get(Job, "j1").full_transcript
    assert back == LONG_TEXT


def test_preview_is_not_canonical():
    assert len(LONG_TEXT[:1000]) == 1000
    assert len(LONG_TEXT) > 1000
    s = _sqlite_session()
    s.add(Job(id="j2", user_id=1, original_url="u", url_hash="h",
              status="DONE", full_transcript=LONG_TEXT))
    s.commit()
    assert len(s.get(Job, "j2").full_transcript) > 1000


def test_chunks_reassemble_without_loss():
    chunks = split_transcript(LONG_TEXT)
    assert len(chunks) > 1
    assert _words(" ".join(chunks)) == _words(LONG_TEXT)
    for c in chunks:
        assert len(c) <= 4096


def test_hard_fallback_single_long_word():
    text = "a" * 9000  # no whitespace at all
    chunks = split_transcript(text)
    assert "".join(chunks) == text
    assert all(len(c) <= 4096 for c in chunks)


def test_document_threshold():
    assert should_send_document("x" * (DOC_THRESHOLD + 1)) is True
    assert should_send_document("x" * DOC_THRESHOLD) is False


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text):
        self.messages.append(text)

    async def send_document(self, chat_id, document, caption=None):
        self.documents.append((document, caption))


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_short_transcript_goes_as_messages():
    bot = FakeBot()
    mode = _run(send_full_transcript(bot, 1, "короткий текст"))
    assert mode == "chunks"
    assert bot.documents == []
    assert _words(" ".join(bot.messages)) == _words(
        "📄 Полная расшифровка короткий текст"
    )


def test_very_long_transcript_goes_as_document():
    bot = FakeBot()
    mode = _run(send_full_transcript(bot, 1, LONG_TEXT * 3))
    assert mode == "document"
    assert bot.messages == []
    doc, _caption = bot.documents[0]
    assert doc.data == (LONG_TEXT * 3).encode("utf-8")
    assert doc.filename == "transcript.txt"


def test_error_and_review_rows_keep_transcript():
    s = _sqlite_session()
    s.add(Job(id="je", user_id=1, original_url="u", url_hash="h",
              status="ERROR", full_transcript=LONG_TEXT))
    s.add(Job(id="jr", user_id=1, original_url="u2", url_hash="h2",
              status="REVIEW_REQUIRED", full_transcript=LONG_TEXT))
    s.commit()
    assert s.get(Job, "je").full_transcript == LONG_TEXT
    assert s.get(Job, "jr").full_transcript == LONG_TEXT


def test_legacy_job_without_transcript():
    s = _sqlite_session()
    s.add(Job(id="jl", user_id=1, original_url="u", url_hash="h", status="DONE"))
    s.commit()
    assert s.get(Job, "jl").full_transcript is None
    assert "не сохранена" in LEGACY_TEXT
