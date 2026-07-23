"""The board's writes: seven operations, one transaction each.

Every action the team can take on the board goes through `apply_mutation()`,
which is the whole of the concurrency story:

1. open a transaction;
2. take a row lock on the retrospective;
3. refuse anyone who may not see the project, with the same 404 an id that was
   never used gets;
4. resolve the ids the request names, against *that* retrospective;
5. ask `projects/permissions.py` whether this person may do this now;
6. write;
7. bump `Retrospective.version` — but only if something actually changed;
8. answer with the whole board state, as `board/serializers.py` builds it.

**One lock, and it is the retrospective's.** Everything on this board hangs off
that row, so locking it serialises every mutation on it against every other:
two simultaneous moves both succeed, in some order, and the version ends up two
higher. Nothing here locks a cycle or a card. The project's lock order is
retrospective < cycle < card — `advance_stage()` takes the retrospective's and
then `reveal_cycle()` takes the cycle's — and taking only the first of the three
cannot invert it. `of=("self",)` matters for that: without it the `select_related`
join would lock the cycle and the project rows too, which is exactly the
inversion the order exists to prevent.

**The version moves when the board does, and not otherwise.** `bump_version()`
from #9 is called inside the same transaction as the write it describes, so a
poller is never told to re-read a board that has not changed yet. Every
operation below answers whether it changed anything, and a request that changes
nothing — moving a card to the cluster it is already in, renaming a cluster to
the name it already has — answers False, leaves the counter alone, and does not
wake every other client's poll. It still gets the full state back, so a client
that thought otherwise is corrected.

**A card is named by `Card.public_id` and never by `Card.pk`.**
`_docs/decisions.md` item 9: the primary key is a table-wide sequence, so
sorting one cycle's ids recovers the submission order `cycles/reveal.py` exists
to destroy. `_card_or_404()` parses the value as a UUID and looks the card up by
that column alone. An integer is not a UUID, so it is a 404 — the same answer as
any other id that does not resolve — and there is no branch anywhere in this
module that falls back to a primary-key lookup. There is deliberately no
`filter(pk=` against `Card` here at all: the only `pk` this module hands to the
database is a cluster's, which is an integer by decision.

**A cluster is named by its integer primary key**, and resolved against this
retrospective, so a cluster from another board is a 404 rather than a cluster
somebody else's team is looking at.

Rejections are exceptions with a status on them, because the view's whole job is
to turn one into a response:

- `InvalidRequest` (400) — the request cannot be carried out as written: a blank
  name, a merge of a cluster into itself, a card that is not in the cluster
  being split;
- `BoardFrozen` (409) — the person may not do this to this board now, which in
  practice means the stage has passed CLUSTER and cluster membership is frozen.
  A conflict rather than a 403: nothing is wrong with the caller, their view of
  the board is out of date, and the fix is to re-read it.

Neither is a silent no-op, and neither leaves a partial write behind: they are
raised inside the transaction, so it rolls back.
"""

import uuid

from django.db import models, transaction
from django.http import Http404

from board.serializers import board_state
from cycles.models import Card
from projects.permissions import (
    can_cast_vote,
    can_create_cluster,
    can_delete_cluster,
    can_merge_cluster,
    can_move_card,
    can_rename_cluster,
    can_split_cluster,
    can_view_project,
)
from retro.models import CLUSTER_NAME_MAX_LENGTH, Cluster, Retrospective, Vote
from retro.services import bump_version

#: The largest value a `BigAutoField` can hold. A cluster id outside 1..this is
#: refused before it reaches the database, so a caller cannot turn a request
#: into a driver-level error by posting a number with forty digits in it.
_MAX_PK = 2**63 - 1


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------


class BoardRejection(Exception):
    """A mutation was refused. Nothing was written."""

    status = 400


class InvalidRequest(BoardRejection):
    """The request cannot be carried out as written."""

    status = 400


class BoardFrozen(BoardRejection):
    """This board does not accept this change at this stage."""

    status = 409


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------


