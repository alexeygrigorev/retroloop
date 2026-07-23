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

from django.conf import settings
from django.db import models
from django.urls import reverse

from cycles.models import FeedbackCycle

#: How many votes each member spends during the VOTE stage. A default and no UI
#: in this task: #40 is the issue that lets a facilitator change it, and until it
#: lands every retrospective gets exactly this many.
#:
#: It is also the ceiling the `Vote` check constraint holds `weight` under. A
#: per-row database check cannot reach across to `Retrospective.votes_per_member`
#: — the value lives on another table — so the constant that *is* that default
#: stands in for it. While `votes_per_member` is fixed at this number the two say
#: the same thing; #40 makes the budget configurable and has to revisit the
#: constraint at the same time, which is called out on `Vote` below.
DEFAULT_VOTES_PER_MEMBER = 3


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
    # A field with a default and no UI in this task: #15 spends them, #40 may one
    # day let a facilitator change the number. The default is the module constant
    # above, which is also the ceiling the `Vote` check constraint enforces.
    votes_per_member = models.PositiveSmallIntegerField(default=DEFAULT_VOTES_PER_MEMBER)
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


class Vote(models.Model):
    """One member's votes on one cluster, during one retrospective's VOTE stage.

    One row per member per cluster, carrying `weight` — how many of their votes
    they stacked there. A member with three votes who puts two on one cluster and
    one on another has two rows; a member who piles all three onto one topic has
    a single row with `weight` 3. Withdrawing the last vote from a cluster deletes
    the row rather than leaving a `weight` of 0 behind, so "no votes here" and "a
    row that says zero" are never two ways to spell the same thing.

    This row is the one place the application knows *who* voted for *what*, and it
    never leaves the server as that. `board/serializers.py` reads a member their
    own votes and, from DISCUSS on, per-cluster totals — never another member's
    allocation, at any stage. `_docs/decisions.md` items 2 and 10: the totals are
    hidden while the stage is VOTE precisely because votes are reassignable then,
    and a running total a member could watch move would leak what the secret
    ballot exists to keep.

    `retrospective` is carried explicitly even though `cluster` already implies
    it: the budget is a fact about a member *within a retrospective*, spanning
    every cluster on the board, so the sum that enforces it and the uniqueness
    that shapes it are both keyed by `(retrospective, user)` and read no join to
    get there. The acceptance criteria name the field, and `board/mutations.py`
    resolves the cluster against the retrospective before it ever writes one, so
    the two can never disagree.

    `user` is CASCADE, unlike `Card.author` which is SET_NULL: a vote is not the
    team's feedback, it is a transient tally that decides the agenda and is spent
    inside a single stage. A member who leaves takes their votes with them and the
    totals settle without them, rather than leaving an orphan row that counts for
    a person who is gone. There is deliberately no anonymised survival here — a
    vote's whole meaning is the link this row holds, and item 3's irreversible
    anonymity is about `Card`, not this.
    """

    retrospective = models.ForeignKey(
        Retrospective,
        on_delete=models.CASCADE,
        related_name="votes",
    )
    cluster = models.ForeignKey(
        Cluster,
        on_delete=models.CASCADE,
        related_name="votes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="votes",
    )
    # How many of the member's votes sit on this cluster. Never below 1 — a row
    # that would fall to 0 is deleted — and never above the budget, because all
    # of a member's votes may pile onto one cluster but no more than all of them.
    weight = models.PositiveSmallIntegerField()

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One row per member per cluster: a second vote on the same cluster
            # by the same member is a heavier `weight` on the row that is already
            # there, not a new row. The database refuses the duplicate rather than
            # trusting every write path to find and update the existing one.
            models.UniqueConstraint(
                fields=["retrospective", "cluster", "user"],
                name="retro_vote_unique_member_cluster",
            ),
            # 1..budget, enforced where a write cannot go round it. The lower
            # bound is the "delete, do not store a zero" rule made structural; the
            # upper bound is the whole budget, since a member may stack every vote
            # on one cluster. `DEFAULT_VOTES_PER_MEMBER` stands in for
            # `Retrospective.votes_per_member`, which a per-row check cannot reach
            # — see the constant. The per-cluster ceiling is a backstop; the
            # across-all-clusters budget is enforced under a row lock in
            # `board/mutations.py`, where the whole tally can be summed.
            models.CheckConstraint(
                condition=models.Q(weight__gte=1) & models.Q(weight__lte=DEFAULT_VOTES_PER_MEMBER),
                name="retro_vote_weight_within_budget",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # The budget sum and the viewer's-own-votes read are both "this
            # retrospective, this member", so the board pays a single indexed
            # lookup per cast however many clusters the member has voted on.
            models.Index(fields=["retrospective", "user"], name="retro_vote_retro_user"),
        ]

    def __str__(self) -> str:
        return f"{self.weight} vote(s) on {self.cluster_id} by {self.user_id}"
