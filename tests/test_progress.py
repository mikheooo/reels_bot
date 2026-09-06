"""Deterministic tests for the Telegram progress helper.

No network, no Telegram, no database: the DB session and the Bot
are injected as fakes. Covers the acceptance rules:
- one message edited through the whole sequence;
- edit failure never breaks the pipeline;
- terminal states (ERROR / REVIEW_REQUIRED) have their own texts;
- jobs without progress ids never trigger a fake flow.
"""

import pytest

from app.worker.progress import STAGES, set_progress, stage_text


class FakeJob:
    def __init__(self, chat_id=123, message_id=456):
        self.tg_progress_chat_id = chat_id
        self.tg_progress_message_id = message_id


class FakeSession:
    def __init__(self, job):
        self._job = job

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, job_id):
        return self._job


class FakeBotSession:
    def __init__(self, bot):
        self._bot = bot

    async def close(self):
        self._bot.closed = True


class FakeBot:
    def __init__(self, fail=False):
        self.edits = []
        self.closed = False
        self.fail = fail
        self.session = FakeBotSession(self)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("message to edit not found")
        self.edits.append((chat_id, message_id, text, reply_markup))


def make_factory(job, bot):
    def session_factory():
        return FakeSession(job)

    def bot_factory():
        return bot

    return session_factory, bot_factory


def test_full_sequence_edits_one_message_in_order():
    job, bot = FakeJob(), FakeBot()
    sf, bf = make_factory(job, bot)
    seq = ["QUEUED", "DOWNLOAD", "TRANSCRIPT", "ANALYSIS", "VERIFY", "FINALIZE", "COMPLETE"]
    for stage in seq:
        assert set_progress_sync(job, bot, sf, bf, stage) is True
    assert [t for (_, _, t, _) in bot.edits] == [stage_text(s) for s in seq]
    assert {(c, m) for (c, m, _, _) in bot.edits} == {(123, 456)}
    assert bot.closed is True


def set_progress_sync(job, bot, sf, bf, stage):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        set_progress("job-1", stage, session_factory=sf, bot_factory=bf)
    )


def test_edit_failure_does_not_raise():
    job, bot = FakeJob(), FakeBot(fail=True)
    sf, bf = make_factory(job, bot)
    assert set_progress_sync(job, bot, sf, bf, "ANALYSIS") is False
    assert bot.edits == []


def test_missing_ids_never_call_bot():
    job, bot = FakeJob(chat_id=None, message_id=None), FakeBot()
    sf, bf = make_factory(job, bot)
    calls = []
    orig = bf

    def counting_factory():
        calls.append(1)
        return orig()

    assert set_progress_sync(job, bot, sf, counting_factory, "DOWNLOAD") is False
    assert calls == []
    assert bot.edits == []


def test_missing_job_returns_false():
    bot = FakeBot()
    sf, bf = make_factory(None, bot)
    assert set_progress_sync(None, bot, sf, bf, "VERIFY") is False


def test_terminal_texts():
    assert stage_text("ERROR").startswith("❌")
    assert "⚠️" in stage_text("REVIEW_REQUIRED")
    assert stage_text("COMPLETE").startswith("✅")
    assert stage_text("NOPE_UNKNOWN") == stage_text("QUEUED")


def test_progress_texts_are_plain():
    for key, text in STAGES.items():
        assert not any(ch in text for ch in "*_[]()~`#"), key
