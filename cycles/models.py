"""The weekly feedback cycle and the cards submitted into it.

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

import uuid
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
        """Whether this cycle is still in the state where cards may be written.

        A description of the row, not an access rule: who may add, edit or
        delete a card is `can_add_card`, `can_edit_card` and `can_delete_card`
        in `projects/permissions.py`, which read the status themselves. The card
        page shows its Edit and Delete controls from those predicates, by way of
        the per-card flags the view attaches to each card (#66).

        No template and no view reads this. It survives because it is an honest
        description of the cycle's state and #7's tests read it as one. A reader
        added back here would be the window in `_docs/decisions.md` item 1
        written in a second place, which is exactly what #66 removed.
        """
        return self.is_collecting


#: What a card may not be longer than. The form says it, the model field says
#: it, and Postgres says it, so a request that goes round the form still hits
#: the cap.
CARD_TEXT_MAX_LENGTH = 500


class CardQuerySet(models.QuerySet):
    """Card queries, and the one ordering a revealed list is allowed to use."""

    def in_reveal_order(self) -> CardQuerySet:
        """Revealed cards, in the shuffled order the reveal handed out.

        `position` and nothing else. Submission order is what the shuffle
        exists to destroy, so `created_at` and `id` are not tie-breakers here:
        after a reveal every card in the cycle has a distinct position, and
        before one there is no revealed list to order.

        This is the single definition of "the order revealed cards are shown
        in". #11 serializes it and #14 renders it; neither builds an ordering
        of its own.
        """
        return self.order_by("position")


class Card(models.Model):
    """One Start, Stop or Continue note, written by one member into one cycle.

    Three fields are shaped by what happens *after* this issue, and are the
    reason they look over-general here:

    - `author` is nullable from this first migration. `_docs/decisions.md` item
      3 has #10 set it to NULL at reveal for an anonymous card, permanently and
      with no archive. Making the column nullable later would be a migration on
      a populated table, which is exactly what that decision exists to avoid.
    - `is_anonymous` is the member's intent, not the state of the row.
      Anonymity is applied at reveal, so `author` is set on every card as it is
      written, including the anonymous ones.
    - `position` is written by #10 when it shuffles revealed cards. Before
      reveal it carries no meaning, so the default ordering below stays by
      creation — which is the order the member typed them in, and the right
      order for the one screen that shows a member their own cards. A list of
      *revealed* cards never uses that default: it goes through
      `revealed_cards()` below, which sorts by `position` and by nothing else.

    `public_id` is the fourth, and it is the one a browser is allowed to see.
    `pk` stays the primary key and the target of every foreign key; it simply
    stops leaving the server — `_docs/decisions.md` item 9.
    """

    class Category(models.TextChoices):
        START = "START", "Start"
        STOP = "STOP", "Stop"
        CONTINUE = "CONTINUE", "Continue"

    cycle = models.ForeignKey(
        FeedbackCycle,
        on_delete=models.CASCADE,
        related_name="cards",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    # A varchar rather than an unbounded text column: the cap is a rule about
    # the data, so the database holds it too.
    text = models.CharField(max_length=CARD_TEXT_MAX_LENGTH)
    # SET_NULL and not CASCADE or PROTECT: a card with no author is a state the
    # product already has, because reveal creates it. Removing a person leaves
    # the team's feedback where it is, in that same state, and needs no new
    # branch anywhere that reads a card.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cards",
    )
    is_anonymous = models.BooleanField(default=False)
    # The group this card is in on the board, or NULL for an ungrouped card.
    # Ungrouped is normal and not an error state: it is what every card is until
    # someone moves it, and what a card returns to when its cluster is deleted.
    #
    # Named as a string rather than imported: `retro.models` imports
    # `FeedbackCycle` from here, so importing `Cluster` back would be a circular
    # import. The relation still points at the cluster's primary key like every
    # other foreign key in the project — there is no `to_field`.
    #
    # SET_NULL, so the database says the same thing #12's delete endpoint says:
    # deleting a cluster returns its cards to ungrouped and never takes a card
    # with it. A card outlives every grouping anyone put it in.
    cluster = models.ForeignKey(
        "retro.Cluster",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cards",
    )
    # Handed out by the reveal, as 1..n in shuffled order. The default of 0
    # therefore means "this card has not been revealed" and can never be
    # mistaken for a real place in the order — see `cycles/reveal.py`.
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    # The handle a card is addressed by outside this process, and the only one:
    # `pk` appears in no response body and in no request the server accepts —
    # `_docs/decisions.md` item 9. `pk` comes from a table-wide sequence, so
    # sorting one cycle's ids recovers submission order, which is exactly what
    # `cycles/reveal.py` shuffles to destroy.
    #
    # `uuid4` and nothing else. A counter allocated in submission order is the
    # same leak in a different type, and so is a time-ordered UUID (v1, v6,
    # v7), because both sort back into the order the cards were written in.
    #
    # A default rather than something a view assigns: the value is written when
    # the row is created, so a card has a handle during the week it is being
    # written, and its identity does not change underneath the board at reveal.
    # `editable=False` keeps it out of every ModelForm, so no request can
    # choose one.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    objects = CardQuerySet.as_manager()

    class Meta:
        # By creation, never by `position`: see the note above.
        ordering: ClassVar[list[str]] = ["created_at", "id"]
        indexes: ClassVar[list[models.Index]] = [
            # Every card query is "this cycle, this author", because a member
            # only ever sees their own.
            models.Index(fields=["cycle", "author"], name="cycles_card_cycle_author"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Whitespace-only text is refused by the form with a sentence; this
            # is the same rule where a form cannot be gone round.
            models.CheckConstraint(
                condition=~models.Q(text__regex=r"^\s*$"),
                name="cycles_card_text_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_category_display()}: {self.text[:40]}"


def revealed_cards(cycle: FeedbackCycle) -> CardQuerySet:
    """Every card in `cycle`, in reveal order.

    The one accessor for a revealed list. It deliberately selects no author and
    offers no way to filter by one: after a reveal an anonymous card has none,
    and an attributed card's author is not something a board needs in order to
    draw it.
    """
    return Card.objects.filter(cycle=cycle).in_reveal_order()


class CycleParticipation(models.Model):
    """Who took part in one cycle, and how much — with no way back to a card.

    Written once, inside the reveal transaction, from information that stops
    existing a few statements later: `card_count` is computed while
    `Card.author` is still set, because after the reveal nulls it there is
    nothing left to count. See `cycles/reveal.py`.

    A row exists for every member of the project, including the people who
    submitted nothing — that is the whole point of the table. "Did not submit"
    is `card_count = 0` and `submitted_at = NULL`, a state that is recorded
    rather than inferred from a missing row.

    Two things this table deliberately does not hold:

    - a card. No id, no list, no count per category. A row that named both a
      card and a user would be the link `_docs/decisions.md` item 3 destroys,
      rebuilt one table to the left;
    - a precise submission time. `submitted_at` is truncated to the day the
      member first submitted, because `Card.created_at` survives the reveal:
      an exact timestamp here would match exactly one card there, and that
      equality is the author link again. Day granularity is all
      "who submitted and who did not" needs — `_docs/decisions.md` item 3a.

    `card_count` is stored and never shown beside a name. Item 3a explains why
    a count is an identifier in a team of six; #25 and #26 show submitted or
    not, plus team-wide totals.
    """

    cycle = models.ForeignKey(
        FeedbackCycle,
        on_delete=models.CASCADE,
        related_name="participation",
    )
    # CASCADE, unlike `Card.author`: a participation row about a person who no
    # longer exists is a name-shaped hole with a number beside it, and the
    # number is the identifier item 3a is about. It goes with them.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cycle_participation",
    )
    card_count = models.PositiveIntegerField(
        default=0,
        help_text="How many cards this member wrote, attributed and anonymous together.",
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The day this member first submitted, or NULL if they submitted nothing.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["cycle_id", "user_id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # A second row for the same person in the same cycle is not an edge
            # case for the reveal to handle, it is a state the database refuses
            # to hold — which is also what makes a second reveal fail loudly
            # rather than double-count.
            models.UniqueConstraint(
                fields=["cycle", "user"],
                name="cycles_cycleparticipation_unique_cycle_user",
            ),
            models.CheckConstraint(
                # The two halves of "did not submit" cannot drift apart: no
                # cards and a time, or cards and no time, are both refused.
                condition=(
                    models.Q(card_count=0, submitted_at__isnull=True)
                    | models.Q(card_count__gt=0, submitted_at__isnull=False)
                ),
                name="cycles_cycleparticipation_count_matches_submitted",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.cycle}"

    @property
    def submitted(self) -> bool:
        """Whether this member submitted anything. The only thing screens show."""
        return self.submitted_at is not None
