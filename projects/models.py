import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.urls import reverse


class Project(models.Model):
    """A team's workspace. Everything else in the product hangs off one of these.

    Names are deliberately not unique: two teams may each run a project called
    "Platform" and neither has a claim on the word.
    """

    name = models.CharField(max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_projects",
    )
    # The join link is the whole invitation mechanism, so the token is the only
    # secret involved. It is looked up on every join, hence the index, and
    # rotating it is what revokes the copies already shared.
    join_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("project-detail", args=[self.pk])

    def join_path(self) -> str:
        return reverse("join-project", args=[self.join_token])

    def rotate_join_token(self) -> uuid.UUID:
        """Give the project a new token, invalidating every link already shared."""
        self.join_token = uuid.uuid4()
        self.save(update_fields=["join_token"])
        return self.join_token


class Membership(models.Model):
    """Who is on a project, and what they are by default.

    `role` is the project-level default that a new feedback cycle copies onto
    its facilitator. It does not by itself authorize anything about a
    retrospective — that authority is per cycle.
    """

    class Role(models.TextChoices):
        MEMBER = "MEMBER", "Member"
        FACILITATOR = "FACILITATOR", "Facilitator"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["joined_at", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # A second membership row for the same person is not an edge case to
            # handle in a view, it is a state the database refuses to hold.
            models.UniqueConstraint(
                fields=["project", "user"],
                name="projects_membership_unique_project_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.project} as {self.get_role_display()}"
