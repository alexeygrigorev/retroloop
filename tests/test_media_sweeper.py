"""The media left behind when the `finally` never ran, and what collects it.

Every test here maps to an acceptance criterion of issue #71. Four themes run
through the file.

The first is that the dangerous half of a sweeper is not what it deletes but
what it must not. A recording of a private meeting being transcribed right now
is exactly what this code walks past, so the tests that matter most assert
*absence of deletion*: a file still there, a row still `TRANSCRIBING`, a report
that says why it was kept. Every one of them proves the refusal by running the
real sweep against the real file, never by reading a flag.

The second is that the kill is real. `test_a_live_transcription_survives_a_sweep`
starts another operating-system process, lets it get as far as the chunks, runs
a sweep beside it, then sends it signal 9 and sweeps again. Nothing about it is
mocked, because the defect is precisely that a `SIGKILL` runs no Python: a fake
one would run the very `finally` whose absence is the bug. It also needs the two
sessions that make an advisory lock mean anything — see `tests/killable_worker.py`.

The third is absence again, in the ordinary tests: a swept volume is asserted
empty by walking it, not by checking one path, so a chunk left in a work
directory fails the test that says the chunks are gone.

The fourth is that the two layers are tested separately. A record cut short
in-process reaches `FAILED` through the pipeline's own `finally`; a record whose
worker is gone reaches it through the sweeper. Each is forced on its own, so
neither can pass by standing in for the other.
"""

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection
from django.test import Client
from django.urls import reverse

from cycles.models import FeedbackCycle
from meetings import pipeline
from meetings.locks import MEDIA_LOCK_NAMESPACE, media_lock, media_lock_key
from meetings.models import MeetingRecord, Transcript
from meetings.pipeline import ABANDONED, CHUNK_SUBDIR
from meetings.sweeper import DEFAULT_MINIMUM_AGE, sweep
from projects.models import Membership, Project
from retro.models import Retrospective

User = get_user_model()

BASE_DIR = Path(django_settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Status = MeetingRecord.Status

#: No waiting: these tests own their scratch directory, so nothing in it is an
#: upload that some other transaction is still streaming.
NOW = timedelta(0)

#: How long a test will wait for another process to reach a point, or for
#: Postgres to notice that one has died. Generous, because a slow machine
#: failing this file should be a test that fails on its assertion rather than
#: one that fails on the clock.
PATIENCE = 30.0


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def scratch(settings, tmp_path) -> Path:
    """Point SCRATCH_DIR at a directory this test owns, as the containers share one."""
    settings.SCRATCH_DIR = tmp_path
    return tmp_path


@pytest.fixture
def facilitator(db) -> User:
    return User.objects.create_user(
        username="facilitator", password=PASSWORD, display_name="Fay Facilitator"
    )


@pytest.fixture
def retro(facilitator: User) -> Retrospective:
    project = Project.objects.create(name="Platform", owner=facilitator)
    Membership.objects.create(project=project, user=facilitator, role=Membership.Role.FACILITATOR)
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )
    return Retrospective.objects.create(cycle=cycle, stage=Retrospective.Stage.DISCUSS)


def make_record(retro: Retrospective, user: User, scratch: Path, **kwargs) -> MeetingRecord:
    """A record whose `temp_path` really is a file on the shared volume."""
    uploads = scratch / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    media = uploads / kwargs.pop("name", "0123456789abcdef")
    media.write_bytes(b"pretend this is a recording")
    return MeetingRecord.objects.create(
        retrospective=retro,
        uploaded_by=user,
        kind=MeetingRecord.Kind.AUDIO,
        temp_path=str(media),
        original_filename="standup.mp3",
        size_bytes=27,
        status=kwargs.pop("status", Status.UPLOADED),
        **kwargs,
    )


@pytest.fixture
def record(retro: Retrospective, facilitator: User, scratch: Path) -> MeetingRecord:
    return make_record(retro, facilitator, scratch)


