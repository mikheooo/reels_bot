from app.core.runtime import release_identity


def test_release_identity_comes_from_environment(monkeypatch):
    monkeypatch.setenv("APP_GIT_SHA", "abc123")
    monkeypatch.setenv("APP_BUILD_DATE", "2026-09-06T00:00:00Z")
    identity = release_identity()
    assert identity["git_sha"] == "abc123"
    assert identity["build_date"] == "2026-09-06T00:00:00Z"
    assert identity["python"]
