"""A card's public handle: what it is, how it arrived, and what it replaces.

Every test here maps to an acceptance criterion of issue #73, which implements
`_docs/decisions.md` item 9: `Card.pk` does not leave the server, and a card is
addressed publicly by `Card.public_id` — a random UUID4 written when the card is
created.

Four themes run through the file.

**Absence is asserted, not presence.** The point of the column is not that a
UUID is in the payload; it is that a primary key is *not*. So the sweep below
takes a cycle whose cards have deliberately distinctive primary keys and asserts
that no such number appears anywhere in what a browser receives — at every
stage, from the state endpoint and from the retrospective page, in the parsed
payload and in the raw bytes.

**Cards and stages are discovered, not listed.** The sweep walks every card the
cycle holds and every stage the retrospective has, so a card the fixtures grow
later, or a stage the machine gains, is covered without anyone remembering to
extend a list. The bodies are searched whole, because a pk in a `data-`
attribute, in an element id, or inside embedded JSON leaks exactly as well as
one in a JSON field.

**A sweep that could pass vacuously proves nothing.** Each one asserts first
that it has cards to look for, that their pks are the distinctive ones, that the
bodies it searches really carry this board, and that the *same* search finds the
handle that is supposed to be there. Only then does it assert what is absent.

**Randomness is asserted as randomness.** A counter, a value derived from
`created_at`, and a time-ordered UUID (v1, v6, v7) all sort back into submission
order, which is the whole thing `cycles/reveal.py` shuffles to destroy. So the
tests check the UUID version, and check that twenty cards written in order do
not come back in that order when sorted by their handles.

Section E belongs to #12 rather than #73, and is here because this is where the
sweep lives. #12's seven mutation endpoints are a new surface: they accept a
card id and they answer with a body full of cards, so both halves of item 9 —
the pk is not accepted, and the pk does not leave — are asserted over every one
of them, in the same style and with the same distinctive primary keys.
"""

import json
import re
import uuid
from datetime import UTC, date, datetime

import pytest
from django.apps import apps as installed_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, migrations, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.converters import IntConverter

from board import serializers
from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import STAGE_ORDER, Cluster, Retrospective
from retro.services import advance_stage
from retro.views import board_bootstrap

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: Every stage the board can be asked about, so no sweep covers only the
#: convenient ones.
ALL_STAGES = list(STAGE_ORDER)

#: Where the sweep's card primary keys start. Eight digits, far above anything a
#: test database allocates on its own, so "this number is not in the body" is a
#: statement about the card's pk and cannot be satisfied or broken by a version
#: number, a retrospective id, a vote budget or a date that happens to collide.
DISTINCTIVE_PK = 90_210_001

#: The `json_script` block the retrospective page renders the bootstrap into.
BOOTSTRAP_BLOCK = re.compile(
    r'<script id="retro-bootstrap" type="application/json">(.*?)</script>', re.DOTALL
)


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


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
    """The other member, whose cards Ada may only see from REVEAL on."""
    user = make_user("bruno", "Bruno Author")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


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
    pk: int | None = None,
    category: str = Card.Category.START,
    anonymous: bool = False,
) -> Card:
    """One card, optionally with its primary key chosen rather than allocated.

    Choosing it is what makes the sweeps decisive. A pk the sequence handed out
    is a single digit in a fresh test database, and "1" is in every response
    body ever written, so a search for it could only ever fail. An eight-digit
    key is unmistakable: if it shows up in a body, it came from this card.
    """
    fields = {
        "cycle": cycle,
        "author": author,
        "text": text,
        "category": category,
        "is_anonymous": anonymous,
    }
    if pk is not None:
        fields["pk"] = pk
    return Card.objects.create(**fields)


