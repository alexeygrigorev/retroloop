"""Project views.

Who may do what is decided in `projects/permissions.py` and asked here. This
module holds the enforcement — the 404s and the 403s — and the templates only
ever hide what a view already refuses.
"""

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from cycles.models import Card, FeedbackCycle
from projects.forms import ProjectForm
from projects.models import Membership, Project
from projects.permissions import (
    can_open_cycle,
    can_rotate_join_token,
    can_update_action_item,
    can_view_project,
)
from retro.models import ActionItem, Retrospective

# --------------------------------------------------------------------------
# Enforcement. Not a rule: it raises, so it does not belong in permissions.py.
# --------------------------------------------------------------------------


def member_or_404(user, project: Project) -> None:
    """Hide a project from everyone who is not on it.

    A non-member gets the same answer as someone guessing at an id that was
    never used, because 403 would confirm that the project exists. The
    condition is `can_view_project`; only the refusal lives here.
    """
    if not can_view_project(user, project):
        raise Http404


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@login_required
def project_list(request: HttpRequest) -> HttpResponse:
    projects = Project.objects.filter(memberships__user=request.user)
    return render(request, "projects/project_list.html", {"projects": projects})


@login_required
def project_create(request: HttpRequest) -> HttpResponse:
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # The creator's membership is part of creating the project, not a
            # step after it: there is no project whose owner is not a member.
            project = form.save(commit=False)
            project.owner = request.user
            project.save()
            Membership.objects.create(
                project=project,
                user=request.user,
                role=Membership.Role.FACILITATOR,
            )
        return redirect(project)
    return render(request, "projects/project_form.html", {"form": form})


# --------------------------------------------------------------------------
# The project dashboard — #26
#
# The project page answers one question on sight: what do I need to do this
# week? It sits above the per-cycle screens and is a set of live queries, never a
# denormalised copy — `_docs/decisions.md` item 5 for the open action items, and
# item 3a for the submission status, which is a yes/no per member and never a
# count. `CycleParticipation.card_count` is not read here and not written until
# reveal; the current cycle is COLLECTING, so its submission status comes from a
# live existence check against `Card` and nothing else.
#
# Every panel is one query whose cost does not grow with the number of past
# cycles, retrospectives or action items: the lists are fetched with one query
# each and select_related everything they render, so a project with thirty weeks
# behind it costs the same number of queries as one with a single week. The
# per-item flags (`is_mine`, `can_update`) read ids already loaded and issue no
# query of their own.
# --------------------------------------------------------------------------


def _submitted_user_ids(cycle: FeedbackCycle) -> set[int]:
    """Who has submitted at least one card to `cycle`, as a set of user ids.

    One query, and a yes/no per member — never a count. During COLLECTING a
    card still carries its author (reveal is what nulls it), so the distinct set
    of `author_id` is exactly "who submitted and who did not", which is all item
    3a lets a screen show. No `card_count` is read: the ORM never selects it, so
    it cannot reach the page.
    """
    return set(Card.objects.filter(cycle=cycle).values_list("author_id", flat=True).distinct())


def _open_actions(project: Project, user) -> list[ActionItem]:
    """Every open, confirmed action item across the project's retrospectives.

    A live query — `_docs/decisions.md` item 5 — never copied rows: ticking an
    item done in any retrospective removes it from this list because the filter
    is `status=OPEN` read at render time. Draft rows #23 extracted and nobody
    confirmed stay invisible, exactly as on #17's outcomes list. One query,
    select_related down to the cycle and the owner, so the owner, the due date
    and which retrospective an item came from all render without a per-item
    query; `is_mine` and `can_update` read only ids already loaded.
    """
    items = list(
        ActionItem.objects.filter(
            retrospective__cycle__project=project,
            status=ActionItem.Status.OPEN,
            review_status=ActionItem.ReviewStatus.CONFIRMED,
        )
        .select_related("owner", "retrospective__cycle")
        # Soonest due first (a null due date sorts last in ascending order),
        # then by retrospective and description for a stable, total order. The
        # tie-breakers are named fields and never the row's own sequence — this
        # module mentions `Card`, and a bare `id`/`pk` ordering is what
        # `_docs/decisions.md` item 9's guard forbids here.
        .order_by("due_date", "retrospective_id", "description")
    )
    # `is_mine` is a display mark — which items are the viewer's own, so they can
    # be told from everyone else's — not an access decision. Whether the viewer
    # may act on an item is `can_update_action_item`, asked below and living in
    # `projects/permissions.py`. The mark is a set membership rather than an
    # identity comparison, because the one file that decides who someone is is the
    # permissions module, and `test_permissions.py` keeps such comparisons there.
    own_owner_ids = {user.pk}
    for item in items:
        item.is_mine = item.owner_id in own_owner_ids
        item.can_update = can_update_action_item(user, item)
    return items


