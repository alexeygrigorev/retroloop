"""The board's read model: hand-written functions returning plain dicts.

This module is the privacy surface of the application. Everything #10 destroys
at reveal and everything `projects/permissions.py` decides about who may see
what either holds here or leaks here, because this is the only place the
board's data is turned into something a browser receives.

Three rules govern every line below.

**What a viewer may not see is never fetched.** It is not fetched and dropped,
not sent with a flag telling the client to hide it, and not left in a nested
object nobody looks at. A field that reaches the browser has leaked, whatever
the client then does with it.

**No rule is decided here.** `projects/permissions.py` holds them all.
`can_view_project` decides access, at the call site in `views.py`, and
`can_see_vote_totals` decides whether the totals key exists at all. The card
selection in `visible_cards()` is `can_view_card` expressed as a query — one
statement instead of one predicate call per card, which is what the fixed query
count requires — and `tests/test_board.py` proves the two agree card by card, at
every stage, rather than trusting the comment you are reading.

**Nothing carries submission order.** `Card.created_at` survives the reveal, so
it is the submission order that #10's shuffle exists to destroy, and it appears
in no payload here at any stage. `Card.Meta.ordering` is still
`["created_at", "id"]`, so submission order is what a plain queryset gives you
by default: every list a member sees after the reveal comes from
`revealed_cards()`, which sorts by `position` and by nothing else.

The one deliberate exception is `Card.id`, which is a monotonic submission order
too and is serialized anyway, because a board with no stable handle for a card
cannot be mutated (#12), voted on, or keyed by React (#14). What that costs, and
why it is not the same leak as `created_at`, is written out on issue #11.


The payload
-----------

`GET /retros/<id>/state?v=<version>` returns one of two bodies.

When `v` equals the stored version — the client is already up to date::

    {"id": 7, "version": 12, "changed": false}

Nothing else. No cards, no clusters, no votes, and nothing is read from the
database to build it.

Otherwise — `v` is absent, stale, or unparseable — the full state::

    {
      "id": 7,                     # the retrospective's id
      "stage": "REVEAL",           # Retrospective.Stage value
      "version": 12,               # Retrospective.version, never a timestamp
      "changed": true,             # this body carries board data
      "cards": [
        {
          "id": 41,                # Card.pk, the handle #12 mutates by
          "category": "START",     # Card.Category value
          "text": "…",             # Card.text
          "cluster": null          # Cluster id, or null for ungrouped
        }
      ],
      "clusters": [],              # see below
      "votes": {"mine": [], "remaining": 3},
      "vote_totals": {}            # PRESENT ONLY from DISCUSS on
    }

`cards` holds the viewer's own cards and nobody else's before `REVEAL`, and
every card in the cycle in `position` order from `REVEAL` on. No card carries an
author, at any stage, anonymous or not — see `card_payload()`.

`vote_totals` is the whole of what a viewer who may not see the totals must not
receive, so it is one key that is simply absent rather than a set of zeroes or
nulls spread through the clusters. `can_see_vote_totals` decides, and it is
False for everyone while the stage is `VOTE`.

What is a fixed empty value today and why:

- `clusters` and `cards[].cluster`: `Cluster` and `Card.cluster` arrive with
  #12. Until then every card is genuinely ungrouped, so the empty list is the
  board's real state and not a placeholder.
- `votes.mine` and `vote_totals`: `Vote` arrives with #15. `votes.remaining` is
  `Retrospective.votes_per_member` until there is anything to spend.

#12 and #15 fill those in by filling in the functions below. They add no second
serializer, so the shape #13 and #14 are written against cannot drift.
"""

from cycles.models import Card, revealed_cards
from projects.permissions import can_see_vote_totals
from retro.models import Retrospective

# --------------------------------------------------------------------------
# The two bodies
# --------------------------------------------------------------------------


def unchanged_state(retro: Retrospective) -> dict:
    """The body for a client that already has this version.

    Small on purpose, and built from the two attributes the caller has already
    read off the row. It touches no card, no cluster and no vote — the poll
    that returns this runs every 1.5s per open board, so it has to cost one
    lookup and no more.
    """
    return {
        "id": retro.pk,
        "version": retro.version,
        "changed": False,
    }


