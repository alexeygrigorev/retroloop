"""The board's seven write endpoints: what they change, and what they refuse.

Every test here maps to an acceptance criterion of issue #12. Five themes run
through the file, and they are the reason it is shaped the way it is.

**The endpoints are driven from a registry.** `ACTIONS` names all seven, with a
body that is valid for each, so the sweeps below — the stage table, access, the
primary key, CSRF, the version — run over every endpoint rather than over the
convenient ones. An endpoint added later without an entry fails
`test_every_write_endpoint_in_the_urlconf_is_exercised` immediately.

**A refusal is proved by attempting it, with a valid CSRF token.** A test that
asserts a 403 for a request the middleware would have refused anyway proves
nothing about the rule it claims to test, so every refusal in this file is
posted through a client that enforces CSRF, carrying a token that works — and
one test proves the token works by getting a 200 with it.

**Absence is asserted, not presence.** A refusal is asserted as *nothing
changed*: every card's cluster, every cluster's fields, the number of cards, the
number of clusters and the version are snapshotted before the request and
compared after it. "Rejected with a 404" and "rejected and acted on anyway" look
identical to a test that only reads the status code.

**A card is named by `Card.public_id`, never by `Card.pk`.**
`_docs/decisions.md` item 9. The pk sweep posts each card's primary key to every
endpoint that takes a card and asserts a 404 with nothing changed — no fallback
to a primary-key lookup, at any endpoint. Clusters are the deliberate
asymmetry: they are named by their integer pk, which the same sweep proves by
using one successfully in the same world.

**Nothing is re-decided.** `projects/permissions.py` holds every rule, including
the five #12 added. The stage table below is the behaviour of the endpoints;
`tests/test_permissions.py` is the behaviour of the predicates, and neither
restates the other's job.
"""

import json
import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.test import Client
from django.urls import get_resolver, reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import CLUSTER_NAME_MAX_LENGTH, STAGE_ORDER, Cluster, Retrospective
from retro.services import advance_stage

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: The two stages in which the board's shape may change, and the four in which
#: it may not. Derived from the stage order rather than listed twice.
OPEN_STAGES = [Stage.REVEAL, Stage.CLUSTER]
FROZEN_STAGES = [stage for stage in STAGE_ORDER if stage not in OPEN_STAGES]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str, **extra) -> User:
    return User.objects.create_user(
        username=username, password=PASSWORD, display_name=display_name, **extra
    )


def log_in(client: Client, user: User) -> None:
    assert client.login(username=user.username, password=PASSWORD)


@pytest.fixture
def owner(db) -> User:
    """The project's owner and this cycle's facilitator, so the board can be advanced."""
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def ada(project: Project) -> User:
    """An ordinary member. Every request here is Ada's unless it says otherwise."""
    user = make_user("ada", "Ada Viewer")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    """A real account, on a project of their own and not on ours."""
    user = make_user("outsider", "Ora Outsider")
    elsewhere = Project.objects.create(name="Payments", owner=user)
    Membership.objects.create(project=elsewhere, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def root(db) -> User:
    """A superuser on no project. `_docs/decisions.md` item 3 has no admin exception."""
    return make_user("root", "Root Rooter", is_superuser=True, is_staff=True)


def make_cycle(project: Project, facilitator: User, week: date) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=week,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
    )


class Board:
    """One retrospective in CLUSTER, with two clusters and four cards.

    Two of the cards are in the first cluster, one is in the second, and one is
    ungrouped — so every operation below has something real to act on and the
    ungrouped state is live from the start rather than only after a test makes
    it so.
    """

    def __init__(self, project: Project, facilitator: User, author: User, week: date) -> None:
        self.cycle = make_cycle(project, facilitator, week)
        self.cards = [
            Card.objects.create(
                cycle=self.cycle,
                author=author,
                category=Card.Category.START,
                text=f"card {index} on {week}",
            )
            for index in range(4)
        ]
        self.retro = Retrospective.objects.create(cycle=self.cycle)
        advance_to(self.retro, facilitator, Stage.CLUSTER)

        self.first = Cluster.objects.create(retrospective=self.retro, name="Deploys", position=1)
        self.second = Cluster.objects.create(retrospective=self.retro, name="Reviews", position=2)
        Card.objects.filter(pk__in=[self.cards[0].pk, self.cards[1].pk]).update(cluster=self.first)
        Card.objects.filter(pk=self.cards[2].pk).update(cluster=self.second)
        self.refresh()

    def refresh(self) -> None:
        self.retro.refresh_from_db()
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.cards = [Card.objects.get(pk=card.pk) for card in self.cards]

    @property
    def grouped(self) -> Card:
        """A card that is in `first`."""
        return Card.objects.get(pk=self.cards[0].pk)

    @property
    def ungrouped(self) -> Card:
        """The card nobody has put in a cluster."""
        return Card.objects.get(pk=self.cards[3].pk)

    @property
    def elsewhere(self) -> Card:
        """A card that is in `second`, and so not in `first`."""
        return Card.objects.get(pk=self.cards[2].pk)


@pytest.fixture
def board(project: Project, owner: User, ada: User) -> Board:
    return Board(project, owner, ada, MONDAY)