@pytest.fixture
def board(cycle: FeedbackCycle, ada: User, bruno: User, owner: User) -> list[Card]:
    """Five cards from three members, with primary keys nobody could mistake.

    Ada writes two, so there is something in her payload at every stage,
    including the stages before REVEAL where she sees only her own. Two are
    anonymous, so the anonymous and the attributed case are both live.
    """
    return [
        make_card(cycle, ada, "we should write the runbook down 3f60", pk=DISTINCTIVE_PK),
        make_card(cycle, bruno, "standups run long and start late 9c04", pk=DISTINCTIVE_PK + 1),
        make_card(
            cycle,
            ada,
            "code review turnaround is too slow 7e15",
            pk=DISTINCTIVE_PK + 2,
            anonymous=True,
            category=Card.Category.CONTINUE,
        ),
        make_card(
            cycle,
            bruno,
            "the deploy checklist is out of date 5b21",
            pk=DISTINCTIVE_PK + 3,
            anonymous=True,
            category=Card.Category.STOP,
        ),
        make_card(
            cycle,
            owner,
            "pairing on the migration went well 1a88",
            pk=DISTINCTIVE_PK + 4,
            category=Card.Category.STOP,
        ),
    ]


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> Retrospective:
    """Walk the retrospective forward through the real stage machine.

    Never by assigning `stage`: the reveal's side effects happen on the way
    through the transition, and one of this issue's criteria is that they leave
    a card's public handle alone.
    """
    while retro.stage != stage:
        advance_stage(facilitator, retro)
    return retro


def get_state(client: Client, retro: Retrospective):
    return client.get(reverse("board-state", args=[retro.pk]))


def get_page(client: Client, retro: Retrospective):
    return client.get(reverse("retro-detail", args=[retro.pk]))


def bootstrap_of(body: str) -> dict:
    """The parsed contents of the `json_script` block on a rendered page."""
    block = BOOTSTRAP_BLOCK.search(body)
    assert block is not None, "no bootstrap block on the page"
    return json.loads(block.group(1))


def values_in(payload) -> list:
    """Every scalar at every depth of a parsed body, keys excluded."""
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


def column_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        description = connection.introspection.get_table_description(cursor, table)
    return {column.name for column in description}


def models_this_project_defines() -> list[type[models.Model]]:
    """Every model whose app lives in this repository, discovered rather than listed.

    Django's own tables and the task backend's are not this project's to have an
    opinion about; a model added to a new app of ours is covered without anyone
    extending a list.
    """
    root = str(settings.BASE_DIR)
    return [
        model
        for model in installed_apps.get_models()
        if str(model._meta.app_config.path).startswith(root)
        and ".venv" not in str(model._meta.app_config.path)
    ]


# --------------------------------------------------------------------------
# A. The column
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_card_has_a_public_id_and_it_is_a_random_uuid4(cycle: FeedbackCycle, ada: User) -> None:
    """Version 4 and nothing else: v1, v6 and v7 sort back into creation order."""
    card = make_card(cycle, ada, "Start writing the runbook")
    card.refresh_from_db()

    assert isinstance(card.public_id, uuid.UUID)
    assert uuid.UUID(str(card.public_id)).version == 4


@pytest.mark.django_db
def test_the_handle_exists_before_the_row_is_ever_written(cycle: FeedbackCycle, ada: User) -> None:
    """Assigned at creation, not at reveal and not by the database.

    A card needs a handle during the week it is being written, and one handed
    out at reveal would change a card's identity underneath the board.
    """
    unsaved = Card(cycle=cycle, author=ada, text="Not saved yet", category=Card.Category.START)

    assert isinstance(unsaved.public_id, uuid.UUID)
    assert uuid.UUID(str(unsaved.public_id)).version == 4

    unsaved.save()
    unsaved.refresh_from_db()
    assert unsaved.public_id == Card.objects.get(pk=unsaved.pk).public_id


def test_the_field_is_a_unique_non_null_uuid_nobody_can_edit() -> None:
    """The declaration itself, so the criterion holds of the column and not of one path."""
    field = Card._meta.get_field("public_id")

    assert isinstance(field, models.UUIDField)
    assert field.unique is True
    assert field.null is False
    assert field.editable is False
    # The callable, not a value: one value shared by every row is the bug the
    # migration below is shaped to avoid.
    assert field.default is uuid.uuid4


