"""Retrospective views.

They are thin on purpose. Who may act is decided in `projects/permissions.py`
and what a transition does lives in `retro/services.py`; a view's whole job is
to find the row, refuse the people who may not see it, call the service, and
turn its rejection into a response. No view assigns `stage`.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from board.serializers import vote_totals
from cycles.models import Card, FeedbackCycle, revealed_cards
from meetings.services import upload_is_open
from projects.permissions import (
    can_advance_stage,
    can_confirm_extraction,
    can_delete_action_item,
    can_delete_decision,
    can_edit_action_item,
    can_edit_decision,
    can_update_action_item,
    can_upload_recording,
    can_view_summary,
)
from projects.views import member_or_404
from retro.forms import ActionItemForm, DecisionForm, ExtractionSummaryForm, ReviewOwnerForm
from retro.models import ActionItem, Cluster, Decision, Retrospective
from retro.services import (
    StageError,
    advance_stage,
    start_retrospective,
)

User = get_user_model()


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
            # The review screen is this cycle's facilitator's alone (#24). The
            # link is a courtesy; the screen refuses anyone who reaches it without
            # being shown it.
            "can_review": can_confirm_extraction(request.user, retro),
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

    # Advancing to COMPLETE discards any draft nobody reviewed (the `_on_complete`
    # hook does the deleting, inside the transaction). Before that happens the
    # facilitator is told how many will go and asked to confirm, so a draft is
    # never thrown away silently. Only someone who could advance is shown the
    # count; a plain member falls through to advance_stage(), which refuses them.
    if (
        retro.next_stage == Retrospective.Stage.COMPLETE
        and can_advance_stage(request.user, retro)
        and not request.POST.get("confirm_discard")
    ):
        draft_count = _outstanding_draft_count(retro)
        if draft_count:
            return render(
                request,
                "retro/confirm_complete.html",
                {
                    "retro": retro,
                    "cycle": retro.cycle,
                    "project": retro.cycle.project,
                    "draft_count": draft_count,
                },
            )

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
    # CONFIRMED only: a draft #23 extracted and nobody has reviewed appears on the
    # review screen alone (#24), never here on the record every member reads. A
    # hand-written item and an accepted draft are both CONFIRMED and both shown.
    items = list(
        retro.action_items.filter(review_status=ActionItem.ReviewStatus.CONFIRMED).select_related(
            "owner"
        )
    )
    for item in items:
        item.can_edit = can_edit_action_item(request.user, item)
        item.can_update = can_update_action_item(request.user, item)
    return items


def _decorated_decisions(request: HttpRequest, retro: Retrospective) -> list[Decision]:
    # CONFIRMED only, for the same reason as the action items above: an unreviewed
    # draft is invisible everywhere but the review screen.
    decisions = list(retro.decisions.filter(status=Decision.Status.CONFIRMED))
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


# --------------------------------------------------------------------------
# Draft review and confirmation — #24
#
# The facilitator reviews the drafts #23 extracted before any of them reaches the
# team's record: each is accepted (promoted to CONFIRMED), edited then accepted,
# or rejected (deleted). A draft action item #23 could not assign shows an owner
# dropdown of project members, picked here.
#
# Only this cycle's facilitator may open the screen or act on it — the whole of
# that rule is `can_confirm_extraction` from `projects/permissions.py` (#6), and
# this issue adds no access rule of its own. A member, a non-member and an
# anonymous user all get 404, the "not told it exists" answer the rest of the
# project uses: `_review_retro_or_404` asks the predicate and raises Http404 on
# False, and the predicate is False for an anonymous or deactivated user, so the
# screen never has to special-case one.
#
# Promotion changes `review_status`/`status` only and never rewrites `source`: an
# accepted draft stays `EXTRACTED`, so where it came from is still on the record.
# A row that was rejected or already accepted by someone else is answered with a
# readable message and a redirect, never a 500 — the lookup is scoped to the
# retrospective and to a still-`DRAFT` row, and a miss is a message rather than a
# 404 the reviewer cannot read.
#
# This screen never touches a card: a draft points at a cluster (an integer id)
# or nothing, names an owner or nobody, and carries an excerpt of the transcript.
# No card author, no `Card.pk`, no anonymity flag is reachable here —
# `_docs/decisions.md` items 9 and 10 are about `Card`, and none of these is one.
# --------------------------------------------------------------------------


def _review_retro_or_404(request: HttpRequest, pk: int) -> Retrospective:
    """The retrospective, if this user may confirm its extraction; otherwise 404.

    No `login_required`: the answer to a member, a non-member and an anonymous
    user is the same 404, because `can_confirm_extraction` is False for every one
    of them and the screen is not told apart from an id that was never used. Only
    this cycle's facilitator gets past here.
    """
    retro = get_object_or_404(
        Retrospective.objects.select_related("cycle__project", "cycle__facilitator"), pk=pk
    )
    if not can_confirm_extraction(request.user, retro):
        raise Http404("No such retrospective to review.")
    return retro


def _draft_decisions(retro: Retrospective) -> list[Decision]:
    return list(
        retro.decisions.filter(
            source=Decision.Source.EXTRACTED, status=Decision.Status.DRAFT
        ).select_related("cluster")
    )


def _draft_action_items(retro: Retrospective) -> list[ActionItem]:
    return list(
        retro.action_items.filter(
            source=ActionItem.Source.EXTRACTED, review_status=ActionItem.ReviewStatus.DRAFT
        ).select_related("cluster", "owner")
    )


def _outstanding_draft_count(retro: Retrospective) -> int:
    """How many extracted drafts are still in review — decisions and action items."""
    return (
        retro.decisions.filter(
            source=Decision.Source.EXTRACTED, status=Decision.Status.DRAFT
        ).count()
        + retro.action_items.filter(
            source=ActionItem.Source.EXTRACTED, review_status=ActionItem.ReviewStatus.DRAFT
        ).count()
    )


def retro_review(request: HttpRequest, pk: int) -> HttpResponse:
    """The draft decisions and action items of one retrospective, for the facilitator."""
    retro = _review_retro_or_404(request, pk)
    return _render_review(request, retro)


@require_POST
def review_accept_all(request: HttpRequest, pk: int) -> HttpResponse:
    """Confirm every outstanding draft at once, unassigned items included.

    An item still missing an owner is accepted unassigned rather than blocked —
    an unowned action is better than a wrongly-owned one — so this promotes and
    resolves no owners. `source` is untouched; only the review state moves.
    """
    retro = _review_retro_or_404(request, pk)
    decisions = retro.decisions.filter(
        source=Decision.Source.EXTRACTED, status=Decision.Status.DRAFT
    ).update(status=Decision.Status.CONFIRMED)
    actions = retro.action_items.filter(
        source=ActionItem.Source.EXTRACTED, review_status=ActionItem.ReviewStatus.DRAFT
    ).update(review_status=ActionItem.ReviewStatus.CONFIRMED)
    if decisions or actions:
        messages.success(
            request,
            f"Confirmed {decisions} decision(s) and {actions} action item(s). "
            "Any still without an owner were kept unassigned.",
        )
    else:
        messages.info(request, "There were no drafts left to confirm.")
    return redirect("retro-review", pk=retro.pk)


@require_POST
def review_decision_accept(request: HttpRequest, pk: int, decision_pk: int) -> HttpResponse:
    """Promote one draft decision to CONFIRMED. Its `source` stays EXTRACTED."""
    retro = _review_retro_or_404(request, pk)
    decision = _draft_decision_or_message(request, retro, decision_pk)
    if decision is None:
        return redirect("retro-review", pk=retro.pk)

    decision.status = Decision.Status.CONFIRMED
    decision.save(update_fields=["status"])
    messages.success(request, "The decision has been confirmed.")
    return redirect("retro-review", pk=retro.pk)


@require_POST
def review_decision_reject(request: HttpRequest, pk: int, decision_pk: int) -> HttpResponse:
    """Discard one draft decision. There is no rejected-drafts archive."""
    retro = _review_retro_or_404(request, pk)
    decision = _draft_decision_or_message(request, retro, decision_pk)
    if decision is None:
        return redirect("retro-review", pk=retro.pk)

    decision.delete()
    messages.success(request, "The draft decision has been discarded.")
    return redirect("retro-review", pk=retro.pk)


def review_decision_edit(request: HttpRequest, pk: int, decision_pk: int) -> HttpResponse:
    """Re-word one draft decision, then accept it. Edited-then-accepted is CONFIRMED.

    An edited draft is indistinguishable from a plainly accepted one afterwards:
    the save both writes the new text and promotes the row, so `source` stays
    EXTRACTED and `status` becomes CONFIRMED, exactly as a bare accept leaves it.
    """
    retro = _review_retro_or_404(request, pk)
    decision = _draft_decision_or_message(request, retro, decision_pk)
    if decision is None:
        return redirect("retro-review", pk=retro.pk)

    if request.method != "POST":
        form = DecisionForm(instance=decision, retrospective=retro)
        return _render_review_decision_edit(request, decision, form)

    form = DecisionForm(request.POST, instance=decision, retrospective=retro)
    if not form.is_valid():
        return _render_review_decision_edit(request, decision, form, status=400)

    edited = form.save(commit=False)
    edited.status = Decision.Status.CONFIRMED
    edited.save()
    messages.success(request, "The decision has been edited and confirmed.")
    return redirect("retro-review", pk=retro.pk)


@require_POST
def review_action_item_accept(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    """Promote one draft action item, picking an owner if the dropdown carried one.

    The owner posted from the dropdown must be a project member; anything else is a
    validation error, not a stored row (`ReviewOwnerForm`). An empty choice accepts
    the item unassigned — displayed, never blocked. `source` is untouched; only the
    owner and the review state move.
    """
    retro = _review_retro_or_404(request, pk)
    action = _draft_action_item_or_message(request, retro, item_pk)
    if action is None:
        return redirect("retro-review", pk=retro.pk)

    form = ReviewOwnerForm(request.POST, project=retro.cycle.project)
    if not form.is_valid():
        messages.error(request, "The owner has to be a member of this project.")
        return redirect("retro-review", pk=retro.pk)

    owner = form.cleaned_data["owner"]
    if owner is not None:
        action.owner = owner
    action.review_status = ActionItem.ReviewStatus.CONFIRMED
    action.save(update_fields=["owner", "review_status"])
    messages.success(request, "The action item has been confirmed.")
    return redirect("retro-review", pk=retro.pk)


@require_POST
def review_action_item_reject(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    """Discard one draft action item. No archive."""
    retro = _review_retro_or_404(request, pk)
    action = _draft_action_item_or_message(request, retro, item_pk)
    if action is None:
        return redirect("retro-review", pk=retro.pk)

    action.delete()
    messages.success(request, "The draft action item has been discarded.")
    return redirect("retro-review", pk=retro.pk)


def review_action_item_edit(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    """Re-word one draft action item — text, owner, due date — then accept it.

    The owner is validated against the roster by the form, so an owner off the
    project is a validation error here too. The save both writes the edits and
    promotes the row to CONFIRMED, leaving `source` EXTRACTED, so an
    edited-then-accepted item behaves like any other accepted one.
    """
    retro = _review_retro_or_404(request, pk)
    action = _draft_action_item_or_message(request, retro, item_pk)
    if action is None:
        return redirect("retro-review", pk=retro.pk)

    project = retro.cycle.project
    if request.method != "POST":
        form = ActionItemForm(instance=action, retrospective=retro, project=project)
        return _render_review_action_item_edit(request, action, form)

    form = ActionItemForm(request.POST, instance=action, retrospective=retro, project=project)
    if not form.is_valid():
        return _render_review_action_item_edit(request, action, form, status=400)

    edited = form.save(commit=False)
    edited.review_status = ActionItem.ReviewStatus.CONFIRMED
    edited.save()
    messages.success(request, "The action item has been edited and confirmed.")
    return redirect("retro-review", pk=retro.pk)


# --------------------------------------------------------------------------
# The extracted meeting summary — reviewed here, gated on the summary page (#25)
#
# #23 writes `Retrospective.extraction_summary` as a DRAFT, exactly as it writes
# draft decisions and action items. Until the facilitator confirms it here, the
# summary screen (#25) does not show it — an unreviewed AI paragraph must not
# publish itself to the team's record the moment extraction runs. Confirming, and
# editing-then-confirming, mirror the draft decision flow above: only this cycle's
# facilitator reaches them (`_review_retro_or_404`, the same
# `can_confirm_extraction` gate #24 uses), and a member or a non-member gets the
# 404 an unused id earns.
# --------------------------------------------------------------------------


@require_POST
def review_summary_confirm(request: HttpRequest, pk: int) -> HttpResponse:
    """Confirm the extracted meeting summary as it stands. This cycle's facilitator."""
    retro = _review_retro_or_404(request, pk)
    retro.extraction_summary_confirmed = True
    retro.save(update_fields=["extraction_summary_confirmed"])
    messages.success(request, "The meeting summary has been confirmed.")
    return redirect("retro-review", pk=retro.pk)