@pytest.fixture
def other_board(project: Project, owner: User, ada: User) -> Board:
    """A second retrospective on the same project, one week later.

    The same project on purpose: a card or cluster from another *retrospective*
    has to be refused even when the person asking is entitled to see both, which
    is a stronger statement than refusing a stranger's board.
    """
    return Board(project, owner, ada, date(2026, 7, 27))


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> Retrospective:
    """Walk the board forward through the real stage machine, never by assignment."""
    while retro.stage != stage:
        advance_stage(facilitator, retro)
    return retro


# --------------------------------------------------------------------------
# The registry
#
# One entry per endpoint: its URL name, a body that is valid on the `board`
# fixture, and which field of that body — if any — names a card. Nothing else
# in this file lists the endpoints.
# --------------------------------------------------------------------------


class Action:
    def __init__(self, url_name: str, body, *, card_field: str | None, broken: dict) -> None:
        self.url_name = url_name
        self._body = body
        #: Which field of the body names a card, if any.
        self.card_field = card_field
        #: An override that makes this endpoint's body invalid, whatever else it
        #: carries — so "a refusal writes nothing" can be swept over all seven.
        self.broken = broken

    def body(self, board: Board) -> dict:
        return self._body(board)

    def url(self, retro: Retrospective) -> str:
        return reverse(self.url_name, args=[retro.pk])


ACTIONS: dict[str, Action] = {
    "move a card to a cluster": Action(
        "board-card-move",
        lambda board: {"card": str(board.ungrouped.public_id), "cluster": board.first.pk},
        card_field="card",
        broken={"card": ""},
    ),
    "move a card out to ungrouped": Action(
        "board-card-ungroup",
        lambda board: {"card": str(board.grouped.public_id)},
        card_field="card",
        broken={"card": ""},
    ),
    "create a cluster": Action(
        "board-cluster-create",
        lambda board: {"name": "Onboarding"},
        card_field=None,
        broken={"name": "   "},
    ),
    "rename a cluster": Action(
        "board-cluster-rename",
        lambda board: {"cluster": board.first.pk, "name": "Deployment pain"},
        card_field=None,
        broken={"name": "   "},
    ),
    "merge two clusters": Action(
        "board-cluster-merge",
        lambda board: {"source": board.second.pk, "target": board.first.pk},
        card_field=None,
        broken={"source": ""},
    ),
    "split a cluster": Action(
        "board-cluster-split",
        lambda board: {
            "cluster": board.first.pk,
            "cards": [str(board.grouped.public_id)],
            "name": "Rollbacks",
        },
        card_field="cards",
        broken={"cards": []},
    ),
    "delete a cluster": Action(
        "board-cluster-delete",
        lambda board: {"cluster": board.first.pk},
        card_field=None,
        broken={"cluster": ""},
    ),
}

NAMES = list(ACTIONS)

#: The endpoints that name a card, and so have to refuse a primary key.
CARD_NAMES = [name for name, action in ACTIONS.items() if action.card_field is not None]

