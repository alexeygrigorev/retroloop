"""The retrospective that follows a feedback cycle.

The row is deliberately thin. It holds where the retrospective is (`stage`),
when it started and finished, how many votes each member gets, and a `version`
counter that is the whole of the board sync mechanism used by #11 and #12.

Two things are *not* here on purpose:

- the stage machine. `advance_stage()` lives in `retro/services.py`, because a
  transition is a transaction with side effects and a lock, not an assignment.
  Nothing else may write `stage`;
- any behaviour that depends on cards. Cards arrive with #8; this module never
  mentions them.
"""

from typing import ClassVar

from django.db import models
from django.urls import reverse

from cycles.models import FeedbackCycle


class Retrospective(models.Model):
    """One retrospective, following one feedback cycle, moving through stages."""

    class Stage(models.TextChoices):
        # Declaration order *is* the stage order — STAGE_ORDER below is derived
        # from it, so the two can never drift apart.
        DRAFT = "DRAFT", "Draft"
        REVEAL = "REVEAL", "Reveal"
        CLUSTER = "CLUSTER", "Cluster"
        VOTE = "VOTE", "Vote"
        DISCUSS = "DISCUSS", "Discuss"
        COMPLETE = "COMPLETE", "Complete"

    # One-to-one, so "a cycle has at most one retrospective" is a state the
    # database refuses to hold rather than a race a view has to win. CASCADE
    # because a retrospective has no meaning without its week.
    cycle = models.OneToOneField(
        FeedbackCycle,
        on_delete=models.CASCADE,
        related_name="retrospective",
    )
    stage = models.CharField(
        max_length=20,
        choices=Stage.choices,
        default=Stage.DRAFT,
    )
    # Set on entering REVEAL and COMPLETE respectively, by advance_stage() and
    # nothing else. Null means "not there yet", which is why neither is
    # auto_now_add.
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(
        default=0,
        help_text="Increases by one on every transaction that mutates the board.",
    )
    # A field with a default and no UI in this task: #14 spends them, #6 and the
    # settings screens may one day let a facilitator change the number.
    votes_per_member = models.PositiveSmallIntegerField(default=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Retrospective for {self.cycle} ({self.get_stage_display()})"

    def get_absolute_url(self) -> str:
        return reverse("retro-detail", args=[self.pk])

    @property
    def is_complete(self) -> bool:
        return self.stage == self.Stage.COMPLETE

    def has_reached(self, stage: str) -> bool:
        """Whether this retrospective is at `stage` or past it.

        A question about where the board has got to, asked by the features that
        only exist from a given stage on — #19's meeting upload is offered from
        DISCUSS. It says nothing about who is asking; that stays in
        `projects/permissions.py`.
        """
        return STAGE_ORDER.index(self.stage) >= STAGE_ORDER.index(stage)

    @property
    def next_stage(self) -> str | None:
        """The one stage this retrospective may move to, or None at the end.

        Read-only, and the single definition of "forward, one step" that
        `advance_stage()` and the templates both work from.
        """
        return next_stage_after(self.stage)


#: The stages, in order. Nothing may be inserted in the middle without a data
#: migration, because a stored row's stage is compared against this list.
STAGE_ORDER: tuple[str, ...] = tuple(Retrospective.Stage.values)


def next_stage_after(stage: str) -> str | None:
    """The stage that follows `stage`, or None if it is the last one.

    Returns None for COMPLETE, which is what makes COMPLETE terminal rather
    than a special case spelled out in three places.
    """
    index = STAGE_ORDER.index(stage)
    if index + 1 == len(STAGE_ORDER):
        return None
    return STAGE_ORDER[index + 1]


def is_legal_transition(from_stage: str, to_stage: str) -> bool:
    """Whether moving from one stage to another is allowed at all.

    True for exactly the five forward single-step pairs. Everything else —
    backwards, skipping, standing still, and anything out of COMPLETE — is
    False. `advance_stage()` asks this before it writes, and the table-driven
    test walks every pair through it.
    """
    if from_stage not in STAGE_ORDER or to_stage not in STAGE_ORDER:
        return False
    return next_stage_after(from_stage) == to_stage