@pytest.fixture
def chunked(monkeypatch):
    """Stand in for #20's `prepare_audio_chunks`, without needing ffmpeg.

    It writes real files into the work directory the pipeline made, so what is
    left behind when a run is cut short is real too.
    """

    def use(count: int = 2) -> list[Path]:
        produced: list[Path] = []

        def fake_prepare(source, *, work_root=None, **kwargs):
            root = Path(work_root)
            root.mkdir(parents=True, exist_ok=True)
            for index in range(count):
                part = root / f"chunk-{index:05d}.opus"
                part.write_bytes(Path(source).read_bytes())
                produced.append(part)
            return list(produced)

        monkeypatch.setattr(pipeline, "prepare_audio_chunks", fake_prepare)
        return produced

    return use


class Refusal:
    """A filesystem that refuses to unlink, until a test says it may again.

    The refusal has to be switchable inside one test: the point of #71's third
    acceptance criterion is that the sweeper comes back for a file the pipeline
    could not delete, and proving that needs one run that is refused and one
    deletion that is not.
    """

    def __init__(self, monkeypatch) -> None:
        self.on = True
        real = Path.unlink

        def maybe_unlink(target: Path, *args, **kwargs):
            if self.on:
                raise OSError(30, "Read-only file system")
            return real(target, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", maybe_unlink)


@pytest.fixture
def refused_unlink(monkeypatch) -> Refusal:
    return Refusal(monkeypatch)


def files_left(root: Path) -> list[Path]:
    """Everything still on the shared volume, however it got there."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def work_dirs(root: Path) -> list[Path]:
    chunks = root / CHUNK_SUBDIR
    return sorted(chunks.iterdir()) if chunks.is_dir() else []


def make_old(path: Path) -> Path:
    """Backdate a path well past any minimum age a test uses."""
    old = time.time() - DEFAULT_MINIMUM_AGE.total_seconds() * 10
    os.utime(path, (old, old))
    return path


def status_fragment(record: MeetingRecord, user: User) -> str:
    """What #19's polled fragment says about this record, fetched as its owner."""
    client = Client()
    client.login(username=user.username, password=PASSWORD)
    response = client.get(reverse("meeting-record-status", args=[record.pk]))
    assert response.status_code == 200
    return response.content.decode()


# --------------------------------------------------------------------------
# The lock the whole design rests on
# --------------------------------------------------------------------------


def test_a_media_lock_key_is_namespaced_by_the_record_it_stands_for() -> None:
    """Two records never share a key, and no other advisory lock shares the space."""
    assert media_lock_key(1) != media_lock_key(2)
    assert media_lock_key(7) >> 32 == MEDIA_LOCK_NAMESPACE
    # A raw record id would collide with every other advisory lock on a small
    # number, which is the mistake the namespace exists to prevent.
    assert media_lock_key(7) != 7


def test_a_media_lock_key_fits_the_argument_postgres_takes() -> None:
    """`pg_try_advisory_lock` takes a signed 64-bit integer, and always gets one."""
    for record_id in (1, 2**31 - 1, 2**31, 2**40):
        assert -(2**63) <= media_lock_key(record_id) < 2**63


@pytest.mark.django_db
def test_the_lock_is_taken_and_released_by_postgres_itself(record: MeetingRecord) -> None:
    """Held inside the block, gone after it, asked of the database both times."""
    with media_lock(record.pk):
        assert _locks_held(record.pk) == 1
    assert _locks_held(record.pk) == 0


def _locks_held(record_id: int) -> int:
    """How many sessions hold this record's media lock, according to Postgres."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND "
            "((classid::bigint << 32) | objid::bigint) = %s",
            [media_lock_key(record_id)],
        )
        return cursor.fetchone()[0]


# --------------------------------------------------------------------------
# A file nobody claims
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_upload_no_record_names_is_deleted(scratch: Path) -> None:
    orphan = make_old(_orphan_upload(scratch))

    report = sweep(minimum_age=NOW)

    assert not orphan.exists()
    assert report.removed == [orphan]
    assert files_left(scratch) == []


@pytest.mark.django_db
def test_an_upload_still_being_written_is_not_deleted_under_its_uploader(scratch: Path) -> None:
    """The one window a lock cannot close, and the only thing age is used for.

    `store_meeting_record` streams the bytes inside the transaction that inserts
    the row, so for as long as that transaction is open there is a real file on
    the volume that no committed row mentions. A sweep at that instant must walk
    past it.
    """
    in_flight = _orphan_upload(scratch)

    report = sweep()

    assert in_flight.exists()
    assert report.removed == []
    assert report.kept_young == [in_flight]


@pytest.mark.django_db
def test_the_sweeper_leaves_the_rest_of_the_scratch_volume_alone(scratch: Path) -> None:
    """It owns two directories. The queue's own marker files are not its business."""
    marker = scratch / "ping" / "worker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("queued\n")
    make_old(marker)

    sweep(minimum_age=NOW)

    assert marker.exists()


def _orphan_upload(scratch: Path, name: str = "orphaned") -> Path:
    uploads = scratch / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / name
    path.write_bytes(b"a recording nobody has a row for")
    return path


# --------------------------------------------------------------------------
# A file its own record still expects
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_queued_record_keeps_the_file_its_job_has_not_read_yet(
    record: MeetingRecord, scratch: Path
) -> None:
    """`UPLOADED` means a worker has not started. Deleting it would break the run."""
    media = Path(record.temp_path)
    make_old(media)

    report = sweep(minimum_age=NOW)

    assert media.exists()
    assert report.removed == []
    assert report.kept_owned == [media]
    record.refresh_from_db()
    assert record.temp_path == str(media)


@pytest.mark.django_db
def test_a_claim_that_fails_leaves_a_record_that_can_still_be_run(
    record: MeetingRecord, scratch: Path, chunked, monkeypatch
) -> None:
    """#71's third path: the database error before the `try`, and what follows it.

    `_claim` runs before the block that would delete the media, so a failure
    there leaves the file on disk with the record still `UPLOADED`. That is the
    one leftover this issue does not treat as a leak — nothing was consumed, and
    the job can be enqueued again — so the test proves both halves: the sweeper
    walks past it, and running the job again really does transcribe it.
    """
    chunked(count=1)
    media = Path(record.temp_path)
    # Switched off rather than undone, because undoing every patch would take
    # the stand-in for #20's chunking with it and the second run would need
    # ffmpeg and a real recording.
    refusing = {"saves": True}
    real_save = MeetingRecord.save

    def maybe_save(self, *args, **kwargs):
        if refusing["saves"]:
            raise DatabaseError("the connection went away while claiming the record")
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(MeetingRecord, "save", maybe_save)
    with pytest.raises(DatabaseError):
        pipeline.run(record)

    record.refresh_from_db()
    assert record.status == Status.UPLOADED
    assert media.exists()

    report = sweep(minimum_age=NOW)
    assert report.kept_owned == [media]
    assert media.exists()

    refusing["saves"] = False
    pipeline.process_meeting(record.pk)

    record.refresh_from_db()
    assert record.status == Status.EXTRACTING
    assert Transcript.objects.filter(record=record).exists()
    assert not media.exists()
    assert files_left(scratch) == []


# --------------------------------------------------------------------------
# The unlink the filesystem refused
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_refused_unlink_leaves_the_row_saying_the_recording_is_still_there(
    record: MeetingRecord, scratch: Path, chunked, refused_unlink: Refusal
) -> None:
    """#71's second path. The row is not allowed to imply the media is gone."""
    chunked(count=1)
    media = Path(record.temp_path)

    pipeline.run(record)

    record.refresh_from_db()
    assert media.exists()
    assert record.temp_path == str(media)
    assert record.media_deleted_at is None
    assert record.media_is_retained
    # The transcript is the durable half and is not thrown away over this: the
    # meeting is the thing that cannot be recovered later.
    assert Transcript.objects.filter(record=record).exists()


@pytest.mark.django_db
def test_the_page_says_the_recording_is_still_there(
    record: MeetingRecord, facilitator: User, scratch: Path, chunked, refused_unlink: Refusal
) -> None:
    """Visible, not only logged: the fragment #19 polls carries the retention."""
    chunked(count=1)

    pipeline.run(record)
    record.refresh_from_db()

    assert "could not be deleted from the shared volume" in status_fragment(record, facilitator)


@pytest.mark.django_db
def test_a_record_whose_media_really_went_says_nothing_about_a_retention(
    record: MeetingRecord, facilitator: User, scratch: Path, chunked
) -> None:
    """The absence half: the ordinary path must not carry the warning."""
    chunked(count=1)

    pipeline.run(record)
    record.refresh_from_db()

    assert not record.media_is_retained
    assert "could not be deleted from the shared volume" not in status_fragment(record, facilitator)


@pytest.mark.django_db
def test_the_sweeper_comes_back_for_a_recording_the_unlink_refused(
    record: MeetingRecord, scratch: Path, chunked, refused_unlink: Refusal
) -> None:
    """The whole point of the third criterion: something comes back for it."""
    chunked(count=1)
    media = Path(record.temp_path)
    pipeline.run(record)
    assert media.exists()

    refused_unlink.on = False
    report = sweep(minimum_age=NOW)

    assert not media.exists()
    assert report.removed == [media]
    record.refresh_from_db()
    assert record.temp_path is None
    assert record.media_deleted_at is not None
    assert not record.media_is_retained
    assert files_left(scratch) == []


@pytest.mark.django_db
def test_a_refusal_the_sweeper_also_hits_is_reported_rather_than_hidden(
    record: MeetingRecord, scratch: Path, chunked, refused_unlink: Refusal
) -> None:
    """A volume that is still read-only: the row stays honest and the sweep says so."""
    chunked(count=1)
    media = Path(record.temp_path)
    pipeline.run(record)

    report = sweep(minimum_age=NOW)

    assert report.refused == [media]
    assert media.exists()
    record.refresh_from_db()
    assert record.temp_path == str(media)
    assert record.media_deleted_at is None


# --------------------------------------------------------------------------
# A record cut short: the pipeline's own layer
# --------------------------------------------------------------------------


class Interrupted:
    """A client that raises a `BaseException` the pipeline cannot catch."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def transcribe(self, path: Path):
        raise self.error


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit(1)], ids=["keyboard-interrupt", "system-exit"]
)
def test_a_base_exception_leaves_a_failed_record_rather_than_one_transcribing(
    record: MeetingRecord, scratch: Path, chunked, error: BaseException
) -> None:
    """#71's status defect. The media already went; the status has to follow it."""
    chunked(count=2)

    with pytest.raises(type(error)):
        pipeline.run(record, client=Interrupted(error))

    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert record.is_final
    assert ABANDONED in record.error_message
    assert "upload the file once more" in record.error_message
    assert files_left(scratch) == []


@pytest.mark.django_db
def test_the_page_stops_polling_a_record_a_base_exception_cut_short(
    record: MeetingRecord, facilitator: User, scratch: Path, chunked
) -> None:
    """The defect stated as #19 sees it: the fragment must stop asking again."""
    chunked(count=1)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(record, client=Interrupted(KeyboardInterrupt()))
    record.refresh_from_db()

    fragment = status_fragment(record, facilitator)

    assert 'data-polling="false"' in fragment
    assert "hx-get" not in fragment
    assert "hx-trigger" not in fragment


@pytest.mark.django_db
def test_a_record_that_finished_is_not_touched_by_the_abandonment_rule(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """The rule only fires on a run that reached no outcome of its own."""
    chunked(count=1)

    pipeline.run(record)

    record.refresh_from_db()
    assert record.status == Status.EXTRACTING
    assert record.error_message == ""


# --------------------------------------------------------------------------
# A record cut short: the sweeper's layer
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_sweeper_fails_a_record_whose_worker_is_gone(
    record: MeetingRecord, scratch: Path
) -> None:
    """Nothing else can move a `TRANSCRIBING` record whose process no longer exists."""
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)

    report = sweep(minimum_age=NOW)

    record.refresh_from_db()
    assert report.abandoned == [record.pk]
    assert record.status == Status.FAILED
    assert ABANDONED in record.error_message


@pytest.mark.django_db
def test_the_media_of_an_abandoned_record_goes_in_the_same_sweep(
    record: MeetingRecord, scratch: Path
) -> None:
    """One pass, not two: the record is failed first so its media is then collectable."""
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)
    media = Path(record.temp_path)
    chunks = scratch / CHUNK_SUBDIR / f"record-{record.pk}-abcd"
    chunks.mkdir(parents=True)
    (chunks / "chunk-00000.opus").write_bytes(b"half a meeting")

    sweep(minimum_age=NOW)

    assert not media.exists()
    assert not chunks.exists()
    assert files_left(scratch) == []
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert record.temp_path is None
    assert record.media_deleted_at is not None


