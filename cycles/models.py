"""The weekly feedback cycle.

A cycle is the container everything else in a week hangs off: the cards
submitted into it (#8) and the retrospective that follows it (#9). It has two
states and no more — it is collecting feedback, or it is closed — because a
richer lifecycle belongs to the retrospective's stage machine.

Two rules are held by the database rather than by a form, because a form check
only holds for the requests that go through that form:

- a project has at most one `COLLECTING` cycle, as a partial unique index;
- a project has at most one cycle per week.

`week_start` is a plain date and always a Monday; `opens_at` and `closes_at`
are timezone-aware datetimes. The two kinds are never compared with each other.
"""

from datetime import date, timedelta
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.urls import reverse

from projects.models import Project


def monday_of(day: date) -> date:
    """The Monday of the week `day` falls in.

    The week a cycle covers is identified by its Monday, so any other day the
    facilitator happens to pick names the same week and is stored as that
    Monday. Without this, two cycles for one week differ by a day and the
    unique constraint below never fires.
    """
    return day - timedelta(days=day.weekday())


class FeedbackCycle(models.Model):
    """One week of Start/Stop/Continue collection for one project."""

    class Status(models.TextChoices):
        COLLECTING = "COLLECTING", "Collecting"
        CLOSED = "CLOSED", "Closed"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="cycles",
    )
    week_start = models.DateField(
        help_text="Any day of the week you mean; it is stored as that week's Monday.",
    )
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField(
        help_text="The deadline the team is told about. Nothing closes the cycle but a person.",
    )
    # Per cycle, not per project: the plan allows handing the facilitator role
    # over for a given week, so this is not read off the Membership row.
    # PROTECT rather than CASCADE, because a cycle belongs to the team, not to
    # whoever ran it — removing a person must not take the team's weeks with it.
    facilitator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="facilitated_cycles",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COLLECTING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-week_start", "-id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # A partial unique index, so a second open cycle is a state the
            # database refuses to hold rather than a race a view has to win.
            # Closed cycles are exempt: a project accumulates as many as it has
            # had weeks.
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(status="COLLECTING"),
                name="cycles_feedbackcycle_one_collecting_per_project",
            ),
            models.UniqueConstraint(
                fields=["project", "week_start"],
                name="cycles_feedbackcycle_unique_project_week",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project} — week of {self.week_start}"

    def save(self, *args, **kwargs) -> None:
        # Normalising here and not only in the form is what makes "week_start is
        # always a Monday" true of the table rather than true of one code path.
        if self.week_start is not None:
            self.week_start = monday_of(self.week_start)
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("cycle-detail", args=[self.pk])

    @property
    def is_collecting(self) -> bool:
        return self.status == self.Status.COLLECTING

    @property
    def accepts_cards(self) -> bool:
        """Whether a card may be created, edited or deleted in this cycle.

        The cycle's status is the whole rule. #8 builds the card screens and
        asks this question before it writes anything; #6 lifts the check into
        `projects/permissions.py` with the rest.
        """
        return self.is_collecting
