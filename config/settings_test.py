"""Settings for the test suite.

`config.settings` refuses to start without a real SECRET_KEY, so rather than
weakening that, the test run supplies its own environment here and then uses the
production settings unchanged.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def dotenv_value(key: str) -> str | None:
    """Read one key out of this checkout's .env, without importing settings.

    Every git worktree points DATABASE_URL at a database of its own, so suites
    running in parallel worktrees never share a test database. Only this one key
    is honoured: the rest of the test environment is fixed below, so a developer
    .env cannot quietly turn DEBUG on inside the suite.
    """
    path = BASE_DIR / ".env"
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("\"'")
    return None


os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-used-outside-the-suite")
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault(
    "DATABASE_URL",
    dotenv_value("DATABASE_URL") or "postgres://postgres:postgres@localhost:5432/feedback",
)

from config.settings import *  # noqa: F403, E402

# The suite runs task bodies inline, in the enqueueing process, so no worker has
# to be running for a test to prove what a task does. Everything else about the
# queue stays as it is in production: django_tasks_db is still installed and its
# tables are still created, so a test can also drive the ORM backend directly
# when it needs to assert on the queue itself.
TASKS = {
    "default": {
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        "QUEUES": ["default"],
    }
}

# A checked-in stand-in for the manifest `npm run build:js` writes, so the Python
# suite runs on a machine that has never installed Node — the same reason nothing
# here needs `npm run build:css` either. It is a fixture, not a relaxation: the
# real manifest, the caching rule and the loud failure when the build is missing
# are each tested against a real file in tests/test_island.py, which points this
# setting back at the build output.
VITE_MANIFEST = BASE_DIR / "tests" / "fixtures" / "vite_manifest.json"
