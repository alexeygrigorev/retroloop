"""Voting: the model, the budget, the secrecy, and who may do it.

Every test here maps to an acceptance criterion of issue #15. The themes are the
ones the rest of the board's tests share, sharpened by what a vote is.

**Absence is asserted, not presence.** A member's votes are the one thing the
application knows that it must never hand to another member. So the secrecy test
below asserts that after Bruno votes, Ada's raw response body carries no trace of
it — not a total, not a count, not a marker — beyond the version tick the board
uses to tell her to re-read, which #15's own criterion says reveals nothing.

**A refusal is proved by attempting it, with a valid CSRF token.** Every rejected
cast and withdraw here is posted through a client that enforces CSRF, carrying a
token that works, so a 403 from the middleware can never stand in for a refusal
the endpoint itself made. One test proves the token works by casting with it.

**The budget is defended under a lock.** The concurrent test drives two real
casts from one member against two connections and asserts they cannot overspend
between them — the criterion, run rather than reasoned about.
"""

import json
import threading
from datetime import UTC, date, datetime

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connections, transaction
from django.test import Client
from django.urls import reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import DEFAULT_VOTES_PER_MEMBER, STAGE_ORDER, Cluster, Retrospective, Vote
from retro.services import advance_stage

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: Every stage but VOTE — the ones in which casting and withdrawing are refused.
NON_VOTE_STAGES = [stage for stage in STAGE_ORDER if stage != Stage.VOTE]

#: A member Ada must never learn anything about through the board.
BRUNO_USERNAME = "bruno-voter-9c31"
BRUNO_DISPLAY_NAME = "Bruno Voter 9c31"


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
    """The viewer and voter. Every request here is Ada's unless it says otherwise."""
    user = make_user("ada", "Ada Viewer")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def bruno(project: Project) -> User:
    """The other member. Nothing about his votes may reach Ada's browser."""
    user = make_user(BRUNO_USERNAME, BRUNO_DISPLAY_NAME)
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    """A real account on a project of their own and not on ours."""
    user = make_user("outsider", "Ora Outsider")
    elsewhere = Project.objects.create(name="Payments", owner=user)
    Membership.objects.create(project=elsewhere, user=user, role=Membership.Role.FACILITATOR)
    return user


def make_cycle(project: Project, facilitator: User, week: date) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=week,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
    )


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> Retrospective:
    """Walk the board forward through the real stage machine, never by assignment."""
    while retro.stage != stage:
        advance_stage(facilitator, retro)
    return retro


class VotingBoard:
    """One retrospective in VOTE, with three clusters and a card in each.

    The clusters are made during CLUSTER, the stage in which the board's shape
    may still change, and then the board is walked into VOTE — the only stage in
    which any of the tests below may cast. A card sits in each cluster so the
    board is a real one rather than a set of empty groups.
    """

    def __init__(self, project: Project, facilitator: User, author: User, week: date) -> None:
        self.cycle = make_cycle(project, facilitator, week)
        self.retro = Retrospective.objects.create(cycle=self.cycle)
        cards = [
            Card.objects.create(
                cycle=self.cycle,
                author=author,
                category=Card.Category.START,
                text=f"card {index} on {week}",
            )
            for index in range(3)
        ]
        advance_to(self.retro, facilitator, Stage.CLUSTER)
        self.clusters = [
            Cluster.objects.create(
                retrospective=self.retro, name=f"Cluster {index}", position=index
            )
            for index in range(3)
        ]
        for card, cluster in zip(cards, self.clusters, strict=True):
            Card.objects.filter(pk=card.pk).update(cluster=cluster)
        advance_to(self.retro, facilitator, Stage.VOTE)
        self.retro.refresh_from_db()

    @property
    def first(self) -> Cluster:
        return self.clusters[0]

    @property
    def second(self) -> Cluster:
        return self.clusters[1]

    @property
    def third(self) -> Cluster:
        return self.clusters[2]


@pytest.fixture
def voting(project: Project, owner: User, ada: User) -> VotingBoard:
    return VotingBoard(project, owner, ada, MONDAY)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def cast_url(retro: Retrospective) -> str:
    return reverse("board-vote-cast", args=[retro.pk])