@pytest.mark.django_db
def test_twenty_cards_written_in_order_do_not_come_back_in_that_order(
    cycle: FeedbackCycle, ada: User
) -> None:
    """The leak, stated as a test: sorting the handles must recover nothing.

    Sorting `pk` recovers submission order exactly, which is why the ids stopped
    being served. This asserts the replacement does not do the same thing. It
    could fail by chance one run in 20!, which is about one in 2.4e18.
    """
    written = [make_card(cycle, ada, f"card number {index}") for index in range(20)]

    by_handle = list(Card.objects.filter(cycle=cycle).order_by("public_id"))

    assert [card.pk for card in written] == sorted(card.pk for card in written)
    assert [card.pk for card in by_handle] != [card.pk for card in written]


@pytest.mark.django_db
def test_the_database_refuses_two_cards_with_the_same_handle(
    cycle: FeedbackCycle, ada: User
) -> None:
    """Unique in the table, not merely unique in practice."""
    first = make_card(cycle, ada, "First card")

    with pytest.raises(IntegrityError), transaction.atomic():
        Card.objects.create(
            cycle=cycle,
            author=ada,
            text="Second card",
            category=Card.Category.START,
            public_id=first.public_id,
        )


@pytest.mark.django_db
def test_the_reveal_leaves_every_cards_handle_exactly_as_it_was(
    retro: Retrospective, owner: User, board: list[Card]
) -> None:
    """A card's identity does not change underneath the board when it is revealed.

    The reveal shuffles positions and destroys anonymous authorship. It does not
    touch this column, which is why #14 may key a component by it across the
    transition.
    """
    before = {card.pk: card.public_id for card in Card.objects.filter(cycle=retro.cycle)}

    advance_to(retro, owner, Stage.REVEAL)

    after = {card.pk: card.public_id for card in Card.objects.filter(cycle=retro.cycle)}
    assert after == before


def test_the_primary_key_is_still_an_integer_and_still_what_foreign_keys_point_at() -> None:
    """No table gains a UUID primary key, and nothing uses `to_field`.

    Discovered over every model this project defines rather than asserted about
    `Card`, so a later model that reached for a UUID primary key — or a foreign
    key that pointed at this column instead of at the row — fails here.
    """
    assert Card._meta.pk.name == "id"
    assert isinstance(Card._meta.pk, models.BigAutoField)

    for model in models_this_project_defines():
        assert not isinstance(model._meta.pk, models.UUIDField), model
        for field in model._meta.get_fields():
            if not isinstance(field, models.ForeignKey):
                continue
            assert field.target_field.name == field.related_model._meta.pk.name, field
            # `to_fields` is `[None]` when the foreign key names no column and
            # the pk's name once Django has resolved it. Anything else is a
            # `to_field=`, which is how a relation would come to point at the
            # public handle instead of at the row.
            assert set(field.to_fields) <= {None, field.related_model._meta.pk.name}, field


def test_no_model_but_card_gained_a_public_identifier() -> None:
    """Item 9 is about `Card`. Everything else keeps its integer pk.

    `Project.join_token` is not one of these: it is the secret in an invitation
    link, not a public handle, and it predates this issue.
    """
    with_a_public_id = [
        model.__name__
        for model in models_this_project_defines()
        if any(field.name == "public_id" for field in model._meta.get_fields())
    ]

    assert with_a_public_id == ["Card"]

    for model in (Project, FeedbackCycle, Retrospective):
        assert isinstance(model._meta.pk, models.AutoField | models.BigAutoField), model


# --------------------------------------------------------------------------
# B. The migration
# --------------------------------------------------------------------------

#: The app the column lands in, and the table it lands on.
APP = "cycles"
TABLE = Card._meta.db_table


