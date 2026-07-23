"""Turn one meeting's stored transcript into draft outcomes, and write them down.

This is the body the `extract_meeting_outcomes` job runs. It is the only half of
extraction that touches a model: `ai/extraction.py` takes plain data and returns
plain data and never imports a row, and everything that reads or writes a
retrospective happens here — the same split `meetings/pipeline.py` keeps with
`ai/transcription.py` and `retro/clustering.py` keeps with `ai/clustering.py`.

Where it sits in the pipeline
-----------------------------

`meetings/pipeline.py` stores the transcript and leaves the record `EXTRACTING`,
then enqueues this with `enqueue_on_commit` (#18), so it runs *after* the
transcript has committed — on a durable transcript, never inside the transaction
that wrote it. The recording is already gone by then (`_docs/decisions.md` item
6): this reads the stored transcript and never the media, which is the whole
reason extraction is retryable where transcription is not.

What it sends, and what it never sends
--------------------------------------

It builds the model's input from three things: the transcript text, the ranked
agenda (#16's clusters ordered by vote weight, exactly the DISCUSS ordering the
board serialises), and the roster — the project members' **display names**, never
an email address or a username where a display name exists (#23; item 8). It
sends no card, no card author, no `Card.pk`, no anonymity flag; a draft it writes
points at a cluster's integer id or at nothing and names an owner or nobody, so
it leaks neither of the two facts `_docs/decisions.md` items 9 and 10 keep off a
screen.

What it writes, and what it guarantees
--------------------------------------

Every decision and action item is written `source=EXTRACTED` and `DRAFT` — never
confirmed, because facilitator approval in #24 is the point of the feature. The
owner name the model returned is resolved against the roster in `ai/extraction.py`
and mapped to a member here, or left NULL when it matched nobody or was
ambiguous. The short summary lands on the retrospective as a draft too. Each
draft carries the supporting excerpt the reviewer reads in #24.

* An empty or near-empty transcript produces no drafts and finishes the record
  READY: a meeting where nothing was decided is a real outcome.
* A failure marks the record FAILED with a readable message and keeps the
  transcript, so a facilitator can run extraction again without re-uploading
  anything — the message says so, and does not point at an upload button.
* Malformed model output is dropped per item by `ai/extraction.py`; the valid
  drafts still land.

The write is one transaction under the retrospective's row lock, and re-running
first clears the previous extracted drafts, so a second attempt after a failure
replaces rather than duplicates and never touches a hand-written or already
confirmed row.
"""

import logging

from django.db import models, transaction

from ai.extraction import AgendaItem, ExtractionError, ExtractionInput, extract_outcomes
from meetings.models import MeetingRecord
from retro.models import ActionItem, Cluster, Decision, Retrospective, Vote

logger = logging.getLogger(__name__)

#: Appended to every failure message. Unlike a transcription failure, nothing was
#: destroyed — the transcript is stored and durable (`_docs/decisions.md` item 6)
#: — so the words say extraction can be re-run, and do *not* point at an upload
#: button the way `meetings/pipeline.py`'s do.
RECOVERY = (
    "The transcript was kept, so extraction can be run again without uploading the meeting "
    "once more."
)

#: What a facilitator is told when the failure is not one we recognised. The real
#: exception goes to the log with its traceback; a stack trace on the page tells
#: them nothing and tells everyone else too much.
UNEXPECTED = "Something went wrong while reading the transcript for decisions and actions."


