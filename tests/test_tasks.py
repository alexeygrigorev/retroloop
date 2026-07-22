"""The background task infrastructure.

Every test here maps to an acceptance criterion of issue #18. The recurring
theme is that the queue lives in Postgres: a job survives leaving the process
that enqueued it, one job is claimed by exactly one worker, and a job that was
enqueued inside a transaction that rolled back was never really enqueued at all.

The suite runs on the immediate backend (see `config/settings_test.py`), so most
tests prove the *task* works. The tests that need to prove the *queue* works
switch to the ORM backend through the `database_queue` fixture and drive the
real `db_worker` command, which is the same code path Compose runs.
"""

import threading
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections, transaction
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction

import config.settings as production_settings
from config.tasks import RETRY_POLICY, always_fails, enqueue_on_commit, ping, ping_marker_path

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default"],
    }
}

# Keeps the batch worker from sleeping or reloading during a test.
WORKER_ARGS = ("--batch", "--no-reload", "--no-startup-delay")


@pytest.fixture
def scratch(settings, tmp_path):
    """Point SCRATCH_DIR at a directory this test owns."""
    settings.SCRATCH_DIR = tmp_path
    return tmp_path


@pytest.fixture
def database_queue(settings):
    """Swap the immediate backend for the real Postgres-backed queue."""
    settings.TASKS = DATABASE_TASKS
    return task_backends["default"]


def run_worker() -> None:
    """Drain the queue with the same command Compose runs, then return."""
    call_command("db_worker", *WORKER_ARGS, verbosity=0)


def compose_worker_service() -> str:
    """The `worker:` block of compose.yaml, as text."""
    lines = (BASE_DIR / "compose.yaml").read_text().splitlines()
    start = lines.index("  worker:")
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


# --- Configuration ---------------------------------------------------------


def test_production_puts_the_queue_in_postgres() -> None:
    assert production_settings.TASKS["default"]["BACKEND"] == "django_tasks_db.DatabaseBackend"
    assert "django_tasks_db" in production_settings.INSTALLED_APPS


def test_the_suite_runs_tasks_immediately(settings) -> None:
    assert (
        settings.TASKS["default"]["BACKEND"] == "django.tasks.backends.immediate.ImmediateBackend"
    )


@pytest.mark.django_db
def test_the_queue_table_is_migrated() -> None:
    """The backend's migrations ran, so the queue is a table and not a process."""
    assert DBTaskResult.objects.count() == 0


def test_compose_worker_runs_the_real_worker_command() -> None:
    worker = compose_worker_service()

    assert "uv run manage.py db_worker" in worker
    # Same image as web, and the same scratch volume, so a file written by a
    # task is the same file the web container uploaded.
    assert "build: ." in worker
    assert "- scratch:/scratch" in worker
    assert "SCRATCH_DIR: /scratch" in worker


# --- It runs ---------------------------------------------------------------


def test_enqueue_then_execute(scratch) -> None:
    """The trivial task runs and its effect is observable."""
    marker = ping_marker_path("proof")
    assert not marker.exists()

    result = ping.enqueue("proof")

    assert result.status == TaskResultStatus.SUCCESSFUL
    assert marker.is_file()
    assert marker.read_text().startswith("proof ")
    assert result.return_value == str(marker)


@pytest.mark.django_db(transaction=True)
def test_the_queue_lives_in_the_database_not_in_the_process(database_queue, scratch) -> None:
    """Enqueue writes a row; a worker started later picks it up and runs it.

    This is the in-process equivalent of `docker compose up` and then enqueueing
    from the web container: nothing but the database connects the two halves.
    """
    result = ping.enqueue("through-postgres")

    row = DBTaskResult.objects.get()
    assert row.status == TaskResultStatus.READY
    assert row.task_path == "config.tasks.ping"
    assert row.args_kwargs == {"args": ["through-postgres"], "kwargs": {}}
    assert not ping_marker_path("through-postgres").exists()

    run_worker()

    row.refresh_from_db()
    assert row.status == TaskResultStatus.SUCCESSFUL
    assert ping_marker_path("through-postgres").is_file()
    assert database_queue.get_result(result.id).status == TaskResultStatus.SUCCESSFUL


