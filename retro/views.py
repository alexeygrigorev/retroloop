"""Retrospective views.

They are thin on purpose. Who may act is decided in `projects/permissions.py`
and what a transition does lives in `retro/services.py`; a view's whole job is
to find the row, refuse the people who may not see it, call the service, and
turn its rejection into a response. No view assigns `stage`.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cycles.models import FeedbackCycle
from meetings.services import upload_is_open
from projects.permissions import can_advance_stage, can_upload_recording
from projects.views import member_or_404
from retro.models import Retrospective
from retro.services import (
    StageError,
    advance_stage,
    start_retrospective,
)


@login_required
@require_POST
def retro_start(request: HttpRequest, cycle_pk: int) -> HttpResponse:
    cycle = get_object_or_404(
        FeedbackCycle.objects.select_related("project", "facilitator"), pk=cycle_pk
    )
    # A retrospective is as private as its project, and answers a non-member
    # the same way an id that was never used does.
    member_or_404(request.user, cycle.project)

    retro = start_retrospective(request.user, cycle)
    messages.success(
        request,
        "The retrospective is open, in Draft. Nothing is revealed until you advance it.",
    )
    return redirect(retro)


@login_required
def retro_detail(request: HttpRequest, pk: int) -> HttpResponse:
    retro = get_object_or_404(
        Retrospective.objects.select_related("cycle__project", "cycle__facilitator"), pk=pk
    )
    member_or_404(request.user, retro.cycle.project)

    return render(
        request,
        "retro/retro_detail.html",
        {
            "retro": retro,
            "cycle": retro.cycle,
            "project": retro.cycle.project,
            "stages": Retrospective.Stage.choices,
            # Two questions, answered here rather than in the template: may this
            # person advance it, and is there anywhere left to advance to.
            "can_advance": can_advance_stage(request.user, retro) and not retro.is_complete,
            # The meeting is handed over from DISCUSS on, by this cycle's
            # facilitator. Both halves are asked again by the page the link
            # points at; this only decides whether to show the link.
            "can_hand_over_meeting": can_upload_recording(request.user, retro)
            and upload_is_open(retro),
            "next_stage_label": _stage_label(retro.next_stage),
        },
    )


@login_required
@require_POST
def retro_advance(request: HttpRequest, pk: int) -> HttpResponse:
    retro = get_object_or_404(Retrospective.objects.select_related("cycle__project"), pk=pk)
    member_or_404(request.user, retro.cycle.project)

    # The page carries the version it was rendered from, so two facilitators —
    # or one double-click — cannot advance twice off one screen. A caller that
    # sends no version is acting on the row as it is right now, which is what
    # the freshly loaded instance already holds.
    posted = request.POST.get("version", "")
    if posted.isdigit():
        retro.version = int(posted)

    try:
        advance_stage(request.user, retro)
    except StageError as rejection:
        messages.error(request, str(rejection))
        return redirect(retro)

    messages.success(request, f"The retrospective is now in {retro.get_stage_display()}.")
    return redirect(retro)


def _stage_label(stage: str | None) -> str:
    """The human label for a stage value, or an empty string for no stage."""
    if stage is None:
        return ""
    return Retrospective.Stage(stage).label
