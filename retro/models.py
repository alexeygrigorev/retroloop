"""The retrospective that follows a feedback cycle, and the clusters on its board.

The retrospective row is deliberately thin. It holds where the retrospective is
(`stage`), when it started and finished, how many votes each member gets, and a
`version` counter that is the whole of the board sync mechanism used by #11 and
#12.

Two things are *not* here on purpose:

- the stage machine. `advance_stage()` lives in `retro/services.py`, because a
  transition is a transaction with side effects and a lock, not an assignment.
  Nothing else may write `stage`;
- any behaviour that depends on cards. Cards arrive with #8; `Cluster` names no
  card, and the relation is `Card.cluster` on the card's side.
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
    # Where #22's clustering job records that it could not group the cards, in
    # words a facilitator reads on the retrospective page. Empty is the normal
    # state: the job runs after the reveal has committed, so a failure here
    # leaves the stage where it is and the cards simply ungrouped. It is a
    # message and never a traceback — the stack trace goes to the worker log,
    # not onto a page every member of the project can open.
    clustering_error = models.TextField(blank=True, default="")
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


#: What a cluster's name may not be longer than. The mutation endpoints say it
#: and Postgres says it, so a request that goes round the endpoint still hits
#: the cap rather than a 500 out of the database driver.
CLUSTER_NAME_MAX_LENGTH = 100


class Cluster(models.Model):
    """One group of cards on one retrospective's board.

    A cluster belongs to the retrospective and not to the cycle: it is made
    during the retrospective, by the team, in front of the team. Cards join it
    from the other side, through `Card.cluster`, which is nullable because an
    ungrouped card is the normal state of every card until someone moves it.

    Two fields exist for issues that are not this one, and are written down here
    because the column is cheaper to add now than to migrate onto a populated
    table later:

    - `is_auto_generated` marks the rows #22's clustering job writes. It affects
      display wording only. A suggested cluster is renamed, merged, split and
      deleted by exactly the same endpoints as a hand-made one, and nothing in
      `board/` branches on it;
    - `status` is the discussion state #16 moves a cluster through. #12 creates
      every cluster `PENDING` and never changes it — the transitions are #16's.

    Unlike `Card`, a cluster is addressed publicly by its integer primary key,
    in requests and in the payload alike — `_docs/decisions.md` item 9 is about
    `Card`, and says so: the order clusters were created in is not a fact about
    a person, so a sequence in the payload gives nothing away. A cluster
    deliberately has no `public_id`, and `tests/test_public_id.py` asserts that
    no model but `Card` has one.

    There is no `created_at` either. Nothing needs it — the board is ordered by
    `position` — and a timestamp on a row a card points at is one more thing for
    a later feature to correlate with `Card.created_at`.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DISCUSSED = "DISCUSSED", "Discussed"
        SKIPPED = "SKIPPED", "Skipped"
        DEFERRED = "DEFERRED", "Deferred"

    retrospective = models.ForeignKey(
        Retrospective,
        on_delete=models.CASCADE,
        related_name="clusters",
    )
    name = models.CharField(max_length=CLUSTER_NAME_MAX_LENGTH)
    # Where the cluster sits on the board. Handed out as max + 1 when a cluster
    # is created, under the retrospective's row lock, so two clusters created at
    # the same instant cannot land on one number.
    position = models.IntegerField(default=0)
    is_auto_generated = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    class Meta:
        # `id` as the tie-breaker, so the board's order is total and a payload
        # cannot come back in a different order from one poll to the next.
        ordering: ClassVar[list[str]] = ["position", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # The endpoints reject a blank name with a sentence; this is the
            # same rule where an endpoint cannot be gone round.
            models.CheckConstraint(
                condition=~models.Q(name__regex=r"^\s*$"),
                name="retro_cluster_name_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.retrospective_id})"
