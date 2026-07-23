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

There is no exception for the card's primary key either. `Card.pk` comes from a
table-wide sequence, so sorting one cycle's ids recovers the same submission
order, and `_docs/decisions.md` item 9 keeps it inside the server: it appears in
no response body and in no JSON embedded in a page. A card is addressed publicly
by `Card.public_id`, a random UUID4 written when the card is created, and that
is what `cards[].id` carries — a handle a board can be mutated by (#12), voted
on, and keyed by in React (#14), which sorts into no order at all.


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
          "id": "6f1c…",           # Card.public_id as a string, the handle
                                   # #12 mutates by. Never Card.pk.
          "category": "START",     # Card.Category value
          "text": "…",             # Card.text
          "cluster": null,         # Cluster id, or null for ungrouped
          "mine": true             # this viewer wrote it and did not mark it
                                   # anonymous — see below and card_payload()
        }
      ],
      "clusters": [
        {
          "id": 4,                 # Cluster.pk — an integer, and see below
          "name": "Deploys",       # Cluster.name, the team's words
          "position": 1,           # Cluster.position, the board's order
          "is_auto_generated": false,  # #22 suggested it; wording only
          "status": "PENDING"      # Cluster.Status, moved by #16
        }
      ],
      "votes": {"mine": [], "remaining": 3},
      "vote_totals": {}            # PRESENT ONLY from DISCUSS on
    }

`cards` holds the viewer's own cards and nobody else's before `REVEAL`, and
every card in the cycle in `position` order from `REVEAL` on. No card carries an
author, at any stage, anonymous or not — see `card_payload()`.

`cards[].mine` is the one person-fact the board is allowed to carry, and it is
only ever a fact about the viewer themselves — `_docs/decisions.md` item 10. It
is `true` when the server can see that this viewer wrote the card *and* did not
mark it anonymous, and `false` for everything else: another member's card, and
the viewer's own anonymous card alike. It excludes the viewer's own anonymous
cards deliberately, so a projected board cannot show the room which card the
facilitator wrote anonymously, and its value for a given card and viewer does not
change at the reveal — an own anonymous card reads `false` before the reveal
because `is_anonymous` is set, and `false` after it because item 3 has nulled the
author. It is never an author, never a name, and it is `false` for both a card
somebody else wrote and a card the viewer wrote anonymously, so it hands a client
no way to tell those two cases apart. See `card_payload()`.

`vote_totals` is the whole of what a viewer who may not see the totals must not
receive, so it is one key that is simply absent rather than a set of zeroes or
nulls spread through the clusters. `can_see_vote_totals` decides, and it is
False for everyone while the stage is `VOTE`.

A cluster is addressed by its integer primary key, in the payload and in #12's
requests alike, and it deliberately has no opaque handle. `_docs/decisions.md`
item 9 is about `Card` and says so: a cluster is made by the team in front of
the team, so the order clusters were created in is not a fact about a person and
a sequence in the payload gives nothing away. The asymmetry with `cards[].id` is
the decision, not an oversight.

`cards[].cluster` is the same integer, or null, and it is the only place the
grouping is stated. A cluster does not carry a list of its cards: two statements
of one relation drift, and the client that draws the board has every card in
front of it already.

What is a fixed empty value today and why:

- `votes.mine` and `vote_totals`: `Vote` arrives with #15. `votes.remaining` is
  `Retrospective.votes_per_member` until there is anything to spend.

#15 fills those in by filling in the functions below. It adds no second
serializer, so the shape #13 and #14 are written against cannot drift — and
neither did #12, whose seven mutation endpoints answer with `board_state()`
itself rather than with a body of their own.
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
        "cards": [card_payload(card, user) for card in visible_cards(user, retro)],
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


def card_payload(card: Card, user) -> dict:
    """One card, as five fields, none of which is another person or a time.

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

    `mine` is the exception `_docs/decisions.md` item 10 carves out, and it is
    only ever a fact about `user` themselves: `true` exactly when this viewer
    wrote the card and did not mark it anonymous. It is read straight off the
    row — `card.author_id`, the foreign-key column, never `card.author`, which
    would fetch the user — and `card.is_anonymous`, both already loaded, so it
    costs no query and joins no table. It is deliberately `false` for the
    viewer's own anonymous card: before the reveal because `is_anonymous` is
    set, and after it because item 3 has nulled the author, which is the same
    `false` on both sides of the reveal. It is `false` for another member's card
    too, and gives no way to tell those two `false`s apart — there is no
    `is_anonymous` in the payload to qualify either one.

    `id` is `public_id` and never `pk` — `_docs/decisions.md` item 9. It is sent
    as a string rather than as whatever `json` would make of a `UUID`, so the
    client receives one type for the handle at every stage and from every
    endpoint.
    """
    return {
        "id": str(card.public_id),
        "category": card.category,
        "text": card.text,
        # `cluster_id`, not `cluster`: the id is already on the row, so a board
        # of forty cards costs no query per card, and an ungrouped card is null
        # here without a branch.
        "cluster": card.cluster_id,
        # `author_id`, the column, not `author`, the relation: reading the FK's
        # own value touches no other table, so the mark is free on a row that is
        # already loaded. False for an own anonymous card at every stage — see
        # above — and never true for anybody but `user`.
        "mine": card.author_id == user.pk and not card.is_anonymous,
    }


# --------------------------------------------------------------------------
# Clusters and votes
# --------------------------------------------------------------------------


def cluster_payloads(retro: Retrospective) -> list[dict]:
    """The board's clusters, in `position` order.

    Nothing about a cluster is private — its name and its cards are the board —
    so this takes no user. What *is* private is how many votes are on it, which
    is why the totals live in their own key and not in these dicts.

    One query whatever the board holds, and no card is reached from here: the
    grouping is stated once, on the card. `Cluster.Meta.ordering` is
    `["position", "id"]`, so the order is total and a poll cannot hand back the
    same board in a different order.

    No timestamp, for the same reason no card carries one: `position` is what
    draws the board, and a creation time on a row that cards point at is one
    more thing to line up against `Card.created_at`.
    """
    return [
        {
            "id": cluster.pk,
            "name": cluster.name,
            "position": cluster.position,
            "is_auto_generated": cluster.is_auto_generated,
            "status": cluster.status,
        }
        for cluster in retro.clusters.all()
    ]


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
