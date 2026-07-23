"""Retrospective views.

They are thin on purpose. Who may act is decided in `projects/permissions.py`
and what a transition does lives in `retro/services.py`; a view's whole job is
to find the row, refuse the people who may not see it, call the service, and
turn its rejection into a response. No view assigns `stage`.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from cycles.models import Card, FeedbackCycle
from meetings.services import upload_is_open
from projects.permissions import (
    can_advance_stage,
    can_delete_action_item,
    can_delete_decision,
    can_edit_action_item,
    can_edit_decision,
    can_update_action_item,
    can_upload_recording,
)
from projects.views import member_or_404
from retro.forms import ActionItemForm, DecisionForm
from retro.models import ActionItem, Decision, Retrospective
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


# --------------------------------------------------------------------------
# Decisions and action items — #17
#
# Server-rendered with HTMX-style POST-and-redirect, not part of the React board
# island: these outcomes are not in `board_state`, so a change to one does not
# touch `Retrospective.version` and never wakes an island poller to re-read a
# board that has not changed. The version bump belongs to the board, and this is
# a different surface.
#
# The views are thin. Who may act is `projects/permissions.py`, what the models
# hold is `retro/models.py`, and what a form refuses is `retro/forms.py`; a
# view's whole job is to find the row, refuse the people who may not see it,
# validate, write, and redirect. The freeze at COMPLETE is not decided here — it
# is `can_edit_*` returning False, which a text edit or delete asks and a status
# flip does not.
# --------------------------------------------------------------------------


@login_required
def retro_outcomes(request: HttpRequest, pk: int) -> HttpResponse:
    """The decisions and action items of one retrospective, visible to members.

    Every project member sees them, with each action item's owner, due date and
    status, unassigned and overdue ones marked. The page also carries the two
    forms for writing one by hand, which any member may do.
    """
    retro = _retro_for_member(request, pk)
    return _render_outcomes(request, retro)


@login_required
@require_POST
def decision_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Write one decision by hand. Any project member may.

    `MANUAL` and `CONFIRMED` come from the model defaults — a person typing it is
    the review step — and `created_by` from the session, so it can never be
    written in another member's name.
    """
    retro = _retro_for_member(request, pk)
    form = DecisionForm(request.POST, retrospective=retro)
    if not form.is_valid():
        return _render_outcomes(request, retro, decision_form=form, status=400)

    decision = form.save(commit=False)
    decision.retrospective = retro
    decision.created_by = request.user
    decision.save()
    messages.success(request, "The decision has been recorded.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
def decision_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Re-word one decision. Its author or the facilitator, while not COMPLETE."""
    decision = get_object_or_404(
        Decision.objects.select_related("retrospective__cycle__project"), pk=pk
    )
    retro = decision.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_edit_decision(request.user, decision):
        raise PermissionDenied(
            "This decision is frozen, or is not yours to change. "
            "After the retrospective is complete its text can no longer be edited."
        )

    if request.method != "POST":
        form = DecisionForm(instance=decision, retrospective=retro)
        return _render_decision_edit(request, decision, form)

    form = DecisionForm(request.POST, instance=decision, retrospective=retro)
    if not form.is_valid():
        return _render_decision_edit(request, decision, form, status=400)

    form.save()
    messages.success(request, "The decision has been updated.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
@require_POST
def decision_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove one decision. Its author or the facilitator, while not COMPLETE."""
    decision = get_object_or_404(
        Decision.objects.select_related("retrospective__cycle__project"), pk=pk
    )
    retro = decision.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_delete_decision(request.user, decision):
        raise PermissionDenied(
            "This decision is frozen, or is not yours to remove. "
            "After the retrospective is complete it can no longer be deleted."
        )

    decision.delete()
    messages.success(request, "The decision has been removed.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
@require_POST
def action_item_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Write one action item by hand. Any project member may.

    An owner has to be a member of the project or empty; the form refuses any
    other value. An unassigned item is allowed and shown, not hidden.
    """
    retro = _retro_for_member(request, pk)
    form = ActionItemForm(request.POST, retrospective=retro, project=retro.cycle.project)
    if not form.is_valid():
        return _render_outcomes(request, retro, action_item_form=form, status=400)

    action = form.save(commit=False)
    action.retrospective = retro
    action.created_by = request.user
    action.save()
    messages.success(request, "The action item has been recorded.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
def action_item_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Re-word one action item. Its author or the facilitator, while not COMPLETE.

    This is the *text* — description, owner, due date. Ticking the item off is a
    separate endpoint that stays open after COMPLETE.
    """
    action = get_object_or_404(
        ActionItem.objects.select_related("retrospective__cycle__project", "owner"), pk=pk
    )
    retro = action.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_edit_action_item(request.user, action):
        raise PermissionDenied(
            "This action item is frozen, or is not yours to change. "
            "After the retrospective is complete its text can no longer be edited, "
            "though it can still be ticked off."
        )

    if request.method != "POST":
        form = ActionItemForm(instance=action, retrospective=retro, project=retro.cycle.project)
        return _render_action_item_edit(request, action, form)

    form = ActionItemForm(
        request.POST, instance=action, retrospective=retro, project=retro.cycle.project
    )
    if not form.is_valid():
        return _render_action_item_edit(request, action, form, status=400)

    form.save()
    messages.success(request, "The action item has been updated.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
@require_POST
def action_item_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove one action item. Its author or the facilitator, while not COMPLETE."""
    action = get_object_or_404(
        ActionItem.objects.select_related("retrospective__cycle__project"), pk=pk
    )
    retro = action.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_delete_action_item(request.user, action):
        raise PermissionDenied(
            "This action item is frozen, or is not yours to remove. "
            "After the retrospective is complete it can no longer be deleted."
        )

    action.delete()
    messages.success(request, "The action item has been removed.")
    return redirect("retro-outcomes", pk=retro.pk)


@login_required
@require_POST
def action_item_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    """Flip one action item between OPEN and DONE. Its owner or the facilitator.

    Allowed at every stage, COMPLETE included: the tick box outlives the
    retrospective, because work agreed one week is finished in another. This is
    the one thing that stays writable after the text is frozen.
    """
    action = get_object_or_404(
        ActionItem.objects.select_related("retrospective__cycle__project", "owner"), pk=pk
    )
    retro = action.retrospective
    member_or_404(request.user, retro.cycle.project)
    if not can_update_action_item(request.user, action):
        raise PermissionDenied(
            "Only this action item's owner or the cycle's facilitator can tick it off."
        )

    action.status = (
        ActionItem.Status.OPEN
        if action.status == ActionItem.Status.DONE
        else ActionItem.Status.DONE
    )
    action.save(update_fields=["status"])
    messages.success(request, f"The action item is now {action.get_status_display().lower()}.")
    return redirect("retro-outcomes", pk=retro.pk)


# --------------------------------------------------------------------------
# The parts the outcome views share
# --------------------------------------------------------------------------


def _retro_for_member(request: HttpRequest, pk: int) -> Retrospective:
    """The retrospective, or the 404 a non-member earns — the same as an unused id."""
    retro = get_object_or_404(
        Retrospective.objects.select_related("cycle__project", "cycle__facilitator"), pk=pk
    )
    member_or_404(request.user, retro.cycle.project)
    return retro


def _decorated_action_items(request: HttpRequest, retro: Retrospective) -> list[ActionItem]:
    """This retrospective's action items, each carrying the two per-viewer flags.

    `can_edit` and `can_update` are attached here rather than computed in the
    template, the same way `card_section` attaches `can_edit`/`can_delete`: the
    template renders a control, and whether this viewer may use it is decided in
    `projects/permissions.py`.
    """
    items = list(retro.action_items.select_related("owner"))
    for item in items:
        item.can_edit = can_edit_action_item(request.user, item)
        item.can_update = can_update_action_item(request.user, item)
    return items


def _decorated_decisions(request: HttpRequest, retro: Retrospective) -> list[Decision]:
    decisions = list(retro.decisions.all())
    for decision in decisions:
        decision.can_edit = can_edit_decision(request.user, decision)
    return decisions


def _render_outcomes(
    request: HttpRequest,
    retro: Retrospective,
    *,
    decision_form: DecisionForm | None = None,
    action_item_form: ActionItemForm | None = None,
    status: int = 200,
) -> HttpResponse:
    """The outcomes page, with fresh create forms unless a rejected one is passed back."""
    project = retro.cycle.project
    context = {
        "retro": retro,
        "cycle": retro.cycle,
        "project": project,
        "decisions": _decorated_decisions(request, retro),
        "action_items": _decorated_action_items(request, retro),
        "decision_form": decision_form or DecisionForm(retrospective=retro),
        "action_item_form": action_item_form
        or ActionItemForm(retrospective=retro, project=project),
    }
    return render(request, "retro/outcomes.html", context, status=status)


def _render_decision_edit(
    request: HttpRequest, decision: Decision, form: DecisionForm, status: int = 200
) -> HttpResponse:
    retro = decision.retrospective
    return render(
        request,
        "retro/decision_edit.html",
        {"retro": retro, "cycle": retro.cycle, "project": retro.cycle.project, "form": form},
        status=status,
    )


def _render_action_item_edit(
    request: HttpRequest, action: ActionItem, form: ActionItemForm, status: int = 200
) -> HttpResponse:
    retro = action.retrospective
    return render(
        request,
        "retro/action_item_edit.html",
        {"retro": retro, "cycle": retro.cycle, "project": retro.cycle.project, "form": form},
        status=status,
    )