def apply_mutation(user, pk: int, data, change) -> dict:
    """Run `change` against retrospective `pk`, and return the new board state.

    `change` is one of the seven operations below: `(user, retro, data) -> bool`,
    where the bool says whether the board actually changed. It is called with
    the locked row, inside the transaction, and everything it writes commits
    with the version bump or not at all.
    """
    with transaction.atomic():
        # The lock is taken before anything is read, so a second mutation waits
        # here rather than deciding from a board that is about to move.
        retro = (
            Retrospective.objects.select_for_update(of=("self",))
            .select_related("cycle__project")
            .filter(pk=pk)
            .first()
        )
        # `.first()` and one branch, so a retrospective that does not exist and
        # one this person may not see raise the same exception from the same
        # line — a 404 that distinguished them would be an existence oracle.
        if retro is None or not can_view_project(user, retro.cycle.project):
            raise Http404

        if change(user, retro, data):
            bump_version(retro)

        # Read inside the transaction, so the state that comes back is the state
        # that was committed — version included.
        return board_state(user, retro)


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


def move_card_to_cluster(user, retro: Retrospective, data) -> bool:
    """`card` joins `cluster`. Last write wins.

    Moving a card to the cluster it is already in is a request that changes
    nothing: it writes nothing, bumps nothing, and answers with the same board.
    """
    card = _card_or_404(retro, data.get("card"))
    cluster = _cluster_or_404(retro, data.get("cluster"))
    _require(can_move_card(user, card))

    if card.cluster_id == cluster.pk:
        return False

    card.cluster = cluster
    card.save(update_fields=["cluster"])
    return True


def move_card_out(user, retro: Retrospective, data) -> bool:
    """`card` leaves whatever cluster it is in and becomes ungrouped.

    Ungrouped is a normal state and not an error one, so ungrouping a card that
    is already ungrouped is not a failure — it is a request that changes
    nothing.
    """
    card = _card_or_404(retro, data.get("card"))
    _require(can_move_card(user, card))

    if card.cluster_id is None:
        return False

    card.cluster = None
    card.save(update_fields=["cluster"])
    return True


# --------------------------------------------------------------------------
# Clusters
# --------------------------------------------------------------------------


def create_cluster(user, retro: Retrospective, data) -> bool:
    """A new, empty cluster called `name`, at the end of the board.

    Empty, because a card joins a cluster by being moved into it. Created by
    hand, so `is_auto_generated` is False: only #22's job writes True, and
    nothing about how a cluster may be changed reads that flag.
    """
    _require(can_create_cluster(user, retro))
    name = _clean_name(data.get("name"))

    Cluster.objects.create(retrospective=retro, name=name, position=_next_position(retro))
    return True


def rename_cluster(user, retro: Retrospective, data) -> bool:
    """`cluster` is called `name` from now on.

    An empty or whitespace-only name is refused, because a nameless group is not
    something the team can talk about. A rename to the name it already has
    changes nothing.
    """
    cluster = _cluster_or_404(retro, data.get("cluster"))
    _require(can_rename_cluster(user, cluster))
    name = _clean_name(data.get("name"))

    if cluster.name == name:
        return False

    cluster.name = name
    cluster.save(update_fields=["name"])
    return True


def merge_clusters(user, retro: Retrospective, data) -> bool:
    """Every card in `source` joins `target`, and `source` is deleted.

    Both clusters are checked, so neither side of the merge is authorized by the
    other's. Merging a cluster into itself is refused rather than treated as a
    no-op: it would delete the cluster whose cards had just been moved into it,
    which is the opposite of what the caller asked for.

    One `UPDATE` for the cards, whatever the cluster holds, and the cards
    themselves are untouched apart from which group they point at.
    """
    source = _cluster_or_404(retro, data.get("source"))
    target = _cluster_or_404(retro, data.get("target"))
    _require(can_merge_cluster(user, source) and can_merge_cluster(user, target))

    if source.pk == target.pk:
        raise InvalidRequest("A cluster cannot be merged into itself.")

    Card.objects.filter(cluster=source).update(cluster=target)
    source.delete()
    return True


