"""Runtime release identity exposed in logs and as a diagnostic command."""

import json
import logging
import os
import platform


def release_identity() -> dict[str, str]:
    return {
        "git_sha": os.getenv("APP_GIT_SHA", "dev"),
        "build_date": os.getenv("APP_BUILD_DATE", "unknown"),
        "python": platform.python_version(),
    }


def log_release_identity(logger: logging.Logger) -> None:
    identity = release_identity()
    logger.info(
        "RELEASE_IDENTITY git_sha=%s build_date=%s python=%s",
        identity["git_sha"],
        identity["build_date"],
        identity["python"],
    )


if __name__ == "__main__":
    print(json.dumps(release_identity(), sort_keys=True))