@pytest.mark.django_db
def test_a_record_already_failed_and_already_swept_is_left_alone(
    record: MeetingRecord, scratch: Path
) -> None:
    """Idempotence, which is what makes it safe to run this from cron."""
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)
    sweep(minimum_age=NOW)
    record.refresh_from_db()
    first_message = record.error_message
    deleted_at = record.media_deleted_at

    second = sweep(minimum_age=NOW)

    record.refresh_from_db()
    assert second.removed == []
    assert second.abandoned == []
    assert second.refused == []
    assert record.error_message == first_message
    assert record.media_deleted_at == deleted_at


# --------------------------------------------------------------------------
# The chunks
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_chunks_left_by_a_run_that_is_over_are_deleted(
    record: MeetingRecord, scratch: Path
) -> None:
    stale = scratch / CHUNK_SUBDIR / f"record-{record.pk}-xyz"
    stale.mkdir(parents=True)
    (stale / "chunk-00000.opus").write_bytes(b"a minute of a private meeting")
    (stale / "chunk-00001.opus").write_bytes(b"another minute")

    sweep(minimum_age=NOW)

    assert not stale.exists()
    assert work_dirs(scratch) == []


@pytest.mark.django_db
def test_chunks_whose_record_no_longer_exists_are_deleted(scratch: Path) -> None:
    """The row can be gone entirely — its retrospective was deleted, say."""
    stale = scratch / CHUNK_SUBDIR / "record-424242-gone"
    stale.mkdir(parents=True)
    (stale / "chunk-00000.opus").write_bytes(b"a meeting whose record is gone")

    sweep(minimum_age=NOW)

    assert not stale.exists()