def migration_that_adds_the_column() -> tuple[str, migrations.Migration]:
    """The one migration in `cycles` that introduces `public_id`, found by reading them.

    Located rather than named, so the tests below describe the shape of the
    change and not the filename someone happened to give it.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    found = [
        (name, migration)
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == APP
        for operation in migration.operations
        if isinstance(operation, migrations.AddField)
        and operation.model_name.lower() == "card"
        and operation.name == "public_id"
    ]

    assert len(found) == 1, found
    return found[0]


def latest_migrations() -> list[tuple[str, str]]:
    """Every app's newest migration — the state the database has to end up in.

    Read off the graph, so a migration added after this test was written is
    restored too. Naming one migration here would silently stop restoring the
    ones that come after it, and the failure would land in whichever test ran
    next rather than in this one.
    """
    return MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes()


def test_the_column_arrives_in_three_operations_and_not_in_one() -> None:
    """Add it nullable, fill it in per row, then tighten it. In that order.

    A single `AddField` carrying `default=uuid.uuid4` evaluates the callable
    once and writes one UUID into every existing row, which then fails the
    unique index it was supposed to satisfy. The shape of the migration is the
    criterion, so it is read off the file rather than inferred from the fact
    that the suite's database happens to be empty.
    """
    _name, migration = migration_that_adds_the_column()

    about_public_id = [
        operation
        for operation in migration.operations
        if isinstance(operation, migrations.RunPython)
        or (
            isinstance(operation, migrations.AddField | migrations.AlterField)
            and operation.model_name.lower() == "card"
            and operation.name == "public_id"
        )
    ]
    added, backfilled, tightened = about_public_id

    assert len(about_public_id) == 3
    assert isinstance(added, migrations.AddField)
    assert added.field.null is True
    assert added.field.unique is False

    assert isinstance(backfilled, migrations.RunPython)

    assert isinstance(tightened, migrations.AlterField)
    assert tightened.field.null is False
    assert tightened.field.unique is True
    assert tightened.field.default is uuid.uuid4


@pytest.mark.django_db
def test_two_cards_that_predate_this_issue_have_handles_of_their_own(
    board: list[Card],
) -> None:
    """The criterion's own check: a fixture's cards do not share one value.

    Cheap, and it is the assertion that would have failed loudly on the
    one-`AddField` migration this issue exists to avoid.
    """
    handles = [card.public_id for card in Card.objects.all()]

    assert len(handles) == len(board)
    assert len(set(handles)) == len(handles)
    assert all(handle is not None for handle in handles)


@pytest.mark.django_db(transaction=True)
def test_the_migration_gives_every_row_of_a_populated_table_its_own_value(
    cycle: FeedbackCycle, ada: User
) -> None:
    """The three steps, run for real against a table that already holds cards.

    The column is dropped by unapplying the migration — the rows stay — and then
    the migration is applied again with the table full. That is the situation
    the deployment hits, and the only way to prove the backfill writes a value
    per row rather than one value for the table.

    It ends where it started, whatever happens in between: the `finally` puts
    the database back at the latest migration so no later test in the session
    meets a half-migrated schema. "The latest" is asked of the graph rather than
    written down as the migration under test — #12 added `cycles.0005`, which
    unapplying this one takes with it, and a restore that stopped here would
    leave every later test looking at a table with no `cluster_id` column.
    """
    name, migration = migration_that_adds_the_column()
    (before,) = [dependency for dependency in migration.dependencies if dependency[0] == APP]
    after = (APP, name)

    for index in range(12):
        make_card(cycle, ada, f"written before the migration {index}")
    existing = set(Card.objects.values_list("pk", flat=True))
    assert len(existing) == 12

    try:
        MigrationExecutor(connection).migrate([before])

        # The situation the migration has to survive: rows, and no column.
        assert "public_id" not in column_names(TABLE)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {TABLE}")
            assert cursor.fetchone()[0] == 12

        MigrationExecutor(connection).migrate([after])

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id, public_id FROM {TABLE}")
            rows = cursor.fetchall()
    finally:
        MigrationExecutor(connection).migrate(latest_migrations())

    handles = [handle for _pk, handle in rows]
    assert {pk for pk, _handle in rows} == existing
    assert all(handle is not None for handle in handles)
    assert len(set(handles)) == len(handles) == 12
    assert {uuid.UUID(str(handle)).version for handle in handles} == {4}

    # And the column the migration leaves behind is the one the model declares.
    assert "public_id" in column_names(TABLE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Card.objects.create(
            cycle=cycle,
            author=ada,
            text="A duplicate handle",
            category=Card.Category.START,
            public_id=handles[0],
        )


# --------------------------------------------------------------------------
# C. What reaches the browser
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_every_card_id_the_browser_receives_is_a_uuid_and_none_is_an_integer(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: list[Card],
    stage: str,
) -> None:
    """Both surfaces, every stage: the state endpoint and the page's bootstrap."""
    advance_to(retro, owner, stage)
    log_in(client, ada)

    payloads = {
        "the state endpoint": get_state(client, retro).json(),
        "the bootstrap": bootstrap_of(get_page(client, retro).content.decode()),
    }
    handles = {str(card.public_id) for card in Card.objects.filter(cycle=retro.cycle)}

    for surface, payload in payloads.items():
        assert payload["cards"], f"{surface} carried no cards at {stage}"
        for card in payload["cards"]:
            assert isinstance(card["id"], str), (surface, card["id"])
            assert uuid.UUID(card["id"]).version == 4, (surface, card["id"])
            assert card["id"] in handles, (surface, card["id"])
            with pytest.raises(ValueError):
                int(card["id"])


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_bootstrap_and_the_first_poll_call_a_card_by_the_same_name(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: list[Card],
    stage: str,
) -> None:
    """A card's identity does not change when the first poll replaces the bootstrap.

    Compared against the viewer's own cards, which are in both bodies at every
    stage — before REVEAL they are all the state endpoint sends, and the
    bootstrap never carries anyone else's.
    """
    advance_to(retro, owner, stage)
    log_in(client, ada)

    bootstrap = bootstrap_of(get_page(client, retro).content.decode())
    state = get_state(client, retro).json()

    mine = {str(card.public_id) for card in Card.objects.filter(cycle=retro.cycle, author=ada)}
    assert mine, "the viewer must own a card for this comparison to mean anything"
    assert {card["id"] for card in bootstrap["cards"]} == mine
    assert mine <= {card["id"] for card in state["cards"]}


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_no_cards_primary_key_appears_in_anything_a_browser_receives(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: list[Card],
    stage: str,
) -> None:
    """The sweep. Every card, every stage, both surfaces, the whole body.

    In the style of #11's `created_at` sweep, and for the same reason: the two
    facts that hand back submission order are `Card.created_at` and `Card.pk`,
    and neither is caught by looking only at the fields a UI would draw. A pk in
    an element id, in a `data-` attribute, in a URL or inside embedded JSON is
    the same leak as one in `cards[].id`, so this searches the raw bytes as well
    as every scalar of the parsed payloads.

    The cards are discovered from the cycle and the stages from the stage
    machine — nothing here is a list that a later card or a later stage could
    fall off. Four guards run before the assertions, so the test cannot pass by
    searching an empty body or by looking for a number that could never appear.
    """
    advance_to(retro, owner, stage)
    log_in(client, ada)

    state = get_state(client, retro)
    page = get_page(client, retro)
    bodies = {
        "the state endpoint": state.content.decode(),
        "the retrospective page": page.content.decode(),
    }
    payloads = {
        "the state endpoint": state.json(),
        "the bootstrap": bootstrap_of(bodies["the retrospective page"]),
    }

    cards = list(Card.objects.filter(cycle=retro.cycle))
    served = {card["id"] for payload in payloads.values() for card in payload["cards"]}

    # Guard 1: there are cards to look for.
    assert cards
    # Guard 2: their pks are the distinctive ones, so a hit is unambiguous and a
    # miss is not luck — no eight-digit number belongs in either body.
    assert all(card.pk >= DISTINCTIVE_PK for card in cards), [card.pk for card in cards]
    # Guard 3: the bodies really carry this board, at this stage.
    assert served
    # Guard 4: the same substring search *does* find an identifier that is
    # present, so "not in body" below is a fact about the pk and not about the
    # search being broken.
    for handle in {card["id"] for card in payloads["the state endpoint"]["cards"]}:
        assert handle in bodies["the state endpoint"]
    for handle in {card["id"] for card in payloads["the bootstrap"]["cards"]}:
        assert handle in bodies["the retrospective page"]

    for card in cards:
        for surface, body in bodies.items():
            assert str(card.pk) not in body, f"card {card.pk} is in {surface} at {stage}"
        for surface, payload in payloads.items():
            for value in values_in(payload):
                assert value != card.pk, f"card {card.pk} is a value in {surface} at {stage}"
                assert value != str(card.pk), f"card {card.pk} is a value in {surface} at {stage}"


