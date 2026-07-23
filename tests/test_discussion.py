"""Discussion mode: the agenda, the cluster statuses, and the notes.

Every test here maps to an acceptance criterion of issue #16. The file shares the
themes the rest of the board's tests keep, sharpened by what the discussion is.

**A refusal is proved by attempting it, with a valid CSRF token.** Every rejected
status change and note write is posted through a client that enforces CSRF,
carrying a token that works, so a 403 from the middleware can never stand in for
a refusal the endpoint itself made — the same discipline #12 and #15 keep.

**Absence is asserted, not presence.** A note is always attributed, and that is
correct — but it must never carry anything about a *card*: not a card's author,
not a card's `pk`. The privacy sweep below asserts a note dict is exactly four
keys and that no card handle rides in on one. And a refusal is asserted as
*nothing changed*, not merely as a status code.

**The agenda is one ordering rule, computed server-side.** The order the clusters
arrive in is the agenda, and the tests assert it is by vote weight — not the
board's `position` order — with the tie-break that keeps it from reshuffling.
"""

import json
import uuid
from datetime import UTC, date, datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import STAGE_ORDER, Cluster, Note, Retrospective, Vote
from retro.services import advance_stage

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage
Status = Cluster.Status

#: Long and unmistakable, so an absence check cannot pass on a substring of some
#: unrelated markup. Bruno is the member whose card data must never ride out on a
#: note; his *note* authorship, by contrast, is shown, and that is the point.
BRUNO_USERNAME = "bruno-discuss-7a20"
BRUNO_DISPLAY_NAME = "Bruno Discuss 7a20"

ADA_DISPLAY_NAME = "Ada Viewer"

#: Every stage but DISCUSS — the ones in which the discussion's writes are refused.
NON_DISCUSS_STAGES = [stage for stage in STAGE_ORDER if stage != Stage.DISCUSS]


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
    """An ordinary member. Most requests here are Ada's unless they say otherwise."""
    user = make_user("ada", ADA_DISPLAY_NAME)
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def bruno(project: Project) -> User:
    """A second ordinary member."""
    user = make_user(BRUNO_USERNAME, BRUNO_DISPLAY_NAME)
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


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> Retrospective:
    """Walk the board forward through the real stage machine, never by assignment."""
    while retro.stage != stage:
        advance_stage(facilitator, retro)
    return retro


class DiscussionBoard:
    """One retrospective in DISCUSS, with four clusters, their votes, and cards.

    The clusters are made during CLUSTER and voted on so the agenda has something
    to order: Bravo and Charlie tie on three votes, Alpha has one, and Delta has
    none. Four cards are grouped one to a cluster and a fifth is left ungrouped, so
    "ungrouped cards remain visible" and "an unvoted cluster is not hidden" are
    both live from the start. Then the board is walked into DISCUSS, where the
    totals are frozen and the agenda is fixed.
    """

    def __init__(
        self, project: Project, facilitator: User, ada: User, bruno: User, week: date = MONDAY
    ) -> None:
        self.cycle = make_cycle(project, facilitator, week)
        self.retro = Retrospective.objects.create(cycle=self.cycle)
        self.cards = [
            Card.objects.create(
                cycle=self.cycle,
                author=ada,
                category=Card.Category.START,
                text=f"discussion card {index}",
            )
            for index in range(5)
        ]
        advance_to(self.retro, facilitator, Stage.CLUSTER)

        self.alpha = Cluster.objects.create(retrospective=self.retro, name="Alpha", position=1)
        self.bravo = Cluster.objects.create(retrospective=self.retro, name="Bravo", position=2)
        self.charlie = Cluster.objects.create(retrospective=self.retro, name="Charlie", position=3)
        self.delta = Cluster.objects.create(retrospective=self.retro, name="Delta", position=4)
        for card, cluster in zip(
            self.cards, [self.alpha, self.bravo, self.charlie, self.delta], strict=False
        ):
            Card.objects.filter(pk=card.pk).update(cluster=cluster)
        # cards[4] stays ungrouped on purpose.
        self.ungrouped_card = self.cards[4]

        advance_to(self.retro, facilitator, Stage.VOTE)
        # Alpha 1, Bravo 3, Charlie 3, Delta 0 — a tie to break and a zero to sink.
        Vote.objects.create(
            retrospective=self.retro, cluster=self.alpha, user=facilitator, weight=1
        )
        Vote.objects.create(retrospective=self.retro, cluster=self.bravo, user=ada, weight=3)
        Vote.objects.create(retrospective=self.retro, cluster=self.charlie, user=bruno, weight=3)

        advance_to(self.retro, facilitator, Stage.DISCUSS)
        self.retro.refresh_from_db()

    def refresh(self) -> None:
        self.retro.refresh_from_db()
        for name in ("alpha", "bravo", "charlie", "delta"):
            getattr(self, name).refresh_from_db()