#: The endpoints that name a card or a cluster, and so can be handed one from
#: another board. Creating a cluster names neither.
ID_NAMES = [name for name in NAMES if name != "create a cluster"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def post(client: Client, action: Action, board: Board, **overrides):
    """Post a valid body for `action`, with any field replaced by `overrides`."""
    body = action.body(board) | overrides
    return client.post(action.url(board.retro), body)


def snapshot(*boards: Board) -> dict:
    """Everything a mutation could change, for every board handed in.

    Compared whole after a refusal. It carries the cards' clusters, every
    cluster's own fields, both counts and the version, so "nothing changed"
    covers a card that moved, a cluster that was renamed, a row that was deleted
    and a version that was bumped alike.
    """
    retro_ids = [board.retro.pk for board in boards]
    cycle_ids = [board.cycle.pk for board in boards]
    return {
        "cards": sorted(
            Card.objects.filter(cycle_id__in=cycle_ids).values_list("pk", "cluster_id")
        ),
        "clusters": sorted(
            Cluster.objects.filter(retrospective_id__in=retro_ids).values_list(
                "pk", "retrospective_id", "name", "position", "is_auto_generated", "status"
            )
        ),
        "card_count": Card.objects.filter(cycle_id__in=cycle_ids).count(),
        "cluster_count": Cluster.objects.filter(retrospective_id__in=retro_ids).count(),
        "versions": list(
            Retrospective.objects.filter(pk__in=retro_ids)
            .order_by("pk")
            .values_list("pk", "version")
        ),
    }


def version_of(retro: Retrospective) -> int:
    return Retrospective.objects.values_list("version", flat=True).get(pk=retro.pk)


def cluster_id_of(card: Card) -> int | None:
    return Card.objects.values_list("cluster_id", flat=True).get(pk=card.pk)


def strict_client(user: User, retro: Retrospective) -> tuple[Client, str]:
    """A client that enforces CSRF, logged in, plus a token that works.

    Every refusal in this file is posted through one of these. Without it a 403
    from the middleware and a refusal from the endpoint are indistinguishable,
    and a test that meant to prove the second would pass on the first.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, user)
    # Rendering a page that carries the token is what sets the cookie, and it is
    # the same thing a browser does before it posts.
    assert client.get(reverse("retro-detail", args=[retro.pk])).status_code == 200
    return client, client.cookies["csrftoken"].value


def post_with_token(client: Client, token: str, action: Action, board: Board, **overrides):
    body = action.body(board) | overrides
    return client.post(action.url(board.retro), body, HTTP_X_CSRFTOKEN=token)


# --------------------------------------------------------------------------
# A. The relation
# --------------------------------------------------------------------------


def test_card_cluster_is_a_nullable_foreign_key_to_cluster() -> None:
    """Ungrouped is a normal state, so the column is nullable and blank-able."""
    field = Card._meta.get_field("cluster")

    assert field.related_model is Cluster
    assert field.null is True
    assert field.blank is True
    # A card outlives every grouping anyone puts it in: deleting a cluster
    # ungroups its cards and never takes one with it.
    assert field.remote_field.on_delete.__name__ == "SET_NULL"
    assert field.target_field.name == "id"


@pytest.mark.django_db
def test_a_card_starts_ungrouped_and_that_is_not_an_error(board: Board) -> None:
    assert cluster_id_of(board.ungrouped) is None
    assert Card.objects.filter(cycle=board.cycle, cluster__isnull=True).count() == 1


def test_a_cluster_carries_exactly_the_five_fields_the_issue_names() -> None:
    concrete = {field.name for field in Cluster._meta.fields}

    assert concrete == {"id", "retrospective", "name", "position", "is_auto_generated", "status"}
    assert Cluster._meta.get_field("is_auto_generated").default is False
    assert Cluster._meta.get_field("status").default == Cluster.Status.PENDING


def test_a_clusters_status_has_the_four_values_and_no_others() -> None:
    """Defined here, moved by #16, and not touched by any endpoint in this issue."""
    assert list(Cluster.Status.values) == ["PENDING", "DISCUSSED", "SKIPPED", "DEFERRED"]


def test_a_cluster_belongs_to_a_retrospective_and_has_no_public_handle() -> None:
    """The asymmetry with `Card`, stated: item 9 is about cards and says so."""
    assert Cluster._meta.get_field("retrospective").related_model is Retrospective
    assert not any(field.name == "public_id" for field in Cluster._meta.get_fields())


@pytest.mark.django_db
def test_a_suggested_cluster_is_renamed_merged_split_and_deleted_like_any_other(
    client: Client, board: Board, ada: User
) -> None:
    """`is_auto_generated` affects display wording only — no endpoint branches on it.

    All four operations are driven against a cluster #22 would have written, in
    one test, because "exactly like a hand-made one" is a statement about the
    set of them and not about any single one.
    """
    Cluster.objects.filter(pk__in=[board.first.pk, board.second.pk]).update(is_auto_generated=True)
    board.refresh()
    log_in(client, ada)

    assert board.first.is_auto_generated is True
    assert post(client, ACTIONS["rename a cluster"], board).status_code == 200
    assert post(client, ACTIONS["split a cluster"], board).status_code == 200
    assert post(client, ACTIONS["merge two clusters"], board).status_code == 200
    assert post(client, ACTIONS["delete a cluster"], board).status_code == 200

    # And the flag survives everything that did not set it: the split's new
    # cluster is hand-made, the merged and deleted ones are gone.
    assert Cluster.objects.filter(retrospective=board.retro, is_auto_generated=True).count() == 0
    assert Cluster.objects.filter(retrospective=board.retro).count() == 1


# --------------------------------------------------------------------------
# B. The endpoints exist, and only as POST
# --------------------------------------------------------------------------


def test_every_write_endpoint_in_the_urlconf_is_exercised() -> None:
    """An endpoint added later without an entry in `ACTIONS` fails here.

    Discovered from the resolver rather than listed, so the registry cannot fall
    behind the URLs it is supposed to cover.
    """
    registered = {
        name
        for name in get_resolver().reverse_dict
        if isinstance(name, str)
        and name.startswith("board-")
        and name != "board-state"
        # #15's voting endpoints (`board-vote-cast`, `-withdraw`, `-progress`)
        # are writes and a read of their own, exercised in `tests/test_votes.py`
        # rather than through this registry. This test guards #12's seven cluster
        # and card mutations, which is what `ACTIONS` covers.
        and not name.startswith("board-vote-")
    }

    assert registered == {action.url_name for action in ACTIONS.values()}
    assert len(ACTIONS) == 7


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_endpoint_answers_a_post_from_a_member(
    client: Client, board: Board, ada: User, name: str
) -> None:
    log_in(client, ada)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 200, response.content
    assert response.headers["Content-Type"] == "application/json"


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_no_get_mutates(client: Client, board: Board, ada: User, name: str) -> None:
    """405, and the board is untouched — the write is not merely undocumented."""
    log_in(client, ada)
    action = ACTIONS[name]
    before = snapshot(board)

    for method in (client.get, client.put, client.delete):
        assert method(action.url(board.retro)).status_code == 405

    assert snapshot(board) == before


# --------------------------------------------------------------------------
# C. What each one does
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_card_moves_into_a_cluster(client: Client, board: Board, ada: User) -> None:
    log_in(client, ada)
    card = board.ungrouped

    response = post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(card.public_id),
        cluster=board.second.pk,
    )

    assert response.status_code == 200
    assert cluster_id_of(card) == board.second.pk


