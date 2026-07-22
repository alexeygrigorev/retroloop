"""The board state endpoint: what it returns, and everything it must not.

Every test here maps to an acceptance criterion of issue #11. Four themes run
through them, and they are the reason the file is shaped the way it is.

**Absence is asserted, not presence.** This project has shipped defects past a
green suite because every test asked whether a required string was there and
none asked whether a forbidden one was gone. So the tests below assert that
another member's card text, username, display name, first and last name, and
every card's `created_at`, are missing — from the *raw response body*, not from
a Python object, because the body is what leaks.

**Fields are discovered, not listed.** The sweeps walk the parsed payload to
every depth and judge the keys and values they find. A later issue that adds a
field carrying a timestamp, or an author, fails here without anyone having
remembered to come back and add it to a list.

**The whole body is searched.** A leak in an id, in an ordering, or inside a
nested object is still a leak, so the checks run over the serialized bytes and
over every nested value, not over the fields a UI would draw.

**Nothing is re-decided.** `projects/permissions.py` holds the rules. The card
selection in the serializer is `can_view_card` written as a query, so one test
walks every card at every stage and asserts the two agree, rather than trusting
that they do.

Two criteria cannot be fully exercised yet and say so where they are tested:
`Cluster` arrives with #12 and `Vote` with #15, so the clusters list and the
vote totals are empty. What is tested today is the *gate* — that the totals key
does not exist while the stage is `VOTE` and does from `DISCUSS` — which is the
half that leaks if it is wrong.
"""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from board import serializers
from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from projects.permissions import can_see_vote_totals, can_view_card
from retro.models import STAGE_ORDER, Retrospective
from retro.services import advance_stage

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: Long and unmistakable, so "this string is not in the body" cannot pass or
#: fail by accident on a substring of some unrelated markup.
BRUNO_USERNAME = "bruno-author-4e71"
BRUNO_DISPLAY_NAME = "Bruno Author 4e71"
BRUNO_FIRST_NAME = "Brunhilde4e71"
BRUNO_LAST_NAME = "Authorsson4e71"

#: Everything that names Bruno, the member whose data the viewer must not get.
BRUNO_IDENTIFIERS = (
    BRUNO_USERNAME,
    BRUNO_DISPLAY_NAME,
    BRUNO_FIRST_NAME,
    BRUNO_LAST_NAME,
)

BRUNO_OPEN_TEXT = "standups run long and start late 9c04"
BRUNO_SECRET_TEXT = "the deploy checklist is out of date and nobody owns it 5b21"
OWNER_TEXT = "pairing on the migration went well 1a88"

#: Everything only Bruno and the owner wrote. Absent before REVEAL, present after.
OTHER_MEMBERS_TEXT = (BRUNO_OPEN_TEXT, BRUNO_SECRET_TEXT, OWNER_TEXT)

ADA_OPEN_TEXT = "we should write the runbook down 3f60"
ADA_SECRET_TEXT = "code review turnaround is too slow 7e15"

#: Every stage the endpoint can be asked about, so no sweep covers only the
#: convenient ones.
ALL_STAGES = list(STAGE_ORDER)

#: The stages from REVEAL on — the ones where every card is in the payload.
REVEALED_STAGES = ALL_STAGES[STAGE_ORDER.index(Stage.REVEAL) :]

#: A key naming a moment in time. `created_at` is the one that matters —
#: it survives the reveal and is the submission order the shuffle destroys —
#: but the pattern is deliberately wider than that one name, so a later issue
#: cannot reintroduce the same information under a different label.
TIME_SHAPED_KEY = re.compile(
    r"created|updated|modified|submitted|opened|closed|started|completed"
    r"|timestamp|when|_at$|^at$|date|time",
    re.IGNORECASE,
)

#: A key naming a person. No card carries one at any stage. Applied to the card
#: dicts rather than to the whole body, because a cluster's `name` (#12) is the
#: team's own words for a topic and not a person.
PERSON_SHAPED_KEY = re.compile(
    r"author|user|member|owner|username|display|name|by$|^who",
    re.IGNORECASE,
)

#: Seconds since the epoch, roughly 2001 to 2065, and the same in milliseconds.
#: A timestamp smuggled through as a number rather than a string still fails.
EPOCH_SECONDS = range(1_000_000_000, 3_000_000_000)
EPOCH_MILLISECONDS = range(1_000_000_000_000, 3_000_000_000_000)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str, **extra) -> User:
    return User.objects.create_user(
        username=username, password=PASSWORD, display_name=display_name, **extra
    )