@pytest.fixture
def board(project: Project, owner: User, ada: User, bruno: User) -> DiscussionBoard:
    return DiscussionBoard(project, owner, ada, bruno)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def state_url(retro: Retrospective) -> str:
    return reverse("board-state", args=[retro.pk])


def get_state(client: Client, retro: Retrospective):
    return client.get(state_url(retro))


def version_of(retro: Retrospective) -> int:
    return Retrospective.objects.values_list("version", flat=True).get(pk=retro.pk)


def strict_client(user: User, retro: Retrospective) -> tuple[Client, str]:
    """A CSRF-enforcing client, logged in, plus a token that works.

    Every refusal in this file is posted through one of these, so a 403 from the
    middleware can never be mistaken for a refusal the endpoint made.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, user)
    assert client.get(reverse("retro-detail", args=[retro.pk])).status_code == 200
    return client, client.cookies["csrftoken"].value


def token_post(client: Client, token: str, url_name: str, retro: Retrospective, body: dict):
    return client.post(reverse(url_name, args=[retro.pk]), body, HTTP_X_CSRFTOKEN=token)


def status_url(retro: Retrospective) -> str:
    return reverse("board-cluster-status", args=[retro.pk])


def note_add_url(retro: Retrospective) -> str:
    return reverse("board-note-add", args=[retro.pk])


def notes_in(payload: dict) -> list[dict]:
    return payload["notes"]


def cluster_order(payload: dict) -> list[int]:
    return [cluster["id"] for cluster in payload["clusters"]]


# --------------------------------------------------------------------------
# A. The agenda
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_agenda_orders_clusters_by_total_vote_weight_highest_first(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """Bravo and Charlie (3), then Alpha (1), then Delta (0) — not board order."""
    log_in(client, ada)

    payload = get_state(client, board.retro).json()

    assert cluster_order(payload) == [
        board.bravo.pk,
        board.charlie.pk,
        board.alpha.pk,
        board.delta.pk,
    ]
    # And that is emphatically not `position` order, which would be Alpha first.
    assert cluster_order(payload) != [
        board.alpha.pk,
        board.bravo.pk,
        board.charlie.pk,
        board.delta.pk,
    ]
    # The total is shown, keyed by cluster id, and a zero-vote cluster is absent
    # from the totals rather than present as a nought.
    assert payload["vote_totals"] == {
        str(board.alpha.pk): 1,
        str(board.bravo.pk): 3,
        str(board.charlie.pk): 3,
    }
    assert str(board.delta.pk) not in payload["vote_totals"]


@pytest.mark.django_db
def test_a_tie_on_votes_is_broken_by_position_then_id(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """Bravo before Charlie: same weight, lower position wins."""
    log_in(client, ada)

    order = cluster_order(get_state(client, board.retro).json())

    assert order.index(board.bravo.pk) < order.index(board.charlie.pk)


@pytest.mark.django_db
def test_a_tie_on_votes_and_position_is_broken_by_id(
    client: Client, project: Project, owner: User, ada: User
) -> None:
    """When even position ties, the lower id comes first, so the order is total.

    Two clusters are forced onto the same position and given the same weight, so
    the only thing left to order them is `id` — and the agenda uses it, which is
    what makes "does not reshuffle between polls" true rather than lucky.
    """
    cycle = make_cycle(project, owner, date(2026, 8, 3))
    retro = Retrospective.objects.create(cycle=cycle)
    advance_to(retro, owner, Stage.CLUSTER)
    first = Cluster.objects.create(retrospective=retro, name="One", position=1)
    second = Cluster.objects.create(retrospective=retro, name="Two", position=1)
    advance_to(retro, owner, Stage.VOTE)
    Vote.objects.create(retrospective=retro, cluster=first, user=ada, weight=2)
    Vote.objects.create(retrospective=retro, cluster=second, user=owner, weight=2)
    advance_to(retro, owner, Stage.DISCUSS)
    log_in(client, ada)

    order = cluster_order(get_state(client, retro).json())

    assert order == sorted([first.pk, second.pk])


@pytest.mark.django_db
def test_a_cluster_with_no_votes_still_appears_at_the_bottom(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """Delta got no votes: deprioritised to last, never dropped from the board."""
    log_in(client, ada)

    order = cluster_order(get_state(client, board.retro).json())

    assert board.delta.pk in order
    assert order[-1] == board.delta.pk


@pytest.mark.django_db
def test_ungrouped_cards_remain_visible_during_discuss(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """The card in no cluster is on the board, with a null cluster — not dropped."""
    log_in(client, ada)

    payload = get_state(client, board.retro).json()
    cards = {card["id"]: card for card in payload["cards"]}

    handle = str(board.ungrouped_card.public_id)
    assert handle in cards
    assert cards[handle]["cluster"] is None


@pytest.mark.django_db
def test_the_agenda_does_not_reshuffle_between_polls(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """Two polls in a row hand back the same order — the totals are frozen."""
    log_in(client, ada)

    first = cluster_order(get_state(client, board.retro).json())
    second = cluster_order(get_state(client, board.retro).json())

    assert first == second


@pytest.mark.django_db
def test_every_viewer_sees_the_same_agenda_order(
    client: Client, board: DiscussionBoard, ada: User, bruno: User, owner: User
) -> None:
    """The agenda is the board's, not the viewer's: everyone gets one order."""
    orders = []
    for viewer in (ada, bruno, owner):
        viewer_client = Client()
        log_in(viewer_client, viewer)
        orders.append(cluster_order(get_state(viewer_client, board.retro).json()))

    assert orders[0] == orders[1] == orders[2]


@pytest.mark.django_db
def test_before_discuss_the_clusters_stay_in_board_position_order(
    client: Client, project: Project, owner: User, ada: User
) -> None:
    """No agenda before DISCUSS: the board is still in `position` order at VOTE."""
    cycle = make_cycle(project, owner, date(2026, 8, 10))
    retro = Retrospective.objects.create(cycle=cycle)
    advance_to(retro, owner, Stage.CLUSTER)
    alpha = Cluster.objects.create(retrospective=retro, name="Alpha", position=1)
    bravo = Cluster.objects.create(retrospective=retro, name="Bravo", position=2)
    advance_to(retro, owner, Stage.VOTE)
    # Bravo outweighs Alpha, but at VOTE the totals are secret and the order is
    # still the board's, so weight cannot have moved anything.
    Vote.objects.create(retrospective=retro, cluster=bravo, user=ada, weight=3)
    log_in(client, ada)

    payload = get_state(client, retro).json()

    assert cluster_order(payload) == [alpha.pk, bravo.pk]
    assert "vote_totals" not in payload


# --------------------------------------------------------------------------
# B. Cluster status
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [Status.DISCUSSED, Status.SKIPPED, Status.DEFERRED, Status.PENDING]
)
def test_the_facilitator_sets_a_cluster_status(
    client: Client, board: DiscussionBoard, owner: User, status: str
) -> None:
    """Every value the issue names, PENDING included for a mis-click."""
    log_in(client, owner)

    response = client.post(status_url(board.retro), {"cluster": board.bravo.pk, "status": status})

    assert response.status_code == 200
    board.refresh()
    assert board.bravo.status == status
    # And the new status is in the body the facilitator gets straight back.
    served = {c["id"]: c["status"] for c in response.json()["clusters"]}
    assert served[board.bravo.pk] == status


@pytest.mark.django_db
def test_the_status_is_visible_to_every_member(
    client: Client, board: DiscussionBoard, owner: User, ada: User
) -> None:
    """The facilitator sets it; an ordinary member polls and sees it."""
    facilitator = Client()
    log_in(facilitator, owner)
    facilitator.post(
        status_url(board.retro), {"cluster": board.bravo.pk, "status": Status.DISCUSSED}
    )

    log_in(client, ada)
    payload = get_state(client, board.retro).json()

    served = {c["id"]: c["status"] for c in payload["clusters"]}
    assert served[board.bravo.pk] == Status.DISCUSSED


@pytest.mark.django_db
def test_setting_a_status_bumps_the_version_once(
    client: Client, board: DiscussionBoard, owner: User
) -> None:
    log_in(client, owner)
    before = version_of(board.retro)

    response = client.post(
        status_url(board.retro), {"cluster": board.bravo.pk, "status": Status.DISCUSSED}
    )

    assert response.status_code == 200
    assert version_of(board.retro) == before + 1
    assert response.json()["version"] == before + 1


@pytest.mark.django_db
def test_setting_a_status_to_the_one_it_already_has_does_not_bump_the_version(
    client: Client, board: DiscussionBoard, owner: User
) -> None:
    """A no-op must not wake every other client's poll."""
    log_in(client, owner)
    assert board.bravo.status == Status.PENDING
    before = version_of(board.retro)

    response = client.post(
        status_url(board.retro), {"cluster": board.bravo.pk, "status": Status.PENDING}
    )

    assert response.status_code == 200
    assert version_of(board.retro) == before