@pytest.mark.django_db
def test_a_card_moves_from_one_cluster_to_another(client: Client, board: Board, ada: User) -> None:
    """Last write wins: a move states where the card ends up, not where it was."""
    log_in(client, ada)
    card = board.grouped
    assert cluster_id_of(card) == board.first.pk

    post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(card.public_id),
        cluster=board.second.pk,
    )

    assert cluster_id_of(card) == board.second.pk


@pytest.mark.django_db
def test_a_card_moves_out_to_ungrouped(client: Client, board: Board, ada: User) -> None:
    log_in(client, ada)
    card = board.grouped

    response = post(client, ACTIONS["move a card out to ungrouped"], board)

    assert response.status_code == 200
    assert cluster_id_of(card) is None
    # The card is still there. Ungrouping is not deleting.
    assert Card.objects.filter(pk=card.pk).exists()


@pytest.mark.django_db
def test_a_cluster_is_created_empty_at_the_end_of_the_board(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)

    response = post(client, ACTIONS["create a cluster"], board, name="  Onboarding  ")

    assert response.status_code == 200
    created = Cluster.objects.get(retrospective=board.retro, name="Onboarding")
    # Trimmed, at the end, hand-made, and holding nothing.
    assert created.position == board.second.position + 1
    assert created.is_auto_generated is False
    assert created.status == Cluster.Status.PENDING
    assert created.cards.count() == 0


@pytest.mark.django_db
def test_a_cluster_is_renamed(client: Client, board: Board, ada: User) -> None:
    log_in(client, ada)

    response = post(client, ACTIONS["rename a cluster"], board, name="  Deployment pain  ")

    assert response.status_code == 200
    board.refresh()
    assert board.first.name == "Deployment pain"


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "   ", "\t\n ", "\r\n", "\u00a0"])
def test_renaming_rejects_an_empty_or_whitespace_only_name(
    board: Board, ada: User, name: str
) -> None:
    """A nameless group is not something the team can talk about.

    Posted with a valid CSRF token, so the refusal is the endpoint's and not the
    middleware's, and asserted as nothing changed rather than as a status code.
    """
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS["rename a cluster"], board, name=name)

    assert response.status_code == 400
    assert response.json()["error"]
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "   ", "\t\n "])
def test_creating_rejects_an_empty_or_whitespace_only_name(
    board: Board, ada: User, name: str
) -> None:
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS["create a cluster"], board, name=name)

    assert response.status_code == 400
    assert snapshot(board) == before


@pytest.mark.django_db
def test_a_name_longer_than_the_column_is_refused_rather_than_truncated(
    board: Board, ada: User
) -> None:
    """The cap is a rule about the data, so it is a 400 and not a driver error."""
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(
        client,
        token,
        ACTIONS["rename a cluster"],
        board,
        name="x" * (CLUSTER_NAME_MAX_LENGTH + 1),
    )

    assert response.status_code == 400
    assert snapshot(board) == before

    # And the length that fits still works, so the boundary is the column's.
    fits = post_with_token(
        client, token, ACTIONS["rename a cluster"], board, name="x" * CLUSTER_NAME_MAX_LENGTH
    )
    assert fits.status_code == 200


