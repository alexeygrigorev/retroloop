"""Issue #14: the cluster board island, wired to #11's state read and #12's writes.

The board itself is drawn by React in the browser, so the behaviour that cannot
be seen from Python — the poll loop, the drag, the mid-drag defer — is asserted
here against the bundle's own source, the same contract style
`tests/test_island.py` uses, and exercised for real with Playwright (see the
issue's report, screenshots under /home/alexey/tmp/retro-shots/wt14).

What Python *can* see, it asserts directly: the endpoint URLs the template hands
the island, the CSRF cookie every member needs to write, that no card's primary
key reaches the payload the board reads (`_docs/decisions.md` item 9), and that
every board class the bundle names is really compiled into the stylesheet.
"""

import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import Retrospective
from retro.services import advance_stage
from retro.views import board_bootstrap

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"
ISLAND_SOURCE = (BASE_DIR / "assets" / "js" / "board.jsx").read_text()

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: The endpoints the island is entitled to talk to: #11's one read, #12's seven
#: writes, and nothing else.
ENDPOINT_KEYS = {
    "state": "board-state",
    "cardMove": "board-card-move",
    "cardUngroup": "board-card-ungroup",
    "clusterCreate": "board-cluster-create",
    "clusterRename": "board-cluster-rename",
    "clusterMerge": "board-cluster-merge",
    "clusterSplit": "board-cluster-split",
    "clusterDelete": "board-cluster-delete",
}

#: Every board component class the island uses. Kept in step with the `named`
#: set in `tests/test_island.py`; asserted compiled by the stylesheet test below.
BOARD_CLASSES = [
    "board",
    "board-note",
    "board-banner",
    "board-toolbar",
    "board-input",
    "board-columns",
    "board-column",
    "board-column-head",
    "board-column-title",
    "board-tag",
    "board-actions",
    "board-select",
    "board-check",
    "board-cards",
    "board-card",
    "board-card-head",
    "board-card-text",
]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


@pytest.fixture
def viewer(db) -> User:
    """The project owner and this cycle's facilitator, so a board can be advanced."""
    return make_user("alexey", "Alexey G")


@pytest.fixture
def other(db) -> User:
    return make_user("mira", "Mira M")


@pytest.fixture
def project(viewer, other) -> Project:
    project = Project.objects.create(name="Platform", owner=viewer)
    Membership.objects.create(project=project, user=viewer, role=Membership.Role.FACILITATOR)
    Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)
    return project


@pytest.fixture
def cycle(project, viewer) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=viewer,
    )


@pytest.fixture
def retro(cycle) -> Retrospective:
    return Retrospective.objects.create(cycle=cycle)


@pytest.fixture
def as_viewer(client: Client, viewer) -> Client:
    client.login(username="alexey", password=PASSWORD)
    return client


def write_card(cycle: FeedbackCycle, author: User, text: str, category: str = "START") -> Card:
    return Card.objects.create(cycle=cycle, author=author, text=text, category=category)


def detail_url(retro: Retrospective) -> str:
    return reverse("retro-detail", args=[retro.pk])


def reach(retro: Retrospective, facilitator: User, stage: str) -> None:
    """Walk the real stage machine forward to `stage`; never assign it."""
    while retro.stage != stage:
        advance_stage(facilitator, retro)


# --------------------------------------------------------------------------
# The URLs the island is handed
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_bootstrap_names_the_state_read_and_the_seven_writes(retro, viewer) -> None:
    urls = board_bootstrap(viewer, retro)["urls"]

    assert set(urls) == set(ENDPOINT_KEYS)
    for key, name in ENDPOINT_KEYS.items():
        assert urls[key] == reverse(name, args=[retro.pk])


@pytest.mark.django_db
def test_every_endpoint_addresses_the_retrospective_and_carries_no_card_handle(
    retro, cycle, viewer
) -> None:
    """The URLs name the board by the retrospective's integer pk — public by item
    9 — and never a card's `public_id` or pk: a card is named in the request body."""
    card = write_card(cycle, viewer, "Start pairing on deploys")
    urls = board_bootstrap(viewer, retro)["urls"]

    for url in urls.values():
        assert url.startswith(f"/retros/{retro.pk}/")
        assert str(card.public_id) not in url
        assert f"/{card.pk}" not in url