@pytest.mark.django_db
def test_a_member_cannot_set_a_status_and_a_direct_post_changes_nothing(
    board: DiscussionBoard, ada: User
) -> None:
    """The criterion, proved with a token: a member's direct POST is a 403 no-op."""
    client, token = strict_client(ada, board.retro)
    before = version_of(board.retro)

    response = token_post(
        client,
        token,
        "board-cluster-status",
        board.retro,
        {"cluster": board.bravo.pk, "status": Status.DISCUSSED},
    )

    assert response.status_code == 403
    assert response.json()["error"]
    board.refresh()
    assert board.bravo.status == Status.PENDING
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", "done", "pending", "DISCUSS", "1", "COMPLETE"])
def test_an_unknown_status_value_is_a_400_and_changes_nothing(
    board: DiscussionBoard, owner: User, value: str
) -> None:
    client, token = strict_client(owner, board.retro)
    before = version_of(board.retro)

    response = token_post(
        client,
        token,
        "board-cluster-status",
        board.retro,
        {"cluster": board.bravo.pk, "status": value},
    )

    assert response.status_code == 400
    board.refresh()
    assert board.bravo.status == Status.PENDING
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("stage", NON_DISCUSS_STAGES)
def test_setting_a_status_outside_discuss_is_a_clear_error(
    project: Project, owner: User, ada: User, bruno: User, stage: str
) -> None:
    """A facilitator outside DISCUSS gets a 409 with a sentence, and nothing moves.

    DRAFT is reached by putting the stage back with an UPDATE — the machine is
    forward-only and only it may move a stage forward — and the rest through the
    real machine, so the row the endpoint reads is real either way.
    """
    board = DiscussionBoard(project, owner, ada, bruno)
    if stage == Stage.DRAFT:
        Retrospective.objects.filter(pk=board.retro.pk).update(stage=Stage.DRAFT)
    else:
        # Every non-DISCUSS stage that is not DRAFT is either behind DISCUSS or
        # ahead of it; walk there through the machine where it is ahead, and put
        # it back with an UPDATE where it is behind.
        if STAGE_ORDER.index(stage) > STAGE_ORDER.index(Stage.DISCUSS):
            board.refresh()
            advance_to(board.retro, owner, stage)
        else:
            Retrospective.objects.filter(pk=board.retro.pk).update(stage=stage)
    board.refresh()
    client, token = strict_client(owner, board.retro)
    before = version_of(board.retro)

    response = token_post(
        client,
        token,
        "board-cluster-status",
        board.retro,
        {"cluster": board.bravo.pk, "status": Status.DISCUSSED},
    )

    assert response.status_code == 409, (stage, response.content)
    assert response.json()["error"]
    board.refresh()
    assert board.bravo.status == Status.PENDING
    assert version_of(board.retro) == before