@pytest.mark.django_db
def test_merging_moves_every_card_and_deletes_the_source(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    moving = board.elsewhere
    cards_before = Card.objects.filter(cycle=board.cycle).count()

    response = post(client, ACTIONS["merge two clusters"], board)

    assert response.status_code == 200
    assert cluster_id_of(moving) == board.first.pk
    assert not Cluster.objects.filter(pk=board.second.pk).exists()
    # Cards are moved, never deleted, whatever happens to the cluster.
    assert Card.objects.filter(cycle=board.cycle).count() == cards_before


@pytest.mark.django_db
def test_merging_a_cluster_into_itself_is_rejected(board: Board, ada: User) -> None:
    """Not a no-op: it would delete the cluster whose cards had just moved in."""
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(
        client,
        token,
        ACTIONS["merge two clusters"],
        board,
        source=board.first.pk,
        target=board.first.pk,
    )

    assert response.status_code == 400
    assert response.json()["error"]
    assert snapshot(board) == before
    assert Cluster.objects.filter(pk=board.first.pk).exists()


@pytest.mark.django_db
def test_splitting_moves_the_named_cards_to_a_new_cluster(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    moving = board.grouped
    staying = Card.objects.get(pk=board.cards[1].pk)

    response = post(
        client,
        ACTIONS["split a cluster"],
        board,
        cards=[str(moving.public_id)],
        name="Rollbacks",
    )

    assert response.status_code == 200
    created = Cluster.objects.get(retrospective=board.retro, name="Rollbacks")
    assert cluster_id_of(moving) == created.pk
    assert cluster_id_of(staying) == board.first.pk
    assert created.position == board.second.position + 1


@pytest.mark.django_db
def test_a_split_without_a_name_starts_under_the_name_it_came_out_of(
    client: Client, board: Board, ada: User
) -> None:
    """Optional, because a split usually happens before anyone has words for it."""
    log_in(client, ada)
    body = {"cluster": board.first.pk, "cards": [str(board.grouped.public_id)]}

    response = client.post(ACTIONS["split a cluster"].url(board.retro), body)

    assert response.status_code == 200
    assert Cluster.objects.filter(retrospective=board.retro, name=board.first.name).count() == 2


@pytest.mark.django_db
def test_splitting_rejects_a_card_that_is_not_in_the_source_cluster(
    board: Board, ada: User
) -> None:
    """Rejected rather than silently ignored, and named in the message.

    A client that believes it moved two cards and moved one has no way to find
    out, so the whole request fails and the board is left alone. Both kinds of
    outsider are tried: a card in another cluster, and an ungrouped one.
    """
    client, token = strict_client(ada, board.retro)

    for outsider_card in (board.elsewhere, board.ungrouped):
        before = snapshot(board)
        response = post_with_token(
            client,
            token,
            ACTIONS["split a cluster"],
            board,
            cards=[str(board.grouped.public_id), str(outsider_card.public_id)],
        )

        assert response.status_code == 400, outsider_card.pk
        assert str(outsider_card.public_id) in response.json()["error"]
        # Including the card that *was* in the cluster: the split is all or nothing.
        assert snapshot(board) == before


@pytest.mark.django_db
def test_a_split_that_names_no_card_is_refused(board: Board, ada: User) -> None:
    """A split moves cards. One that moves none is a create wearing a costume."""
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS["split a cluster"], board, cards=[])

    assert response.status_code == 400
    assert snapshot(board) == before


@pytest.mark.django_db
def test_deleting_a_cluster_returns_its_cards_to_ungrouped_and_deletes_no_card(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    held = list(Card.objects.filter(cluster=board.first))
    cards_before = Card.objects.filter(cycle=board.cycle).count()
    assert held

    response = post(client, ACTIONS["delete a cluster"], board)

    assert response.status_code == 200
    assert not Cluster.objects.filter(pk=board.first.pk).exists()
    assert Card.objects.filter(cycle=board.cycle).count() == cards_before
    for card in held:
        assert cluster_id_of(card) is None


# --------------------------------------------------------------------------
# D. The card handle — `_docs/decisions.md` item 9
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", CARD_NAMES)
def test_posting_a_cards_primary_key_is_a_404_and_changes_nothing(
    board: Board, ada: User, name: str
) -> None:
    """The criterion, endpoint by endpoint: an integer where a card id belongs.

    404 — the same answer as any other id that does not resolve — and never a
    fallback to a primary-key lookup, which is what the snapshot proves: the
    card whose pk was posted is exactly where it was.

    Posted with a valid CSRF token, so a 404 cannot be a 403 in disguise, and
    the guard below asserts the pk really is that card's, so the test cannot
    pass by posting a number that was never going to resolve anyway.
    """
    client, token = strict_client(ada, board.retro)
    action = ACTIONS[name]
    card = board.grouped if name != "move a card to a cluster" else board.ungrouped
    before = snapshot(board)

    # Guard: this integer is a real card's real primary key, in this cycle.
    assert Card.objects.filter(pk=card.pk, cycle=board.cycle).exists()

    value = [str(card.pk)] if action.card_field == "cards" else str(card.pk)
    response = post_with_token(client, token, action, board, **{action.card_field: value})

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", CARD_NAMES)
def test_the_same_request_works_when_the_card_is_named_by_its_handle(
    board: Board, ada: User, name: str
) -> None:
    """The control for the sweep above. Without it, "404" proves only that
    something was refused — not that the primary key is what was refused."""
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS[name], board)

    assert response.status_code == 200
    assert snapshot(board) != before


@pytest.mark.django_db
@pytest.mark.parametrize("name", CARD_NAMES)
@pytest.mark.parametrize(
    "raw",
    [
        "1",
        "0",
        "-1",
        "",
        "abc",
        "null",
        "None",
        "1.5",
        "0x10",
        "9" * 5000,
        "99999999999999999999999999",
        "1;DROP TABLE cycles_card",
        "١٢",  # Arabic-Indic digits, which int() accepts and uuid.UUID does not
    ],
)
def test_a_card_id_that_is_not_a_handle_is_a_404(
    board: Board, ada: User, name: str, raw: str
) -> None:
    """Junk is the same answer as a pk: 404, with nothing changed and no 500."""
    client, token = strict_client(ada, board.retro)
    action = ACTIONS[name]
    before = snapshot(board)

    value = [raw] if action.card_field == "cards" else raw
    response = post_with_token(client, token, action, board, **{action.card_field: value})

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", CARD_NAMES)
def test_a_handle_that_belongs_to_nothing_is_a_404(board: Board, ada: User, name: str) -> None:
    """A well-formed UUID4 that is nobody's card. The shape is not the answer."""
    client, token = strict_client(ada, board.retro)
    action = ACTIONS[name]
    stranger = str(uuid.uuid4())
    before = snapshot(board)

    value = [stranger] if action.card_field == "cards" else stranger
    response = post_with_token(client, token, action, board, **{action.card_field: value})

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
def test_a_cluster_is_named_by_its_integer_primary_key(
    client: Client, board: Board, ada: User
) -> None:
    """The deliberate asymmetry, driven: item 9 is about `Card` and says so.

    The same request that refuses a card's integer accepts a cluster's, because
    a cluster is made by the team in front of the team and its creation order is
    not a fact about a person.
    """
    log_in(client, ada)

    response = post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(board.ungrouped.public_id),
        cluster=board.first.pk,
    )

    assert response.status_code == 200
    assert cluster_id_of(board.ungrouped) == board.first.pk
    # And the payload calls the cluster by that same integer.
    payload = response.json()
    assert board.first.pk in [cluster["id"] for cluster in payload["clusters"]]


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["", "abc", "1.5", "0", "-3", "9" * 5000, "١٢٣٤٥٦"])
def test_a_cluster_id_that_does_not_resolve_is_a_404(board: Board, ada: User, raw: str) -> None:
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS["rename a cluster"], board, cluster=raw)

    assert response.status_code == 404
    assert snapshot(board) == before


