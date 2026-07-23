"""Taking one uploaded meeting from `UPLOADED` to a stored transcript.

This is the body the `process_meeting_record` job runs. It is the only place
that knows both halves of the work: `ai/audio.py` and `ai/transcription.py` take
paths and return data and never touch a model, and everything that reads or
writes a row happens here.

The shape of it is one `try`/`except`/`finally`, and the `finally` is the reason
the module exists:

    try:      turn the media into a transcript
    except:   the record goes FAILED, in words a facilitator can act on
    finally:  the media is deleted, whatever happened above

`_docs/decisions.md` item 6 settles that last line. The recording is deleted
whether transcription succeeded, failed or raised, so there is no retention
window, no storage policy, and no retry button — because after this function
returns there is nothing left to retry against. What there is instead is a
message that says so, and a second upload.

Which is also why the retry lives *inside* the try. A transient API failure — a
rate limit, a 5xx — is retried by `ai.transcription.transcribe_chunks` while the
audio is still on disk. Nothing is retried after the `finally`: the queue
retries nothing automatically (AGENTS.md, "Background tasks"), and a job re-run
by hand finds the record is no longer `UPLOADED` and leaves it alone.

What the `finally` cannot cover, and what does (#71)
---------------------------------------------------

The `finally` runs on every way out of this process. It does not run when there
is no process left: a `SIGKILL` between two chunks executes nothing, and the
upload and the extracted chunks stay on the volume with nobody to collect them.
Nor can it help when the unlink is refused by the filesystem, which leaves a
recording of a private meeting on disk with the row honestly saying so and
nothing coming back for it.

So two things surround it, and both are here rather than in a scheduler:

* the run holds its record's media lock (`meetings/locks.py`) from the first
  line to the last. Postgres releases it when the process ends, however it ends,
  so "is anyone still working on this record?" has an answer that survives
  `kill -9`. `meetings/sweeper.py` asks it before it deletes anything;
* the record reaches a terminal state on every way out. A `BaseException` used
  to leave it `TRANSCRIBING` for ever, which is #19's status page polling for
  ever, so the same `finally` marks it `FAILED` when the run did not get as far
  as an outcome of its own. If even that write cannot happen — the database is
  where the failure was — the sweeper does it later, from another process.
"""

import logging
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from ai.audio import MediaProcessingError, prepare_audio_chunks
from ai.transcription import (
    Transcription,
    TranscriptionError,
    build_client,
    transcribe_chunks,
)
from config.tasks import enqueue_on_commit, extract_meeting_outcomes
from meetings.locks import media_lock
from meetings.models import MeetingRecord, Transcript

logger = logging.getLogger(__name__)

#: Every failure message ends with this. `_docs/decisions.md` item 6: the
#: recording is gone by the time the facilitator reads it, so the only recovery
#: is to upload the file again, and the message has to say so rather than leave
#: them looking for a button.
RECOVERY = (
    "The recording was deleted as the attempt ended, so there is nothing left to run again — "
    "upload the file once more to try it."
)

#: What a facilitator is told when the failure is not one we recognised. The
#: real exception goes to the log with its traceback; a stack trace on a web
#: page tells them nothing and tells everyone else too much.
UNEXPECTED = "Something went wrong while transcribing this meeting."

#: What a record says when the run that held it stopped without finishing — a
#: `SIGKILL`, a shutdown, a `KeyboardInterrupt`. It is written by whichever of
#: the two gets there first: this module's `finally`, or the sweeper once
#: Postgres has released the dead worker's lock. Either way the record stops
#: being `TRANSCRIBING`, which is what #19's page polls on.
ABANDONED = "The worker processing this meeting stopped before it finished"

#: Where chunk files live while they are being transcribed, inside the scratch
#: volume `web` and `worker` share. A directory per run, deleted in the same
#: `finally` as the upload itself.
CHUNK_SUBDIR = "transcription"


def process_meeting(record_id: int, *, client=None) -> None:
    """Run the pipeline for one record, if it is still there and still ours.

    Takes an id and re-fetches, per the conventions in AGENTS.md: time passes
    between the enqueue and the run, so the row may have gone, and another
    worker may already have claimed it. Both are a return rather than an error.

    `client` is the transcription client, for a test that wants to script one.
    Left unset, `ai.transcription` builds whatever `TRANSCRIPTION_CLIENT` names.
    """
    record = MeetingRecord.objects.filter(pk=record_id).first()
    if record is None:
        logger.info("meeting record %s is gone; nothing to process", record_id)
        return
    if record.status != MeetingRecord.Status.UPLOADED:
        logger.info("meeting record %s is already %s; leaving it alone", record_id, record.status)
        return

    run(record, client=client)