@pytest.mark.django_db
def test_the_sweep_would_notice_a_primary_key_if_one_were_served(
    client: Client, retro: Retrospective, ada: User, board: list[Card]
) -> None:
    """The sweep's own control: the search finds a pk that really is in a body.

    Without this, "no pk appears" is indistinguishable from a search that could
    never find anything. The pk is put into a body here by asking for a URL that
    contains it — an address, not the board — and the same expression the sweep
    uses is asserted to find it.
    """
    log_in(client, ada)
    card = board[0]

    body = client.get(reverse("card-show", args=[card.pk])).content.decode()

    assert str(card.pk) in body


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_retrospective_and_the_cycle_keep_their_integer_ids(
    client: Client,
    retro: Retrospective,
    ada: User,
    owner: User,
    board: list[Card],
    stage: str,
) -> None:
    """Scope: item 9 is about `Card`, and changes nothing else in the payload."""
    advance_to(retro, owner, stage)
    log_in(client, ada)

    state = get_state(client, retro).json()
    bootstrap = bootstrap_of(get_page(client, retro).content.decode())

    assert state["id"] == retro.pk
    assert bootstrap["id"] == retro.pk
    assert isinstance(state["id"], int)
    assert isinstance(bootstrap["id"], int)


@pytest.mark.django_db
def test_the_payload_gains_no_key_and_loses_none(
    client: Client, retro: Retrospective, ada: User, board: list[Card]
) -> None:
    """The type of `cards[].id` changed here; `cards[].mine` was added by #75.

    Still an exact key set, not a subset: a stray key on a card fails this as
    loudly as a missing one. The `mine` boolean is the viewer's own-card mark
    (`_docs/decisions.md` item 10); the bootstrap deliberately does not carry it,
    since the first poll from the state endpoint replaces the bootstrap with a
    body that does. #14 added `urls` to the bootstrap — the endpoints the island
    reads and writes through, each addressing the retrospective by its integer
    pk and never a card by any handle — and nothing else.
    """
    log_in(client, ada)

    state = get_state(client, retro).json()
    bootstrap = bootstrap_of(get_page(client, retro).content.decode())

    assert set(state) == {"id", "stage", "version", "changed", "cards", "clusters", "votes"}
    state_card_keys = {"id", "category", "text", "cluster", "mine"}
    assert all(set(card) == state_card_keys for card in state["cards"])
    assert set(bootstrap) == {"id", "stage", "version", "cards", "urls"}
    assert all(set(card) == {"id", "category", "text"} for card in bootstrap["cards"])


