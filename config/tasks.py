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

import logging
import re
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.tasks import task
from django.utils import timezone

logger = logging.getLogger(__name__)

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


@task()
def process_meeting_record(record_id: int) -> None:
    """Take an uploaded meeting through the pipeline. Enqueued by #19's upload.

    The work is `meetings.pipeline.process_meeting`: it transcribes the media,
    stores the transcript, and deletes the recording in a `finally` block
    whatever happened (`_docs/decisions.md`, item 6).

    It takes an id and re-fetches, per the conventions above. Time passes
    between the enqueue and the run, so the row may have moved on — a second
    worker may have claimed it, or the retrospective may be gone — and both of
    those are a return rather than an error.

    Nothing about this job is retried by the queue, per :data:`RETRY_POLICY`,
    and it is this job that is the reason: after it returns, the recording it
    would run against has been deleted. The second attempt it does get is
    arranged in its own body, around the API call, while the audio still
    exists — see `ai/transcription.py`.
    """
    # Imported here rather than at module scope: this module is imported for
    # its `enqueue_on_commit` helper by code that runs while the app registry
    # is still loading, and a model import at the top would be too early.
    from meetings.pipeline import process_meeting

    process_meeting(record_id)


@task()
def cluster_retrospective(retro_id: int) -> None:
    """Group a revealed retrospective's cards into suggested clusters. Enqueued by #9.

    The work is `retro.clustering.cluster_retrospective_cards`: it sends the
    cards' text to the model, writes the groups as auto-generated `Cluster` rows
    and moves the cards into them, and bumps the board version so open boards
    pick the suggestions up (#22).

    It takes an id and re-fetches, per the conventions above. Time passes
    between the enqueue and the run, so the retrospective may be gone, or may
    already carry suggestions, and either is a return rather than an error.

    Enqueued by the `-> REVEAL` transition on commit (`retro/services.py`), so
    it runs on the committed, frozen, anonymised cards — never inside the
    transition, where it would read cards a rollback might throw away. Nothing
    retries it: a clustering failure leaves the cards ungrouped and the team
    clusters by hand, which needs no second attempt.
    """
    # Imported here rather than at module scope, for the same reason
    # `process_meeting_record` imports its pipeline lazily: this module is
    # imported for `enqueue_on_commit` while the app registry is still loading.
    from retro.clustering import cluster_retrospective_cards

    cluster_retrospective_cards(retro_id)


@task()
def extract_meeting_outcomes(record_id: int) -> None:
    """Read a stored transcript into draft outcomes. Enqueued by #21's pipeline.

    The work is `meetings.extraction.extract_meeting_outcomes`: it builds the
    model's input from the transcript, the ranked agenda and the roster, writes
    the decisions and action items as `EXTRACTED`/`DRAFT` rows and the summary
    onto the retrospective, and finishes the record READY (or FAILED, keeping the
    transcript for a retry).

    It takes an id and re-fetches, per the conventions above. Time passes between
    the enqueue and the run, so the record may be gone or already moved on, and
    either is a return rather than an error.

    Enqueued by the pipeline's transcript store on commit
    (`meetings/pipeline.py`), so it runs on the committed, durable transcript —
    the recording is already deleted by then (`_docs/decisions.md` item 6), which
    is exactly why this one *is* retryable where transcription is not. Nothing
    retries it automatically; a facilitator re-runs it.
    """
    # Imported here rather than at module scope, for the same reason the two jobs
    # above import lazily: this module is imported for `enqueue_on_commit` while
    # the app registry is still loading.
    from meetings.extraction import extract_meeting_outcomes as run_extraction

    run_extraction(record_id)