def split_cluster(user, retro: Retrospective, data) -> bool:
    """The named cards leave `cluster` for a new one, at the end of the board.

    The cards are given as repeated `cards` fields, each one a card's
    `public_id`. Every one of them has to be in the cluster being split: an id
    that is not is refused with a sentence naming it, rather than silently
    ignored, because a client that thinks it moved five cards and moved three
    has no way to find out.

    `name` is optional. A split usually happens before anyone has words for the
    new group, so when it is absent the new cluster starts under the name of the
    one it came out of, and renaming it is one more request. When it is given it
    is held to the same rule as a rename.
    """
    source = _cluster_or_404(retro, data.get("cluster"))
    cards = _cards_or_404(retro, data.getlist("cards"))
    _require(can_split_cluster(user, source))

    raw_name = data.get("name")
    name = source.name if raw_name is None else _clean_name(raw_name)
    if not cards:
        raise InvalidRequest("A split needs at least one card to move to the new cluster.")

    outside = [card for card in cards if card.cluster_id != source.pk]
    if outside:
        raise InvalidRequest(
            "These cards are not in the cluster being split: "
            + ", ".join(str(card.public_id) for card in outside)
            + "."
        )

    moved = Cluster.objects.create(retrospective=retro, name=name, position=_next_position(retro))
    Card.objects.filter(pk__in=[card.pk for card in cards]).update(cluster=moved)
    return True


def delete_cluster(user, retro: Retrospective, data) -> bool:
    """`cluster` goes; its cards return to ungrouped.

    Never a card. The cards are ungrouped in their own statement first, so what
    happens to them is stated here rather than inferred from the foreign key's
    `on_delete` — which says the same thing, and is the reason a card cannot be
    taken with a cluster even by a code path that is not this one.
    """
    cluster = _cluster_or_404(retro, data.get("cluster"))
    _require(can_delete_cluster(user, cluster))

    Card.objects.filter(cluster=cluster).update(cluster=None)
    cluster.delete()
    return True


# --------------------------------------------------------------------------
# Votes
#
# Two operations, both `change` functions like the seven above and run the same
# way: `apply_mutation()` takes the retrospective's row lock first, refuses a
# non-member with the same 404 an unused id gets, then calls one of these inside
# the transaction and bumps the version once if it returns True.
#
# The budget is the reason the lock matters here as much as it does for a card
# move. A member has `votes_per_member` votes across the whole board; a cast that
# would carry their spend past it is refused. Two casts from one member racing
# each other would both read the same "already spent" and both think there was
# room — so they are serialised on the retrospective's row, the same lock every
# other mutation on this board takes, and the second reads the first's write
# rather than the stale total. Nothing here locks a member's rows on their own:
# the retrospective's lock already serialises every write on the board, and the
# lock order retrospective < cycle < card is kept by taking only the first of
# the three, exactly as the card and cluster operations do.
# --------------------------------------------------------------------------


def cast_vote(user, retro: Retrospective, data) -> bool:
    """Add `weight` of the member's votes to `cluster`. Refused past the budget.

    `weight` is optional and defaults to one, so a single click spends a single
    vote and a member who cares a lot posts a larger number or clicks again;
    either way the votes stack onto the one row this member has for this cluster.

    Refused, writing nothing, when the stage is not VOTE — `can_cast_vote` is
    True only then, so a cast before voting opens and a cast after it closes are
    the same 409 — and when the member's total spend across every cluster would
    pass their budget. The budget total is read here, under the row lock
    `apply_mutation()` already holds, so two casts cannot both slip past it.
    """
    cluster = _cluster_or_404(retro, data.get("cluster"))
    weight = _vote_weight(retro, data.get("weight"))
    _require_voting_open(can_cast_vote(user, retro))

    spent = _spent(user, retro)
    if spent + weight > retro.votes_per_member:
        remaining = retro.votes_per_member - spent
        raise InvalidRequest(
            f"That would spend {spent + weight} of your {retro.votes_per_member} votes. "
            f"You have {remaining} {_votes(remaining)} left."
        )

    existing = Vote.objects.filter(retrospective=retro, cluster=cluster, user=user).first()
    if existing is None:
        Vote.objects.create(retrospective=retro, cluster=cluster, user=user, weight=weight)
    else:
        # Read-then-write is safe under the retrospective's row lock: no other
        # write on this board runs between the read above and the save here.
        existing.weight += weight
        existing.save(update_fields=["weight"])
    return True