@pytest.mark.django_db(transaction=True)
def test_two_workers_do_not_run_the_same_job(database_queue, scratch) -> None:
    """A claimed job is invisible to every other worker.

    The worker claims a job with `SELECT ... FOR UPDATE SKIP LOCKED`, so this
    holds the lock from one connection and asserts a second connection sees an
    empty queue rather than the same job. That is what makes
    `docker compose up --scale worker=2` run a queued task exactly once.
    """
    ping.enqueue("claimed-once")

    claimed = threading.Event()
    release = threading.Event()
    first_worker_saw = {}

    def hold_the_job() -> None:
        try:
            with exclusive_transaction(DBTaskResult.objects.db):
                first_worker_saw["job"] = DBTaskResult.objects.ready().get_locked()
                claimed.set()
                release.wait(timeout=10)
        finally:
            claimed.set()
            connections.close_all()

    first_worker = threading.Thread(target=hold_the_job)
    first_worker.start()
    try:
        assert claimed.wait(timeout=10)
        with exclusive_transaction(DBTaskResult.objects.db):
            assert DBTaskResult.objects.ready().get_locked() is None
    finally:
        release.set()
        first_worker.join(timeout=10)

    assert first_worker_saw["job"] is not None

    # The first worker let go without running it, so the job is still queued —
    # and running the queue down now executes it exactly once.
    run_worker()
    run_worker()

    row = DBTaskResult.objects.get()
    assert row.status == TaskResultStatus.SUCCESSFUL
    assert len(row.worker_ids) == 1


# --- Enqueueing safely -----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_enqueue_on_commit_waits_for_the_commit(database_queue, scratch) -> None:
    with transaction.atomic():
        assert enqueue_on_commit(ping, "after-commit") is None
        # A worker polling right now must not find a job for work that is not
        # visible to it yet.
        assert DBTaskResult.objects.count() == 0

    assert DBTaskResult.objects.count() == 1

    run_worker()

    assert ping_marker_path("after-commit").is_file()


@pytest.mark.django_db(transaction=True)
def test_a_task_enqueued_in_a_rolled_back_transaction_never_runs(database_queue, scratch) -> None:
    with pytest.raises(ValueError, match="the work failed"):
        with transaction.atomic():
            enqueue_on_commit(ping, "rolled-back")
            raise ValueError("the work failed")

    assert DBTaskResult.objects.count() == 0

    run_worker()

    assert not ping_marker_path("rolled-back").exists()


@pytest.mark.django_db(transaction=True)
def test_enqueue_on_commit_outside_a_transaction_enqueues_straight_away(
    database_queue, scratch
) -> None:
    """The helper is always the safe call, never the wrong one."""
    enqueue_on_commit(ping, "no-transaction")

    assert DBTaskResult.objects.count() == 1


@pytest.mark.django_db
def test_on_commit_work_is_driven_by_capturing_the_callbacks(
    scratch, django_capture_on_commit_callbacks
) -> None:
    """How an ordinary `django_db` test asserts on work enqueued after commit.

    An ordinary test never commits — pytest-django rolls it back — so an
    `on_commit` callback would never fire and the task would silently not run.
    This fixture is the way to test one without paying for `transaction=True`.
    """
    with django_capture_on_commit_callbacks(execute=True):
        enqueue_on_commit(ping, "captured")

    assert ping_marker_path("captured").is_file()


@pytest.mark.django_db(transaction=True)
def test_a_model_instance_cannot_be_enqueued(database_queue, django_user_model) -> None:
    """Arguments are primitives. A model has to be re-fetched by id in the body."""
    user = django_user_model.objects.create_user(username="rita", password="x")

    with pytest.raises(TypeError):
        ping.enqueue(user)

    assert DBTaskResult.objects.count() == 0


# --- Failure ---------------------------------------------------------------


def test_a_failing_task_is_recorded_with_a_readable_message() -> None:
    result = always_fails.enqueue("the recording was already deleted")

    assert result.status == TaskResultStatus.FAILED
    assert result.errors[0].exception_class_path == "builtins.RuntimeError"
    assert "the recording was already deleted" in result.errors[0].traceback


@pytest.mark.django_db(transaction=True)
def test_a_failing_task_does_not_stop_the_worker(database_queue, scratch) -> None:
    always_fails.enqueue("the recording was already deleted")
    ping.enqueue("queued-behind-a-failure")

    run_worker()

    failed = DBTaskResult.objects.failed().get()
    assert failed.exception_class_path == "builtins.RuntimeError"
    assert "the recording was already deleted" in failed.traceback

    # The worker came back for the next job instead of dying on the first one.
    assert DBTaskResult.objects.successful().count() == 1
    assert ping_marker_path("queued-behind-a-failure").is_file()


@pytest.mark.django_db(transaction=True)
def test_a_failed_task_is_never_retried(database_queue, scratch) -> None:
    """Retries are off on purpose — see RETRY_POLICY and decisions.md item 6."""
    always_fails.enqueue("no second attempt")

    run_worker()
    run_worker()

    failed = DBTaskResult.objects.get()
    assert failed.status == TaskResultStatus.FAILED
    assert len(failed.worker_ids) == 1

    # The policy is written down next to the tasks, not left implicit.
    assert "No task is retried automatically" in RETRY_POLICY