def log_in(client: Client, user: User) -> None:
    assert client.login(username=user.username, password=PASSWORD)


@pytest.fixture
def owner(db) -> User:
    """The project's owner and this cycle's facilitator, so it can be advanced."""
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def ada(project: Project) -> User:
    """The viewer. Every request in this file is made as Ada unless it says otherwise."""
    user = make_user("ada", "Ada Viewer")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def bruno(project: Project) -> User:
    """The other member. Nothing that names him may reach Ada's browser."""
    user = make_user(
        BRUNO_USERNAME,
        BRUNO_DISPLAY_NAME,
        first_name=BRUNO_FIRST_NAME,
        last_name=BRUNO_LAST_NAME,
    )
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    """A real account on no project of ours."""
    return make_user("outsider", "Ora Outsider")


@pytest.fixture
def root(db) -> User:
    """A superuser on no project. `_docs/decisions.md` item 3 has no admin exception."""
    return make_user("root", "Root Rooter", is_superuser=True, is_staff=True)


@pytest.fixture
def cycle(project: Project, owner: User) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=owner,
    )


@pytest.fixture
def retro(cycle: FeedbackCycle) -> Retrospective:
    return Retrospective.objects.create(cycle=cycle)


def make_card(
    cycle: FeedbackCycle,
    author: User,
    text: str,
    *,
    anonymous: bool = False,
    category: str = Card.Category.START,
    written_at: datetime | None = None,
) -> Card:
    """One card, with `created_at` pinned to a known moment when it is given.

    `created_at` is `auto_now_add`, so it is written afterwards with an UPDATE.
    The sweeps that look for it in a response body need it to be a fact rather
    than whatever the clock said while the test ran.
    """
    card = Card.objects.create(
        cycle=cycle, author=author, text=text, category=category, is_anonymous=anonymous
    )
    if written_at is not None:
        Card.objects.filter(pk=card.pk).update(created_at=written_at)
        card.refresh_from_db()
    return card


@pytest.fixture
def board(cycle: FeedbackCycle, ada: User, bruno: User, owner: User) -> dict[str, Card]:
    """Five cards from three members: two of Ada's, two of Bruno's, one of the owner's.

    Written in a known order, an hour apart, so "this is submission order" is
    something a test can state rather than infer. Two of them are anonymous, so
    the anonymous and the attributed case are both live at every stage.
    """
    return {
        "ada_open": make_card(
            cycle,
            ada,
            ADA_OPEN_TEXT,
            category=Card.Category.START,
            written_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        ),
        "bruno_open": make_card(
            cycle,
            bruno,
            BRUNO_OPEN_TEXT,
            category=Card.Category.STOP,
            written_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        ),
        "ada_secret": make_card(
            cycle,
            ada,
            ADA_SECRET_TEXT,
            anonymous=True,
            category=Card.Category.CONTINUE,
            written_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
        ),
        "bruno_secret": make_card(
            cycle,
            bruno,
            BRUNO_SECRET_TEXT,
            anonymous=True,
            category=Card.Category.START,
            written_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        ),
        "owner_open": make_card(
            cycle,
            owner,
            OWNER_TEXT,
            category=Card.Category.STOP,
            written_at=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
        ),
    }


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> Retrospective:
    """Walk the retrospective forward to `stage` through the real stage machine.

    Never by assigning `stage`: the reveal's side effects — the shuffle and the
    destroyed authors — are the whole subject of this file, and they only
    happen on the way through the transition.
    """
    while retro.stage != stage:
        advance_stage(facilitator, retro)
    return retro


def pin_positions(cards: list[Card]) -> list[Card]:
    """Force the reveal order to a known permutation, 1..n in the order given.

    The reveal's shuffle is genuinely random, so a test that asserted "the
    payload is in position order" against it would pass by luck one time in n!
    when the code was in fact ordering by something else. Pinning the positions
    to an order that is not submission order makes the assertion decisive.
    """
    for position, card in enumerate(cards, start=1):
        Card.objects.filter(pk=card.pk).update(position=position)
        card.refresh_from_db()
    return cards


def state_url(retro: Retrospective, version: str | int | None = None) -> str:
    url = reverse("board-state", args=[retro.pk])
    return url if version is None else f"{url}?v={version}"


def get_state(client: Client, retro: Retrospective, version: str | int | None = None):
    return client.get(state_url(retro, version))


