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
