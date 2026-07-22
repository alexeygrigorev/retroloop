"""What entering REVEAL does to a cycle's cards.

`reveal_cycle()` is the whole public surface, and it is called from exactly one
place: the `-> REVEAL` hook in `retro/services.py`, inside that transition's
transaction. It does three things, in an order that is not an implementation
detail:

1. records participation, while `Card.author` still says who wrote what;
2. gives every card in the cycle a shuffled `position`;
3. sets `author` to NULL for every anonymous card in the cycle.

Step 1 has to precede step 3 because step 3 destroys the only copy of the
information step 1 records. Steps 2 and 3 are one `UPDATE` each, against the
cycle's cards, rather than a Python loop that saves a row at a time: a loop
that dies half way through leaves some authors destroyed and some not, and
there is no way back from the first half.

Nothing here is reversible and nothing here is recorded anywhere else. There is
no archive table, no `previous_author`, no soft-delete flag and no admin
override — `_docs/decisions.md` item 3, which says in words that a recoverable
link is not anonymity but a delay. The absence of those columns is the feature,
so a later issue that wants one is asking for this decision to be reopened in
that file first.

Why the shuffle covers every card and not only the anonymous ones: a list
ordered by submission time with the anonymous cards moved about inside it still
says when each anonymous card was written relative to the attributed ones
around it, and the attributed ones carry names. The order has to stop meaning
anything at all, so every card is reordered together.

The shuffle uses `random.SystemRandom`. `random`'s default generator is a
Mersenne Twister seeded from a small state that a test, a fixture or a
`random.seed()` anywhere else in the process can reproduce, and a reproducible
shuffle is a way back to submission order.
"""

import random
from datetime import datetime
from typing import Final

from django.db import models, transaction
from django.utils import timezone

from cycles.models import Card, CycleParticipation, FeedbackCycle
from projects.models import Membership

#: The first position handed out at reveal. Positions run 1..n, so `position`'s
#: default of 0 means "not revealed" and is never a real place in the order.
FIRST_POSITION: Final[int] = 1

#: Seeded from the operating system's entropy pool and unaffected by
#: `random.seed()`. Module level because it holds no state worth per-call
#: construction, not because anything about it is shared.
_shuffler: Final[random.SystemRandom] = random.SystemRandom()


def reveal_cycle(cycle: FeedbackCycle) -> None:
    """Record participation, shuffle the cards, and destroy anonymous authorship.

    Runs inside the caller's transaction, and refuses to run outside one: the
    three steps below are only safe together. Participation that commits
    without the nulling would be a count of cards whose authors are still
    named; the nulling committing without the participation would lose the
    count for good.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "reveal_cycle() must run inside the transaction that advances the stage, "
            "so participation, the shuffle and the destroyed authors commit together."
        )

    # Lock the cycle row before reading its cards. `card_create` takes the same
    # lock before it decides whether the cycle still accepts cards, so a card
    # being written at this instant either lands before the three steps below
    # see it — counted, positioned and anonymised with the rest — or waits and
    # is refused. Without the lock it would commit into a revealed cycle
    # afterwards, keeping its author for good with nothing left to null it.
    # Held here rather than relied on from the caller's status UPDATE, which
    # does not happen at all when the facilitator closed the cycle by hand.
    FeedbackCycle.objects.select_for_update().get(pk=cycle.pk)

    _record_participation(cycle)
    _shuffle_positions(cycle)
    _destroy_anonymous_authorship(cycle)


# --------------------------------------------------------------------------
# The three steps
# --------------------------------------------------------------------------


def _record_participation(cycle: FeedbackCycle) -> None:
    """One `CycleParticipation` row per member, computed before anything is destroyed.

    Counted per author over the whole cycle, attributed and anonymous cards
    alike, because "how much did this person contribute" is a fact about the
    person and not about which box they ticked. No card id is carried across —
    the aggregate is the only thing that survives this function.

    Rows are written for every current member, and additionally for anyone who
    wrote a card into this cycle and is no longer one. Without the second half
    an ex-member's cards would vanish from the team totals #26 shows while
    still sitting on the board.
    """
    submitted = {
        row["author_id"]: row
        for row in (
            # `author__isnull=False` skips the cards whose author was deleted
            # before the reveal. There is no one to record: `Card.author` is
            # SET_NULL, so that card already arrived here anonymous in fact.
            Card.objects.filter(cycle=cycle, author__isnull=False)
            .values("author_id")
            .annotate(
                card_count=models.Count("pk"),
                first_at=models.Min("created_at"),
            )
        )
    }
    member_ids = Membership.objects.filter(project_id=cycle.project_id).values_list(
        "user_id", flat=True
    )
    # dict.fromkeys, so a member who also submitted appears once and the order
    # is the members first — a set would make the row order depend on hashing.
    user_ids = list(dict.fromkeys([*member_ids, *submitted]))

    CycleParticipation.objects.bulk_create(
        [
            CycleParticipation(
                cycle=cycle,
                user_id=user_id,
                card_count=submitted[user_id]["card_count"] if user_id in submitted else 0,
                submitted_at=(
                    _start_of_day(submitted[user_id]["first_at"]) if user_id in submitted else None
                ),
            )
            for user_id in user_ids
        ]
    )


def _shuffle_positions(cycle: FeedbackCycle) -> None:
    """Give every card in the cycle a distinct position, in a random order.

    One `UPDATE` with a `CASE`, not a save per row. Positions are 1..n and
    contiguous, so the resulting order is total: nothing has to fall back on a
    tie-breaker, and `created_at` never gets to decide anything again.
    """
    card_ids = list(Card.objects.filter(cycle=cycle).values_list("pk", flat=True))
    if not card_ids:
        return

    _shuffler.shuffle(card_ids)
    Card.objects.filter(cycle=cycle).update(
        position=models.Case(
            *[
                models.When(pk=card_id, then=models.Value(position))
                for position, card_id in enumerate(card_ids, start=FIRST_POSITION)
            ],
            # Only reachable by a card inserted between the SELECT above and
            # this UPDATE, which the cycle closing in the same transaction
            # already prevents. It leaves that card where it was rather than
            # writing NULL into a NOT NULL column.
            default=models.F("position"),
            output_field=models.IntegerField(),
        )
    )


def _destroy_anonymous_authorship(cycle: FeedbackCycle) -> None:
    """Set `author` to NULL for every anonymous card in the cycle. One statement.

    `is_anonymous` is left alone: it is what still lets the card be drawn as
    anonymous once there is no author to draw instead. Attributed cards are not
    in the filter and are not touched.
    """
    Card.objects.filter(cycle=cycle, is_anonymous=True).update(author=None)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _start_of_day(moment: datetime) -> datetime:
    """Midnight of the day `moment` falls on, in the project's timezone.

    Deliberately coarse. `Card.created_at` survives the reveal, so a
    participation row carrying an exact submission time would match exactly one
    card and hand back the authorship this module has just destroyed. A day is
    what "who submitted and who did not" needs and nothing more.
    """
    local = timezone.localtime(moment) if timezone.is_aware(moment) else moment
    return local.replace(hour=0, minute=0, second=0, microsecond=0)