def review_summary_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Re-word the extracted meeting summary, then confirm it. Facilitator only.

    The save both writes the new text and confirms it, so an edited-then-confirmed
    summary is indistinguishable from a plainly confirmed one afterwards — the same
    shape as an edited-then-accepted decision.
    """
    retro = _review_retro_or_404(request, pk)
    if request.method != "POST":
        form = ExtractionSummaryForm(instance=retro)
        return _render_review_summary_edit(request, retro, form)

    form = ExtractionSummaryForm(request.POST, instance=retro)
    if not form.is_valid():
        return _render_review_summary_edit(request, retro, form, status=400)

    edited = form.save(commit=False)
    edited.extraction_summary_confirmed = True
    edited.save(update_fields=["extraction_summary", "extraction_summary_confirmed"])
    messages.success(request, "The meeting summary has been edited and confirmed.")
    return redirect("retro-review", pk=retro.pk)


def _draft_decision_or_message(
    request: HttpRequest, retro: Retrospective, decision_pk: int
) -> Decision | None:
    """The still-draft extracted decision, or None with a readable message set.

    A row someone else just rejected, or already accepted, is gone from the set of
    drafts: this returns None and leaves a message rather than a 404 or a 500, so
    acting on a stale screen reads as "already handled" instead of an error.
    """
    decision = retro.decisions.filter(
        pk=decision_pk, source=Decision.Source.EXTRACTED, status=Decision.Status.DRAFT
    ).first()
    if decision is None:
        messages.info(request, "That draft has already been reviewed or removed.")
    return decision


def _draft_action_item_or_message(
    request: HttpRequest, retro: Retrospective, item_pk: int
) -> ActionItem | None:
    action = retro.action_items.filter(
        pk=item_pk, source=ActionItem.Source.EXTRACTED, review_status=ActionItem.ReviewStatus.DRAFT
    ).first()
    if action is None:
        messages.info(request, "That draft has already been reviewed or removed.")
    return action


def _render_review(request: HttpRequest, retro: Retrospective) -> HttpResponse:
    """The review screen: the drafts, their excerpts, and the roster for the dropdowns."""
    project = retro.cycle.project
    context = {
        "retro": retro,
        "cycle": retro.cycle,
        "project": project,
        "draft_decisions": _draft_decisions(retro),
        "draft_action_items": _draft_action_items(retro),
        # The roster the owner dropdowns are built from — project members only,
        # so a draft can never be assigned to someone outside the project.
        "members": User.objects.filter(memberships__project=project).order_by("username"),
        "summary": retro.extraction_summary,
        "summary_confirmed": retro.extraction_summary_confirmed,
    }
    return render(request, "retro/review.html", context)


def _render_review_summary_edit(
    request: HttpRequest, retro: Retrospective, form: ExtractionSummaryForm, status: int = 200
) -> HttpResponse:
    return render(
        request,
        "retro/review_summary_edit.html",
        {"retro": retro, "cycle": retro.cycle, "project": retro.cycle.project, "form": form},
        status=status,
    )


def _render_review_decision_edit(
    request: HttpRequest, decision: Decision, form: DecisionForm, status: int = 200
) -> HttpResponse:
    retro = decision.retrospective
    return render(
        request,
        "retro/review_decision_edit.html",
        {"retro": retro, "cycle": retro.cycle, "project": retro.cycle.project, "form": form},
        status=status,
    )


def _render_review_action_item_edit(
    request: HttpRequest, action: ActionItem, form: ActionItemForm, status: int = 200
) -> HttpResponse:
    retro = action.retrospective
    return render(
        request,
        "retro/review_action_item_edit.html",
        {"retro": retro, "cycle": retro.cycle, "project": retro.cycle.project, "form": form},
        status=status,
    )


# --------------------------------------------------------------------------
# The retrospective summary — #25
#
# One page that is the retrospective's record: the discussion topics and their
# outcomes, the notes, the confirmed decisions and action items, the
# participation, and the feedback cards grouped by cluster. It is a single record
# that looks the same to every member who opens it — nothing on it varies with
# who is reading it.
#
# `_docs/decisions.md` item 10 governs the card list absolutely: a card is its
# category and its text and nothing else. No card carries an author (anonymous or
# attributed), nothing distinguishes an anonymous card from an attributed one, no
# count splits the cards by anonymity, and no card handle (`pk` or `public_id`)
# reaches the page. Cards come from `revealed_cards()` — `position` order, never
# `Card.Meta.ordering`'s submission order — and the card list lives in one
# `#feedback-cards` container the absence sweeps assert over. The page carries
# display names elsewhere — note authors, action owners, the participation list —
# so a name never appears *beside a card*, which is what the container makes
# assertable.
#
# The summary marks none of the viewer's own cards, unlike the board (#75): the
# cards are frozen and quoted rather than moved, so the mark would buy nothing and
# would cost the one property this page needs — that it is one record, identical
# for everyone. No `mine` here.
#
# No `login_required`: a member, a non-member and an anonymous visitor are told
# apart by `can_view_summary` alone, which is False for the last two, so all three
# non-members get the same 404 an unused id would — the answer the rest of the
# project uses, rather than a login redirect that confirms the page exists.
# --------------------------------------------------------------------------


def _agenda_clusters(retro: Retrospective) -> tuple[list[Cluster], dict[int, int]]:
    """This retrospective's clusters in agenda order, and the vote weight of each.

    Agenda order is #16's, and the same rule `board/serializers.py` renders the
    board with from DISCUSS on: highest vote weight first, ties broken by
    `position` then `id`, so it is a total order that does not reshuffle and a
    topic with no votes falls to the bottom rather than being hidden. The weights
    come from `vote_totals()`, the one definition the board already uses, so the
    summary's agenda cannot drift from the board's. The summary exists only from
    DISCUSS on, where the allocation is frozen, so reading the totals here reveals
    no member's individual vote — it is the aggregate weight per topic and nothing
    about who voted for what.
    """
    totals = vote_totals(retro)
    clusters = sorted(
        retro.clusters.all(),
        key=lambda cluster: (-totals.get(cluster.pk, 0), cluster.position, cluster.pk),
    )
    return clusters, totals


def _card_groups(retro: Retrospective, ordered_clusters: list[Cluster]) -> tuple[list[dict], list]:
    """The revealed cards grouped by cluster in agenda order, plus the ungrouped.

    Cards come from `revealed_cards()` and nothing else — `position` order, which
    is the shuffled order the reveal handed out, never `Card.Meta.ordering`'s
    `created_at`/`id` submission order that #10's shuffle exists to destroy. The
    groups follow the same agenda order as the topics above, so a reader meets each
    topic once; a card carries no author, no anonymity flag and no handle, and this
    function reads none — it buckets by `cluster_id`, the column already on the
    row. The ungrouped cards are returned separately so the template can render
    them last, in a group of their own.
    """
    buckets: dict[int, list] = {cluster.pk: [] for cluster in ordered_clusters}
    ungrouped: list = []
    for card in revealed_cards(retro.cycle):
        if card.cluster_id is None:
            ungrouped.append(card)
        else:
            buckets.setdefault(card.cluster_id, []).append(card)
    groups = [
        {"name": cluster.name, "cards": buckets[cluster.pk]}
        for cluster in ordered_clusters
        if buckets[cluster.pk]
    ]
    return groups, ungrouped


def _note_groups(retro: Retrospective, ordered_clusters: list[Cluster]) -> tuple[list[dict], list]:
    """The notes grouped under their topic in agenda order, plus the retro-wide ones.

    A note is always attributed — `_docs/decisions.md` item 10 says so in its
    scope: a note has no anonymous alternative, so naming its author eliminates
    nobody. The notes render in their own region, never inside the card list, so a
    name never appears beside a card. A note against no cluster is about the
    retrospective as a whole and is returned separately.
    """
    notes = list(retro.notes.select_related("author"))
    groups = [
        {
            "name": cluster.name,
            "notes": [note for note in notes if note.cluster_id == cluster.pk],
        }
        for cluster in ordered_clusters
    ]
    groups = [group for group in groups if group["notes"]]
    retro_wide = [note for note in notes if note.cluster_id is None]
    return groups, retro_wide


def _participation(retro: Retrospective) -> tuple[list, list]:
    """The project's members split into submitted and did-not-submit, by name.

    Read from `CycleParticipation`, the only surviving record of who took part:
    it holds a row for everyone who was a member when the cycle was revealed,
    including anyone who has since left while their cards are still on the page.
    Only the yes/no is read — no `card_count` beside a name (`_docs/decisions.md`
    item 3a) and no `submitted_at`, day-truncated or otherwise — and the two lists
    are sorted by display name so the record reads the same for everyone.
    """
    rows = list(retro.cycle.participation.select_related("user"))
    rows.sort(key=lambda row: (row.user.display_name or row.user.username).lower())
    submitted = [row.user for row in rows if row.submitted]
    did_not_submit = [row.user for row in rows if not row.submitted]
    return submitted, did_not_submit


def retro_summary(request: HttpRequest, pk: int) -> HttpResponse:
    """The retrospective's record, readable by every project member.

    Available from DISCUSS on — before then there is no agenda to summarise, so a
    request for it is answered the way an unused id is. A non-member and an
    anonymous visitor get the same 404, from `can_view_summary` alone.

    No `login_required`: a member, a non-member and an anonymous visitor are all
    answered the same 404 by `can_view_summary`, which is False for the last two,
    rather than a login redirect that would confirm the page exists — the answer
    `_review_retro_or_404` gives for the same reason.
    """
    retro = get_object_or_404(
        Retrospective.objects.select_related("cycle__project", "cycle__facilitator"), pk=pk
    )
    if not can_view_summary(request.user, retro):
        raise Http404("No such retrospective summary.")
    # The summary is a live view from DISCUSS and the final record at COMPLETE;
    # before DISCUSS there is no agenda, no discussion and no outcome to show, so
    # it is not told apart from an id that was never used.
    if not retro.has_reached(Retrospective.Stage.DISCUSS):
        raise Http404("This retrospective has no summary yet.")

    ordered_clusters, totals = _agenda_clusters(retro)
    topics = [
        {
            "name": cluster.name,
            "weight": totals.get(cluster.pk, 0),
            "outcome": cluster.get_status_display(),
        }
        for cluster in ordered_clusters
    ]
    card_groups, ungrouped_cards = _card_groups(retro, ordered_clusters)
    note_groups, retro_wide_notes = _note_groups(retro, ordered_clusters)
    submitted, did_not_submit = _participation(retro)

    # Every query filters to confirmed rows at the queryset level, not in the
    # template: a DRAFT decision or action item #23 extracted and nobody reviewed
    # is invisible here, exactly as it is on #17's outcomes list.
    decisions = list(retro.decisions.filter(status=Decision.Status.CONFIRMED))
    action_items = list(
        retro.action_items.filter(review_status=ActionItem.ReviewStatus.CONFIRMED).select_related(
            "owner"
        )
    )

    # Team-wide totals a reader could reach by counting what the page already
    # shows — how many cards, how many of each category, how many submitted and how
    # many did not — and nothing that splits the cards any other way. No anonymity
    # count: `_docs/decisions.md` item 10 with item 3a.
    all_cards = [card for group in card_groups for card in group["cards"]] + ungrouped_cards
    category_counts = [
        {"label": label, "count": sum(1 for card in all_cards if card.category == value)}
        for value, label in Card.Category.choices
    ]

    # The extracted meeting summary appears only once the facilitator has confirmed
    # it on the review screen (#24). #23 writes it as a draft, so an unconfirmed one
    # is withheld exactly as an unconfirmed draft decision is — the record shows
    # only what a person has reviewed.
    summary_text = retro.extraction_summary if retro.extraction_summary_confirmed else ""
    recorded_anything = bool(
        topics
        or note_groups
        or retro_wide_notes
        or decisions
        or action_items
        or all_cards
        or summary_text
    )

    return render(
        request,
        "retro/summary.html",
        {
            "retro": retro,
            "cycle": retro.cycle,
            "project": retro.cycle.project,
            "is_complete": retro.is_complete,
            "summary_text": summary_text,
            "topics": topics,
            "note_groups": note_groups,
            "retro_wide_notes": retro_wide_notes,
            "decisions": decisions,
            "action_items": action_items,
            "submitted": submitted,
            "did_not_submit": did_not_submit,
            "card_groups": card_groups,
            "ungrouped_cards": ungrouped_cards,
            "total_cards": len(all_cards),
            "category_counts": category_counts,
            "submitted_count": len(submitted),
            "did_not_submit_count": len(did_not_submit),
            "recorded_anything": recorded_anything,
        },
    )
