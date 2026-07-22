"""Background tasks, and the conventions every task in this project follows.

Work that must not block a request goes on the Postgres-backed queue configured
by the ``TASKS`` setting. `manage.py db_worker` drains it. The rules:

* A task is a module-level function decorated with ``@task``. It takes only
  values that survive a round trip through JSON — an id, a path, a flag. Never a
  model instance: the queue row stores arguments as JSON, and a model cannot be
  written into one.
* A task body re-fetches whatever it needs by id and tolerates the row having
  changed, or gone, since the moment it was enqueued. Time passes between the
  enqueue and the run.
* Anything enqueued from inside a transaction goes through
  :func:`enqueue_on_commit`. See its docstring for why.
* Nothing is retried automatically. See :data:`RETRY_POLICY` below.
"""

import re
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.tasks import task
from django.utils import timezone

RETRY_POLICY = """No task is retried automatically, and no backoff is configured.

A task that raises is marked FAILED with its traceback and is left alone; the
worker logs it and moves on to the next job. Re-running one is a deliberate act:
enqueue it again.

This is a decision, not a default we never looked at. The media pipeline deletes
its source recording in a `finally` block (_docs/decisions.md, item 6), so a
retry would run against a file that no longer exists — it could not succeed, and
it would bury the real error under a second, more confusing one. A task that
genuinely wants to be re-attempted has to say so in its own body, where it can
also say what it is safe to re-attempt against.
"""

# Marker names become filenames, so they are kept to a boring alphabet.
_MARKER_NAME = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def enqueue_on_commit(task, /, *args, **kwargs):
    """Enqueue ``task`` once the current transaction commits.

    Use this, not ``task.enqueue(...)``, whenever the enqueue happens inside an
    ``atomic`` block. A worker is a separate process reading a separate
    connection: if the queue row is written by the same transaction as the rows
    the task is about to read, the worker can claim the job before that
    transaction commits and find nothing there — or find the job still queued
    after a rollback threw the rest of the work away.

    Outside a transaction ``on_commit`` runs its callback immediately, so this
    is always the safe call and never the wrong one.

    Returns None rather than a task result: the job does not exist yet, and its
    id cannot be known until the commit happens.
    """
    transaction.on_commit(lambda: task.enqueue(*args, **kwargs))


def ping_marker_path(name: str) -> Path:
    """Where :func:`ping` writes the marker called ``name``."""
    if not _MARKER_NAME.match(name):
        raise ValueError(f"marker name must match {_MARKER_NAME.pattern!r}, got {name!r}")
    return Path(settings.SCRATCH_DIR) / "ping" / f"{name}.txt"


@task()
def ping(name: str) -> str:
    """Write a marker file into the scratch directory, and return its path.

    The trivial job that proves the queue works end to end. The effect lands on
    the volume `web` and `worker` share, so a marker written by the worker is
    visible from the web container — which is the part a per-process queue
    cannot fake.
    """
    path = ping_marker_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{name} {timezone.now().isoformat()}\n")
    return str(path)


@task()
def always_fails(message: str) -> None:
    """Raise on purpose, to prove a failing job does not take the worker down.

    Kept in the application rather than the test suite so the same thing can be
    checked against a running Compose stack.
    """
    raise RuntimeError(message)