def run(record: MeetingRecord, *, client=None) -> None:
    """Take this record's media lock, and do the work while holding it.

    The lock is taken before anything is read, written or created, so from the
    outside there is no instant at which this record's files exist without an
    owner: the sweeper either sees the lock held and leaves everything alone, or
    finds it free and knows no process is here. A lock it cannot take belongs to
    another worker, and the answer to that is to leave, exactly as it is for a
    record another worker has already moved out of `UPLOADED`.
    """
    with media_lock(record.pk) as held:
        if not held:
            logger.info("meeting record %s is held by another worker; leaving it alone", record.pk)
            return
        _run_while_held(record, client=client)


def _run_while_held(record: MeetingRecord, *, client=None) -> None:
    """Transcribe this record's media, store the result, and delete the media.

    Returns normally whether it worked or not: the outcome is on the record, and
    raising would only mark a queue row failed for something already recorded in
    words. What it never does is return with the media still on disk, or with
    the record in a status the pipeline will never move it out of again.
    """
    source = Path(record.temp_path) if record.temp_path else None
    work_dir: Path | None = None
    _claim(record)

    try:
        if record.skips_transcription:
            transcription = _read_text(source)
        else:
            work_dir = _work_dir(record)
            transcription = _transcribe(source, work_dir, client)
    except (TranscriptionError, MediaProcessingError) as exc:
        # A failure we know the shape of: its message was written to be read by
        # the person who uploaded the file.
        logger.warning("meeting record %s failed to transcribe: %s", record.pk, exc)
        _fail(record, str(exc))
    except Exception:
        logger.exception("meeting record %s raised while transcribing", record.pk)
        _fail(record, UNEXPECTED)
    else:
        _store(record, transcription)
    finally:
        # Item 6, and the only lines in this function that run whatever happened
        # above — including a `BaseException` this function does not catch,
        # which is the case the `except` clauses cannot cover.
        #
        # The media goes first and the status second, in nested `finally`s, so
        # a failure to write the status cannot leave the recording on disk. The
        # other order would trade the thing that cannot be undone for the thing
        # the sweeper can redo from another process.
        try:
            _discard_media(record, source, work_dir)
        finally:
            _abandon_if_unfinished(record)


# --------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------


def _claim(record: MeetingRecord) -> None:
    """Count the attempt, and say what is happening on the page from #19.

    `attempts` counts real runs of this pipeline, not scheduled retries: there
    are none of those. It goes up once here, before anything can fail, so a
    record that failed halfway still shows it was tried.

    A record that arrived as text never enters TRANSCRIBING. There is nothing to
    transcribe, the page already says so, and a status it passes through in a
    microsecond is a status the facilitator would only see by accident.
    """
    record.attempts += 1
    fields = ["attempts"]
    if not record.skips_transcription:
        record.status = MeetingRecord.Status.TRANSCRIBING
        fields.append("status")
    record.save(update_fields=fields)


def _work_dir(record: MeetingRecord) -> Path:
    """A directory of this run's own, on the volume both containers mount."""
    root = Path(settings.SCRATCH_DIR) / CHUNK_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"record-{record.pk}-", dir=root))


def _transcribe(source: Path | None, work_dir: Path, client) -> Transcription:
    """Prepare the audio, then transcribe every chunk in order.

    The 25 MB request cap is `ai/audio.py`'s problem and is already handled by
    the time the chunks come back; nothing here re-chunks or re-checks a size.

    The client is built first, before a frame is decoded: a missing
    `OPENAI_API_KEY` is then reported as a missing key rather than after several
    minutes of ffmpeg.
    """
    if client is None:
        client = build_client()
    if source is None or not source.is_file():
        raise MediaProcessingError(
            "The uploaded file was no longer on the shared volume when the worker reached it"
        )
    chunks = prepare_audio_chunks(source, work_root=work_dir)
    logger.info("transcribing %s chunk(s) from %s", len(chunks), source.name)
    return transcribe_chunks(chunks, client=client)


def _read_text(source: Path | None) -> Transcription:
    """A transcript file or pasted text is already what transcription produces.

    It goes straight into a `Transcript`, unedited: whatever speaker labels the
    team's own tooling wrote are the labels #23 will read, and rewriting them
    here would be guessing at someone else's format.
    """
    if source is None or not source.is_file():
        raise MediaProcessingError(
            "The transcript that was handed over was no longer on the shared volume"
        )
    text = source.read_bytes().decode("utf-8", errors="replace").strip()
    if not text:
        raise MediaProcessingError("The transcript that was handed over was empty")
    return Transcription(text=text, chunk_count=1, attempts=0)


