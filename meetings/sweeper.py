"""Collecting the media the pipeline could not, without ever racing one that can.

`meetings/pipeline.py` deletes its recording in a `finally` block, and #21's
tests show that block surviving a `KeyboardInterrupt`, a `SystemExit`, an
exception mid-stitch and a `DatabaseError` on the way out. What no `finally` can
survive is having no process left to run it, and what it cannot do at all is
delete a file the filesystem refuses to unlink. This module is what comes back
for those (#71):

1. a worker killed between chunks — `SIGKILL` runs nothing, so the upload and
   every extracted chunk stay on the scratch volume;
2. an unlink the filesystem refused — the row says the file is still there, and
   until now nothing ever looked at it again;
3. a file whose row never committed, or whose row was rolled back after the
   bytes were written.

A leftover file here is not disk usage. It is a recording of a private meeting
that `_docs/decisions.md` item 6 says should not exist, which is why the sweeper
deletes rather than reports, and why it is careful about exactly one thing.

The one thing it is careful about
---------------------------------

A sweeper that deletes the recording out from under a live transcription is
worse than the leak it fixes. Two workers and a sweep run against one Postgres,
so "is anybody still working on this?" cannot be answered by reading a status:
`TRANSCRIBING` is what a live worker leaves in the row and it is also what a
killed one leaves behind.

So ownership is decided in one of two ways, and never by anything else:

* **A file whose owner can be named** — an upload some `MeetingRecord.temp_path`
  points at, a work directory named after a record id — is decided by that
  record's media lock (`meetings/locks.py`). The lock is a Postgres session
  advisory lock the pipeline holds for the whole of its run and that Postgres
  releases when the process dies. If the sweep cannot take it, a live worker has
  it and the file is left exactly where it is. If it can take it, no process is
  working on that record, and it goes on to ask the row what should happen.
  Nothing is deleted while the lock is not held.
* **A file whose owner cannot be named** — no row points at it, or it is not
  something the pipeline would have written — is decided by age. This is the one
  window a lock cannot close: `meetings/services.py` streams the upload to disk
  *inside* the transaction that inserts the row, so between the first byte and
  the commit there is a real file that no committed row mentions and no worker
  has claimed. A file younger than :data:`DEFAULT_MINIMUM_AGE` is therefore left
  alone. An upload is capped at 500 MB and the commit follows the last byte
  immediately, so an hour is not a guess about how long transcription takes —
  it is several orders of magnitude more than the window it covers.

Everything follows from that. Each record is handled in a transaction of its own
with the row locked, so a sweep that dies half way through has still finished
whatever it had started, and the next one carries on. It is safe to run
repeatedly: a second sweep over a clean volume does nothing.

Lock order, and why this cannot deadlock
----------------------------------------

`meetings/locks.py` states it: the media lock first, then the row. This module
takes nothing else — no cycle, no retrospective, no card — so it cannot appear
in a cycle with #10's retrospective < cycle < card order. And it only ever
*tries* for the media lock, so it never waits for anything at all: the worst a
mistake here can cost is a file collected by the next sweep instead of this one.

Running it
----------

`manage.py sweep_media`, from a cron entry, a Compose one-shot, or by hand. It
is a command rather than a scheduled task because the queue has no scheduler and
adding one would be a second piece of infrastructure; a command needs nothing
that is not already there.
"""

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from meetings.locks import media_lock
from meetings.models import MeetingRecord
from meetings.pipeline import ABANDONED, CHUNK_SUBDIR, failure_message
from meetings.uploads import upload_root

logger = logging.getLogger(__name__)

#: How old a file has to be before the sweeper will judge it with nothing but
#: its age — that is, when no record claims it. It covers the gap between the
#: first byte of an upload being written and the transaction that inserts its
#: row committing, which is milliseconds after the last byte of a file capped at
#: 500 MB. Nothing about a transcription's own duration comes into it: a file a
#: record does own is decided by that record's lock, however long the run takes.
DEFAULT_MINIMUM_AGE: Final[timedelta] = timedelta(hours=1)