@pytest.mark.django_db
def test_something_in_the_chunk_directory_that_is_not_ours_waits_for_its_age(
    scratch: Path,
) -> None:
    """No record to ask, so age decides — the same rule as an unclaimed upload."""
    foreign = scratch / CHUNK_SUBDIR / "notes.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not written by this pipeline\n")

    assert sweep().kept_young == [foreign]
    assert foreign.exists()

    make_old(foreign)
    assert sweep().removed == [foreign]
    assert not foreign.exists()


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_command_sweeps_and_says_what_it_did(record: MeetingRecord, scratch: Path, capsys):
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)
    media = Path(record.temp_path)

    call_command("sweep_media", "--min-age", "0")

    output = capsys.readouterr().out
    assert not media.exists()
    assert str(media) in output
    assert f"record {record.pk}" in output
    record.refresh_from_db()
    assert record.status == Status.FAILED


@pytest.mark.django_db
def test_the_command_refuses_an_age_that_makes_no_sense(scratch: Path) -> None:
    with pytest.raises(CommandError):
        call_command("sweep_media", "--min-age", "-1")


def test_the_command_defaults_to_the_documented_minimum_age() -> None:
    """The default is the constant, so there is one number and not two."""
    assert DEFAULT_MINIMUM_AGE == timedelta(hours=1)


