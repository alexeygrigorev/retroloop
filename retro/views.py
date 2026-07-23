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
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from cycles.models import Card, FeedbackCycle
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
@ensure_csrf_cookie
def retro_detail(request: HttpRequest, pk: int) -> HttpResponse:
    # `ensure_csrf_cookie`, so the CSRF token is set as a cookie for every
    # member who opens the board, not only the facilitator whose advance form
    # would otherwise be the sole thing rendering `{% csrf_token %}`. The React
    # island reads that cookie to write through #12's POST endpoints — the token
    # is never rendered into a script, so no token sits in the page source.
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


#: The endpoints the island talks to, keyed by the name the bundle uses. #11's
#: state read and #12's seven writes, and nothing else — the island performs no
#: other read and no other write. Named here, resolved from the URLconf, so the
#: bundle never writes a path down and cannot drift from the addresses the server
#: owns. Every one carries the retrospective's integer pk, which is public by
#: `_docs/decisions.md` item 9 — a card's pk is what never leaves the server, and
#: none of these is a card's.
_ENDPOINT_NAMES = {
    "state": "board-state",
    "cardMove": "board-card-move",
    "cardUngroup": "board-card-ungroup",
    "clusterCreate": "board-cluster-create",
    "clusterRename": "board-cluster-rename",
    "clusterMerge": "board-cluster-merge",
    "clusterSplit": "board-cluster-split",
    "clusterDelete": "board-cluster-delete",
}


def board_bootstrap(user, retro: Retrospective) -> dict:
    """The initial state the React island mounts with, and the URLs it talks to.

    The retrospective's id, its `stage`, its `version`, this viewer's own cards,
    and the endpoint URLs — nothing else, and in particular not another member's
    card text.

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

    A card is carried by its `public_id`, as a string, and never by its `pk` —
    `_docs/decisions.md` item 9, and the same value under the same key as
    #11's state endpoint sends. A card's identity therefore does not change
    when the first poll replaces this bootstrap.
    """
    cards = Card.objects.filter(cycle=retro.cycle, author=user)
    return {
        "id": retro.pk,
        "stage": retro.stage,
        "version": retro.version,
        "cards": [
            {"id": str(card.public_id), "category": card.category, "text": card.text}
            for card in cards
        ],
        "urls": {key: reverse(name, args=[retro.pk]) for key, name in _ENDPOINT_NAMES.items()},
    }


def _stage_label(stage: str | None) -> str:
    """The human label for a stage value, or an empty string for no stage."""
    if stage is None:
        return ""
    return Retrospective.Stage(stage).label