def keys_in(payload) -> set[str]:
    """Every key at every depth of the parsed body. Discovery, not a list."""
    found: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found |= set(node)
            stack += list(node.values())
        elif isinstance(node, list):
            stack += node
    return found


def values_in(payload) -> list:
    """Every scalar at every depth of the parsed body, keys excluded."""
    found = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack += list(node.values())
        elif isinstance(node, list):
            stack += node
        else:
            found.append(node)
    return found


def renderings_of(moment: datetime) -> list[str]:
    """The ways a datetime could plausibly have been written into a body.

    Nothing here has to be the format anyone chose; the point is that a
    timestamp is recognisable however it was formatted, so the search for it
    does not depend on guessing the serializer's encoder.
    """
    return [
        moment.isoformat(),
        str(moment),
        moment.strftime("%Y-%m-%dT%H:%M:%S"),
        moment.strftime("%Y-%m-%d %H:%M:%S"),
        moment.strftime("%Y-%m-%d"),
        str(int(moment.timestamp())),
        str(moment.timestamp()),
    ]


def body_of(response) -> str:
    return response.content.decode()


# --------------------------------------------------------------------------
# A. The endpoint, and the version parameter
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_member_gets_the_documented_payload_before_reveal(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    """The whole body, asserted exactly, so an extra field is a failing test.

    This is the shape #13 and #14 are written against, written out rather than
    poked at, and it is also the strongest statement of absence in the file:
    equality fails on a key nobody expected as loudly as on a missing one.
    """
    log_in(client, ada)

    response = get_state(client, retro)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == {
        "id": retro.pk,
        "stage": "DRAFT",
        "version": 0,
        "changed": True,
        "cards": [
            {
                "id": str(board["ada_open"].public_id),
                "category": "START",
                "text": ADA_OPEN_TEXT,
                "cluster": None,
            },
            {
                "id": str(board["ada_secret"].public_id),
                "category": "CONTINUE",
                "text": ADA_SECRET_TEXT,
                "cluster": None,
            },
        ],
        "clusters": [],
        "votes": {"mine": [], "remaining": 3},
    }


@pytest.mark.django_db
def test_a_member_gets_the_documented_payload_from_reveal(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    """The same shape with every card in it, and still no author and no time."""
    advance_to(retro, owner, Stage.REVEAL)
    ordered = pin_positions(
        [
            board["bruno_secret"],
            board["owner_open"],
            board["ada_open"],
            board["bruno_open"],
            board["ada_secret"],
        ]
    )
    log_in(client, ada)

    response = get_state(client, retro)

    assert response.json() == {
        "id": retro.pk,
        "stage": "REVEAL",
        "version": 1,
        "changed": True,
        "cards": [
            {
                "id": str(card.public_id),
                "category": card.category,
                "text": card.text,
                "cluster": None,
            }
            for card in ordered
        ],
        "clusters": [],
        "votes": {"mine": [], "remaining": 3},
    }


@pytest.mark.django_db
def test_a_matching_version_returns_a_small_body_with_no_board_data(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    response = get_state(client, retro, version=retro.version)

    assert response.status_code == 200
    assert response.json() == {"id": retro.pk, "version": 1, "changed": False}

    body = body_of(response)
    for text in (*OTHER_MEMBERS_TEXT, ADA_OPEN_TEXT, ADA_SECRET_TEXT):
        assert text not in body
    assert "cards" not in body
    assert "clusters" not in body
    assert "votes" not in body


@pytest.mark.django_db
def test_a_stale_version_returns_the_full_state(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    response = get_state(client, retro, version=retro.version - 1)

    payload = response.json()
    assert payload["changed"] is True
    assert payload["version"] == retro.version
    assert len(payload["cards"]) == len(board)


@pytest.mark.django_db
def test_an_absent_version_returns_the_full_state(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    """ "No known version" is the state every client starts in, not an error."""
    log_in(client, ada)

    assert get_state(client, retro).json()["changed"] is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "-1",
        "1.5",
        "1e3",
        "0x10",
        "null",
        "NaN",
        "None",
        "true",
        " 1 ",
        "1;DROP TABLE retro_retrospective",
        "²",  # superscript two: str.isdigit() is True and int() raises
        "١٢",  # Arabic-Indic digits, which int() does accept
        "9" * 5000,  # longer than CPython will convert at all
        "99999999999999999999999999",
    ],
)
def test_a_junk_version_is_treated_as_no_known_version_and_never_500s(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card], raw: str
) -> None:
    """Every unparseable `v` is the same answer: this caller knows nothing."""
    log_in(client, ada)

    response = client.get(f"{reverse('board-state', args=[retro.pk])}?v={raw}")

    assert response.status_code == 200
    assert response.json()["changed"] is True


@pytest.mark.django_db
def test_a_version_that_matches_after_junk_still_matches(
    client: Client, retro: Retrospective, ada: User, owner: User
) -> None:
    """The guard rejects the junk without breaking the number that is valid."""
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    assert get_state(client, retro, version="1").json()["changed"] is False
    assert get_state(client, retro, version="01").json()["changed"] is False


@pytest.mark.django_db
def test_the_unchanged_response_reads_no_cards_clusters_or_votes(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    """It reads the version and returns. The poll runs every 1.5s per open board."""
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    with CaptureQueriesContext(connection) as captured:
        response = get_state(client, retro, version=retro.version)

    assert response.json()["changed"] is False
    for query in captured.captured_queries:
        assert "cycles_card" not in query["sql"], query["sql"]
        assert "cycles_cycleparticipation" not in query["sql"], query["sql"]


@pytest.mark.django_db
def test_the_endpoint_is_read_only(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    """#12 owns the writes. This one refuses anything that is not a GET."""
    log_in(client, ada)
    url = state_url(retro)

    assert client.post(url).status_code == 405
    assert client.put(url).status_code == 405
    assert client.delete(url).status_code == 405
    assert Card.objects.filter(cycle=retro.cycle).count() == len(board)


# --------------------------------------------------------------------------
# B. Filtering happens here, not in the client
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_before_reveal_the_body_holds_the_viewers_own_cards_and_nobody_elses(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    """Asserted against the bytes, and against the absence of the other three."""
    log_in(client, ada)

    response = get_state(client, retro)
    body = body_of(response)
    payload = response.json()

    assert [card["id"] for card in payload["cards"]] == [
        str(board["ada_open"].public_id),
        str(board["ada_secret"].public_id),
    ]
    assert ADA_OPEN_TEXT in body
    for text in OTHER_MEMBERS_TEXT:
        assert text not in body, text


@pytest.mark.django_db
@pytest.mark.parametrize("stage", REVEALED_STAGES)
def test_from_reveal_every_card_is_present_in_position_order(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """`position`, and nothing else — the order the shuffle handed out.

    The positions are pinned to a permutation that is not submission order, so
    a serializer that had fallen back on the model's default ordering by
    `created_at` would fail here rather than pass one time in n!.
    """
    advance_to(retro, owner, stage)
    ordered = pin_positions(
        [
            board["bruno_secret"],
            board["ada_secret"],
            board["owner_open"],
            board["ada_open"],
            board["bruno_open"],
        ]
    )
    # `Card.Meta.ordering` is `["created_at", "id"]`, so a plain queryset is
    # submission order — the order the payload must not be in, whatever the
    # handles are called.
    submission_order = [str(card.public_id) for card in Card.objects.filter(cycle=retro.cycle)]
    log_in(client, ada)

    payload = get_state(client, retro).json()

    assert [card["id"] for card in payload["cards"]] == [str(card.public_id) for card in ordered]
    assert [card["id"] for card in payload["cards"]] != submission_order


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_no_card_carries_an_author_at_any_stage(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """Not `null`, not an empty string, not an id — no author field at all.

    The anonymous card is the criterion; the attributed one is here because an
    author on it would identify the anonymous ones by elimination in a team
    this size, and because a shape difference between the two is itself a way
    to read which is which.

    Discovery, not a list: every key at every depth is judged by pattern, so a
    field added later that names a person fails here too.
    """
    advance_to(retro, owner, stage)
    log_in(client, ada)

    response = get_state(client, retro)
    body = body_of(response)
    payload = response.json()

    for card in payload["cards"]:
        for key in keys_in(card):
            assert not PERSON_SHAPED_KEY.search(key), f"{key} names a person"

    for identifier in BRUNO_IDENTIFIERS:
        assert identifier not in body, identifier
    assert "Olive Owner" not in body
    assert "Ada Viewer" not in body


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_no_response_carries_a_cards_created_at_at_any_stage(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """The one thing that survives the reveal and would hand back what it destroyed.

    `Card.created_at` is submission order. `Card.Meta.ordering` is still
    `["created_at", "id"]`, so it is what a plain queryset gives you by
    default, which is why this sweep exists rather than a note asking the next
    person not to serialize it.

    Three checks, none of which names a field: no key at any depth is shaped
    like a moment in time, no value parses as one, and no card's actual
    `created_at` appears anywhere in the bytes however it might have been
    formatted.
    """
    advance_to(retro, owner, stage)
    log_in(client, ada)

    response = get_state(client, retro)
    body = body_of(response)
    payload = response.json()

    for key in keys_in(payload):
        assert not TIME_SHAPED_KEY.search(key), f"{key} names a moment in time"

    for value in values_in(payload):
        if isinstance(value, str):
            with pytest.raises(ValueError):
                datetime.fromisoformat(value)
        if isinstance(value, int) and not isinstance(value, bool):
            assert value not in EPOCH_SECONDS, value
            assert value not in EPOCH_MILLISECONDS, value

    for card in Card.objects.filter(cycle=retro.cycle):
        for rendering in renderings_of(card.created_at):
            assert rendering not in body, rendering


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_card_selection_agrees_with_can_view_card_at_every_stage(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """The serializer re-decides nothing: it is `can_view_card`, as a query.

    A predicate called once per card would make the endpoint's cost grow with
    the board, so the rule is expressed as a filter instead. This is what makes
    that safe — the two are compared card by card, in both directions, at every
    stage, so a filter that drifted from the rule fails here.
    """
    advance_to(retro, owner, stage)
    log_in(client, ada)

    payload = get_state(client, retro).json()
    served = {card["id"] for card in payload["cards"]}

    for card in Card.objects.filter(cycle=retro.cycle):
        assert (str(card.public_id) in served) is can_view_card(ada, card), (card.pk, stage)


@pytest.mark.django_db
def test_no_vote_total_appears_while_the_stage_is_vote(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    """Totals are secret during VOTE, so the key that would hold them is absent.

    Absent rather than empty: `_docs/decisions.md` item 2 lets a member move
    their votes freely while the stage is VOTE, which is only safe while nobody
    can see the running totals — including "nobody has voted here yet", which
    an empty object would say.

    `Vote` arrives with #15, so there is no other member's vote to create here.
    What this pins is the gate, which is the half that leaks if it is wrong;
    #15's own criteria cover the numbers.
    """
    advance_to(retro, owner, Stage.VOTE)
    log_in(client, ada)

    response = get_state(client, retro)
    payload = response.json()

    assert can_see_vote_totals(ada, retro) is False
    assert "vote_totals" not in payload
    assert "vote_totals" not in body_of(response)
    for key in keys_in(payload):
        assert "total" not in key.lower(), key
        assert "count" not in key.lower(), key


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [Stage.DRAFT, Stage.REVEAL, Stage.CLUSTER, Stage.VOTE])
def test_the_totals_key_does_not_exist_before_discuss(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    advance_to(retro, owner, stage)
    log_in(client, ada)

    assert "vote_totals" not in get_state(client, retro).json()


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [Stage.DISCUSS, Stage.COMPLETE])
def test_totals_per_cluster_are_included_from_discuss_on(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """Empty until #15 computes them, but present — the gate opens here."""
    advance_to(retro, owner, stage)
    log_in(client, ada)

    payload = get_state(client, retro).json()

    assert can_see_vote_totals(ada, retro) is True
    assert payload["vote_totals"] == {}


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_viewers_own_votes_and_budget_are_in_the_payload(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: dict[str, Card],
    stage: str,
) -> None:
    """Scoped to the viewer by construction — there is no branch that widens it."""
    advance_to(retro, owner, stage)
    log_in(client, ada)

    payload = get_state(client, retro).json()

    assert payload["votes"] == {"mine": [], "remaining": retro.votes_per_member}


# --------------------------------------------------------------------------
# C. Access
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_project_member_gets_the_state(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    log_in(client, ada)

    assert get_state(client, retro).status_code == 200


@pytest.mark.django_db
def test_a_non_member_gets_the_same_404_as_an_id_that_was_never_used(
    client: Client, retro: Retrospective, outsider: User, board: dict[str, Card]
) -> None:
    """Byte for byte the same, so the response cannot confirm the retro exists."""
    log_in(client, outsider)
    missing_pk = retro.pk + 10_000

    refused = get_state(client, retro)
    never_existed = client.get(reverse("board-state", args=[missing_pk]))

    assert refused.status_code == 404
    assert never_existed.status_code == 404
    assert refused.content == never_existed.content


@pytest.mark.django_db
def test_an_anonymous_user_gets_404_and_is_not_sent_to_the_login_page(
    client: Client, retro: Retrospective, board: dict[str, Card]
) -> None:
    """A redirect would confirm the retrospective exists as surely as a 403 would."""
    response = get_state(client, retro)

    assert response.status_code == 404
    assert "Location" not in response.headers
    for text in (*OTHER_MEMBERS_TEXT, ADA_OPEN_TEXT, ADA_SECRET_TEXT):
        assert text not in body_of(response)


@pytest.mark.django_db
def test_a_member_of_another_project_gets_404(
    client: Client, retro: Retrospective, outsider: User, board: dict[str, Card]
) -> None:
    """Membership of some project is not membership of this one."""
    elsewhere = Project.objects.create(name="Elsewhere", owner=outsider)
    Membership.objects.create(project=elsewhere, user=outsider, role=Membership.Role.FACILITATOR)
    log_in(client, outsider)

    assert get_state(client, retro).status_code == 404


@pytest.mark.django_db
def test_a_superuser_who_is_not_a_member_gets_404(
    client: Client, retro: Retrospective, root: User, board: dict[str, Card]
) -> None:
    """Being staff reveals nothing — `_docs/decisions.md` item 3 has no exception."""
    log_in(client, root)

    response = get_state(client, retro)

    assert response.status_code == 404
    for text in OTHER_MEMBERS_TEXT:
        assert text not in body_of(response)


@pytest.mark.django_db
def test_a_deactivated_member_gets_404(
    client: Client, retro: Retrospective, ada: User, board: dict[str, Card]
) -> None:
    log_in(client, ada)
    ada.is_active = False
    ada.save(update_fields=["is_active"])

    assert get_state(client, retro).status_code == 404


# --------------------------------------------------------------------------
# D. Efficiency
# --------------------------------------------------------------------------

#: Session, user, the retrospective with its cycle and project, the membership
#: `can_view_project` reads, and the cards. Five, whatever the board holds.
FULL_STATE_QUERIES = 5


@pytest.mark.django_db
def test_the_query_count_does_not_grow_with_the_number_of_cards(
    client: Client, retro: Retrospective, ada: User, owner: User, bruno: User
) -> None:
    """A fixed count, measured against a board of forty cards and one of four.

    Clusters and votes are not in the comparison because `Cluster` (#12) and
    `Vote` (#15) do not exist yet — the serializer reaches them through one
    function each, and the endpoint's cost cannot grow with rows that no table
    holds. #12 and #15 extend this test with their own models.
    """
    for index in range(4):
        make_card(retro.cycle, bruno, f"small board card {index}")
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    with CaptureQueriesContext(connection) as small:
        assert get_state(client, retro).status_code == 200

    for index in range(36):
        make_card(retro.cycle, bruno, f"large board card {index}")

    with CaptureQueriesContext(connection) as large:
        response = get_state(client, retro)

    assert len(response.json()["cards"]) == 40
    assert len(small.captured_queries) == FULL_STATE_QUERIES
    assert len(large.captured_queries) == FULL_STATE_QUERIES


@pytest.mark.django_db
def test_the_unchanged_response_is_cheaper_than_the_full_one(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    advance_to(retro, owner, Stage.REVEAL)
    log_in(client, ada)

    with CaptureQueriesContext(connection) as unchanged:
        get_state(client, retro, version=retro.version)

    assert len(unchanged.captured_queries) < FULL_STATE_QUERIES


# --------------------------------------------------------------------------
# E. The shape is documented, and stays documented
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_key_the_endpoint_emits_is_named_in_the_serializer_docstring(
    client: Client, retro: Retrospective, ada: User, owner: User, board: dict[str, Card]
) -> None:
    """#13 and #14 are written against this shape, so it is documented in one place.

    Discovered from a live response rather than listed, so a field added later
    fails until it is written down.
    """
    documentation = serializers.__doc__
    log_in(client, ada)

    emitted = set()
    for stage in ALL_STAGES:
        advance_to(retro, owner, stage)
        emitted |= keys_in(get_state(client, retro).json())
    emitted |= keys_in(get_state(client, retro, version=retro.version).json())

    for key in emitted:
        assert f'"{key}"' in documentation, key


# --------------------------------------------------------------------------
# F. Carrying conditions
# --------------------------------------------------------------------------


def test_the_board_decides_no_access_rule_of_its_own() -> None:
    """One permissions module. The board imports from it and defines nothing."""
    board_dir = BASE_DIR / "board"

    assert not (board_dir / "permissions.py").exists()
    for path in sorted(board_dir.rglob("*.py")):
        source = path.read_text()
        assert "def can_" not in source, path
        assert "request.user ==" not in source, path
