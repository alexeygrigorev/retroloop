"""Settings for the test suite.

`config.settings` refuses to start without a real SECRET_KEY, so rather than
weakening that, the test run supplies its own environment here and then uses the
production settings unchanged.
"""

import os

os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-used-outside-the-suite")
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DATABASE_URL", "postgres://postgres:postgres@localhost:5432/feedback")

from config.settings import *  # noqa: F403
