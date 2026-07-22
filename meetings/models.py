"""The record of one meeting handed to the system, and how far it has got.

A `MeetingRecord` is the paperwork around a file that does not survive. The
recording itself lives in the shared scratch volume for as long as the pipeline
needs it and is deleted in a `finally` block by #21 — `_docs/decisions.md` item
6 — so everything here is written on the assumption that the media is already
gone: `temp_path` goes null, `media_deleted_at` is stamped, and a failure is
explained in words rather than offered a retry that could not work.

One rule is held by the database rather than by a view: a retrospective has at
most one record that is not FAILED. Two facilitators uploading at the same
second both pass whatever a view checks, and one of them then loses to a unique
index instead of quietly creating a second pipeline over the same meeting.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models

from retro.models import Retrospective


class MeetingRecord(models.Model):
    """One recording, video, transcript file or pasted transcript, in progress."""

    class Kind(models.TextChoices):
        AUDIO = "AUDIO", "Audio recording"
        VIDEO = "VIDEO", "Video recording"
        TRANSCRIPT_FILE = "TRANSCRIPT_FILE", "Transcript file"
        PASTED_TEXT = "PASTED_TEXT", "Pasted transcript"

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        TRANSCRIBING = "TRANSCRIBING", "Transcribing"
        EXTRACTING = "EXTRACTING", "Extracting"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    #: The kinds that arrive as text and so have nothing to transcribe.
    TEXT_KINDS: ClassVar[tuple[str, ...]] = (Kind.TRANSCRIPT_FILE, Kind.PASTED_TEXT)

    #: The statuses the pipeline never moves out of, so the page stops polling.
    FINAL_STATUSES: ClassVar[tuple[str, ...]] = (Status.READY, Status.FAILED)

    # Many per retrospective over time, at most one of them live: re-uploading
    # after a failure is the documented way to recover, so a failed row stays
    # where it is and the next attempt is a new row rather than an overwrite.
    retrospective = models.ForeignKey(
        Retrospective,
        on_delete=models.CASCADE,
        related_name="meeting_records",
    )
    # PROTECT, like a cycle's facilitator: who handed the meeting over is part
    # of the record, and deleting the account should not quietly rewrite it.
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="meeting_records",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Null once the media is deleted, which is the normal end state of every
    # record — see `media_deleted_at`. Nullable on purpose, and DJ001 is
    # silenced rather than obeyed: "there is no longer a file" and "the path is
    # the empty string" are different facts, the pipeline sets the first of
    # them, and #19 names the field nullable.
    temp_path = models.CharField(  # noqa: DJ001
        max_length=500,
        null=True,
        blank=True,
        help_text="Where the media sits in the shared scratch volume, until it is deleted.",
    )
    # Display only. It is never part of a path and is escaped wherever it is
    # rendered, because it is a string a person chose.
    original_filename = models.CharField(max_length=255, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
    )
    # Counted by the pipeline, not by the upload: nothing is retried
    # automatically (AGENTS.md, "Background tasks"), so this records what was
    # actually attempted rather than what a scheduler decided.
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    media_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # "At most one record per retrospective that is not FAILED", as an
            # index rather than as a check in a view. A view's check is a race:
            # both requests read no live record, both then insert. Here the
            # second insert is refused by Postgres and the caller turns that
            # into a readable message.
            models.UniqueConstraint(
                fields=["retrospective"],
                condition=~models.Q(status="FAILED"),
                name="one_live_meeting_record_per_retrospective",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} for {self.retrospective_id} ({self.status})"

    @property
    def is_final(self) -> bool:
        """Whether the pipeline is done with this record, either way."""
        return self.status in self.FINAL_STATUSES

    @property
    def skips_transcription(self) -> bool:
        """Text arrived as text, so there is nothing to transcribe.

        A transcript file and pasted text go straight to extraction — the
        pipeline reads this rather than re-deciding it from the file.
        """
        return self.kind in self.TEXT_KINDS

    @property
    def progress(self) -> str:
        """Where this record has got to, in a sentence rather than an enum.

        The facilitator watching the page is told what is happening to their
        meeting, not which constant the row holds.
        """
        return PROGRESS[self.status]


class Transcript(models.Model):
    """What was said, in text, and the only thing that outlives the recording.

    One per record, and the durable record of the meeting: the media is deleted
    in a `finally` block as the pipeline ends (`_docs/decisions.md` item 6), so
    after that this row is all there is. It is a column in Postgres and nothing
    else — no file, no object store, no second copy to keep in sync or forget to
    delete.

    `text` carries speaker labels, `Speaker 1:` and so on, one line per turn.
    They are what makes owner extraction in #23 possible at all, and how they
    are assigned across chunks is documented in `ai/transcription.py`.
    """

    record = models.OneToOneField(
        MeetingRecord,
        on_delete=models.CASCADE,
        related_name="transcript",
    )
    text = models.TextField()
    # What the API reported, when it reported anything. The diarizing model
    # does not return a detected language, so this is usually blank rather than
    # guessed at; a blank language is "nobody said", not "English".
    language = models.CharField(max_length=20, blank=True)
    # Null for a transcript that arrived as text: there was no audio, so there
    # is no length, and zero would be a measurement nobody made.
    duration_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Transcript of record {self.record_id} ({len(self.text)} characters)"


#: One sentence per status, in the words the facilitator sees. Keyed by the
#: stored value so a status added later shows up here as a KeyError rather than
#: as a blank line on the page.
PROGRESS: dict[str, str] = {
    MeetingRecord.Status.UPLOADED: (
        "Received. It is queued, and a worker picks it up within a few seconds."
    ),
    MeetingRecord.Status.TRANSCRIBING: "Listening to the meeting and writing down what was said.",
    MeetingRecord.Status.EXTRACTING: (
        "Reading the transcript for the decisions and the actions the team agreed."
    ),
    MeetingRecord.Status.READY: "Finished. The decisions and actions are ready for you to review.",
    MeetingRecord.Status.FAILED: "This one did not get through.",
}