@pytest.mark.django_db
def test_a_status_for_a_cluster_on_another_board_is_a_404(
    project: Project, owner: User, ada: User, bruno: User
) -> None:
    """A cluster from another retrospective is refused, never acted on."""
    board = DiscussionBoard(project, owner, ada, bruno)
    other = DiscussionBoard(project, owner, ada, bruno, week=date(2026, 8, 24))
    client, token = strict_client(owner, board.retro)

    response = token_post(
        client,
        token,
        "board-cluster-status",
        board.retro,
        {"cluster": other.bravo.pk, "status": Status.DISCUSSED},
    )

    assert response.status_code == 404
    other.refresh()
    assert other.bravo.status == Status.PENDING


# --------------------------------------------------------------------------
# C. The Note model
# --------------------------------------------------------------------------


def test_a_note_carries_exactly_the_five_fields_the_issue_names() -> None:
    concrete = {field.name for field in Note._meta.fields}

    assert concrete == {"id", "retrospective", "cluster", "author", "text", "created_at"}
    # The cluster is nullable — a note may be against the retrospective as a whole.
    assert Note._meta.get_field("cluster").null is True
    # A note is not a card: it has no public handle, so item 9 does not touch it.
    assert not any(field.name == "public_id" for field in Note._meta.get_fields())