@pytest.mark.django_db
def test_the_serializer_documents_the_handle_rather_than_excusing_the_primary_key() -> None:
    """The module docstring is where #12 and #14 read the shape from."""
    documentation = serializers.__doc__

    assert "public_id" in documentation
    assert "deliberate exception is `Card.id`" not in documentation
    assert "Card.pk" in documentation  # it is named, as the thing that stays inside


# --------------------------------------------------------------------------
# D. What keeps the integer pk, on purpose
# --------------------------------------------------------------------------

#: The three own-card URLs `_docs/decisions.md` item 9 exempts by name.
OWN_CARD_URLS = ["card-show", "card-edit", "card-delete"]


@pytest.mark.parametrize("name", OWN_CARD_URLS)
def test_the_own_card_urls_still_take_an_integer_primary_key(name: str) -> None:
    """A decided exception, not deferred work: item 9 says there is no follow-up.

    Every card these address is one the viewer wrote, on a screen that shows
    nobody else's, so the only ordering they expose is the viewer's own — which
    they already know.
    """
    _possibilities, pattern, _defaults, converters = get_resolver().reverse_dict[name]

    # The compiled pattern, so this is what the resolver will actually match
    # and not a re-reading of the URLconf's source.
    assert "(?P<pk>[0-9]+)" in str(pattern)
    assert isinstance(converters["pk"], IntConverter)