def withdraw_url(retro: Retrospective) -> str:
    return reverse("board-vote-withdraw", args=[retro.pk])


def progress_url(retro: Retrospective) -> str:
    return reverse("board-vote-progress", args=[retro.pk])


def state_url(retro: Retrospective) -> str:
    return reverse("board-state", args=[retro.pk])


def version_of(retro: Retrospective) -> int:
    return Retrospective.objects.values_list("version", flat=True).get(pk=retro.pk)


def spent_by(user: User, retro: Retrospective) -> int:
    return sum(Vote.objects.filter(retrospective=retro, user=user).values_list("weight", flat=True))


def strict_client(user: User, retro: Retrospective) -> tuple[Client, str]:
    """A client that enforces CSRF, logged in, plus a token that works.

    Every refusal here is posted through one of these. Without it a 403 from the
    middleware and a refusal from the endpoint are indistinguishable.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, user)
    assert client.get(reverse("retro-detail", args=[retro.pk])).status_code == 200
    return client, client.cookies["csrftoken"].value


def cast(client: Client, retro: Retrospective, cluster: Cluster, **extra):
    return client.post(cast_url(retro), {"cluster": cluster.pk, **extra})


def withdraw(client: Client, retro: Retrospective, cluster: Cluster, **extra):
    return client.post(withdraw_url(retro), {"cluster": cluster.pk, **extra})


# --------------------------------------------------------------------------
# A. The model
# --------------------------------------------------------------------------


def test_a_vote_carries_exactly_the_fields_the_issue_names() -> None:
    concrete = {field.name for field in Vote._meta.fields}

    assert concrete == {"id", "retrospective", "cluster", "user", "weight"}


def test_the_vote_relations_point_where_the_issue_says() -> None:
    retrospective = Vote._meta.get_field("retrospective")
    cluster = Vote._meta.get_field("cluster")
    user = Vote._meta.get_field("user")

    assert retrospective.related_model is Retrospective
    assert cluster.related_model is Cluster
    assert user.related_model is get_user_model()
    # A vote is a transient tally, not the team's feedback: a member who leaves
    # takes their votes with them rather than orphaning a row that counts.
    for field in (retrospective, cluster, user):
        assert field.remote_field.on_delete.__name__ == "CASCADE", field.name


def test_a_member_has_at_most_one_row_per_cluster() -> None:
    """`unique(retrospective, cluster, user)` — one row, a heavier weight."""
    names = {
        tuple(constraint.fields)
        for constraint in Vote._meta.constraints
        if hasattr(constraint, "fields")
    }
    assert ("retrospective", "cluster", "user") in names


@pytest.mark.django_db
def test_a_second_vote_on_one_cluster_by_one_member_is_refused_by_the_database(
    voting: VotingBoard,
    ada: User,
) -> None:
    Vote.objects.create(retrospective=voting.retro, cluster=voting.first, user=ada, weight=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Vote.objects.create(
                retrospective=voting.retro, cluster=voting.first, user=ada, weight=1
            )


@pytest.mark.django_db
def test_a_weight_of_zero_is_refused_by_the_database(voting: VotingBoard, ada: User) -> None:
    """A row with weight 0 is deleted, not stored — the database says so too."""
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Vote.objects.create(
                retrospective=voting.retro, cluster=voting.first, user=ada, weight=0
            )


@pytest.mark.django_db
def test_a_weight_over_the_budget_is_refused_by_the_database(
    voting: VotingBoard, ada: User
) -> None:
    """The per-row ceiling is the whole budget: all votes may pile on one cluster, no more."""
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Vote.objects.create(
                retrospective=voting.retro,
                cluster=voting.first,
                user=ada,
                weight=DEFAULT_VOTES_PER_MEMBER + 1,
            )


def test_the_default_budget_is_three() -> None:
    assert DEFAULT_VOTES_PER_MEMBER == 3
    assert Retrospective._meta.get_field("votes_per_member").default == 3


# --------------------------------------------------------------------------
# B. Casting and the budget
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_cast_adds_a_vote_and_answers_with_the_board(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)

    response = cast(client, voting.retro, voting.first)

    assert response.status_code == 200
    payload = response.json()
    assert payload["votes"] == {
        "mine": [{"cluster": voting.first.pk, "weight": 1}],
        "remaining": 2,
    }
    assert spent_by(ada, voting.retro) == 1


@pytest.mark.django_db
def test_a_cast_defaults_to_one_vote_and_stacks_on_repeat(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)

    cast(client, voting.retro, voting.first)
    cast(client, voting.retro, voting.first)

    row = Vote.objects.get(retrospective=voting.retro, cluster=voting.first, user=ada)
    assert row.weight == 2


@pytest.mark.django_db
def test_all_three_votes_may_go_on_one_cluster(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)

    response = cast(client, voting.retro, voting.first, weight=3)

    assert response.status_code == 200
    assert response.json()["votes"] == {
        "mine": [{"cluster": voting.first.pk, "weight": 3}],
        "remaining": 0,
    }


@pytest.mark.django_db
def test_the_budget_edge_at_exactly_three_is_accepted(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)

    assert cast(client, voting.retro, voting.first, weight=2).status_code == 200
    response = cast(client, voting.retro, voting.second, weight=1)

    assert response.status_code == 200
    assert response.json()["votes"]["remaining"] == 0
    assert spent_by(ada, voting.retro) == 3


@pytest.mark.django_db
def test_the_budget_edge_at_four_is_rejected_and_writes_nothing(
    voting: VotingBoard, ada: User
) -> None:
    """Exceeding the budget is refused with how many remain, and nothing is written."""
    client = Client()
    log_in(client, ada)
    assert cast(client, voting.retro, voting.first, weight=2).status_code == 200
    before = version_of(voting.retro)

    response = cast(client, voting.retro, voting.second, weight=2)

    assert response.status_code == 400
    assert "1 vote left" in response.json()["error"]
    # Nothing written: the second cluster has no row, the spend is still two, and
    # the version did not move.
    assert not Vote.objects.filter(
        retrospective=voting.retro, cluster=voting.second, user=ada
    ).exists()
    assert spent_by(ada, voting.retro) == 2
    assert version_of(voting.retro) == before


@pytest.mark.django_db
def test_a_cast_bumps_the_version_exactly_once(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)
    before = version_of(voting.retro)

    cast(client, voting.retro, voting.first)

    assert version_of(voting.retro) == before + 1


@pytest.mark.django_db
@pytest.mark.parametrize("weight", ["0", "-1", "1.5", "abc", ""])
def test_a_cast_with_a_weight_that_is_not_a_count_of_votes_is_refused(
    voting: VotingBoard, ada: User, weight: str
) -> None:
    client = Client()
    log_in(client, ada)

    response = cast(client, voting.retro, voting.first, weight=weight)

    assert response.status_code == 400
    assert not Vote.objects.filter(retrospective=voting.retro, user=ada).exists()


# --------------------------------------------------------------------------
# C. Withdrawing and changing your mind
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_withdrawing_returns_votes_to_the_budget(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)
    cast(client, voting.retro, voting.first, weight=3)

    response = withdraw(client, voting.retro, voting.first, weight=1)

    assert response.status_code == 200
    assert response.json()["votes"] == {
        "mine": [{"cluster": voting.first.pk, "weight": 2}],
        "remaining": 1,
    }


@pytest.mark.django_db
def test_withdrawing_the_last_vote_deletes_the_row_rather_than_storing_zero(
    voting: VotingBoard, ada: User
) -> None:
    client = Client()
    log_in(client, ada)
    cast(client, voting.retro, voting.first, weight=1)

    withdraw(client, voting.retro, voting.first, weight=1)

    assert not Vote.objects.filter(retrospective=voting.retro, cluster=voting.first).exists()
    payload = cast(client, voting.retro, voting.second, weight=1).json()
    # The emptied cluster is gone from the payload, not present as a zero.
    assert payload["votes"]["mine"] == [{"cluster": voting.second.pk, "weight": 1}]


@pytest.mark.django_db
def test_votes_are_freely_reassignable_while_the_stage_is_vote(
    voting: VotingBoard, ada: User
) -> None:
    """`_docs/decisions.md` item 2: withdraw from one cluster, place on another."""
    client = Client()
    log_in(client, ada)
    cast(client, voting.retro, voting.first, weight=3)

    withdraw(client, voting.retro, voting.first, weight=2)
    response = cast(client, voting.retro, voting.second, weight=2)

    assert response.status_code == 200
    assert spent_by(ada, voting.retro) == 3
    mine = {row["cluster"]: row["weight"] for row in response.json()["votes"]["mine"]}
    assert mine == {voting.first.pk: 1, voting.second.pk: 2}


@pytest.mark.django_db
def test_withdrawing_from_a_cluster_with_no_votes_is_refused(
    voting: VotingBoard, ada: User
) -> None:
    client = Client()
    log_in(client, ada)

    response = withdraw(client, voting.retro, voting.first, weight=1)

    assert response.status_code == 400
    assert "no votes" in response.json()["error"]


@pytest.mark.django_db
def test_withdrawing_more_than_you_placed_is_refused_and_writes_nothing(
    voting: VotingBoard, ada: User
) -> None:
    client = Client()
    log_in(client, ada)
    cast(client, voting.retro, voting.first, weight=1)
    before = version_of(voting.retro)

    response = withdraw(client, voting.retro, voting.first, weight=2)

    assert response.status_code == 400
    assert spent_by(ada, voting.retro) == 1
    assert version_of(voting.retro) == before


# --------------------------------------------------------------------------
# D. Rejection at every stage other than VOTE
# --------------------------------------------------------------------------


def _board_at(project: Project, facilitator: User, author: User, stage: str) -> VotingBoard:
    """A retrospective walked to `stage`, with a cluster that exists there.

    The cluster is created before the walk so it is present whatever the stage,
    which is what lets the stage gate be tested in isolation: `cast_vote`
    resolves the cluster before it checks the stage, so a missing cluster would
    give a 404 and hide the 409 the stage gate raises.
    """
    cycle = make_cycle(project, facilitator, MONDAY)
    retro = Retrospective.objects.create(cycle=cycle)
    Card.objects.create(cycle=cycle, author=author, category=Card.Category.START, text="a card")
    cluster = Cluster.objects.create(retrospective=retro, name="Deploys", position=1)
    advance_to(retro, facilitator, stage)
    board = VotingBoard.__new__(VotingBoard)
    board.cycle = cycle
    board.retro = retro
    board.clusters = [cluster]
    return board


@pytest.mark.django_db
@pytest.mark.parametrize("stage", NON_VOTE_STAGES)
def test_casting_is_refused_at_every_stage_but_vote(
    project: Project, owner: User, ada: User, stage: str
) -> None:
    """Before voting opens and after it closes are the same 409, proven with a token."""
    board = _board_at(project, owner, ada, stage)
    client, token = strict_client(ada, board.retro)
    before = version_of(board.retro)

    response = client.post(
        cast_url(board.retro),
        {"cluster": board.first.pk, "weight": 1},
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 409
    assert not Vote.objects.filter(retrospective=board.retro).exists()
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("stage", NON_VOTE_STAGES)
def test_withdrawing_is_refused_at_every_stage_but_vote(
    project: Project, owner: User, ada: User, stage: str
) -> None:
    board = _board_at(project, owner, ada, stage)
    client, token = strict_client(ada, board.retro)

    response = client.post(
        withdraw_url(board.retro),
        {"cluster": board.first.pk, "weight": 1},
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_a_valid_csrf_token_lets_a_member_vote(voting: VotingBoard, ada: User) -> None:
    """The refusals above are refusals, not a token that never worked."""
    client, token = strict_client(ada, voting.retro)

    response = client.post(
        cast_url(voting.retro),
        {"cluster": voting.first.pk, "weight": 1},
        HTTP_X_CSRFTOKEN=token,
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_the_vote_endpoints_refuse_a_get(voting: VotingBoard, ada: User) -> None:
    client = Client()
    log_in(client, ada)

    assert client.get(cast_url(voting.retro)).status_code == 405
    assert client.get(withdraw_url(voting.retro)).status_code == 405


# --------------------------------------------------------------------------
# E. Access
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_member_casting_gets_404_and_writes_nothing(
    voting: VotingBoard, outsider: User
) -> None:
    client = Client()
    log_in(client, outsider)

    response = cast(client, voting.retro, voting.first)

    assert response.status_code == 404
    assert not Vote.objects.filter(retrospective=voting.retro).exists()


@pytest.mark.django_db
def test_an_anonymous_user_casting_gets_404(voting: VotingBoard) -> None:
    client = Client()

    response = cast(client, voting.retro, voting.first)

    assert response.status_code == 404
    assert not Vote.objects.filter(retrospective=voting.retro).exists()


# --------------------------------------------------------------------------
# F. Secrecy
# --------------------------------------------------------------------------


def _without_version(body: str) -> dict:
    """The parsed body with `version` normalised, so a version tick is not a diff."""
    payload = json.loads(body)
    payload["version"] = 0
    return payload


@pytest.mark.django_db
def test_another_members_vote_leaves_no_trace_in_the_viewers_body(
    voting: VotingBoard, ada: User, bruno: User
) -> None:
    """After Bruno votes, Ada's body is byte-for-byte free of any indication of it.

    Captured before and after Bruno votes, and compared with the version
    normalised away — the version does move, because every mutation bumps it, and
    #15's criterion says that bump reveals nothing about who voted. What must not
    change is everything else: Ada's own votes, the clusters, the cards, and the
    absence of any total or count. The two bodies are identical once the version
    is set aside, so Bruno's vote left no other mark.
    """
    ada_client = Client()
    log_in(ada_client, ada)
    ada_client.post(cast_url(voting.retro), {"cluster": voting.first.pk, "weight": 1})

    before = ada_client.get(state_url(voting.retro)).content.decode()

    bruno_client = Client()
    log_in(bruno_client, bruno)
    bruno_client.post(cast_url(voting.retro), {"cluster": voting.first.pk, "weight": 3})

    after_response = ada_client.get(state_url(voting.retro))
    after = after_response.content.decode()

    # The only difference is the version.
    assert json.loads(before)["version"] != json.loads(after)["version"]
    assert _without_version(before) == _without_version(after)

    # And nothing about Bruno or a running total is anywhere in the raw bytes.
    assert "vote_totals" not in after
    assert "total" not in after.lower()
    assert "count" not in after.lower()
    assert BRUNO_USERNAME not in after
    assert BRUNO_DISPLAY_NAME not in after
    # Bruno's three votes on the first cluster are invisible: Ada sees only her
    # own single vote there.
    payload = after_response.json()
    assert payload["votes"]["mine"] == [{"cluster": voting.first.pk, "weight": 1}]
    assert "vote_totals" not in payload


@pytest.mark.django_db
def test_no_totals_reach_a_viewer_while_the_stage_is_vote(
    voting: VotingBoard, ada: User, bruno: User
) -> None:
    """Totals are secret during VOTE, however many members have voted."""
    for member in (ada, bruno):
        client = Client()
        log_in(client, member)
        cast(client, voting.retro, voting.first, weight=2)

    viewer = Client()
    log_in(viewer, ada)
    payload = viewer.get(state_url(voting.retro)).json()

    assert "vote_totals" not in payload


@pytest.mark.django_db
def test_totals_appear_for_everyone_from_discuss_on(
    voting: VotingBoard, owner: User, ada: User, bruno: User
) -> None:
    """From DISCUSS the per-cluster totals are visible, and only then."""
    for member in (ada, bruno):
        client = Client()
        log_in(client, member)
        cast(client, voting.retro, voting.first, weight=2)
        cast(client, voting.retro, voting.second, weight=1)

    # The casts bumped the version; refresh before advancing so the stage
    # machine's own concurrency guard does not see a stale instance.
    voting.retro.refresh_from_db()
    advance_to(voting.retro, owner, Stage.DISCUSS)

    viewer = Client()
    log_in(viewer, ada)
    payload = viewer.get(state_url(voting.retro)).json()

    assert payload["vote_totals"] == {
        str(voting.first.pk): 4,
        str(voting.second.pk): 2,
    }


@pytest.mark.django_db
def test_a_cluster_with_no_votes_is_absent_from_the_totals(
    voting: VotingBoard, owner: User, ada: User
) -> None:
    """Absent, not a zero: an unvoted cluster says nothing rather than "nobody here"."""
    client = Client()
    log_in(client, ada)
    cast(client, voting.retro, voting.first, weight=1)
    voting.retro.refresh_from_db()
    advance_to(voting.retro, owner, Stage.DISCUSS)

    payload = client.get(state_url(voting.retro)).json()

    assert payload["vote_totals"] == {str(voting.first.pk): 1}
    assert str(voting.third.pk) not in payload["vote_totals"]


# --------------------------------------------------------------------------
# G. The facilitator's progress count
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_facilitator_sees_how_many_members_spent_every_vote_as_a_count(
    voting: VotingBoard, owner: User, ada: User, bruno: User
) -> None:
    """A count only — never which members, never a partial tally per person."""
    ada_client = Client()
    log_in(ada_client, ada)
    cast(ada_client, voting.retro, voting.first, weight=3)  # Ada spends all three.

    bruno_client = Client()
    log_in(bruno_client, bruno)
    cast(bruno_client, voting.retro, voting.second, weight=1)  # Bruno is not done.

    facilitator = Client()
    log_in(facilitator, owner)
    response = facilitator.get(progress_url(voting.retro))

    assert response.status_code == 200
    # Exactly one member has spent everything, and the body says nothing else.
    assert response.json() == {"finished": 1}
    body = response.content.decode()
    assert "ada" not in body
    assert BRUNO_USERNAME not in body


@pytest.mark.django_db
def test_a_plain_member_cannot_see_the_progress_count(voting: VotingBoard, ada: User) -> None:
    """It is the facilitator's to see; a member gets the same 404 a stranger does."""
    client = Client()
    log_in(client, ada)

    assert client.get(progress_url(voting.retro)).status_code == 404


