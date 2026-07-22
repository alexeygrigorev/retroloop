"""Creating a meeting record: the row, the bytes, and the job, in that order.

One function, `store_meeting_record`, and the order inside it is the whole
point:

1. the row is inserted first, so the "one live record per retrospective" index
   refuses a second upload *before* anything is written to disk;
2. the bytes are streamed into the shared scratch volume inside the same
   transaction, so a write that fails takes the row with it;
3. the job is enqueued with `enqueue_on_commit`, so the worker cannot claim it
   until both the row and the file are really there.

Who may upload is not decided here. `projects/permissions.py` answers that and
the view asks it — this function is reached only after it said yes.
"""

from pathlib import Path

from django.db import transaction

from config.tasks import enqueue_on_commit, process_meeting_record
from meetings.models import MeetingRecord
from meetings.uploads import (
    generated_upload_path,
    stream_to_scratch,
    write_text_to_scratch,
)
from retro.models import Retrospective

#: The stage from which there is a meeting to hand over. Before DISCUSS the
#: team has not met, so the upload is not offered and a POST is refused.
UPLOAD_FROM_STAGE = Retrospective.Stage.DISCUSS


def upload_is_open(retro: Retrospective) -> bool:
    """Whether this retrospective has got as far as its discussion.

    A stage question, not an access question: it is the same answer for
    everyone, and who may act on it is `can_upload_recording`.
    """
    return retro.has_reached(UPLOAD_FROM_STAGE)


def store_meeting_record(
    *,
    retro: Retrospective,
    user,
    kind: str,
    upload=None,
    text: str = "",
) -> MeetingRecord:
    """Create the record, write the media into scratch, and queue the work.

    Raises `django.db.IntegrityError` when the retrospective already has a
    record that is not FAILED. The caller turns that into a message; it is not
    caught here, because a partly-created upload is not something to paper over.
    """
    destination = generated_upload_path()

    with transaction.atomic():
        record = MeetingRecord.objects.create(
            retrospective=retro,
            uploaded_by=user,
            kind=kind,
            temp_path=str(destination),
            # Display only, and only ever for a file. Pasted text was not
            # called anything.
            original_filename=upload.name if upload is not None else "",
            status=MeetingRecord.Status.UPLOADED,
        )
        record.size_bytes = _write(destination, upload, text)
        record.save(update_fields=["size_bytes"])

        # After the commit, per #18: the worker reads its own connection and
        # would otherwise be able to claim a job for a row it cannot see yet.
        enqueue_on_commit(process_meeting_record, record.pk)

    return record


def _write(destination: Path, upload, text: str) -> int:
    """Put the submission on the shared volume and return how big it turned out."""
    try:
        if upload is not None:
            return stream_to_scratch(upload, destination)
        return write_text_to_scratch(text, destination)
    except OSError:
        # The transaction is about to roll the row back, so the half-written
        # file it pointed at has nothing left referring to it.
        destination.unlink(missing_ok=True)
        raise