def test_a_note_is_ordered_by_creation() -> None:
    assert Note._meta.ordering == ["created_at", "id"]


# --------------------------------------------------------------------------
# D. Adding notes
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_member_adds_a_note_against_a_cluster(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    log_in(client, ada)

    response = client.post(
        note_add_url(board.retro), {"cluster": board.bravo.pk, "text": "  we keep hitting this  "}
    )

    assert response.status_code == 200
    note = Note.objects.get(retrospective=board.retro)
    # Trimmed, attributed to Ada, against Bravo.
    assert note.text == "we keep hitting this"
    assert note.author_id == ada.pk
    assert note.cluster_id == board.bravo.pk


@pytest.mark.django_db
def test_a_member_adds_a_note_against_the_retrospective_as_a_whole(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """No cluster field means a note about the whole retrospective, not a topic."""
    log_in(client, ada)

    response = client.post(note_add_url(board.retro), {"text": "good session overall"})

    assert response.status_code == 200
    note = Note.objects.get(retrospective=board.retro)
    assert note.cluster_id is None


@pytest.mark.django_db
def test_a_note_appears_in_the_payload_with_its_authors_display_name(
    client: Client, board: DiscussionBoard, ada: User, bruno: User
) -> None:
    """Attributed, and by name — the criterion, and what item 10 allows for notes."""
    ada_client = Client()
    log_in(ada_client, ada)
    ada_client.post(note_add_url(board.retro), {"cluster": board.bravo.pk, "text": "ada's point"})

    viewer = Client()
    log_in(viewer, bruno)
    payload = get_state(viewer, board.retro).json()

    assert len(notes_in(payload)) == 1
    note = notes_in(payload)[0]
    assert note["author"] == ADA_DISPLAY_NAME
    assert note["text"] == "ada's point"
    assert note["cluster"] == board.bravo.pk
    # And the raw body says who wrote it — attribution is not hidden.
    assert ADA_DISPLAY_NAME in get_state(viewer, board.retro).content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("text", ["", "   ", "\t\n ", "\r\n", "\u00a0"])
def test_empty_or_whitespace_only_note_text_is_rejected(
    board: DiscussionBoard, ada: User, text: str
) -> None:
    client, token = strict_client(ada, board.retro)

    response = token_post(
        client, token, "board-note-add", board.retro, {"cluster": board.bravo.pk, "text": text}
    )

    assert response.status_code == 400
    assert response.json()["error"]
    assert Note.objects.filter(retrospective=board.retro).count() == 0


@pytest.mark.django_db
def test_notes_appear_in_creation_order(client: Client, board: DiscussionBoard, ada: User) -> None:
    log_in(client, ada)
    for index in range(3):
        assert (
            client.post(note_add_url(board.retro), {"text": f"note number {index}"}).status_code
            == 200
        )

    payload = get_state(client, board.retro).json()

    assert [note["text"] for note in notes_in(payload)] == [
        "note number 0",
        "note number 1",
        "note number 2",
    ]


@pytest.mark.django_db
def test_adding_a_note_bumps_the_version_and_answers_with_the_board(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """The same full state the read endpoint gives, so a client can skip a poll."""
    log_in(client, ada)
    before = version_of(board.retro)

    added = client.post(note_add_url(board.retro), {"text": "concurrency-safe write"})
    polled = get_state(client, board.retro)

    assert added.status_code == 200
    assert version_of(board.retro) == before + 1
    assert added.json()["version"] == before + 1
    assert json.loads(added.content) == json.loads(polled.content)


@pytest.mark.django_db
def test_a_note_against_a_cluster_on_another_board_is_a_404(
    project: Project, owner: User, ada: User, bruno: User
) -> None:
    board = DiscussionBoard(project, owner, ada, bruno)
    other = DiscussionBoard(project, owner, ada, bruno, week=date(2026, 8, 24))
    client, token = strict_client(ada, board.retro)

    response = token_post(
        client,
        token,
        "board-note-add",
        board.retro,
        {"cluster": other.bravo.pk, "text": "wrong board"},
    )

    assert response.status_code == 404
    assert Note.objects.filter(retrospective=board.retro).count() == 0


# --------------------------------------------------------------------------
# E. Editing and deleting notes
# --------------------------------------------------------------------------


def make_note(board: DiscussionBoard, author: User, text: str = "a note") -> Note:
    return Note.objects.create(
        retrospective=board.retro, cluster=board.bravo, author=author, text=text
    )


@pytest.mark.django_db
def test_a_member_edits_their_own_note(client: Client, board: DiscussionBoard, ada: User) -> None:
    note = make_note(board, ada, "first draft")
    log_in(client, ada)

    response = client.post(
        reverse("board-note-edit", args=[board.retro.pk]),
        {"note": note.pk, "text": "  second draft  "},
    )

    assert response.status_code == 200
    note.refresh_from_db()
    assert note.text == "second draft"


@pytest.mark.django_db
def test_a_member_cannot_edit_another_members_note(
    board: DiscussionBoard, ada: User, bruno: User
) -> None:
    """A 403, proved with a token, and the text is untouched."""
    note = make_note(board, bruno, "bruno wrote this")
    client, token = strict_client(ada, board.retro)

    response = token_post(
        client, token, "board-note-edit", board.retro, {"note": note.pk, "text": "ada's rewrite"}
    )

    assert response.status_code == 403
    note.refresh_from_db()
    assert note.text == "bruno wrote this"


@pytest.mark.django_db
def test_the_facilitator_cannot_edit_another_members_note(
    board: DiscussionBoard, owner: User, bruno: User
) -> None:
    """Delete any, edit none: a facilitator may not put words in a member's mouth."""
    note = make_note(board, bruno, "bruno wrote this")
    client, token = strict_client(owner, board.retro)

    response = token_post(
        client, token, "board-note-edit", board.retro, {"note": note.pk, "text": "facilitator edit"}
    )

    assert response.status_code == 403
    note.refresh_from_db()
    assert note.text == "bruno wrote this"


@pytest.mark.django_db
def test_a_member_deletes_their_own_note(client: Client, board: DiscussionBoard, ada: User) -> None:
    note = make_note(board, ada)
    log_in(client, ada)

    response = client.post(reverse("board-note-delete", args=[board.retro.pk]), {"note": note.pk})

    assert response.status_code == 200
    assert not Note.objects.filter(pk=note.pk).exists()


@pytest.mark.django_db
def test_the_facilitator_deletes_any_note(
    client: Client, board: DiscussionBoard, owner: User, bruno: User
) -> None:
    note = make_note(board, bruno)
    log_in(client, owner)

    response = client.post(reverse("board-note-delete", args=[board.retro.pk]), {"note": note.pk})

    assert response.status_code == 200
    assert not Note.objects.filter(pk=note.pk).exists()


@pytest.mark.django_db
def test_a_member_cannot_delete_another_members_note(
    board: DiscussionBoard, ada: User, bruno: User
) -> None:
    note = make_note(board, bruno)
    client, token = strict_client(ada, board.retro)

    response = token_post(client, token, "board-note-delete", board.retro, {"note": note.pk})

    assert response.status_code == 403
    assert Note.objects.filter(pk=note.pk).exists()


@pytest.mark.django_db
def test_editing_or_deleting_a_note_bumps_the_version(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    note = make_note(board, ada, "before")
    log_in(client, ada)
    before = version_of(board.retro)

    client.post(
        reverse("board-note-edit", args=[board.retro.pk]), {"note": note.pk, "text": "after"}
    )
    assert version_of(board.retro) == before + 1

    client.post(reverse("board-note-delete", args=[board.retro.pk]), {"note": note.pk})
    assert version_of(board.retro) == before + 2


@pytest.mark.django_db
def test_editing_a_note_to_the_text_it_already_has_does_not_bump_the_version(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    note = make_note(board, ada, "unchanged")
    log_in(client, ada)
    before = version_of(board.retro)

    response = client.post(
        reverse("board-note-edit", args=[board.retro.pk]), {"note": note.pk, "text": "unchanged"}
    )

    assert response.status_code == 200
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "1.5", "9" * 5000, str(uuid.uuid4())])
def test_a_note_id_that_does_not_resolve_is_a_404(
    board: DiscussionBoard, ada: User, raw: str
) -> None:
    note = make_note(board, ada)
    client, token = strict_client(ada, board.retro)

    for url_name in ("board-note-edit", "board-note-delete"):
        response = token_post(client, token, url_name, board.retro, {"note": raw, "text": "x"})
        assert response.status_code == 404, (url_name, raw)
    assert Note.objects.filter(pk=note.pk).exists()


@pytest.mark.django_db
def test_a_note_from_another_board_is_a_404(
    project: Project, owner: User, ada: User, bruno: User
) -> None:
    board = DiscussionBoard(project, owner, ada, bruno)
    other = DiscussionBoard(project, owner, ada, bruno, week=date(2026, 8, 24))
    stranger = Note.objects.create(
        retrospective=other.retro, cluster=other.bravo, author=ada, text="on the other board"
    )
    client, token = strict_client(ada, board.retro)

    for url_name in ("board-note-edit", "board-note-delete"):
        response = token_post(
            client, token, url_name, board.retro, {"note": stranger.pk, "text": "x"}
        )
        assert response.status_code == 404, url_name
    stranger.refresh_from_db()
    assert stranger.text == "on the other board"


# --------------------------------------------------------------------------
# F. The COMPLETE freeze
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_notes_are_read_only_once_the_stage_is_complete(
    board: DiscussionBoard, owner: User, ada: User
) -> None:
    """Add, edit and delete are all refused with a 409, for member and facilitator.

    The note is still there and still in the payload; it just cannot be changed.
    """
    note = make_note(board, ada, "said in the meeting")
    board.refresh()
    advance_to(board.retro, owner, Stage.COMPLETE)
    board.refresh()

    for user in (ada, owner):
        client, token = strict_client(user, board.retro)
        assert (
            token_post(client, token, "board-note-add", board.retro, {"text": "late"}).status_code
            == 409
        )
        assert (
            token_post(
                client, token, "board-note-edit", board.retro, {"note": note.pk, "text": "late"}
            ).status_code
            == 409
        )
        assert (
            token_post(
                client, token, "board-note-delete", board.retro, {"note": note.pk}
            ).status_code
            == 409
        )

    assert Note.objects.filter(pk=note.pk).exists()
    # The note still shows on the (now read-only) board.
    viewer = Client()
    log_in(viewer, ada)
    payload = get_state(viewer, board.retro).json()
    assert any(item["text"] == "said in the meeting" for item in notes_in(payload))


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [Stage.CLUSTER, Stage.VOTE])
def test_adding_a_note_before_discuss_is_a_clear_error(
    project: Project, owner: User, ada: User, bruno: User, stage: str
) -> None:
    """No agenda yet, no notes yet — a note before DISCUSS is a 409."""
    cycle = make_cycle(project, owner, date(2026, 8, 17))
    retro = Retrospective.objects.create(cycle=cycle)
    Card.objects.create(cycle=cycle, author=ada, category=Card.Category.START, text="c")
    advance_to(retro, owner, stage)
    client, token = strict_client(ada, retro)

    response = token_post(client, token, "board-note-add", retro, {"text": "too early"})

    assert response.status_code == 409
    assert Note.objects.filter(retrospective=retro).count() == 0


# --------------------------------------------------------------------------
# G. A note leaks nothing about a card
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_note_carries_no_card_author_and_no_card_pk(
    client: Client, board: DiscussionBoard, ada: User
) -> None:
    """Attributed to its own author, and to nothing about any card.

    A note dict is exactly four keys — id, cluster, author, text — so it cannot
    smuggle a card's author or a card's primary key, and neither a card's
    `public_id` nor its `pk` appears on a note.
    """
    log_in(client, ada)
    client.post(note_add_url(board.retro), {"cluster": board.bravo.pk, "text": "a note"})

    payload = get_state(client, board.retro).json()
    note = notes_in(payload)[0]

    assert set(note) == {"id", "cluster", "author", "text"}
    # No timestamp, no anonymity flag, no card handle rides on the note.
    for key in note:
        assert "created" not in key and "_at" not in key, key
        assert "anon" not in key.lower(), key
    card = Card.objects.filter(cycle=board.cycle).first()
    assert str(card.public_id) != str(note["id"])
    # The note's cluster is an integer cluster id, never a card's pk or handle.
    assert note["cluster"] == board.bravo.pk


# --------------------------------------------------------------------------
# H. Access — non-members, anonymous, and the same 404
# --------------------------------------------------------------------------


DISCUSSION_WRITES = [
    ("board-cluster-status", lambda board: {"cluster": board.bravo.pk, "status": Status.DISCUSSED}),
    ("board-note-add", lambda board: {"text": "a note"}),
    ("board-note-edit", lambda board: {"note": 1, "text": "a note"}),
    ("board-note-delete", lambda board: {"note": 1}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,body", DISCUSSION_WRITES)
def test_a_non_member_gets_404_and_changes_nothing(
    board: DiscussionBoard, outsider: User, url_name: str, body
) -> None:
    client = Client()
    log_in(client, outsider)
    before = version_of(board.retro)

    response = client.post(reverse(url_name, args=[board.retro.pk]), body(board))

    assert response.status_code == 404
    assert version_of(board.retro) == before


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,body", DISCUSSION_WRITES)
def test_an_anonymous_user_gets_404_and_is_not_sent_to_the_login_page(
    board: DiscussionBoard, url_name: str, body
) -> None:
    """A redirect would confirm the retrospective exists as surely as a 403 would."""
    client = Client()

    response = client.post(reverse(url_name, args=[board.retro.pk]), body(board))

    assert response.status_code == 404
    assert "Location" not in response.headers


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,body", DISCUSSION_WRITES)
def test_a_superuser_from_outside_the_project_gets_404(
    board: DiscussionBoard, root: User, url_name: str, body
) -> None:
    """`_docs/decisions.md` item 3 has no admin exception, so neither has this."""
    client = Client()
    log_in(client, root)

    response = client.post(reverse(url_name, args=[board.retro.pk]), body(board))

    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,body", DISCUSSION_WRITES)
def test_every_discussion_write_is_get_refused(
    client: Client, board: DiscussionBoard, ada: User, url_name: str, body
) -> None:
    """None of them is a GET — the writes are POST-only, answered with a 405."""
    log_in(client, ada)

    assert client.get(reverse(url_name, args=[board.retro.pk])).status_code == 405


@pytest.mark.django_db
@pytest.mark.parametrize("url_name,body", DISCUSSION_WRITES)
def test_every_discussion_write_refuses_a_post_without_a_csrf_token(
    board: DiscussionBoard, ada: User, owner: User, url_name: str, body
) -> None:
    client = Client(enforce_csrf_checks=True)
    # The facilitator, so the status write is not refused for permission first.
    log_in(client, owner)

    response = client.post(reverse(url_name, args=[board.retro.pk]), body(board))

    assert response.status_code == 403