@pytest.mark.django_db
def test_the_state_url_is_the_only_read_and_the_writes_are_the_mutation_endpoints(
    retro, viewer
) -> None:
    urls = board_bootstrap(viewer, retro)["urls"]

    assert urls["state"].endswith(f"/retros/{retro.pk}/state")
    assert urls["cardMove"].endswith("/cards/move")
    assert urls["cardUngroup"].endswith("/cards/ungroup")
    assert urls["clusterCreate"].endswith("/clusters/create")
    assert urls["clusterMerge"].endswith("/clusters/merge")
    assert urls["clusterSplit"].endswith("/clusters/split")
    assert urls["clusterDelete"].endswith("/clusters/delete")


# --------------------------------------------------------------------------
# Writing needs the CSRF cookie — for every member, not just the facilitator
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_board_page_sets_the_csrf_cookie_for_an_ordinary_member(
    client: Client, retro, other
) -> None:
    """The island reads `csrftoken` from the cookie to POST #12's writes, so the
    cookie has to be there for a member who is not the facilitator and sees none
    of the facilitator-only forms. `@ensure_csrf_cookie` on the detail view puts
    it there directly rather than leaning on some other form happening to render
    `{% csrf_token %}`; without a cookie, a member could see the board and change
    nothing."""
    client.login(username="mira", password=PASSWORD)

    response = client.get(detail_url(retro))

    assert response.status_code == 200
    assert "csrftoken" in response.cookies


# --------------------------------------------------------------------------
# No card's primary key reaches the board — item 9
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_card_primary_key_reaches_the_payload_the_board_reads(
    as_viewer, retro, cycle, viewer, other
) -> None:
    """The board reads through #11's state endpoint; every card there is addressed
    by its `public_id`, and its pk appears nowhere in the body."""
    reach(retro, viewer, Stage.REVEAL)
    mine = write_card(cycle, viewer, "Start writing the runbook")
    theirs = write_card(cycle, other, "Stop the Wednesday sync", category="STOP")
    Card.objects.filter(pk=mine.pk).update(position=1)
    Card.objects.filter(pk=theirs.pk).update(position=2)

    body = as_viewer.get(reverse("board-state", args=[retro.pk])).content.decode()
    data = json.loads(body)

    by_id = {card["id"]: card for card in data["cards"]}
    for card in (mine, theirs):
        assert str(card.public_id) in by_id, "a card is not addressed by its public_id"
        # The payload dict carries no author, no pk, no anonymity flag.
        assert set(by_id[str(card.public_id)]) == {"id", "category", "text", "cluster", "mine"}
        # Its id parses as the UUID and is not the integer primary key.
        assert uuid.UUID(by_id[str(card.public_id)]["id"]) == card.public_id


@pytest.mark.django_db
def test_the_page_source_addresses_the_viewers_cards_by_public_id_not_pk(
    as_viewer, retro, cycle, viewer
) -> None:
    card = write_card(cycle, viewer, "Continue the Friday demo", category="CONTINUE")

    body = as_viewer.get(detail_url(retro)).content.decode()

    assert str(card.public_id) in body
    # The bootstrap's card entry keys on the public_id, never the pk.
    assert re.search(rf'"id":\s*"{card.public_id}"', body)
    assert re.search(rf'"id":\s*{card.pk}\b', body) is None


# --------------------------------------------------------------------------
# The stylesheet really builds every class the island names
# --------------------------------------------------------------------------


def test_the_stylesheet_compiles_every_board_class_the_island_uses(built_stylesheet) -> None:
    """`app.css` scans templates only, so a board class that lived in the `.jsx`
    alone would never compile. Each is defined as a component in `app.css`; this
    asserts each is present in the built stylesheet, so a name in the island's
    allowed set cannot be one the browser never sees."""
    css = built_stylesheet.read_text()

    for name in BOARD_CLASSES:
        assert f".{name}{{" in css, name

    # And each is actually used by the bundle, so the set does not rot.
    for name in BOARD_CLASSES:
        assert name in ISLAND_SOURCE, name


# --------------------------------------------------------------------------
# The poll loop, in the bundle's source — #14's "Staying in sync"
# --------------------------------------------------------------------------