@pytest.mark.django_db
def test_an_own_card_page_answers_on_the_integer_pk_and_not_on_the_handle(
    client: Client, ada: User, board: list[Card]
) -> None:
    """Driven, not read: the exempted URL still works, and the handle is not a pk."""
    log_in(client, ada)
    card = board[0]

    assert client.get(reverse("card-show", args=[card.pk])).status_code == 200

    with pytest.raises(NoReverseMatch):
        reverse("card-show", args=[card.public_id])


@pytest.mark.django_db
def test_the_bootstrap_function_itself_returns_no_primary_key(
    retro: Retrospective, ada: User, board: list[Card]
) -> None:
    """The function, called directly: no pk in what it returns, at any depth."""
    payload = board_bootstrap(ada, retro)

    for value in values_in(payload):
        for card in Card.objects.filter(cycle=retro.cycle):
            assert value != card.pk
            assert value != str(card.pk)


# --------------------------------------------------------------------------
# E. The mutation endpoints (#12), which are a new surface
# --------------------------------------------------------------------------

#: The card in the cluster, the ungrouped one, and the one a move puts back.
HELD, LOOSE = 0, 1


@pytest.fixture
def clusters(retro: Retrospective, owner: User, board: list[Card]) -> list[Cluster]:
    """The same board, in CLUSTER, with two clusters and one card in the first.

    Two, because a merge needs a source and a target and merging a cluster into
    itself is refused — the sweeps below run against successful requests, so
    every body has to be one the endpoint accepts.
    """
    advance_to(retro, owner, Stage.CLUSTER)
    made = [
        Cluster.objects.create(retrospective=retro, name=name, position=position)
        for position, name in enumerate(["Deploys", "Reviews"], start=1)
    ]
    Card.objects.filter(pk=board[HELD].pk).update(cluster=made[0])
    return made


def mutation_bodies(board: list[Card], clusters: list[Cluster]) -> dict[str, dict]:
    """A valid body for each of #12's seven endpoints, keyed by URL name."""
    first, second = clusters
    return {
        "board-card-move": {"card": str(board[LOOSE].public_id), "cluster": first.pk},
        "board-card-ungroup": {"card": str(board[HELD].public_id)},
        "board-cluster-create": {"name": "Onboarding"},
        "board-cluster-rename": {"cluster": first.pk, "name": "Deployment pain"},
        "board-cluster-merge": {"source": second.pk, "target": first.pk},
        "board-cluster-split": {"cluster": first.pk, "cards": [str(board[HELD].public_id)]},
        "board-cluster-delete": {"cluster": first.pk},
    }