# --------------------------------------------------------------------------
# E. Another retrospective's ids
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", ID_NAMES)
def test_an_id_from_another_retrospective_is_refused_with_404_and_never_acted_on(
    board: Board, other_board: Board, ada: User, name: str
) -> None:
    """One id at a time is swapped for the other board's, every other id valid.

    One at a time on purpose: a merge whose source is local and whose target is
    foreign has to be refused as surely as one where both are, and swapping the
    whole body at once would never ask that question.

    The other board is on the *same project*, and the person asking is a member
    of it, so this is about the retrospective the ids belong to and not about
    who may see them. Both boards are snapshotted: an id acted on over there is
    as bad as one acted on here.
    """
    client, token = strict_client(ada, board.retro)
    action = ACTIONS[name]
    foreign = {
        "card": str(other_board.grouped.public_id),
        "cards": [str(other_board.grouped.public_id)],
        "cluster": other_board.first.pk,
        "source": other_board.second.pk,
        "target": other_board.first.pk,
    }
    fields = [field for field in action.body(board) if field in foreign]
    assert fields, name

    for field in fields:
        before = snapshot(board, other_board)

        response = post_with_token(client, token, action, board, **{field: foreign[field]})

        assert response.status_code == 404, (name, field, response.content)
        assert snapshot(board, other_board) == before


# --------------------------------------------------------------------------
# F. Transactions and versioning
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_mutation_bumps_the_version_exactly_once(
    client: Client, board: Board, ada: User, name: str
) -> None:
    """Once, not twice: #14's polling and #11's `?v=` short circuit both read it."""
    log_in(client, ada)
    before = version_of(board.retro)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 200
    assert version_of(board.retro) == before + 1
    assert response.json()["version"] == before + 1


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_mutation_answers_with_the_state_the_read_endpoint_would_give(
    client: Client, board: Board, ada: User, name: str
) -> None:
    """The same serializer, so a client can replace its state and skip a poll.

    Compared body to body against a fresh GET, which is the strongest form of
    "the same full board state": a mutation that built a payload of its own
    would differ in a key, in an order, or in the version.
    """
    log_in(client, ada)

    mutated = post(client, ACTIONS[name], board)
    polled = client.get(reverse("board-state", args=[board.retro.pk]))

    assert mutated.status_code == 200
    assert json.loads(mutated.content) == json.loads(polled.content)
    assert mutated.json()["changed"] is True


@pytest.mark.django_db
def test_the_response_carries_the_clusters_and_the_cards_new_grouping(
    client: Client, board: Board, ada: User
) -> None:
    """What a client applies: the cluster it just made, and where every card is."""
    log_in(client, ada)

    payload = post(client, ACTIONS["create a cluster"], board, name="Onboarding").json()

    created = Cluster.objects.get(retrospective=board.retro, name="Onboarding")
    assert {cluster["id"] for cluster in payload["clusters"]} == {
        board.first.pk,
        board.second.pk,
        created.pk,
    }
    assert payload["clusters"][-1] == {
        "id": created.pk,
        "name": "Onboarding",
        "position": created.position,
        "is_auto_generated": False,
        "status": "PENDING",
    }
    grouping = {card["id"]: card["cluster"] for card in payload["cards"]}
    assert grouping[str(board.grouped.public_id)] == board.first.pk
    assert grouping[str(board.ungrouped.public_id)] is None


@pytest.mark.django_db
def test_moving_a_card_to_the_cluster_it_is_already_in_does_not_bump_the_version(
    client: Client, board: Board, ada: User
) -> None:
    """A no-op must not wake every other client's poll.

    The response is still the full state, so a client that thought the card was
    somewhere else is corrected by the same body it would have got anyway.
    """
    log_in(client, ada)
    card = board.grouped
    before = version_of(board.retro)

    response = post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(card.public_id),
        cluster=board.first.pk,
    )

    assert response.status_code == 200
    assert version_of(board.retro) == before
    assert response.json()["version"] == before
    assert cluster_id_of(card) == board.first.pk


