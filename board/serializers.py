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
      "votes": {                   # this viewer's own votes, never anyone else's
        "mine": [
          {"cluster": 4,           # a cluster id the viewer has votes on
           "weight": 2}            # how many of their votes sit there
        ],
        "remaining": 1             # budget minus everything they have spent
      },
      "vote_totals": {"4": 5},     # votes per cluster id; PRESENT ONLY from
                                   # DISCUSS on, absent (not empty, not zeroed)
                                   # at every earlier stage
      "notes": [                   # the discussion's notes; PRESENT ONLY from
                                   # DISCUSS on, absent at every earlier stage
        {
          "id": 9,                 # Note.pk — an integer, a note is not a card
          "cluster": 4,            # the cluster it is against, or null for a
                                   # note about the retrospective as a whole
          "author": "Ada Viewer",  # the writer's display name; a note is always
                                   # attributed — see note_payloads() and item 10
          "text": "…"              # Note.text
        }
      ]
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

`votes` is scoped to the viewer by construction — `vote_payload()` filters on
the viewer's own rows and computes their remaining budget from them, so there is
no branch that could widen it to another member and no aggregate over the room
for one to hide in. `votes.mine` carries a `cluster` id and a `weight` per
cluster the viewer has voted on, and `votes.remaining` is what is left of their
`votes_per_member` budget. A member who has not voted gets `{"mine": [],
"remaining": votes_per_member}`.

`vote_totals` is the whole of what a viewer who may not see the totals must not
receive, so it is one key that is simply absent rather than a set of zeroes or
nulls spread through the clusters. `can_see_vote_totals` decides, and it is
False for everyone while the stage is `VOTE`: the totals appear only from
`DISCUSS` on, once the allocation is final, so no member can watch a running
count move and difference two polls into another member's ballot. A member's own
votes are never in `vote_totals` as anything separable from the aggregate, and no
member's identity is ever attached to a count. How many members have spent their
whole budget — the one thing the facilitator watches to know when to close voting
— is not here at all: it is a facilitator-only count on its own endpoint
(`board.views.vote_progress_view`), never a per-member fact and never mixed into
the board every member polls.

A cluster is addressed by its integer primary key, in the payload and in #12's
requests alike, and it deliberately has no opaque handle. `_docs/decisions.md`
item 9 is about `Card` and says so: a cluster is made by the team in front of
the team, so the order clusters were created in is not a fact about a person and
a sequence in the payload gives nothing away. The asymmetry with `cards[].id` is
the decision, not an oversight.

The `clusters` list is in `position` order up to VOTE, and in agenda order from
DISCUSS on — #16. The agenda is highest vote weight first, ties broken by
`position` then `id`, so it is a total order that does not reshuffle between
polls and a cluster with no votes falls to the bottom rather than being hidden.
It reuses the same totals `vote_totals` is built from, which appear only once the
allocation is frozen, so ordering by weight reveals no member's individual vote —
exactly the guarantee #15's gate already holds. The client renders the list in
the order it arrives; there is one ordering rule, computed server-side, not two.

`notes` is #16's, and carries the discussion's notes from DISCUSS on. A note is
always attributed — `author` is the writer's display name — which item 10 allows
for notes precisely because it forbids it for cards: a note has no anonymous
alternative, so naming its author eliminates nobody. A note never carries a
card's author or a card's `pk`: it points at a cluster's integer id, or at
nothing, and at its own author, and at none of the facts the board keeps off
itself. It carries no timestamp either — `created_at` orders the notes and stays
on the row. See `note_payloads()`.

`cards[].cluster` is the same integer, or null, and it is the only place the
grouping is stated. A cluster does not carry a list of its cards: two statements
of one relation drift, and the client that draws the board has every card in
front of it already.

