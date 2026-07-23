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

# No test makes a network call, and no test needs a key. The whole pipeline runs
# against a stand-in client instead — not a mock reached for by one test, the
# default for the suite, so a test that forgets to inject one still cannot reach
# the API. The key is blanked as well, so a developer .env that has a real one
# cannot be spent by a test run; the tests that prove the missing-key failure
# and the SDK call itself set what they need for themselves.
TRANSCRIPTION_CLIENT = "ai.fakes.EchoTranscriptionClient"
# Inert on purpose: the clustering job is enqueued by every reveal, and a
# stand-in that grouped cards would rewrite the board under every fixture that
# reveals a cycle with cards. The suite's default therefore suggests nothing,
# and the clustering tests inject a client that does — a scripted one, or the
# real client driven by a fake SDK.
CLUSTERING_CLIENT = "ai.fakes.NullClusteringClient"
# Inert on purpose, exactly like CLUSTERING_CLIENT above: extraction is enqueued
# after every transcription, and a stand-in that invented decisions and action
# items would write draft rows under every fixture that runs the meeting
# pipeline. The suite's default therefore extracts nothing, and the extraction
# tests inject a client that does — a scripted one, or the real client driven by
# a fake SDK.
EXTRACTION_CLIENT = "ai.fakes.NullExtractionClient"
OPENAI_API_KEY = ""