@login_required
def project_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """The project dashboard: this week's cycle, the retrospectives, open actions."""
    project = get_object_or_404(Project, pk=pk)
    member_or_404(request.user, project)

    memberships = list(project.memberships.select_related("user"))

    # The one open cycle, if there is one. At most one is COLLECTING per project
    # (a partial unique index holds it), so this is `.first()` and not a list.
    open_cycle = (
        project.cycles.filter(status=FeedbackCycle.Status.COLLECTING)
        .select_related("facilitator")
        .first()
    )

    # Submission status for the open cycle, as a yes/no per member. Derived from
    # a live existence check — item 3a — and never a count.
    member_submissions = None
    viewer_submitted = False
    if open_cycle is not None:
        submitted_ids = _submitted_user_ids(open_cycle)
        member_submissions = [
            {"user": membership.user, "submitted": membership.user_id in submitted_ids}
            for membership in memberships
        ]
        viewer_submitted = request.user.pk in submitted_ids

    # The active retrospective is the one that has not completed. Its link lands
    # the viewer on retro-detail, which the React island renders as the board
    # (CLUSTER, VOTE) or the discussion (DISCUSS) for the stage it is in; a
    # completed retrospective is not "active" and appears below with a summary
    # link instead.
    active_retro = (
        Retrospective.objects.filter(cycle__project=project)
        .exclude(stage=Retrospective.Stage.COMPLETE)
        .select_related("cycle")
        .order_by("-cycle__week_start", "-cycle__id")
        .first()
    )
    previous_retros = list(
        Retrospective.objects.filter(cycle__project=project, stage=Retrospective.Stage.COMPLETE)
        .select_related("cycle")
        .order_by("-cycle__week_start", "-cycle__id")
    )

    return render(
        request,
        "projects/project_detail.html",
        {
            "project": project,
            "memberships": memberships,
            "join_url": request.build_absolute_uri(project.join_path()),
            "can_rotate": can_rotate_join_token(request.user, project),
            # Most recent week first; the model's ordering says so.
            "cycles": project.cycles.select_related("facilitator"),
            "can_open_cycle": can_open_cycle(request.user, project),
            # Dashboard sections.
            "open_cycle": open_cycle,
            "member_submissions": member_submissions,
            "viewer_submitted": viewer_submitted,
            "active_retro": active_retro,
            "previous_retros": previous_retros,
            "open_actions": _open_actions(project, request.user),
        },
    )


@login_required
@require_POST
def dashboard_action_toggle(request: HttpRequest, pk: int, item_pk: int) -> HttpResponse:
    """Tick one open action item done (or back to open) from the dashboard.

    The tick-done interaction #26 names, going through `can_update_action_item`
    from #17 — the owner or this cycle's facilitator, at any stage, COMPLETE
    included, because work agreed one week is finished in another. A plain member
    who owns nothing here gets a 403; a non-member gets the project's 404. The
    action item is scoped to this project, so an id from another project is a 404
    rather than a cross-project tick.

    It answers with the open-actions fragment, so a done item drops out of the
    live list in place without reloading the page.
    """
    project = get_object_or_404(Project, pk=pk)
    member_or_404(request.user, project)

    action = get_object_or_404(
        ActionItem.objects.select_related("retrospective__cycle__project", "owner"),
        pk=item_pk,
        retrospective__cycle__project=project,
    )
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

    return render(
        request,
        "projects/project_detail.html#open_actions",
        {"project": project, "open_actions": _open_actions(project, request.user)},
    )


@login_required
def join_project(request: HttpRequest, token: uuid.UUID) -> HttpResponse:
    """Turn a link into a membership.

    An unknown token — never issued, or replaced by a rotation — is a 404 that
    says nothing about whether it ever worked.
    """
    project = get_object_or_404(Project, join_token=token)

    # get_or_create, so a second visit neither duplicates the row nor demotes a
    # facilitator back to member. A join never grants FACILITATOR.
    _membership, created = Membership.objects.get_or_create(
        project=project,
        user=request.user,
        defaults={"role": Membership.Role.MEMBER},
    )
    if created:
        messages.success(request, f"You have joined {project.name}.")
    else:
        messages.info(request, f"You are already a member of {project.name}.")
    return redirect(project)


@login_required
@require_POST
def rotate_join_token(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(Project, pk=pk)
    member_or_404(request.user, project)
    if not can_rotate_join_token(request.user, project):
        raise PermissionDenied

    project.rotate_join_token()
    messages.success(
        request,
        "The join link has been replaced. Every copy of the old link has stopped working.",
    )
    return redirect(project)