def _store(record: MeetingRecord, transcription: Transcription) -> None:
    """Write the transcript, and hand the record to the next stage.

    `update_or_create` rather than `create`: a record re-run by hand after its
    row was put back to UPLOADED should end with one transcript, not two, and
    the one-to-one would refuse the second anyway.

    The next stage is EXTRACTING, and #23's extraction job is enqueued on commit
    to carry the record on from there. It runs on the committed, durable
    transcript — the recording is deleted in the `finally` below, but extraction
    never needs it (`_docs/decisions.md` item 6), which is why it is retryable
    where transcription is not. `enqueue_on_commit` (#18) keeps the worker from
    claiming the job before the transcript it reads has committed.
    """
    Transcript.objects.update_or_create(
        record=record,
        defaults={
            "text": transcription.text,
            "language": transcription.language,
            "duration_seconds": transcription.duration_seconds,
        },
    )
    record.status = MeetingRecord.Status.EXTRACTING
    record.error_message = ""
    record.save(update_fields=["status", "error_message"])
    enqueue_on_commit(extract_meeting_outcomes, record.pk)
    logger.info(
        "meeting record %s transcribed: %s characters, %s chunk(s), %s speaker(s)",
        record.pk,
        len(transcription.text),
        transcription.chunk_count,
        transcription.speaker_count,
    )


def failure_message(reason: str) -> str:
    """`reason`, then the one instruction that can follow it (item 6).

    Shared with the sweeper, which writes the same sentence from another process
    for a run that ended without being able to write anything at all.
    """
    return f"{reason.rstrip('.')}. {RECOVERY}"


def _fail(record: MeetingRecord, reason: str) -> None:
    """Record the failure in words, with the instruction that goes with it."""
    record.status = MeetingRecord.Status.FAILED
    record.error_message = failure_message(reason)
    record.save(update_fields=["status", "error_message"])


def _abandon_if_unfinished(record: MeetingRecord) -> None:
    """Make sure this run left a terminal record, whatever cut it short.

    Every ordinary way out of the block above has already set a status: a stored
    transcript is `EXTRACTING`, a handled failure is `FAILED`. Anything still
    `UPLOADED` or `TRANSCRIBING` by the time this runs was interrupted — a
    `KeyboardInterrupt`, a `SystemExit`, a `DatabaseError` that stopped `_fail`
    from writing — and the media has just been deleted, so there is nothing left
    for a second attempt to run against. It is a failure, and saying so is what
    stops #19's page polling a record nothing will ever move again.

    Written as one conditional `UPDATE` against the database rather than from
    the in-memory row, because the in-memory row is exactly what is not to be
    trusted here: `_fail` sets `status` before it saves, so a `_fail` whose save
    raised leaves an instance claiming a state the database never took. The
    `exclude` is what makes it safe to run on the success path too — it changes
    nothing when the run reached an outcome of its own.

    Its own errors are logged and swallowed. It runs inside a `finally`, so
    raising here would replace whatever failure brought us here with a second,
    less informative one — and the sweeper marks the record from another process
    when this could not.
    """
    try:
        changed = (
            MeetingRecord.objects.filter(pk=record.pk)
            .exclude(status__in=MeetingRecord.PAST_MEDIA_STATUSES)
            .update(status=MeetingRecord.Status.FAILED, error_message=failure_message(ABANDONED))
        )
    except Exception:
        logger.exception(
            "meeting record %s was cut short and could not be marked failed", record.pk
        )
        return

    if changed:
        record.status = MeetingRecord.Status.FAILED
        record.error_message = failure_message(ABANDONED)
        logger.warning(
            "meeting record %s was cut short before it finished; marked failed", record.pk
        )


def _discard_media(record: MeetingRecord, source: Path | None, work_dir: Path | None) -> None:
    """Delete the recording and every chunk cut from it, and say so on the row.

    The deletion and the two fields that describe it are one step: a row saying
    the media is gone while the file is still there would be worse than either
    fact on its own. If the unlink fails the row is left alone and the log
    carries it, so what the record says stays true.
    """
    if work_dir is not None:
        shutil.rmtree(work_dir, ignore_errors=True)

    if source is not None:
        try:
            source.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "meeting record %s: the recording at %s could not be deleted", record.pk, source
            )
            return

    record.temp_path = None
    record.media_deleted_at = timezone.now()
    record.save(update_fields=["temp_path", "media_deleted_at"])