# --------------------------------------------------------------------------
# The real kill: two processes, one Postgres, and signal 9
# --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_live_transcription_survives_a_sweep_and_its_remains_do_not(
    record: MeetingRecord, scratch: Path, tmp_path_factory
) -> None:
    """#71's second and sixth criteria, in the only way either can honestly be shown.

    A second process runs the real pipeline on this record and stops inside the
    transcription client, holding the media lock, with the upload and three
    chunks on the volume. Then:

    * a sweep runs beside it and is asserted to have deleted nothing and moved
      nothing — this is the sweeper racing a live transcription, and losing on
      purpose;
    * the process is killed with `SIGKILL`, which runs no `finally`, no
      `atexit`, and no signal handler. Everything it was holding is still on
      disk and the row still says `TRANSCRIBING`;
    * Postgres drops the dead session's advisory lock by itself, which is the
      signal the next sweep reads;
    * a second sweep leaves no upload and no chunk behind, and the record it
      stranded reaches a terminal state instead of polling for ever.
    """
    media = Path(record.temp_path)
    # Outside the scratch volume: everything inside it is media this test is
    # about to assert the absence of.
    ready = tmp_path_factory.mktemp("worker-signals") / "worker-has-started"
    worker = _start_killable_worker(record.pk, ready, scratch)

    try:
        assert _wait_for(ready.exists), "the worker never reached the transcription client"
        record.refresh_from_db()
        assert record.status == Status.TRANSCRIBING
        chunks = work_dirs(scratch)
        assert len(chunks) == 1, chunks
        assert len(files_left(chunks[0])) == 3

        # The race, run on purpose: a whole sweep while the worker holds it all.
        beside_it = sweep(minimum_age=NOW)

        assert beside_it.removed == []
        assert beside_it.abandoned == []
        assert media.exists()
        assert len(files_left(scratch)) == 4
        assert sorted(beside_it.kept_live) == sorted([media, chunks[0]])
        record.refresh_from_db()
        assert record.status == Status.TRANSCRIBING

        os.kill(worker.pid, signal.SIGKILL)
        assert worker.wait(timeout=PATIENCE) == -signal.SIGKILL
    finally:
        # Whatever the assertions above did, this process does not outlive the
        # test: a run left holding the lock would fail every later sweep.
        worker.kill()

    # Nothing ran on the way out, so everything is exactly where it was.
    assert media.exists()
    assert len(files_left(scratch)) == 4
    record.refresh_from_db()
    assert record.status == Status.TRANSCRIBING

    assert _wait_for(lambda: _lock_is_free(record.pk)), "the dead session kept its lock"
    after = sweep(minimum_age=NOW)

    assert files_left(scratch) == []
    assert work_dirs(scratch) == []
    assert not media.exists()
    assert after.abandoned == [record.pk]
    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert record.is_final
    assert record.temp_path is None
    assert record.media_deleted_at is not None
    assert ABANDONED in record.error_message


def _start_killable_worker(record_id: int, ready: Path, scratch: Path) -> subprocess.Popen:
    """Another process, on another connection, running the real pipeline."""
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings_test",
        "SCRATCH_DIR": str(scratch),
        # pytest-django built this database for this run; the child has to reach
        # the very same one, or it will not find the row at all.
        "SWEEPER_TEST_DATABASE": connection.settings_dict["NAME"],
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.killable_worker", str(record_id), str(ready)],
        cwd=BASE_DIR,
        env=environment,
    )


def _lock_is_free(record_id: int) -> bool:
    """Whether this record's media lock can be taken right now."""
    with media_lock(record_id) as held:
        return held


def _wait_for(condition, timeout: float = PATIENCE) -> bool:
    """Poll `condition` until it holds. Another process is doing the work."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False
