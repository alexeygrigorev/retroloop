"""Project views.

The access rules live here as one-line predicates rather than inside the
queries or the templates. Issue #6 moves them into `projects/permissions.py`
unchanged; until then this module is the single place that decides who may see
or do what. Templates only ever hide what a view already refuses.
"""

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from projects.forms import ProjectForm
from projects.models import Membership, Project

# --------------------------------------------------------------------------
# Rules. One condition each, so #6 can lift them out as they are.
# --------------------------------------------------------------------------


def is_member(user, project: Project) -> bool:
    return Membership.objects.filter(project=project, user=user).exists()


def is_facilitator(user, project: Project) -> bool:
    return Membership.objects.filter(
        project=project, user=user, role=Membership.Role.FACILITATOR
    ).exists()


def can_rotate_join_token(user, project: Project) -> bool:
    return project.owner_id == user.pk or is_facilitator(user, project)


def member_or_404(user, project: Project) -> None:
    """Hide a project from everyone who is not on it.

    A non-member gets the same answer as someone guessing at an id that was
    never used, because 403 would confirm that the project exists.
    """
    if not is_member(user, project):
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


@login_required
def project_detail(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(Project, pk=pk)
    member_or_404(request.user, project)

    memberships = project.memberships.select_related("user")
    return render(
        request,
        "projects/project_detail.html",
        {
            "project": project,
            "memberships": memberships,
            "join_url": request.build_absolute_uri(project.join_path()),
            "can_rotate": can_rotate_join_token(request.user, project),
        },
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