def extract_meeting_outcomes(record_id: int, *, client=None) -> None:
    """Extract one record's outcomes, if it is still there and still EXTRACTING.

    Takes an id and re-fetches, per the queue conventions (AGENTS.md): time
    passes between the enqueue and the run, so the row may have gone, or another
    worker may already have moved it on. Both are a return rather than an error.

    `client` is the extraction client, for a test that wants to script one. Left
    unset, `ai.extraction` builds whatever `EXTRACTION_CLIENT` names.
    """
    record = MeetingRecord.objects.select_related("retrospective").filter(pk=record_id).first()
    if record is None:
        logger.info("meeting record %s is gone; nothing to extract", record_id)
        return
    if record.status != MeetingRecord.Status.EXTRACTING:
        logger.info(
            "meeting record %s is %s, not EXTRACTING; leaving it alone", record_id, record.status
        )
        return

    transcript = _transcript_text(record)
    if transcript is None:
        # EXTRACTING with no transcript should not happen — the pipeline writes
        # the transcript before it moves the record on — but if it does, there is
        # nothing to read, and that is a failure the facilitator can retry.
        logger.warning("meeting record %s is EXTRACTING with no transcript", record_id)
        _fail(record, "The transcript for this meeting could not be found")
        return

    meeting = ExtractionInput(
        transcript=transcript,
        meeting_date=record.created_at.date(),
        agenda=_agenda(record.retrospective),
        roster=tuple(name for name, _ in _roster(record.retrospective)),
    )

    try:
        outcomes = extract_outcomes(meeting, client=client)
    except ExtractionError as exc:
        logger.exception("extraction for meeting record %s failed", record_id)
        _fail(record, str(exc))
        return
    except Exception:
        logger.exception("extraction for meeting record %s failed unexpectedly", record_id)
        _fail(record, UNEXPECTED)
        return

    _write_outcomes(record, outcomes)


# --------------------------------------------------------------------------
# Building the model's input
# --------------------------------------------------------------------------


def _transcript_text(record: MeetingRecord) -> str | None:
    """The stored transcript for this record, or None if there is not one.

    Reads the durable transcript and never the media, which is already deleted by
    the time extraction runs (`_docs/decisions.md` item 6).
    """
    transcript = getattr(record, "transcript", None)
    return transcript.text if transcript is not None else None


def _agenda(retro: Retrospective) -> tuple[AgendaItem, ...]:
    """The discussion's clusters, ranked by vote weight — #16's agenda order.

    The same total order the board serialises from DISCUSS on: highest vote
    weight first, ties broken by `position` then `id`, a cluster with no votes at
    the bottom on weight 0. A cluster is a public handle the whole team made in
    front of the team, so its integer id, its name and its weight are sent; no
    card, no author, no `Card.pk` is reachable from here.
    """
    totals = dict(
        Vote.objects.filter(retrospective=retro)
        .values_list("cluster_id")
        .annotate(weight=models.Sum("weight"))
        .values_list("cluster_id", "weight")
    )
    clusters = sorted(
        Cluster.objects.filter(retrospective=retro),
        key=lambda cluster: (-totals.get(cluster.pk, 0), cluster.position, cluster.pk),
    )
    return tuple(
        AgendaItem(id=cluster.pk, name=cluster.name, weight=totals.get(cluster.pk, 0))
        for cluster in clusters
    )


def _roster(retro: Retrospective) -> list[tuple[str, int]]:
    """The project's members as ``(display name, user pk)``, in a stable order.

    The name is the display name, or the username where there is none — exactly
    `str(User)` — and never an email address (item 8: there are none) or a
    username where a display name exists (#23). Ordered by username, which is
    unique, so the same roster is sent from one run to the next and
    `resolve_owner`'s ambiguity check sees the same entries in the same order.
    The order is a member fact and never a card's: it is by `username`, not by a
    creation order, so nothing here recovers the submission order the reveal
    destroyed (`_docs/decisions.md` item 9).
    """
    members = (
        get_user_model()
        .objects.filter(memberships__project=retro.cycle.project_id)
        .order_by("username")
        .values_list("display_name", "username", "pk")
    )
    return [(display_name or username, pk) for display_name, username, pk in members]


# --------------------------------------------------------------------------
# Writing the drafts
# --------------------------------------------------------------------------