#: What `meetings.pipeline._work_dir` names the directory it cuts chunks into.
#: The id in it is how a stray directory is traced back to its record.
WORK_DIR_OWNER: Final[re.Pattern[str]] = re.compile(r"\Arecord-(?P<pk>\d+)-")


@dataclass
class SweepReport:
    """What one sweep did, and — as importantly — what it left and why.

    The kept lists are not bookkeeping. "I did not delete this, because a worker
    holds it" is the sweeper's most important output: it is what a test asserts
    when it runs a sweep against a live transcription, and what an operator
    reads when a file they expected to go is still there.
    """

    removed: list[Path] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)
    kept_live: list[Path] = field(default_factory=list)
    kept_owned: list[Path] = field(default_factory=list)
    kept_young: list[Path] = field(default_factory=list)
    refused: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.removed)} file(s) or directory removed, "
            f"{len(self.abandoned)} record(s) marked failed, "
            f"{len(self.kept_live)} left to a live worker, "
            f"{len(self.kept_owned)} still owned, "
            f"{len(self.kept_young)} too new to judge, "
            f"{len(self.refused)} refused by the filesystem"
        )


def sweep(*, minimum_age: timedelta = DEFAULT_MINIMUM_AGE) -> SweepReport:
    """Collect everything on the scratch volume that no live run can still want.

    Safe to run at any time, including while transcriptions are in progress, and
    safe to run again immediately afterwards.

    The order is deliberate. Records stranded by a dead worker are marked failed
    first, so the same sweep then sees their media as media no run will ever
    come back for and collects it, instead of leaving it for an hour or a day.
    """
    report = SweepReport()
    _abandon_stranded_records(report)
    _sweep_uploads(report, minimum_age)
    _sweep_work_dirs(report, minimum_age)
    logger.info("media sweep: %s", report.summary())
    return report


# --------------------------------------------------------------------------
# The three passes
# --------------------------------------------------------------------------


def _abandon_stranded_records(report: SweepReport) -> None:
    """Fail every record whose transcribing worker is no longer there.

    A record is `TRANSCRIBING` because a worker wrote that and then took its
    time. If that worker's media lock is free, the process that wrote it is gone
    — Postgres releases a session lock only when the session ends — and nothing
    will ever move this record again. It stays `TRANSCRIBING` for ever, and #19's
    page polls it for ever, three seconds at a time.

    The one case it is worth being honest about: a worker that is alive but has
    lost its connection to Postgres also loses its lock, and would be marked
    failed here. It could not have written its own outcome either, so the record
    was going to be wrong whatever happened; this way it is wrong in the
    direction that stops polling and tells the facilitator to upload the file
    again.
    """
    stranded = MeetingRecord.objects.filter(status=MeetingRecord.Status.TRANSCRIBING)
    for record_id in list(stranded.values_list("pk", flat=True)):
        with media_lock(record_id) as held:
            if not held:
                # A worker is transcribing it right now. Nothing to see.
                continue
            with transaction.atomic():
                record = MeetingRecord.objects.select_for_update().filter(pk=record_id).first()
                if record is None or record.status != MeetingRecord.Status.TRANSCRIBING:
                    continue
                record.status = MeetingRecord.Status.FAILED
                record.error_message = failure_message(ABANDONED)
                record.save(update_fields=["status", "error_message"])
        report.abandoned.append(record_id)
        logger.warning(
            "meeting record %s was left transcribing by a worker that is gone; marked failed",
            record_id,
        )


def _sweep_uploads(report: SweepReport, minimum_age: timedelta) -> None:
    """Delete every uploaded file that no record can still use."""
    root = upload_root()
    if not root.is_dir():
        return

    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        record_id = (
            MeetingRecord.objects.filter(temp_path=str(path)).values_list("pk", flat=True).first()
        )
        if record_id is None:
            _collect_unclaimed(path, report, minimum_age)
        else:
            _collect_from_record(path, record_id, report)


