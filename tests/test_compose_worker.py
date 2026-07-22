"""The worker service in `compose.yaml`, read as text.

Every test here maps to an acceptance criterion of issue #55: a `db_worker`
whose main thread has died used to keep reporting as `Up`, because Django's
autoreloader kept the process alive around a dead worker thread, and it never
recovered once the cause was fixed.

What #55 is really about cannot be asserted in a unit test. Proving it needs a
running Compose stack: a container that stops saying `Up` when the worker dies,
and a restart policy that brings it back. CI has no Docker - the workflow runs
Postgres as a service container and never invokes `docker compose`, which
`tests/test_ci_workflow.py` asserts on purpose - so that half was proved by hand
against the real stack and written up on the issue.

What is left for the suite is the same job `tests/test_ci_workflow.py` does for
the workflow: read the file as text and assert that what has to be in it is in
it, and that what must not be there is gone. The two mechanisms are one line
each, and one line is exactly what a later edit deletes by accident.

The absences carry as much weight as the presences. A `compose.yaml` that grew
a `restart: always`, lost `--no-reload`, silenced the worker by turning `DEBUG`
off, or answered the same problem with a supervisor container would still be a
valid Compose file and would still bring the stack up. It would just have given
back what #55 bought.

The file is read as text rather than parsed: no YAML library is installed, the
suite already reads `AGENTS.md`, `package.json` and the CI workflow this way,
and the comments are part of what is being asserted - criterion three of #55 is
that the reasoning is written where someone reading the worker service will see
it, and a parser throws comments away.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
COMPOSE = BASE_DIR / "compose.yaml"

#: Every service `compose.yaml` is allowed to define. Postgres is the only
#: infrastructure (`AGENTS.md`), and #55 forbids answering it with a supervisor
#: process or a second broker, so the fix may not have added a fourth service.
ALLOWED_SERVICES = {"db", "web", "worker"}


def source() -> str:
    """`compose.yaml` as written, comments and all."""
    assert COMPOSE.is_file(), f"{COMPOSE} does not exist"
    return COMPOSE.read_text()


def service_block(name: str) -> str:
    """The `<name>:` service block, comments and all.

    Services are indented two spaces under `services:` and their keys four, so
    the block runs until the next line that is neither blank nor indented past
    the service name.
    """
    lines = source().splitlines()
    start = lines.index(f"  {name}:")
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def settings_of(block: str) -> str:
    """A service block with its comment lines removed.

    Comments explain the decision, and they quote what they argue against -
    "`restart: on-failure` and not `unless-stopped`", "a healthcheck was the
    alternative". An assertion about what Compose is actually told to do has to
    look only at the lines that Compose reads.
    """
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def comments_of(block: str) -> str:
    """Only the comment lines of a service block."""
    return "\n".join(line for line in block.splitlines() if line.lstrip().startswith("#"))


@pytest.fixture
def worker() -> str:
    return service_block("worker")


@pytest.fixture
def web() -> str:
    return service_block("web")


# --- The worker cannot lie about being alive -------------------------------


def test_the_worker_does_not_run_under_the_autoreloader(worker: str) -> None:
    """`--no-reload`, so a dead worker thread ends the process.

    `db_worker` defaults `--reload` to `DEBUG`, which Compose sets true. Under
    the reloader the worker runs in a daemon thread while the main thread
    watches files: the thread dies, the reloader keeps ticking, and the
    container goes on reporting `Up` with nothing draining the queue. Without
    it there is one process, and its death is the container's death.
    """
    run = settings_of(worker)

    assert "command: uv run manage.py db_worker --no-reload" in run, run
    # Not `--reload`, and not the other spelling of turning it off either: the
    # flag has to be on the command Compose runs, not merely mentioned.
    assert re.search(r"command:.*\s--reload(\s|$)", run) is None, run


def test_the_worker_is_brought_back_when_the_cause_is_fixed(worker: str) -> None:
    """`restart: on-failure`, so the crash loop heals itself.

    Exiting is only half of #55. The other half is that a worker killed by
    something that gets fixed - a missing migration, a database that was not up
    yet - is draining the queue again afterwards without anyone running
    `docker compose up`.
    """
    run = settings_of(worker)

    assert re.search(r"^    restart: on-failure$", run, re.MULTILINE), run
    # `always` and `unless-stopped` would also restart a crash, and would also
    # restart a worker that exited 0 because it was asked to stop - a SIGTERM
    # from `docker compose stop`, or `--batch` finishing the queue.
    assert "restart: always" not in run, run
    assert "restart: unless-stopped" not in run, run


def test_the_worker_still_runs_the_queue_the_way_it_did(worker: str) -> None:
    """The fix changed how failure is surfaced, and nothing else.

    Same image as web, same scratch volume, same database, same wait for
    Postgres to be healthy. A worker that no longer shares the scratch volume
    would pass every other test in this file and break the media pipeline.
    """
    run = settings_of(worker)

    assert "build: ." in run
    assert "uv run manage.py db_worker" in run
    assert "SCRATCH_DIR: /scratch" in run
    assert "- scratch:/scratch" in run
    assert "condition: service_healthy" in run


def test_the_worker_was_not_quietened_by_turning_debug_off(worker: str) -> None:
    """`DEBUG` stays true for the worker.

    Turning it off would also switch the reloader off - that is where the
    default comes from - and would have looked like a fix. It is not one. It
    silently changes how the worker handles errors and what it logs, for a
    reason that has nothing to do with either, and it leaves the next person to
    set `DEBUG=true` for an unrelated debugging session holding the original
    bug again.
    """
    assert 'DEBUG: "true"' in settings_of(worker)


# --- web is untouched ------------------------------------------------------


def test_web_still_reloads_on_code_changes(web: str) -> None:
    """Criterion four: nothing here costs `web` its reloader.

    `runserver` reloads unless it is told not to, so this is an assertion about
    an absence: no `--noreload`, and `DEBUG` still true.
    """
    run = settings_of(web)

    assert "command: uv run manage.py runserver 0.0.0.0:8000" in run, run
    assert "--noreload" not in run, run
    assert "--no-reload" not in run, run
    assert 'DEBUG: "true"' in run, run


def test_the_restart_policy_belongs_to_the_worker_alone() -> None:
    """Only the worker restarts.

    Restarting `web` would paper over a development server that crashed on an
    import error, which is a thing a developer wants to see and fix, not a
    thing to be hidden behind a container that comes back.
    """
    # Over the lines Compose reads: the worker's comment quotes the policy it
    # argues for, and a quotation is not a second policy.
    assert settings_of(source()).count("restart:") == 1, source()
    assert "restart:" not in settings_of(service_block("web"))
    assert "restart:" not in settings_of(service_block("db"))


# --- The reasoning is where the worker is read -----------------------------


def test_the_decision_is_written_next_to_the_service_it_is_about(worker: str) -> None:
    """Criterion three: someone reading the worker service reads why.

    Both mechanisms are one word each. Out of context either looks like it
    could go: `--no-reload` reads as a worker that someone forgot to let
    reload, and `restart: on-failure` reads as boilerplate. Together they are
    the fix for a specific, expensive bug, and the comment is what stops the
    next reader from undoing it.
    """
    why = comments_of(worker)

    assert "#55" in why, why
    # The bug, both halves of the answer, and the price paid for it.
    assert "--no-reload" in why
    assert "restart" in why
    assert re.search(r"autoreloader|reloader", why), why
    assert "healthcheck" in why, why


# --- No new infrastructure -------------------------------------------------


def test_compose_grew_no_new_service() -> None:
    """Postgres stays the only infrastructure.

    #55 rules out a supervisor process and a second broker by name, and both
    are the shape of thing that arrives as a fourth service in this file.
    """
    services = set(re.findall(r"^  ([a-z][a-z0-9_-]*):$", source(), re.MULTILINE))
    # `pgdata:` and `scratch:` sit at the same indent under `volumes:`.
    assert services - {"pgdata", "scratch"} == ALLOWED_SERVICES, services
