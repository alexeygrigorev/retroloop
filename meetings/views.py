"""The upload page and the fragment it polls.

Both views ask `projects/permissions.py` who this is and `meetings/services.py`
whether the retrospective has got far enough; neither decides anything itself.
The refusals are deliberately different, and each says something true:

* someone who is not on the project gets a 404, the same answer as an id that
  was never used — a 403 would confirm the retrospective exists;
* a member of the project who is not this week's facilitator gets a 403;
* a retrospective that has not reached DISCUSS is not offered the form, and a
  POST to it is refused, because there has been no meeting yet;
* a request whose body is over the cap is refused before it is parsed, so the
  501st megabyte is never read.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from meetings.forms import MeetingUploadForm
from meetings.models import MeetingRecord
from meetings.services import store_meeting_record, upload_is_open
from meetings.uploads import (
    ALLOWED_EXTENSIONS,
    AUDIO_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_LABEL,
    TRANSCRIPT_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from projects.permissions import can_upload_recording
from projects.views import member_or_404
from retro.models import Retrospective

#: What a browser is told when its request was refused for its size. 413 rather
#: than a redirect, so a client that is not a browser is told the same thing.
REQUEST_TOO_LARGE = 413

TOO_LARGE_MESSAGE = (
    f"That upload is larger than the {MAX_UPLOAD_LABEL} limit, so none of it was kept. "
    "Upload the audio on its own rather than the video, or paste the transcript."
)

ALREADY_RUNNING_MESSAGE = (
    "This retrospective already has a meeting being processed. "
    "Wait for it to finish, or reload the page to see where it has got to."
)


@login_required
def meeting_upload(request: HttpRequest, pk: int) -> HttpResponse:
    """Hand a meeting over, and watch it being processed."""
    retro = _retrospective_for(request, pk)
    record = _latest_record(retro)
    is_open = upload_is_open(retro)

    if request.method != "POST":
        return _page(request, retro, record, MeetingUploadForm())

    if not is_open:
        # Nothing on the page offers this, so a POST is either a stale tab or
        # somebody trying it on. Both get the same answer.
        raise PermissionDenied("There is no meeting to hand over until the retrospective discusses")

    # Before `request.POST` is touched: reading the body is what would spool
    # half a gigabyte through this worker.
    if _declared_size(request) > MAX_UPLOAD_BYTES:
        messages.error(request, TOO_LARGE_MESSAGE)
        return _page(request, retro, record, MeetingUploadForm(), status=REQUEST_TOO_LARGE)

    form = MeetingUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _page(request, retro, record, form)

    try:
        record = store_meeting_record(
            retro=retro,
            user=request.user,
            kind=form.kind(),
            upload=form.cleaned_data.get("media"),
            text=form.cleaned_data.get("pasted_text") or "",
        )
    except IntegrityError:
        # The index refused it, which is the only answer that is safe when two
        # uploads arrive at once. Nothing was written.
        messages.error(request, ALREADY_RUNNING_MESSAGE)
        return _page(request, retro, _latest_record(retro), MeetingUploadForm())

    messages.success(
        request,
        "The meeting is with the system now. This page keeps itself up to date while it works.",
    )
    return redirect("meeting-upload", pk=retro.pk)


@login_required
def meeting_record_status(request: HttpRequest, pk: int) -> HttpResponse:
    """The polled fragment: one record's progress, and whether to ask again."""
    record = get_object_or_404(
        MeetingRecord.objects.select_related(
            "retrospective__cycle__project", "retrospective__cycle__facilitator"
        ),
        pk=pk,
    )
    retro = record.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_upload_recording(request.user, retro):
        raise PermissionDenied(
            "Only this cycle's facilitator can watch the meeting being processed"
        )

    return render(request, "meetings/meeting_status.html", _status_context(record))


# --------------------------------------------------------------------------
# The parts both views share
# --------------------------------------------------------------------------


def _retrospective_for(request: HttpRequest, pk: int) -> Retrospective:
    """The retrospective, or the refusal the caller has earned."""
    retro = get_object_or_404(
        Retrospective.objects.select_related("cycle__project", "cycle__facilitator"), pk=pk
    )
    member_or_404(request.user, retro.cycle.project)
    if not can_upload_recording(request.user, retro):
        raise PermissionDenied("Only this cycle's facilitator can hand over the meeting")
    return retro


def _latest_record(retro: Retrospective) -> MeetingRecord | None:
    """The most recent record, which is the one the page is about.

    A live record is the only one there can be, so this is it. When the last
    attempt failed this is that failure, which is what has to stay on the page:
    it carries the reason and the instruction to upload the file again.
    """
    return retro.meeting_records.first()


def _declared_size(request: HttpRequest) -> int:
    """What the request says it is carrying, or 0 when it does not say.

    A header a client controls, so anything that is not a plain number is read
    as "did not say" rather than trusted or raised on. The form checks the size
    it actually received afterwards, which is what catches a body that lied.
    """
    declared = str(request.META.get("CONTENT_LENGTH") or "").strip()
    return int(declared) if declared.isdigit() else 0


def _status_context(record: MeetingRecord | None) -> dict:
    """Everything the polled fragment renders, computed here rather than in it.

    `status_url` is passed in rather than reversed in the template: the
    fragment is also rendered on its own, and `{% url %}` inside it would then
    have no record to reverse against.
    """
    return {
        "record": record,
        "status_url": reverse("meeting-record-status", args=[record.pk]) if record else "",
        # Polling stops at READY and at FAILED, because neither moves again.
        "polling": record is not None and not record.is_final,
        "failed": record is not None and record.status == MeetingRecord.Status.FAILED,
    }


def _page(
    request: HttpRequest,
    retro: Retrospective,
    record: MeetingRecord | None,
    form: MeetingUploadForm,
    status: int = 200,
) -> HttpResponse:
    """The whole page, in whichever state this request left it."""
    is_open = upload_is_open(retro)
    context = {
        "retro": retro,
        "cycle": retro.cycle,
        "project": retro.cycle.project,
        "form": form,
        # The form is offered only from DISCUSS, and only when there is nothing
        # already in flight. A failed record leaves it offered, which is how
        # re-uploading after a failure happens.
        "upload_is_open": is_open,
        "can_upload": is_open and (record is None or record.status == MeetingRecord.Status.FAILED),
        "audio_extensions": ", ".join(AUDIO_EXTENSIONS),
        "video_extensions": ", ".join(VIDEO_EXTENSIONS),
        "transcript_extensions": ", ".join(TRANSCRIPT_EXTENSIONS),
        "allowed_extensions": ", ".join(ALLOWED_EXTENSIONS),
        "max_upload_label": MAX_UPLOAD_LABEL,
    }
    context.update(_status_context(record))
    return render(request, "meetings/meeting_upload.html", context, status=status)