def _sweep_work_dirs(report: SweepReport, minimum_age: timedelta) -> None:
    """Delete every chunk directory whose run is over.

    A work directory is made by a run that already holds the record's media lock
    and is deleted by that same run, so — unlike an upload — it never exists
    without an owner. If the lock is free, the run that made it is over: either
    it cleaned up and this is not its directory, or it did not get the chance.
    Both mean the chunks are cut from a recording nobody is transcribing.
    """
    root = Path(settings.SCRATCH_DIR) / CHUNK_SUBDIR
    if not root.is_dir():
        return

    for path in sorted(root.iterdir()):
        owner = WORK_DIR_OWNER.match(path.name)
        if owner is None:
            # Not something this pipeline writes, so there is no record to ask
            # about it and age is all there is to go on.
            _collect_unclaimed(path, report, minimum_age)
            continue
        with media_lock(int(owner["pk"])) as held:
            if not held:
                report.kept_live.append(path)
                continue
            _remove(path, report)


# --------------------------------------------------------------------------
# Deciding one path
# --------------------------------------------------------------------------


def _collect_unclaimed(path: Path, report: SweepReport, minimum_age: timedelta) -> None:
    """Delete a path no record names, once it is old enough to be sure.

    The uncertainty is real and it is short: `meetings/services.py` writes the
    bytes inside the transaction that inserts the row, so an upload in flight is
    a file no committed row mentions. Waiting is the only way to tell it apart
    from one whose row was rolled back, and the wait is generous because the
    cost of being wrong is a facilitator's upload deleted from under them.
    """
    if _younger_than(path, minimum_age):
        report.kept_young.append(path)
        return
    _remove(path, report)


def _collect_from_record(path: Path, record_id: int, report: SweepReport) -> None:
    """Delete an upload its own record has finished with, and say so on the row.

    Three answers, in this order:

    * the media lock is held elsewhere — a worker is using this file right now,
      so it is left alone and nothing about the row is read or written;
    * the record is `UPLOADED` or `TRANSCRIBING` with the lock free — the file
      is still the record's own. A queued job has not started yet, and a job
      whose `_claim` failed can be enqueued again and will find its file where
      it left it. Deleting it would turn a re-runnable record into one that can
      only fail;
    * the record is past the media and the file is still there — a refused
      unlink, or a run that died after writing its outcome. That is the leak.
      It goes, and the row is corrected in the same transaction.

    The unlink happens before the row is written and the row is only written if
    it succeeded, so what the record says stays true: a row claiming a recording
    was destroyed while it is still on the volume is the one outcome worse than
    either fact on its own.
    """
    with media_lock(record_id) as held:
        if not held:
            report.kept_live.append(path)
            return
        with transaction.atomic():
            record = MeetingRecord.objects.select_for_update().filter(pk=record_id).first()
            if record is None or record.temp_path != str(path) or not record.media_is_retained:
                report.kept_owned.append(path)
                return
            if not _remove(path, report):
                return
            record.temp_path = None
            record.media_deleted_at = timezone.now()
            record.save(update_fields=["temp_path", "media_deleted_at"])
            logger.info(
                "meeting record %s: the recording left at %s has been deleted", record_id, path
            )


def _remove(path: Path, report: SweepReport) -> bool:
    """Delete one file or one directory tree, and record which it was.

    A refusal is reported rather than raised: one unreadable path must not stop
    the sweep from collecting everything else, and the next sweep tries again.
    """
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.exception("the media at %s could not be deleted", path)
        report.refused.append(path)
        return False
    report.removed.append(path)
    return True


def _younger_than(path: Path, minimum_age: timedelta) -> bool:
    """Whether `path` was last written to less than `minimum_age` ago.

    A path that vanished between the listing and here counts as young: there is
    nothing left to delete, and saying "removed" about it would be a lie in a
    report an operator reads.
    """
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return True
    return timezone.now() - modified < minimum_age