#15 filled `votes` and `vote_totals` in by filling in the functions below, and
added the two vote-casting endpoints and the facilitator's progress endpoint in
`board/`. It added no second serializer, so the shape #13 and #14 are written
against cannot drift — and neither did #12, whose seven mutation endpoints answer
with `board_state()` itself rather than with a body of their own. The cast and
withdraw endpoints answer with `board_state()` too, so a voter's own updated
`votes` comes straight back in the response to their POST.
"""

from django.db.models import Sum

from cycles.models import Card, revealed_cards
from projects.permissions import can_see_vote_totals
from retro.models import Retrospective, Vote

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
    # The totals are computed once and used twice: as the `vote_totals` key, and
    # as the weight the DISCUSS agenda orders the clusters by. They are reached
    # only when they may be seen, and from DISCUSS on every member may — which is
    # exactly the stage the agenda exists in, so the two line up rather than
    # needing separate gates. `None` outside that window, where the board keeps
    # its `position` order and the totals key does not exist at all.
    totals = vote_totals(retro) if can_see_vote_totals(user, retro) else None

    state = {
        "id": retro.pk,
        "stage": retro.stage,
        "version": retro.version,
        "changed": True,
        "cards": [card_payload(card, user) for card in visible_cards(user, retro)],
        "clusters": cluster_payloads(retro, totals),
        "votes": vote_payload(user, retro),
    }

    # Not a zero, not a null, not an empty object the client is trusted to
    # ignore: while the totals are secret the key does not exist. #15's
    # secrecy criterion is that a voter's raw response body is free of any
    # indication that anyone else voted, and an absent key is the only shape
    # that stays true however the numbers are computed later.
    if totals is not None:
        state["vote_totals"] = totals

    # The discussion's notes, from DISCUSS on. Absent before then, the same way
    # the totals are: there are no notes to carry until the agenda exists, and
    # keeping the key out of the earlier stages leaves #11's and #12's payloads
    # exactly as they were. Present and possibly empty from DISCUSS, where a
    # member adds them and everyone polls them; still present and read-only at
    # COMPLETE.
    if retro.has_reached(Retrospective.Stage.DISCUSS):
        state["notes"] = note_payloads(retro)

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


def cluster_payloads(retro: Retrospective, totals: dict | None = None) -> list[dict]:
    """The board's clusters, in board order — or, from DISCUSS, in agenda order.

    Nothing about a cluster is private — its name and its cards are the board —
    so this takes no user. What *is* private is how many votes are on it, which
    is why the totals live in their own key and not in these dicts.

    The order is the whole of #16's agenda. Before DISCUSS `totals` is None and
    the order is `position`, then `id` — `Cluster.Meta.ordering`, which is total,
    so a poll cannot hand back the same board in a different order. From DISCUSS
    `totals` carries each cluster's vote weight and the order becomes the agenda:
    highest weight first, then `position`, then `id`. The tie-break is what keeps
    the agenda from reshuffling itself between polls — two clusters on the same
    weight always sort the same way — and a cluster with no votes has weight 0, so
    it appears at the bottom rather than being hidden. The client renders the list
    in the order it arrives and sorts by nothing itself, so there is one ordering
    rule here and not two.

    The weight comes from `totals`, the same grouped query `vote_totals()` already
    ran for the totals key, so the agenda costs no query of its own: one query for
    the clusters whatever the board holds, and no card is reached from here — the
    grouping is stated once, on the card.

    No timestamp, for the same reason no card carries one: `position` is what
    draws the board, and a creation time on a row that cards point at is one
    more thing to line up against `Card.created_at`.
    """
    clusters = retro.clusters.all()
    if totals is not None:
        # Sorted in Python over the handful of clusters a board holds, using the
        # totals already in hand: `-weight` first for highest-first, then the
        # model's own `position` and `id` as the stable tie-break.
        clusters = sorted(
            clusters,
            key=lambda cluster: (-totals.get(cluster.pk, 0), cluster.position, cluster.pk),
        )
    return [
        {
            "id": cluster.pk,
            "name": cluster.name,
            "position": cluster.position,
            "is_auto_generated": cluster.is_auto_generated,
            "status": cluster.status,
        }
        for cluster in clusters
    ]


def vote_payload(user, retro: Retrospective) -> dict:
    """This viewer's own votes and what is left of their budget. Never anyone else's.

    `_docs/decisions.md` item 2: votes are reassignable while the stage is
    `VOTE`, which is only safe while nobody can see the running totals. So this
    is scoped to `user` by construction — the query filters on `user=user`, there
    is no branch here that could widen it to another member, and no aggregate for
    one to hide in.

    `mine` is one entry per cluster this viewer has votes on — `cluster` (the
    integer id the payload and #12's requests both address a cluster by) and
    `weight` (how many they stacked there) — ordered by cluster id so the same
    votes come back in the same order from one poll to the next. A cluster the
    viewer has not voted on is simply absent, the same way a member with no votes
    gets an empty list rather than a row of zeroes.

    `remaining` is the budget minus everything they have spent across every
    cluster. It is what an about-to-vote client checks and what the cast endpoint
    enforces under a lock; computed here from the same rows so the number the
    board shows and the number the server defends are read the same way.

    One query, whatever the board holds: the viewer's rows for this
    retrospective, summed in Python over the handful a single member can have
    (at most `votes_per_member` votes spread across at most that many clusters).
    """
    mine = [
        {"cluster": cluster_id, "weight": weight}
        for cluster_id, weight in Vote.objects.filter(retrospective=retro, user=user)
        .order_by("cluster_id")
        .values_list("cluster_id", "weight")
    ]
    spent = sum(vote["weight"] for vote in mine)
    return {
        "mine": mine,
        "remaining": retro.votes_per_member - spent,
    }


def vote_totals(retro: Retrospective) -> dict:
    """Votes per cluster, keyed by cluster id. Reached only when they may be seen.

    Called from `board_state()` behind `can_see_vote_totals`, which is False for
    everyone while the stage is `VOTE` and True for project members from
    `DISCUSS` on. That gate is the whole of the secrecy guarantee: totals become
    visible only once voting is over and the allocation is final, so there is no
    moment at which a member could poll twice, watch a total move, and difference
    two snapshots into another member's vote — by the time this function is ever
    reached, nothing about the tally can change again.

    A cluster with no votes is absent rather than present as a zero: an empty
    board is `{}`, which is what `tests/test_board.py` pins, and a cluster nobody
    voted for says nothing at all rather than "nobody voted here". The sum is one
    grouped query over the retrospective's votes, so the cost does not grow with
    the number of clusters or of voters. Keys are cluster ids; JSON renders them
    as strings, which is how a cluster id already travels as an object key.
    """
    totals = (
        Vote.objects.filter(retrospective=retro).values("cluster_id").annotate(total=Sum("weight"))
    )
    return {row["cluster_id"]: row["total"] for row in totals}


# --------------------------------------------------------------------------
# Notes — #16
# --------------------------------------------------------------------------


def note_payloads(retro: Retrospective) -> list[dict]:
    """The discussion's notes, in `created_at` order, each with its author's name.

    Reached only from DISCUSS on, so before then there is no notes key and no
    query. A note is always attributed and the board says so — `author` is the
    writer's display name — and that is correct: `_docs/decisions.md` item 10
    destroys authorship on *cards*, because a name on one card identifies the
    anonymous ones by elimination, and it says in as many words that a note is
    different. A note is written in a live discussion and is already attributable
    to whoever said it, so naming its author eliminates nothing.

    What a note never carries is anything about a *card*: no card's author, and no
    card's `pk`. It points at a `cluster` — the integer id the whole team made in
    front of the team, `_docs/decisions.md` item 9 keeps *cards* to `public_id`
    and says a cluster's is fine — or at nothing, a note about the retrospective
    as a whole. So the board's card-privacy is untouched: a note leaks neither of
    the two facts items 9 and 10 keep off the board.

    No `created_at` in the body, only the order it imposes. The timestamp survives
    on the row and orders the notes, but sending it would put a moment on the
    board — one more thing a later feature could line up against `Card.created_at`,
    which is the submission order the reveal's shuffle exists to destroy. A note's
    `id` is its integer primary key — a note is not a card — and it is the handle
    the edit and delete endpoints take.

    One query, whatever the board holds: the notes for this retrospective with
    their authors joined, so a board of forty notes costs no user fetch per note.
    """
    return [
        {
            "id": note.pk,
            "cluster": note.cluster_id,
            "author": note.author.display_name,
            "text": note.text,
        }
        for note in retro.notes.select_related("author").all()
    ]