@pytest.mark.django_db
def test_a_non_member_and_an_anonymous_user_cannot_see_the_progress_count(
    voting: VotingBoard, outsider: User
) -> None:
    anonymous = Client()
    assert anonymous.get(progress_url(voting.retro)).status_code == 404

    stranger = Client()
    log_in(stranger, outsider)
    assert stranger.get(progress_url(voting.retro)).status_code == 404


@pytest.mark.django_db
def test_the_progress_endpoint_refuses_a_post(voting: VotingBoard, owner: User) -> None:
    client = Client()
    log_in(client, owner)

    assert client.post(progress_url(voting.retro)).status_code == 405


# --------------------------------------------------------------------------
# H. Concurrency — the budget under a lock
# --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_casts_from_one_member_cannot_overspend_the_budget(
    project: Project, owner: User, ada: User
) -> None:
    """The criterion, run for real: two casts of two votes each, budget of three.

    Both threads try to spend two of Ada's three votes at the same instant, on
    different clusters. Without the lock both would read "nothing spent" and both
    would write, spending four. The retrospective's row lock serialises them: the
    first spends two, the second reads two already spent and is refused, and Ada's
    total across the board is at most three — never four.
    """
    board = VotingBoard(project, owner, ada, MONDAY)
    ready = threading.Barrier(2, timeout=30)
    statuses: dict[int, int] = {}

    def cast_two(cluster_pk: int) -> None:
        try:
            client = Client()
            log_in(client, ada)
            ready.wait()
            response = client.post(
                cast_url(board.retro),
                {"cluster": cluster_pk, "weight": 2},
            )
            statuses[cluster_pk] = response.status_code
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=cast_two, args=(board.first.pk,)),
        threading.Thread(target=cast_two, args=(board.second.pk,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a cast never finished — the lock did not release"

    # Exactly one succeeded and one was refused, and the budget held.
    assert sorted(statuses.values()) == [200, 400]
    assert spent_by(ada, board.retro) <= DEFAULT_VOTES_PER_MEMBER
    assert spent_by(ada, board.retro) == 2
    connections.close_all()