def _write_outcomes(record: MeetingRecord, outcomes: dict) -> None:
    """Write the drafts and the summary, and finish the record, in one transaction.

    Under the retrospective's row lock, so the drafts, the summary and the
    record's move to READY commit together and a re-run cannot half-see them.
    Re-fetches the record's status under the lock and stands down if it is no
    longer EXTRACTING, the same way clustering re-checks the board it is about to
    write.

    Owners are mapped from the resolved display name back to a member here — the
    name is one of the roster entries verbatim, and a name that maps to more than
    one member (two members sharing a display name) is treated as ambiguous and
    left NULL, a second guard behind `resolve_owner`'s own.
    """
    retro = record.retrospective
    with transaction.atomic():
        locked = Retrospective.objects.select_for_update(of=("self",)).get(pk=retro.pk)
        fresh = MeetingRecord.objects.select_for_update(of=("self",)).get(pk=record.pk)
        if fresh.status != MeetingRecord.Status.EXTRACTING:
            logger.info("meeting record %s moved on while we worked; standing down", record.pk)
            return

        # A re-run after a failure replaces the previous extracted drafts rather
        # than duplicating them, and never touches a hand-written or already
        # confirmed row: only EXTRACTED + DRAFT rows are cleared.
        Decision.objects.filter(
            retrospective=locked,
            source=Decision.Source.EXTRACTED,
            status=Decision.Status.DRAFT,
        ).delete()
        ActionItem.objects.filter(
            retrospective=locked,
            source=ActionItem.Source.EXTRACTED,
            review_status=ActionItem.ReviewStatus.DRAFT,
        ).delete()

        name_to_pks = _name_to_pks(locked)

        for decision in outcomes["decisions"]:
            Decision.objects.create(
                retrospective=locked,
                text=decision["text"],
                excerpt=decision["excerpt"],
                source=Decision.Source.EXTRACTED,
                status=Decision.Status.DRAFT,
            )

        for action in outcomes["action_items"]:
            ActionItem.objects.create(
                retrospective=locked,
                description=action["description"],
                excerpt=action["excerpt"],
                owner_id=_owner_pk(action["owner"], name_to_pks),
                due_date=action["due_date"],
                source=ActionItem.Source.EXTRACTED,
                review_status=ActionItem.ReviewStatus.DRAFT,
                status=ActionItem.Status.OPEN,
            )

        locked.extraction_summary = outcomes["summary"]
        locked.save(update_fields=["extraction_summary"])

        fresh.status = MeetingRecord.Status.READY
        fresh.error_message = ""
        fresh.save(update_fields=["status", "error_message"])

    logger.info(
        "meeting record %s extracted: %s decision(s), %s action item(s)",
        record.pk,
        len(outcomes["decisions"]),
        len(outcomes["action_items"]),
    )


def _name_to_pks(retro: Retrospective) -> dict[str, list[int]]:
    """Roster display name to the member pks that carry it.

    A list per name, not a single pk: two members can share a display name, and
    that collision is the ambiguity `_owner_pk` refuses to resolve rather than
    silently picking one.
    """
    mapping: dict[str, list[int]] = {}
    for name, pk in _roster(retro):
        mapping.setdefault(name, []).append(pk)
    return mapping


def _owner_pk(name, name_to_pks: dict[str, list[int]]) -> int | None:
    """The member the resolved owner name belongs to, or None.

    `resolve_owner` already returns None for an unmatched or ambiguous name, so
    this normally receives either None or a name that maps to exactly one member.
    The length check is the second guard: a name that maps to two members — a
    display name two people share — is ambiguous and stays NULL.
    """
    if name is None:
        return None
    pks = name_to_pks.get(name, [])
    return pks[0] if len(pks) == 1 else None


def failure_message(reason: str) -> str:
    """`reason`, then the one instruction that can follow it — re-run, not re-upload."""
    return f"{reason.rstrip('.')}. {RECOVERY}"


def _fail(record: MeetingRecord, reason: str) -> None:
    """Mark the record FAILED in words, keeping the transcript for a retry.

    The transcript is left exactly where it is: extraction's input is durable, so
    unlike a transcription failure this one is retryable, and the message says so.
    """
    record.status = MeetingRecord.Status.FAILED
    record.error_message = failure_message(reason)
    record.save(update_fields=["status", "error_message"])


def get_user_model():
    """`django.contrib.auth.get_user_model`, imported lazily.

    Kept out of module scope so this module imports without the app registry
    being ready, the same reason the pure `ai` modules defer their Django
    imports.
    """
    from django.contrib.auth import get_user_model as _get_user_model

    return _get_user_model()