def test_the_island_polls_the_state_endpoint_on_a_timer() -> None:
    """Reads with `fetch`, paced by `setTimeout` at 1.5s, and only ever #11's
    state URL — taken from the bootstrap, never written down here."""
    assert "fetch(" in ISLAND_SOURCE
    assert "setTimeout(" in ISLAND_SOURCE
    assert "1500" in ISLAND_SOURCE
    assert "urls.state" in ISLAND_SOURCE
    # setInterval would fire regardless of whether the last request returned.
    assert "setInterval" not in ISLAND_SOURCE


def test_the_island_pauses_polling_while_the_tab_is_hidden() -> None:
    assert "visibilitychange" in ISLAND_SOURCE
    assert "document.hidden" in ISLAND_SOURCE


def test_the_island_defers_a_poll_that_lands_mid_drag() -> None:
    """A poll arriving during a drag is stashed and applied when the drag ends,
    rather than replacing the board under the user's hand."""
    assert "draggingRef" in ISLAND_SOURCE
    assert "pendingRef" in ISLAND_SOURCE
    assert "flushPending" in ISLAND_SOURCE


def test_the_island_replaces_the_whole_board_and_does_not_diff() -> None:
    """Full-state replacement, last-write-wins: a version behind is ignored, and
    nothing here merges or reconciles."""
    assert "version" in ISLAND_SOURCE
    assert "diff" not in ISLAND_SOURCE.lower()


# --------------------------------------------------------------------------
# The writes, in the bundle's source — #14's "Reorganising"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(ENDPOINT_KEYS))
def test_the_island_writes_through_each_mutation_endpoint(key: str) -> None:
    """Every one of #12's endpoints is reached by name from the bootstrap."""
    assert f"urls.{key}" in ISLAND_SOURCE


def test_the_island_writes_with_the_csrf_token_from_the_cookie() -> None:
    assert "csrftoken" in ISLAND_SOURCE
    assert "X-CSRFToken" in ISLAND_SOURCE


def test_the_island_offers_a_keyboard_reachable_move_control() -> None:
    """Drag is not the only route: a `<select>` on each card moves it to any
    cluster or to ungrouped, reachable and operable from the keyboard."""
    assert "<select" in ISLAND_SOURCE
    assert "draggable" in ISLAND_SOURCE
    assert "onDrop" in ISLAND_SOURCE


def test_the_island_applies_the_state_a_mutation_returns() -> None:
    """A write's response is the whole new board; the island commits it directly
    rather than waiting for the next poll."""
    assert "commit(data)" in ISLAND_SOURCE


# --------------------------------------------------------------------------
# Rendering — item 10 and the suggested mark
# --------------------------------------------------------------------------


def test_the_island_renders_the_mine_mark_and_computes_nothing() -> None:
    """It draws the server's `mine` boolean and never compares an id, a name or a
    text to decide what is the viewer's."""
    assert "card.mine" in ISLAND_SOURCE


def test_the_island_shows_nothing_about_any_other_cards_author_or_anonymity() -> None:
    """Item 10: no author, no anonymity label, no distinguishing field. The
    payload carries none of these and the island reaches for none."""
    assert "Anonymous" not in ISLAND_SOURCE
    for forbidden in ("author", "is_anonymous", "display_name", "username", "avatar", "initials"):
        assert forbidden not in ISLAND_SOURCE, forbidden


def test_the_island_marks_a_suggested_cluster_from_the_payload_flag() -> None:
    """It renders the `is_auto_generated` flag as a mark and gates no action on
    it — a suggested cluster is renamed, merged, split and deleted like any other."""
    assert "is_auto_generated" in ISLAND_SOURCE
    assert "Suggested" in ISLAND_SOURCE


def test_the_island_keys_cards_by_the_opaque_id_and_orders_by_nothing_it_derives() -> None:
    """Keyed by #11's `id` (the `public_id` from #73), and no ordering is built
    from an id — `position`, which the server sorted the cards into, is the only
    order on the board."""
    assert "key={card.id}" in ISLAND_SOURCE
    assert ".pk" not in ISLAND_SOURCE
    # No client-side sort recovers an ordering the server withheld.
    assert ".sort(" not in ISLAND_SOURCE
