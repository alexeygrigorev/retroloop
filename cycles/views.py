"""Feedback cycle views.

The access rules live at the top of this module as one-line predicates, in the
shape `projects/views.py` already uses. Issue #6 lifts `can_open_cycle` and
`can_close_cycle` into `projects/permissions.py` unchanged and deletes them from
here; until then this module is the single place that decides who may open or
close a cycle. Templates only ever hide what a view already refuses.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from cycles.forms import FeedbackCycleForm
from cycles.models import FeedbackCycle, monday_of
from projects.models import Project
from projects.views import is_facilitator, member_or_404

# --------------------------------------------------------------------------
# Rules. One condition each, so #6 can lift them out as they are.
# --------------------------------------------------------------------------


def can_open_cycle(user, project: Project) -> bool:
    return project.owner_id == user.pk or is_facilitator(user, project)


def can_close_cycle(user, cycle: FeedbackCycle) -> bool:
    return cycle.facilitator_id == user.pk and cycle.status == FeedbackCycle.Status.COLLECTING


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@login_required
def cycle_create(request: HttpRequest, project_pk: int) -> HttpResponse:
    project = get_object_or_404(Project, pk=project_pk)
    member_or_404(request.user, project)
    if not can_open_cycle(request.user, project):
        raise PermissionDenied

    now = timezone.now()
    form = FeedbackCycleForm(
        request.POST or None,
        project=project,
        initial={
            "week_start": monday_of(timezone.localdate()),
            "opens_at": now,
            # A working week later, as a starting point the facilitator edits.
            # It is a deadline the team is shown, not a trigger: see cycle_close.
            "closes_at": now + timedelta(days=5),
            "facilitator": request.user,
        },
    )
    if request.method == "POST" and form.is_valid():
        cycle = form.save(commit=False)
        cycle.project = project
        cycle.save()
        messages.success(
            request,
            f"The cycle for the week of {date_format(cycle.week_start, 'j F Y')} is open.",
        )
        return redirect(cycle)

    return render(request, "cycles/cycle_form.html", {"form": form, "project": project})


@login_required
def cycle_detail(request: HttpRequest, pk: int) -> HttpResponse:
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project", "facilitator"), pk=pk)
    # A cycle is as private as its project, and answers a non-member the same
    # way an id that was never used does.
    member_or_404(request.user, cycle.project)

    return render(
        request,
        "cycles/cycle_detail.html",
        {
            "cycle": cycle,
            "project": cycle.project,
            "can_close": can_close_cycle(request.user, cycle),
        },
    )


@login_required
@require_POST
def cycle_close(request: HttpRequest, pk: int) -> HttpResponse:
    """Close a cycle, on a person's decision and nothing else.

    Nothing here waits for everyone to have submitted — `_docs/decisions.md`
    item 4 — and nothing closes a cycle because `closes_at` has passed: there is
    no scheduler in the MVP, and a cycle past its deadline stays `COLLECTING`
    until a human acts. There is no reopening: `can_close_cycle` is false for a
    cycle that is already `CLOSED`, so a second attempt is refused here rather
    than merely hidden in the template.
    """
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project"), pk=pk)
    member_or_404(request.user, cycle.project)
    if not can_close_cycle(request.user, cycle):
        raise PermissionDenied

    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])
    messages.success(
        request,
        "The cycle is closed. Nothing more can be submitted to it, and it cannot be reopened.",
    )
    return redirect(cycle)