@pytest.mark.django_db
def test_ungrouping_an_ungrouped_card_does_not_bump_the_version(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    before = version_of(board.retro)

    response = post(
        client,
        ACTIONS["move a card out to ungrouped"],
        board,
        card=str(board.ungrouped.public_id),
    )

    assert response.status_code == 200
    assert version_of(board.retro) == before


@pytest.mark.django_db
def test_renaming_a_cluster_to_the_name_it_already_has_does_not_bump_the_version(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    before = version_of(board.retro)

    response = post(client, ACTIONS["rename a cluster"], board, name=board.first.name)

    assert response.status_code == 200
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_refused_mutation_bumps_nothing(board: Board, ada: User, name: str) -> None:
    """The counter moves with the board or not at all, refusals included."""
    client, token = strict_client(ada, board.retro)
    action = ACTIONS[name]
    before = snapshot(board)

    response = post_with_token(client, token, action, board, **action.broken)

    assert response.status_code in {400, 404}, (name, response.content)
    assert snapshot(board) == before


@pytest.mark.django_db
def test_two_moves_in_a_row_move_the_version_by_two(
    client: Client, board: Board, ada: User
) -> None:
    log_in(client, ada)
    before = version_of(board.retro)

    post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(board.ungrouped.public_id),
        cluster=board.first.pk,
    )
    post(
        client,
        ACTIONS["move a card to a cluster"],
        board,
        card=str(board.ungrouped.public_id),
        cluster=board.second.pk,
    )

    assert version_of(board.retro) == before + 2


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_moves_both_succeed_and_the_version_ends_two_higher(
    project: Project, owner: User, ada: User
) -> None:
    """The concurrency criterion, run for real against two connections.

    Both threads move the *same* card to different clusters at the same moment.
    The row lock on the retrospective serialises them, so both succeed, the
    version ends up exactly two higher — not one, which is what a read-then-write
    counter would give — and the card ends up in one of the two clusters. Which
    one is not asserted: last write wins, and which write is last is the whole
    thing the lock leaves undecided.
    """
    board = Board(project, owner, ada, MONDAY)
    before = version_of(board.retro)
    card = board.ungrouped
    ready = threading.Barrier(2, timeout=30)
    responses: dict[int, int] = {}

    def move(cluster_pk: int) -> None:
        try:
            client = Client()
            log_in(client, ada)
            ready.wait()
            response = client.post(
                reverse("board-card-move", args=[board.retro.pk]),
                {"card": str(card.public_id), "cluster": cluster_pk},
            )
            responses[cluster_pk] = response.status_code
        finally:
            # Each thread has a connection of its own, and the test database is
            # only torn down once every one of them has let go of it.
            connections.close_all()

    threads = [
        threading.Thread(target=move, args=(board.first.pk,)),
        threading.Thread(target=move, args=(board.second.pk,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a mutation never finished — the lock did not release"

    assert responses == {board.first.pk: 200, board.second.pk: 200}
    assert version_of(board.retro) == before + 2
    assert cluster_id_of(card) in {board.first.pk, board.second.pk}


@pytest.mark.django_db(transaction=True)
def test_a_mutation_waits_for_a_lock_already_held_on_the_retrospective(
    project: Project, owner: User, ada: User
) -> None:
    """The serialisation is a lock on the retrospective, not luck about timing.

    The main thread holds `select_for_update` on the row inside an open
    transaction. A mutation posted from another thread must not complete while
    that is held, and must complete once it is released — which is what
    "serialised by a row lock on the retrospective" means, stated as something
    that can fail.
    """
    board = Board(project, owner, ada, MONDAY)
    card = board.ungrouped
    finished = threading.Event()
    status: dict[str, int] = {}

    def move() -> None:
        try:
            client = Client()
            log_in(client, ada)
            response = client.post(
                reverse("board-card-move", args=[board.retro.pk]),
                {"card": str(card.public_id), "cluster": board.first.pk},
            )
            status["code"] = response.status_code
        finally:
            finished.set()
            connections.close_all()

    with transaction.atomic():
        Retrospective.objects.select_for_update(of=("self",)).get(pk=board.retro.pk)
        worker = threading.Thread(target=move)
        worker.start()
        # Long enough that a mutation which did not take the lock would have
        # finished several times over.
        assert not finished.wait(timeout=2), "the mutation did not wait for the lock"
        assert cluster_id_of(card) is None

    worker.join(timeout=30)
    assert not worker.is_alive()
    assert status == {"code": 200}
    assert cluster_id_of(card) == board.first.pk
    connections.close_all()


# --------------------------------------------------------------------------
# G. Stage gating
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("stage", OPEN_STAGES)
def test_every_endpoint_is_allowed_in_reveal_and_cluster(
    client: Client, board: Board, ada: User, owner: User, name: str, stage: str
) -> None:
    """The permitted half of the table. The board fixture starts in CLUSTER.

    The fixture walks the real stage machine to CLUSTER, so the reveal's side
    effects — the shuffle, the destroyed anonymous authors, the closed cycle —
    have really happened. REVEAL is then reached by putting the stage back with
    an UPDATE, because the machine is forward-only and nothing else may write
    `stage`; the row the endpoints read is the same either way.
    """
    if stage == Stage.REVEAL:
        Retrospective.objects.filter(pk=board.retro.pk).update(stage=Stage.REVEAL)
        board.refresh()
    log_in(client, ada)
    before = version_of(board.retro)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 200, response.content
    assert version_of(board.retro) == before + 1


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("stage", FROZEN_STAGES)
def test_every_endpoint_is_refused_from_vote_onward_and_before_reveal(
    board: Board, ada: User, owner: User, name: str, stage: str
) -> None:
    """The `-> VOTE` transition freezes cluster membership, and DRAFT precedes it.

    A clear status the client can show — 409, with a sentence in the body — and
    not a silent no-op: the snapshot proves the board is untouched, and the
    status proves the client is told.

    VOTE, DISCUSS and COMPLETE are reached through the real machine. DRAFT is
    reached by putting the stage back with an UPDATE, for the same reason as
    above: it is behind the fixture, and only `advance_stage()` may move a stage
    forward.
    """
    if stage == Stage.DRAFT:
        Retrospective.objects.filter(pk=board.retro.pk).update(stage=Stage.DRAFT)
        board.refresh()
    else:
        advance_to(board.retro, owner, stage)
        board.refresh()
    client, token = strict_client(ada, board.retro)
    before = snapshot(board)

    response = post_with_token(client, token, ACTIONS[name], board)

    assert response.status_code == 409, (name, stage, response.content)
    assert response.json()["error"]
    assert response.headers["Content-Type"] == "application/json"
    assert snapshot(board) == before


@pytest.mark.django_db
def test_the_transition_into_vote_closes_a_board_that_was_open(
    board: Board, other_board: Board, ada: User, owner: User
) -> None:
    """The same request, from the same client, on either side of one transition.

    Two boards rather than one, because the request that succeeds consumes the
    ids it names: the second board is the control, still in CLUSTER, and it
    proves the 409 below is about the stage and not about the client, the
    session or the token.
    """
    client, token = strict_client(ada, board.retro)
    move = ACTIONS["move a card to a cluster"]

    assert post_with_token(client, token, move, board).status_code == 200

    # Re-read before advancing: the mutation bumped the version, and
    # `advance_stage()` refuses to move a retrospective from a stale view of it.
    # That refusal is #9's, and it firing here is the version doing its job.
    board.refresh()
    advance_to(board.retro, owner, Stage.VOTE)
    board.refresh()
    before = snapshot(board)
    refused = post_with_token(client, token, move, board, card=str(board.elsewhere.public_id))

    assert refused.status_code == 409
    assert snapshot(board) == before
    # The control: nothing about this client stopped working.
    assert post_with_token(client, token, move, other_board).status_code == 200


# --------------------------------------------------------------------------
# H. Access
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_non_member_gets_404_and_changes_nothing(board: Board, outsider: User, name: str) -> None:
    """A member of some project is not a member of this one."""
    client = Client()
    log_in(client, outsider)
    before = snapshot(board)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_superuser_from_outside_the_project_gets_404(
    client: Client, board: Board, root: User, name: str
) -> None:
    """`_docs/decisions.md` item 3 has no admin exception, so neither has this."""
    log_in(client, root)
    before = snapshot(board)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_an_anonymous_user_gets_404_and_is_not_sent_to_the_login_page(
    client: Client, board: Board, name: str
) -> None:
    """A redirect would confirm the retrospective exists as surely as a 403 would."""
    before = snapshot(board)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 404
    assert "Location" not in response.headers
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_deactivated_member_gets_404(client: Client, board: Board, ada: User, name: str) -> None:
    log_in(client, ada)
    ada.is_active = False
    ada.save(update_fields=["is_active"])
    before = snapshot(board)

    response = post(client, ACTIONS[name], board)

    assert response.status_code == 404
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_retrospective_that_does_not_exist_answers_exactly_like_a_forbidden_one(
    client: Client, board: Board, outsider: User, name: str
) -> None:
    """Byte for byte, so the 404 cannot be used to find out which boards exist."""
    log_in(client, outsider)
    action = ACTIONS[name]

    refused = client.post(action.url(board.retro), action.body(board))
    never_existed = client.post(
        reverse(action.url_name, args=[board.retro.pk + 10_000]), action.body(board)
    )

    assert refused.status_code == 404
    assert never_existed.status_code == 404
    assert refused.content == never_existed.content


@pytest.mark.django_db
def test_an_ordinary_member_may_mutate_without_being_a_facilitator(
    client: Client, board: Board, ada: User
) -> None:
    """The board is the team's. Clustering is not the facilitator's privilege."""
    log_in(client, ada)
    assert not Membership.objects.filter(
        project=board.cycle.project, user=ada, role=Membership.Role.FACILITATOR
    ).exists()

    assert post(client, ACTIONS["create a cluster"], board).status_code == 200


# --------------------------------------------------------------------------
# I. CSRF
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_endpoint_refuses_a_post_without_a_csrf_token(
    board: Board, ada: User, name: str
) -> None:
    client = Client(enforce_csrf_checks=True)
    log_in(client, ada)
    action = ACTIONS[name]
    before = snapshot(board)

    response = client.post(action.url(board.retro), action.body(board))

    assert response.status_code == 403
    assert snapshot(board) == before


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_endpoint_accepts_the_same_post_with_a_valid_token(
    board: Board, ada: User, name: str
) -> None:
    """The other half, and the reason every refusal in this file uses a token."""
    client, token = strict_client(ada, board.retro)

    response = post_with_token(client, token, ACTIONS[name], board)

    assert response.status_code == 200


# --------------------------------------------------------------------------
# J. Carrying conditions
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cluster_rules_come_from_the_one_permissions_module() -> None:
    """#12 added five predicates, and it added them there — not to `board/`."""
    from projects import permissions

    for rule in (
        "can_create_cluster",
        "can_rename_cluster",
        "can_merge_cluster",
        "can_split_cluster",
        "can_delete_cluster",
    ):
        assert callable(getattr(permissions, rule)), rule
