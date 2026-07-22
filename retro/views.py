"""Retrospective views.

They are thin on purpose. Every rule lives in `retro/services.py`; a view's
whole job is to find the row, refuse the people who may not see it, call the
service, and turn its rejection into a response. No view assigns `stage`.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cycles.models import Card, FeedbackCycle
from projects.views import member_or_404
from retro.models import Retrospective
from retro.services import (
    StageError,
    advance_stage,
    can_advance_stage,
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
            "next_stage_label": _stage_label(retro.next_stage),
            # What the React island is handed, rendered into the page with
            # `json_script`. See board_bootstrap() for why it is this and no more.
            "board_bootstrap": board_bootstrap(request.user, retro),
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


def board_bootstrap(user, retro: Retrospective) -> dict:
    """The initial state the React island mounts with — four things, and no more.

    The retrospective's id, its `stage`, its `version`, and this viewer's own
    cards. Nothing else, and in particular not another member's card text.

    That is the whole point of the shape. The island renders on the real
    retrospective page, so anything put in here is in the page source of a page
    every member of the project can open — which is exactly what #10 (anonymity
    at reveal) and #11 (the state endpoint, which decides what a member may see
    at each stage) exist to prevent. A placeholder that dumped the board into
    the document would leak it in a way no stage machine could take back.

    So the board's real state does not come from here. #14 wires the island to
    #11's state endpoint and the filtering rule lives there, where it belongs;
    this function stays as it is, or goes.

    `author=user` is also why anonymised cards cannot appear: #10 sets `author`
    to NULL at reveal, and a NULL author matches nobody.
    """
    cards = Card.objects.filter(cycle=retro.cycle, author=user)
    return {
        "id": retro.pk,
        "stage": retro.stage,
        "version": retro.version,
        "cards": [{"id": card.pk, "category": card.category, "text": card.text} for card in cards],
    }


def _stage_label(stage: str | None) -> str:
    """The human label for a stage value, or an empty string for no stage."""
    if stage is None:
        return ""
    return Retrospective.Stage(stage).label