def board_state(user, retro: Retrospective) -> dict:
    """Everything this viewer is entitled to see of this board, as plain dicts.

    The caller has already established that `user` may view the project — this
    function answers *what*, never *whether*.
    """
    state = {
        "id": retro.pk,
        "stage": retro.stage,
        "version": retro.version,
        "changed": True,
        "cards": [card_payload(card) for card in visible_cards(user, retro)],
        "clusters": cluster_payloads(retro),
        "votes": vote_payload(user, retro),
    }

    # Not a zero, not a null, not an empty object the client is trusted to
    # ignore: while the totals are secret the key does not exist. #15's
    # secrecy criterion is that a voter's raw response body is free of any
    # indication that anyone else voted, and an absent key is the only shape
    # that stays true however the numbers are computed later.
    if can_see_vote_totals(user, retro):
        state["vote_totals"] = vote_totals(retro)

    return state


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


def visible_cards(user, retro: Retrospective):
    """The cards this viewer may see, in the order they may see them in.

    `can_view_card` in `projects/permissions.py` says: the author always, and
    everyone else in the project only from `REVEAL` on. This is that rule as a
    query rather than as a predicate called once per card, because the endpoint
    has to cost the same number of statements for forty cards as for four.
    `tests/test_board.py` asserts the two agree for every card at every stage,
    so the equivalence is checked rather than asserted in a comment.

    From `REVEAL` the list comes from `revealed_cards()` — `position` order,
    which is the shuffled order the reveal handed out. Before `REVEAL` the
    viewer sees only their own cards, so the model's default ordering by
    creation is their own submission order and reveals nothing they did not
    write themselves.
    """
    if retro.has_reached(Retrospective.Stage.REVEAL):
        return revealed_cards(retro.cycle)
    return Card.objects.filter(cycle=retro.cycle, author=user)


def card_payload(card: Card) -> dict:
    """One card, as four fields, none of which is a person or a time.

    No author, at any stage. The criterion is that an anonymous card carries no
    author field at all — not null, not an empty string, not an id — and the
    strongest way to hold that is for no card to carry one, so there is no
    branch to get wrong and no shape difference between an anonymous card and
    an attributed one for anyone to read backwards. `revealed_cards()` selects
    no author for the same reason: a board does not need one to be drawn.

    No `created_at`, and no other timestamp. It survives the reveal, so
    serializing it would hand back the submission order the shuffle destroyed.

    No `is_anonymous` either. With no author on any card there is nothing for it
    to qualify, and a per-card "this one was written anonymously" flag is a
    fact about a member that the board does not need in order to render.
    """
    return {
        "id": card.pk,
        "category": card.category,
        "text": card.text,
        # Every card is ungrouped until #12 adds `Card.cluster`. It fills this
        # in with `card.cluster_id`, which is null for an ungrouped card then
        # as it is now.
        "cluster": None,
    }


# --------------------------------------------------------------------------
# Clusters and votes
# --------------------------------------------------------------------------


def cluster_payloads(retro: Retrospective) -> list[dict]:
    """The board's clusters. Empty until #12 creates the model.

    Nothing about a cluster is private — its name and its cards are the board —
    so this takes no user. What *is* private is how many votes are on it, which
    is why the totals live in their own key and not in these dicts.
    """
    return []


def vote_payload(user, retro: Retrospective) -> dict:
    """This viewer's own votes and what is left of their budget. Never anyone else's.

    `_docs/decisions.md` item 2: votes are reassignable while the stage is
    `VOTE`, which is only safe while nobody can see the running totals. So this
    is scoped to `user` by construction — there is no branch here that could
    widen it to another member, and no aggregate for one to hide in.

    #15 fills `mine` with the viewer's rows and computes `remaining` from them.
    """
    return {
        "mine": [],
        "remaining": retro.votes_per_member,
    }


def vote_totals(retro: Retrospective) -> dict:
    """Votes per cluster, keyed by cluster id. Reached only when they may be seen.

    Called from `board_state()` behind `can_see_vote_totals`, which is False for
    everyone while the stage is `VOTE` and True for project members from
    `DISCUSS` on. #15 computes the numbers; the gate is already here so that
    filling it in cannot accidentally publish them a stage early.
    """
    return {}