#: Every endpoint #12 adds, so no sweep here covers only the convenient ones.
MUTATION_URLS = [
    "board-card-move",
    "board-card-ungroup",
    "board-cluster-create",
    "board-cluster-rename",
    "board-cluster-merge",
    "board-cluster-split",
    "board-cluster-delete",
]

#: The endpoints that name a card: the field it is named in, and which card of
#: the fixture that body names.
CARD_FIELDS = {
    "board-card-move": ("card", LOOSE),
    "board-card-ungroup": ("card", HELD),
    "board-cluster-split": ("cards", HELD),
}


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", list(CARD_FIELDS))
def test_a_mutation_endpoint_refuses_a_bare_primary_key_and_acts_on_nothing(
    client: Client,
    retro: Retrospective,
    ada: User,
    board: list[Card],
    clusters: list[Cluster],
    url_name: str,
) -> None:
    """Item 9's request half, at every endpoint that takes a card.

    404 — the same answer as any id that does not resolve — and never a fallback
    to a primary-key lookup, which the unchanged grouping is what proves. The
    guards assert first that the number really is this card's primary key, so
    the test cannot pass by posting something that could never have resolved.
    """
    log_in(client, ada)
    field, index = CARD_FIELDS[url_name]
    card = board[index]
    before = sorted(Card.objects.values_list("pk", "cluster_id"))

    assert card.pk >= DISTINCTIVE_PK
    assert Card.objects.filter(pk=card.pk, cycle=retro.cycle).exists()

    posted = str(card.pk)
    body = mutation_bodies(board, clusters)[url_name] | {
        field: [posted] if field == "cards" else posted
    }
    response = client.post(reverse(url_name, args=[retro.pk]), body)

    assert response.status_code == 404
    assert sorted(Card.objects.values_list("pk", "cluster_id")) == before


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", list(CARD_FIELDS))
def test_the_same_mutation_succeeds_when_the_card_is_named_by_its_handle(
    client: Client,
    retro: Retrospective,
    ada: User,
    board: list[Card],
    clusters: list[Cluster],
    url_name: str,
) -> None:
    """The control: what the refusal above is a refusal *of*."""
    log_in(client, ada)

    response = client.post(
        reverse(url_name, args=[retro.pk]), mutation_bodies(board, clusters)[url_name]
    )

    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("url_name", MUTATION_URLS)
def test_no_cards_primary_key_appears_in_what_a_mutation_answers_with(
    client: Client,
    retro: Retrospective,
    ada: User,
    board: list[Card],
    clusters: list[Cluster],
    url_name: str,
) -> None:
    """The sweep, over the seven new bodies, with the same guards as the others.

    A mutation answers with the whole board, so it is exactly as capable of
    leaking a primary key as the state endpoint is. It is swept rather than
    assumed to be covered by the fact that it calls the same serializer, because
    "it calls the same function" is the kind of thing that is true until it is
    not.
    """
    log_in(client, ada)

    response = client.post(
        reverse(url_name, args=[retro.pk]), mutation_bodies(board, clusters)[url_name]
    )
    raw = response.content.decode()
    payload = response.json()
    cards = list(Card.objects.filter(cycle=retro.cycle))

    # Guard 1: the request succeeded, so there is a real board in the body.
    assert response.status_code == 200, raw
    # Guard 2: it carries cards.
    assert payload["cards"]
    # Guard 3: their pks are the distinctive ones, so a hit is unambiguous.
    assert all(card.pk >= DISTINCTIVE_PK for card in cards)
    # Guard 4: the same substring search finds a handle that is present, so
    # "not in body" below is a fact about the pk and not about a broken search.
    for card in payload["cards"]:
        assert card["id"] in raw
        assert uuid.UUID(card["id"]).version == 4

    for card in cards:
        assert str(card.pk) not in raw, f"card {card.pk} is in the {url_name} body"
        for value in values_in(payload):
            assert value != card.pk, f"card {card.pk} is a value in the {url_name} body"
            assert value != str(card.pk), f"card {card.pk} is a value in the {url_name} body"