def withdraw_vote(user, retro: Retrospective, data) -> bool:
    """Take `weight` of the member's votes back off `cluster`, returning them to budget.

    The mirror of `cast_vote`: `weight` defaults to one, the stage must be VOTE,
    and the votes come back to the member's remaining budget for them to place
    elsewhere — `_docs/decisions.md` item 2, votes are freely reassignable while
    the stage is VOTE. Withdrawing the last vote on a cluster deletes the row
    rather than leaving a zero behind, so `votes.mine` never carries a cluster
    the member has no votes on.

    Withdrawing from a cluster the member has not voted on, or withdrawing more
    than they placed there, is refused with a sentence — a client that thinks it
    removed two votes and removed one has no way to find out otherwise.
    """
    cluster = _cluster_or_404(retro, data.get("cluster"))
    weight = _vote_weight(retro, data.get("weight"))
    _require_voting_open(can_cast_vote(user, retro))

    existing = Vote.objects.filter(retrospective=retro, cluster=cluster, user=user).first()
    if existing is None:
        raise InvalidRequest("You have no votes on that cluster to withdraw.")
    if weight > existing.weight:
        raise InvalidRequest(
            f"You have only {existing.weight} {_votes(existing.weight)} on that cluster, "
            f"so {weight} cannot be withdrawn."
        )

    remaining = existing.weight - weight
    if remaining == 0:
        # A row that would fall to zero is deleted, not stored — the model's
        # check constraint refuses a zero weight, and the payload must not carry
        # a cluster the member no longer has any votes on.
        existing.delete()
    else:
        existing.weight = remaining
        existing.save(update_fields=["weight"])
    return True


def _spent(user, retro: Retrospective) -> int:
    """How many votes this member has already placed across the whole board.

    The budget is a fact about a member within a retrospective, spanning every
    cluster, so this sums their rows for the retrospective and reads no cluster.
    Called inside `apply_mutation()`'s transaction, under the row lock, so the
    number it returns cannot go stale before the cast that depends on it writes.
    """
    total = Vote.objects.filter(retrospective=retro, user=user).aggregate(
        spent=models.Sum("weight")
    )
    return total["spent"] or 0


def members_who_spent_everything(retro: Retrospective) -> int:
    """How many members have placed every one of their votes. A count, never who.

    What the facilitator watches to know when to close voting — the one thing the
    board's secrecy lets them see about the room, and only as a number.
    `_docs/decisions.md` item 10 and #15's secrecy criteria: never which members,
    never a partial tally per person. This groups the votes by member, sums each
    member's spend, and counts the members whose spend equals the budget. It
    names no member and returns nothing but an integer.
    """
    spends = (
        Vote.objects.filter(retrospective=retro)
        .values("user_id")
        .annotate(spent=models.Sum("weight"))
    )
    return sum(1 for row in spends if row["spent"] >= retro.votes_per_member)


# --------------------------------------------------------------------------
# Resolving what a request names
# --------------------------------------------------------------------------


def _card_or_404(retro: Retrospective, raw) -> Card:
    """The card this request names, by its public handle and by nothing else.

    An integer, an empty value, a misspelt UUID and a card belonging to another
    retrospective are one answer: 404. There is no fallback to a primary-key
    lookup — `_docs/decisions.md` item 9 — so posting a card's `pk` here does
    not find that card, or any other.
    """
    if not isinstance(raw, str):
        raise Http404
    try:
        handle = uuid.UUID(raw)
    except ValueError:
        raise Http404 from None

    card = Card.objects.filter(cycle_id=retro.cycle_id, public_id=handle).first()
    if card is None:
        raise Http404
    return card


