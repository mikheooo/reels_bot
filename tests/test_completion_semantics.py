from app.worker.tasks import deferred_audit_fields, determine_completion_status


def test_done_requires_all_applicable_steps_to_succeed():
    outcomes = {
        "user": "SUCCEEDED",
        "channel": "SUCCEEDED",
        "plan": "SUCCEEDED",
        "task_db": "SUCCEEDED",
    }
    assert determine_completion_status(outcomes) == "DONE"


def test_idempotent_channel_skip_is_complete():
    outcomes = {
        "user": "SUCCEEDED",
        "channel": "SKIPPED_DUPLICATE",
        "plan": "NOT_APPLICABLE",
        "task_db": "NOT_APPLICABLE",
    }
    assert determine_completion_status(outcomes) == "DONE"


def test_any_delivery_failure_is_partial():
    outcomes = {
        "user": "SUCCEEDED",
        "channel": "FAILED:TelegramError",
        "plan": "SUCCEEDED",
        "task_db": "SUCCEEDED",
    }
    assert determine_completion_status(outcomes) == "PARTIAL"


def test_new_jobs_do_not_create_dormant_audit_schedules():
    assert deferred_audit_fields() == {
        "audit_scheduled_at": None,
        "audit_state": "DEFERRED",
    }