def _cards_or_404(retro: Retrospective, raws: list) -> list[Card]:
    """Every card the request names, in the order it named them, without repeats.

    Resolved one by one rather than with a single `public_id__in`, so an id that
    does not resolve is a 404 instead of a shorter list than the caller sent.
    """
    cards: dict[int, Card] = {}
    for raw in raws:
        card = _card_or_404(retro, raw)
        cards.setdefault(card.pk, card)
    return list(cards.values())


def _cluster_or_404(retro: Retrospective, raw) -> Cluster:
    """The cluster this request names, resolved against this retrospective.

    An integer primary key, by decision: item 9 is about `Card`. A cluster
    belonging to another retrospective is a 404 and is never acted on, which is
    what scoping the query to `retro` buys.
    """
    if not isinstance(raw, str):
        raise Http404
    try:
        value = int(raw)
    except ValueError:
        raise Http404 from None
    if not 1 <= value <= _MAX_PK:
        raise Http404

    cluster = Cluster.objects.filter(retrospective=retro, pk=value).first()
    if cluster is None:
        raise Http404
    return cluster


# --------------------------------------------------------------------------
# Small rules
# --------------------------------------------------------------------------


def _require(permitted: bool) -> None:
    """Turn a False from `projects/permissions.py` into the 409.

    Reached only after the caller has been established as someone who may view
    the project, so the membership half of every predicate above is already
    True and the only thing left for it to be False about is the stage. That is
    what makes one message honest for all seven operations.
    """
    if not permitted:
        raise BoardFrozen(
            "The board is no longer being clustered, so it cannot be changed. "
            "Reload the page to see where the retrospective has got to."
        )


def _require_voting_open(permitted: bool) -> None:
    """Turn `can_cast_vote` being False into the 409 a stale board gets.

    A cast or withdraw is refused the same way a card move on a frozen board is:
    a conflict, not a 403 — nothing is wrong with the caller, their view of the
    stage is out of date, and reloading is the fix. `can_cast_vote` is True only
    during VOTE, so a request before voting opens and one after it closes are the
    same answer. Reached only after `apply_mutation()` has established the caller
    as a project member, so the only thing left for it to be False about is the
    stage.
    """
    if not permitted:
        raise BoardFrozen(
            "Voting is not open on this retrospective, so votes cannot be changed. "
            "Reload the page to see where the retrospective has got to."
        )


def _vote_weight(retro: Retrospective, raw) -> int:
    """How many votes a cast or withdraw names, defaulting to one.

    Absent means one — a single click spends a single vote. Anything present has
    to be a whole number of votes from 1 to the budget: a member may move all of
    their votes in one request but never more than exist, and zero, a fraction, a
    negative and a word are each a request that cannot be carried out as written.
    The across-the-board budget is enforced by the caller against the running
    total; this only rejects a single value that is not a count of votes at all.
    """
    if raw is None:
        return 1
    try:
        weight = int(raw)
    except TypeError, ValueError:
        raise InvalidRequest("A vote is a whole number of votes.") from None
    if not 1 <= weight <= retro.votes_per_member:
        raise InvalidRequest(
            f"A vote is between 1 and {retro.votes_per_member} {_votes(retro.votes_per_member)}."
        )
    return weight


def _votes(count: int) -> str:
    """ "vote" or "votes", so the budget messages read as sentences."""
    return "vote" if count == 1 else "votes"


def _clean_name(raw) -> str:
    """A cluster's name, trimmed, or a rejection saying why it is not one."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        raise InvalidRequest("A cluster needs a name.")
    if len(name) > CLUSTER_NAME_MAX_LENGTH:
        raise InvalidRequest(f"A cluster's name is at most {CLUSTER_NAME_MAX_LENGTH} characters.")
    return name


def _next_position(retro: Retrospective) -> int:
    """One past the last cluster on this board, or 1 for the first one.

    Safe against two clients creating a cluster at the same moment because the
    retrospective's row is locked for the whole of this transaction.
    """
    highest = Cluster.objects.filter(retrospective=retro).aggregate(models.Max("position"))
    return (highest["position__max"] or 0) + 1
