"""Anonymity at reveal: the promise, and the proof that it holds.

Every test here maps to an acceptance criterion of issue #10, and the file is
meant to be readable on its own. Someone who never opens `cycles/reveal.py`
should be able to finish this file convinced of four things:

1. after a reveal, the database contains no way to say who wrote an anonymous
   card — not in `cycles_card`, not in another table, not in the admin, not by
   arithmetic on a timestamp;
2. the order the cards come back in says nothing about who wrote them or when;
3. participation survives the destruction, because it is computed before it and
   in the same transaction;
4. the destruction happens exactly once, all at once, or not at all.

Three habits run through the tests, inherited from #8 and #9 and tightened
here because this is the one issue whose defects cannot be fixed afterwards.

The first is that absence is asserted, not assumed. A test that only checks
what *is* on a page is how a rendering defect stayed invisible through a
browser pass once already. So the sweep below walks every URL a member can
reach, at every stage, and asserts the author's username, display name and real
name are in none of them — in the whole response body, not the visible text,
because a name in a `title`, a `data-` attribute, an element id or a
`json_script` block leaks exactly as well as a name in a paragraph.

The second is that a refusal is proved by attempting the thing. Where a member
must not be able to reach a card, the test drives the endpoint as that member,
with a valid CSRF token, and asserts the server refused.

The third is that the guarantee is asserted at the level it is made at. "The
link is gone" is checked with raw SQL against `cycles_card` and against the
schema's foreign keys, not by asking the ORM whether `card.author` is None.
"""

import ast
import json
import random
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.forms.models import model_to_dict
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cycles.models import Card, CycleParticipation, FeedbackCycle, revealed_cards
from cycles.reveal import reveal_cycle
from projects.models import Membership, Project
from projects.permissions import can_delete_card, can_edit_card, can_view_card
from retro import services
from retro.models import STAGE_ORDER, Retrospective, is_legal_transition
from retro.services import ConcurrentAdvance, advance_stage

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: Long and unmistakable, so "this string is not in the response body" cannot
#: pass or fail by accident on a substring of some unrelated markup.
ADA_USERNAME = "ada-author-9f3c"
ADA_DISPLAY_NAME = "Ada Author 9f3c"
ADA_FIRST_NAME = "Adalind9f3c"
ADA_LAST_NAME = "Authorsdottir9f3c"

#: Everything that names Ada. The sweep asserts every one of them is absent.
ADA_IDENTIFIERS = (ADA_USERNAME, ADA_DISPLAY_NAME, ADA_FIRST_NAME, ADA_LAST_NAME)

#: The text of the card Ada wrote anonymously. Distinctive for the same reason.
SECRET_TEXT = "the deploy checklist is out of date and nobody owns it 5b21"


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
    """The project's owner and this cycle's facilitator. Never an anonymous author."""
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def ada(project: Project) -> User:
    """The member whose anonymity is the subject of this file."""
    user = make_user(
        ADA_USERNAME,
        ADA_DISPLAY_NAME,
        first_name=ADA_FIRST_NAME,
        last_name=ADA_LAST_NAME,
    )
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def bruno(project: Project) -> User:
    """A member who writes attributed cards, so "untouched" has something to hold."""
    user = make_user("bruno", "Bruno Bystander")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def cleo(project: Project) -> User:
    """A member who submits nothing. "Did not submit" has to be representable."""
    user = make_user("cleo", "Cleo Quiet")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


@pytest.fixture
def root(db) -> User:
    """A superuser on no project. Decision 3 has no admin exception."""
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
    """One card, optionally with `created_at` pinned to a known moment.

    `created_at` is `auto_now_add`, so it is written afterwards with an UPDATE.
    The tests that care about submission order need it to be a fact rather than
    whatever the clock said during the test.
    """
    card = Card.objects.create(
        cycle=cycle, author=author, text=text, category=category, is_anonymous=anonymous
    )
    if written_at is not None:
        Card.objects.filter(pk=card.pk).update(created_at=written_at)
        card.refresh_from_db()
    return card


@pytest.fixture
def anonymous_card(cycle: FeedbackCycle, ada: User) -> Card:
    return make_card(
        cycle,
        ada,
        SECRET_TEXT,
        anonymous=True,
        written_at=datetime(2026, 7, 21, 9, 14, tzinfo=UTC),
    )


@pytest.fixture
def attributed_card(cycle: FeedbackCycle, bruno: User) -> Card:
    return make_card(
        cycle,
        bruno,
        "we should keep the Friday demo",
        category=Card.Category.CONTINUE,
        written_at=datetime(2026, 7, 21, 11, 30, tzinfo=UTC),
    )


def reveal(retro: Retrospective, facilitator: User) -> Retrospective:
    """Advance DRAFT -> REVEAL, which is the only way the reveal ever happens."""
    return advance_stage(facilitator, retro)


def raw_author_ids(cycle: FeedbackCycle, *, anonymous: bool) -> list[int | None]:
    """`author_id` straight out of `cycles_card`, with no ORM in the way."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT author_id FROM cycles_card WHERE cycle_id = %s AND is_anonymous = %s",
            [cycle.pk, anonymous],
        )
        return [row[0] for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# Destroying authorship
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_entering_reveal_destroys_the_author_of_every_anonymous_card(
    retro: Retrospective, owner: User, ada: User, cycle: FeedbackCycle
) -> None:
    first = make_card(cycle, ada, "one", anonymous=True)
    second = make_card(cycle, ada, "two", anonymous=True, category=Card.Category.STOP)

    reveal(retro, owner)

    assert Card.objects.get(pk=first.pk).author_id is None
    assert Card.objects.get(pk=second.pk).author_id is None


@pytest.mark.django_db
def test_the_link_is_gone_at_the_database_level_and_not_only_in_the_orm(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, anonymous_card: Card
) -> None:
    """Raw SQL, because "the ORM returns None" is a statement about the ORM.

    The column itself has to be NULL. A default manager that filtered the
    author out, or a property that hid it, would pass an ORM assertion and
    leave the answer sitting in the table for anyone with a psql prompt.
    """
    assert raw_author_ids(cycle, anonymous=True) == [anonymous_card.author_id]

    reveal(retro, owner)

    assert raw_author_ids(cycle, anonymous=True) == [None]


@pytest.mark.django_db
def test_is_anonymous_stays_true_so_the_card_can_still_be_shown_as_anonymous(
    retro: Retrospective, owner: User, anonymous_card: Card
) -> None:
    reveal(retro, owner)

    stored = Card.objects.get(pk=anonymous_card.pk)
    assert stored.is_anonymous is True
    assert stored.author_id is None


@pytest.mark.django_db
def test_attributed_cards_keep_their_author(
    retro: Retrospective, owner: User, bruno: User, cycle: FeedbackCycle, attributed_card: Card
) -> None:
    reveal(retro, owner)

    assert Card.objects.get(pk=attributed_card.pk).author_id == bruno.pk
    assert raw_author_ids(cycle, anonymous=False) == [bruno.pk]


@pytest.mark.django_db
def test_a_card_whose_author_was_already_deleted_is_revealed_without_error(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    """`author IS NULL` means "no author to reveal", never an error.

    `Card.author` is SET_NULL, so removing a person leaves their cards behind
    with no author. The reveal has nothing to destroy and nothing to count, and
    says so by leaving the row alone rather than by raising.
    """
    orphan = make_card(cycle, ada, "written by someone since removed", anonymous=True)
    Card.objects.filter(pk=orphan.pk).update(author=None)

    reveal(retro, owner)

    stored = Card.objects.get(pk=orphan.pk)
    assert stored.author_id is None
    assert stored.position >= 1
    assert not CycleParticipation.objects.filter(cycle=cycle, card_count__gt=0).exists()


@pytest.mark.django_db
def test_no_table_in_the_schema_can_name_both_a_card_and_a_user(
    retro: Retrospective, owner: User, anonymous_card: Card
) -> None:
    """The structural half of "no row anywhere references both".

    `cycles_card` is the one table where a card and a user may be named
    together, and the test below proves that column is NULL for anonymous rows.
    Every other table in the schema is checked here for the pair of foreign
    keys that would rebuild the link one table to the left — an archive, an
    audit row, a history table, a clustering table that carried the author
    along for convenience.
    """
    reveal(retro, owner)

    card_table = Card._meta.db_table
    user_table = User._meta.db_table

    with connection.cursor() as cursor:
        offenders = []
        for table in connection.introspection.table_names(cursor):
            if table == card_table:
                continue
            referenced = {
                other_table
                for _other_column, other_table in connection.introspection.get_relations(
                    cursor, table
                ).values()
            }
            if card_table in referenced and user_table in referenced:
                offenders.append(table)

    assert offenders == []


@pytest.mark.django_db
def test_the_card_table_carries_no_second_place_to_keep_a_former_author(
    retro: Retrospective, owner: User, anonymous_card: Card
) -> None:
    """No `deleted_author_id`, no `previous_author`, no soft-delete flag.

    The absence of those columns is the feature — `_docs/decisions.md` item 3 —
    so the column list is asserted whole rather than by searching it for names
    somebody might not have used.
    """
    reveal(retro, owner)

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, "cycles_card")
        }

    assert columns == {
        "id",
        "cycle_id",
        "category",
        "text",
        "author_id",
        "is_anonymous",
        "position",
        "created_at",
    }


@pytest.mark.django_db
def test_no_admin_view_retains_the_mapping(retro: Retrospective, owner: User) -> None:
    """Being staff reveals nothing. Neither model is registered at all.

    A `ModelAdmin` for `Card` would show `author` on a changelist and let a
    superuser page through the cycle, and one for `CycleParticipation` would
    show a count beside a name. Registering either is what this asserts against
    — not a `readonly_fields` setting on one, which the next person to open the
    file could relax.
    """
    registered = set(admin.site._registry)

    assert Card not in registered
    assert CycleParticipation not in registered

    for model in registered:
        forward_relations = [
            field
            for field in model._meta.concrete_fields
            if field.is_relation and field.related_model in {Card, CycleParticipation}
        ]
        assert forward_relations == [], model


@pytest.mark.django_db
def test_nothing_a_revealed_card_carries_names_its_author(
    retro: Retrospective, owner: User, anonymous_card: Card
) -> None:
    """Not the object, not its text form, not its dict, not its JSON.

    #11 has not been written, so this is the closest a test can get to "any
    payload": everything a serializer could reach for on a revealed card is
    checked for Ada's names.
    """
    reveal(retro, owner)

    card = revealed_cards(anonymous_card.cycle).get(pk=anonymous_card.pk)
    payloads = [
        str(card),
        repr(card),
        json.dumps(model_to_dict(card), default=str),
        json.dumps({"id": card.pk, "text": card.text, "position": card.position}),
    ]

    for payload in payloads:
        for identifier in ADA_IDENTIFIERS:
            assert identifier not in payload, payload
    assert card.author_id is None
    assert card.author is None


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_positions_are_zero_until_the_cycle_is_revealed(
    cycle: FeedbackCycle, ada: User, bruno: User
) -> None:
    """0 means "not revealed", which is why the reveal hands out 1..n."""
    make_card(cycle, ada, "one", anonymous=True)
    make_card(cycle, bruno, "two")

    assert list(cycle.cards.values_list("position", flat=True)) == [0, 0]


@pytest.mark.django_db
def test_every_card_is_repositioned_not_only_the_anonymous_ones(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, bruno: User
) -> None:
    """Shuffling the anonymous cards inside a list ordered by time still leaks.

    An attributed card carries a name. If it stays where submission time put it
    and only the anonymous ones move, the anonymous cards are still located
    relative to named neighbours, and the ones written between two of Bruno's
    are still known to have been written between two of Bruno's.
    """
    for index in range(6):
        make_card(cycle, ada, f"anonymous {index}", anonymous=True)
        make_card(cycle, bruno, f"attributed {index}", category=Card.Category.STOP)

    reveal(retro, owner)

    assert not cycle.cards.filter(position=0).exists()
    assert cycle.cards.filter(is_anonymous=False, position=0).count() == 0


@pytest.mark.django_db
def test_positions_are_unique_and_contiguous_across_the_whole_cycle(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, bruno: User
) -> None:
    for index in range(9):
        author = ada if index % 2 else bruno
        make_card(cycle, author, f"card {index}", anonymous=bool(index % 2))

    reveal(retro, owner)

    positions = sorted(cycle.cards.values_list("position", flat=True))
    assert positions == list(range(1, 10))


@pytest.mark.django_db
def test_the_revealed_order_is_not_the_order_the_cards_were_written_in(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    """Twelve cards written in a known order, revealed, and compared.

    Twelve because a shuffle may legitimately return the order it was given:
    with twelve cards that happens once in 479,001,600 runs, which is a smaller
    risk than the defect this test exists to catch.
    """
    written = [
        make_card(
            cycle,
            ada,
            f"card {index:02d}",
            anonymous=True,
            written_at=datetime(2026, 7, 21, 9, index, tzinfo=UTC),
        )
        for index in range(12)
    ]
    submission_order = [card.pk for card in written]
    assert list(cycle.cards.values_list("pk", flat=True)) == submission_order

    reveal(retro, owner)

    assert list(revealed_cards(cycle).values_list("pk", flat=True)) != submission_order


@pytest.mark.django_db
def test_the_shuffle_cannot_be_reproduced_by_seeding_the_random_module(
    project: Project, owner: User, ada: User
) -> None:
    """`random.SystemRandom`, so `random.seed()` is not a way back to the order.

    Two identical cycles are revealed after the same seed. A Mersenne Twister
    seeded twice the same way deals the same order both times, which would make
    the shuffle a permutation anyone who knows the seed can undo.
    """
    orders = []
    for week in (date(2026, 7, 20), date(2026, 7, 27)):
        cycle = FeedbackCycle.objects.create(
            project=project,
            week_start=week,
            opens_at=OPENS_AT,
            closes_at=CLOSES_AT,
            facilitator=owner,
        )
        for index in range(12):
            make_card(cycle, ada, f"card {index:02d}", anonymous=True)
        retro = Retrospective.objects.create(cycle=cycle)

        random.seed(20260722)
        reveal(retro, owner)

        orders.append([card.text for card in revealed_cards(cycle)])

    assert orders[0] != orders[1]


@pytest.mark.django_db
def test_a_revealed_list_is_ordered_by_position_and_by_nothing_else(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    """The ordering criterion, asserted on the SQL that reaches the database.

    `Card.Meta.ordering` is by creation, which is right for the one screen that
    shows a member their own cards and wrong for every list of revealed ones.
    `revealed_cards()` overrides it, and this asserts the override reaches the
    query rather than sitting in a docstring.
    """
    for index in range(4):
        make_card(cycle, ada, f"card {index}", anonymous=True)
    reveal(retro, owner)

    queryset = revealed_cards(cycle)
    assert queryset.query.order_by == ("position",)

    order_by_clause = str(queryset.query).lower().split(" order by ")[-1]
    assert "position" in order_by_clause
    assert "created_at" not in order_by_clause
    assert ".id" not in order_by_clause and '"id"' not in order_by_clause

    assert [card.position for card in queryset] == [1, 2, 3, 4]


def order_by_literals(source: str) -> list[str]:
    """Every string literal handed to an `.order_by(...)` call in `source`."""
    literals = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "order_by"
        ):
            literals += [
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
    return literals


def test_no_module_that_knows_about_cards_orders_a_query_by_created_at_or_id() -> None:
    """The source-level half: submission order is never asked for.

    A revealed list ordered by `created_at` or `id` is submission order under
    another name, and the shuffle above would have been for nothing. The check
    runs over every module in the application that mentions `Card`, so a view
    or a serializer added later that reaches for the wrong ordering fails here.
    """
    forbidden = {"created_at", "-created_at", "id", "-id", "pk", "-pk"}
    checked = []

    for path in sorted(BASE_DIR.glob("*/*.py")):
        if ".venv" in path.parts or "migrations" in path.parts or path.parts[-2] == "tests":
            continue
        source = path.read_text()
        if "Card" not in source:
            continue
        checked.append(path.name)
        assert forbidden.isdisjoint(order_by_literals(source)), path

    assert "reveal.py" in checked
    assert "views.py" in checked


# --------------------------------------------------------------------------
# Participation
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_row_is_written_for_every_member_including_the_ones_who_submitted_nothing(
    retro: Retrospective,
    owner: User,
    cycle: FeedbackCycle,
    ada: User,
    bruno: User,
    cleo: User,
    anonymous_card: Card,
    attributed_card: Card,
) -> None:
    """ "Did not submit" is a row that says so, not a row that is missing.

    A missing row is indistinguishable from a bug, and #25 and #26 have to show
    who did not submit as confidently as who did.
    """
    reveal(retro, owner)

    rows = {row.user_id: row for row in CycleParticipation.objects.filter(cycle=cycle)}
    assert set(rows) == {owner.pk, ada.pk, bruno.pk, cleo.pk}

    assert rows[cleo.pk].card_count == 0
    assert rows[cleo.pk].submitted_at is None
    assert rows[cleo.pk].submitted is False
    assert rows[owner.pk].card_count == 0
    assert rows[ada.pk].submitted is True


@pytest.mark.django_db
def test_card_count_is_computed_before_the_authors_are_destroyed(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    """The whole reason the two steps are ordered.

    Ada writes three cards and every one of them is anonymous. Counted after
    the authors are nulled, her count would be 0 and the information would be
    gone for good; counted before, it is 3. Nothing else in the system can tell
    these two apart afterwards, which is why the order is a test and not a
    comment.
    """
    for index in range(3):
        make_card(cycle, ada, f"anonymous {index}", anonymous=True)

    reveal(retro, owner)

    assert CycleParticipation.objects.get(cycle=cycle, user=ada).card_count == 3
    assert not cycle.cards.filter(author__isnull=False).exists()


@pytest.mark.django_db
def test_card_count_counts_attributed_and_anonymous_cards_together(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    make_card(cycle, ada, "attributed one")
    make_card(cycle, ada, "attributed two", category=Card.Category.STOP)
    make_card(cycle, ada, "anonymous one", anonymous=True, category=Card.Category.CONTINUE)

    reveal(retro, owner)

    assert CycleParticipation.objects.get(cycle=cycle, user=ada).card_count == 3


@pytest.mark.django_db
def test_participation_records_no_card_identifier(
    retro: Retrospective, owner: User, anonymous_card: Card
) -> None:
    """The fields are asserted whole. A card id here would be the link, rebuilt.

    A row already names a user; the only thing that would turn it back into
    authorship is something that also names a card — an id, a list of ids, a
    count per category narrow enough to single one out.
    """
    reveal(retro, owner)

    field_names = {field.name for field in CycleParticipation._meta.concrete_fields}
    assert field_names == {"id", "cycle", "user", "card_count", "submitted_at", "created_at"}

    relations = {
        field.name: field.related_model
        for field in CycleParticipation._meta.concrete_fields
        if field.is_relation
    }
    assert relations == {"cycle": FeedbackCycle, "user": User}
    assert Card not in relations.values()


@pytest.mark.django_db
def test_two_rows_for_one_member_in_one_cycle_are_refused_by_the_database(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, anonymous_card: Card
) -> None:
    reveal(retro, owner)

    with pytest.raises(IntegrityError), transaction.atomic():
        CycleParticipation.objects.create(cycle=cycle, user=ada, card_count=0)


@pytest.mark.django_db
def test_a_row_cannot_say_a_member_submitted_nothing_at_a_time(
    cycle: FeedbackCycle, ada: User
) -> None:
    """The two halves of "did not submit" are held together by the database.

    A count with no time, or a time with no count, is a row that two screens
    would read two different ways. #25 and #26 both ask "did this person
    submit"; they must not be able to disagree.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        CycleParticipation.objects.create(
            cycle=cycle, user=ada, card_count=0, submitted_at=OPENS_AT
        )


@pytest.mark.django_db
def test_a_participation_time_never_matches_a_card_that_could_be_joined_to_it(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, bruno: User
) -> None:
    """`submitted_at` is deliberately coarse, and this is why.

    `Card.created_at` survives the reveal — the shuffle hides submission order
    from the board, but the column is still in the table. A participation row
    holding an exact submission time would equal exactly one card's timestamp,
    and that equality is the author link, rebuilt by a join and available to
    anyone who can read the database. So the day is stored and the time of day
    is not.

    `card_count` is a weaker channel of the same kind, and
    `_docs/decisions.md` item 3a accepts it in as many words: it is stored
    because aggregates need it, and never shown beside a name. An exact
    timestamp is a different thing — it identifies the card every time rather
    than sometimes — and is not stored at all.
    """
    make_card(
        cycle, ada, "first", anonymous=True, written_at=datetime(2026, 7, 21, 9, 14, tzinfo=UTC)
    )
    make_card(
        cycle,
        ada,
        "second",
        anonymous=True,
        category=Card.Category.STOP,
        written_at=datetime(2026, 7, 22, 16, 2, tzinfo=UTC),
    )
    make_card(cycle, bruno, "third", written_at=datetime(2026, 7, 21, 9, 45, tzinfo=UTC))

    reveal(retro, owner)

    card_times = set(cycle.cards.values_list("created_at", flat=True))
    participation_times = {
        row.submitted_at
        for row in CycleParticipation.objects.filter(cycle=cycle)
        if row.submitted_at is not None
    }

    assert participation_times.isdisjoint(card_times)
    for moment in participation_times:
        assert (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0)
    assert CycleParticipation.objects.get(cycle=cycle, user=ada).submitted_at == datetime(
        2026, 7, 21, 0, 0, tzinfo=UTC
    )


@pytest.mark.django_db
def test_no_screen_shows_one_members_card_count(
    retro: Retrospective,
    owner: User,
    ada: User,
    bruno: User,
    client: Client,
    anonymous_card: Card,
) -> None:
    """`_docs/decisions.md` item 3a: a count beside a name is an identifier.

    Two halves. No template mentions participation at all — this task builds no
    screen, and #25 and #26 are told to show submitted or not — and no page a
    member can reach carries a count next to anybody.
    """
    templates = list((BASE_DIR / "templates").rglob("*.html"))
    assert templates
    for template in templates:
        source = template.read_text()
        assert "card_count" not in source, template
        assert "participation" not in source.lower(), template

    reveal(retro, owner)
    log_in(client, bruno)

    for url in member_urls(retro):
        body = client.get(url).content.decode()
        assert "card_count" not in body, url


# --------------------------------------------------------------------------
# Safety: exactly once, all at once, or not at all
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_reveal_is_two_statements_against_the_cards_however_many_there_are(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User
) -> None:
    """One UPDATE for the positions, one for the authors. Never a loop of saves.

    A loop that saves a row at a time is twenty chances to die half way through
    with ten authors destroyed and ten not, and no way back from the first ten.
    The count is asserted against twenty cards, so a per-row implementation
    cannot pass by being small.
    """
    for index in range(20):
        make_card(cycle, ada, f"card {index}", anonymous=bool(index % 2))

    with CaptureQueriesContext(connection) as captured:
        reveal(retro, owner)

    updates = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE") and "cycles_card" in query["sql"]
    ]
    inserts = [
        query["sql"]
        for query in captured.captured_queries
        if "INSERT INTO" in query["sql"].upper() and "cycles_cycleparticipation" in query["sql"]
    ]

    assert len(updates) == 2, updates
    assert len(inserts) == 1, inserts


def test_the_reveal_refuses_to_run_outside_a_transaction() -> None:
    """Half a reveal that commits is worse than none at all.

    Deliberately not a database test: the guard fires before the first query,
    so a caller who forgets the transaction is stopped rather than allowed to
    destroy the authors and then fail to record the counts. Asserted against
    the function itself, because the transition hook is not the only thing that
    could ever call it.
    """
    with pytest.raises(RuntimeError, match="must run inside the transaction"):
        reveal_cycle(FeedbackCycle(pk=1))


@pytest.mark.django_db
def test_a_reveal_that_fails_part_way_through_destroys_nothing(
    monkeypatch: pytest.MonkeyPatch,
    retro: Retrospective,
    owner: User,
    cycle: FeedbackCycle,
    ada: User,
    anonymous_card: Card,
    attributed_card: Card,
) -> None:
    """Forced to fail after the authors were nulled, and asserted to have rolled back.

    `bump_version` runs after the hook, so patching it to raise interrupts the
    transition at the latest possible moment — with the participation rows
    written, the positions handed out and the authors already destroyed inside
    the transaction. Nothing may survive: not the stage, not the closed cycle,
    not the counts, and above all not the destroyed author.
    """

    def explode(_retro: Retrospective) -> int:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(services, "bump_version", explode)

    with pytest.raises(RuntimeError, match="the database went away"):
        reveal(retro, owner)

    assert raw_author_ids(cycle, anonymous=True) == [ada.pk]
    assert Card.objects.get(pk=anonymous_card.pk).author_id == ada.pk
    assert Card.objects.get(pk=anonymous_card.pk).position == 0
    assert Card.objects.get(pk=attributed_card.pk).position == 0
    assert CycleParticipation.objects.filter(cycle=cycle).count() == 0

    stored = Retrospective.objects.get(pk=retro.pk)
    assert stored.stage == Stage.DRAFT
    assert FeedbackCycle.objects.get(pk=cycle.pk).status == FeedbackCycle.Status.COLLECTING


@pytest.mark.django_db
def test_the_stage_machine_lets_the_reveal_happen_exactly_once(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, bruno: User
) -> None:
    """Advancing again moves on to CLUSTER; it does not reveal a second time.

    REVEAL is entered by one transition and the machine is forward-only, so
    there is no second entry to make. What this asserts is that carrying on
    advancing leaves the reveal's work exactly as it was — the same positions,
    the same participation rows, the same surviving authors.
    """
    make_card(cycle, ada, "anonymous", anonymous=True)
    make_card(cycle, bruno, "attributed")

    reveal(retro, owner)
    positions = dict(cycle.cards.values_list("pk", "position"))
    counts = dict(
        CycleParticipation.objects.filter(cycle=cycle).values_list("user_id", "card_count")
    )

    for _ in range(4):
        advance_stage(owner, retro)

    assert retro.stage == Stage.COMPLETE
    assert dict(cycle.cards.values_list("pk", "position")) == positions
    assert (
        dict(CycleParticipation.objects.filter(cycle=cycle).values_list("user_id", "card_count"))
        == counts
    )
    assert cycle.cards.get(is_anonymous=False).author_id == bruno.pk


@pytest.mark.parametrize("stage", [s for s in STAGE_ORDER if s != Stage.DRAFT])
def test_no_stage_but_draft_can_move_into_reveal(stage: str) -> None:
    assert is_legal_transition(stage, Stage.REVEAL) is False


@pytest.mark.django_db
def test_a_second_advance_off_a_stale_page_is_rejected(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, anonymous_card: Card
) -> None:
    """One double-click cannot reveal and then cluster.

    The second caller holds the version the page was rendered from, which the
    first advance has already moved past.
    """
    stale = Retrospective.objects.get(pk=retro.pk)

    reveal(retro, owner)

    with pytest.raises(ConcurrentAdvance):
        advance_stage(owner, stale)

    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.REVEAL


@pytest.mark.django_db
def test_a_reveal_forced_to_run_twice_is_refused_by_the_database(
    retro: Retrospective, owner: User, cycle: FeedbackCycle, ada: User, anonymous_card: Card
) -> None:
    """Defence behind the stage machine, for the day someone edits `stage` directly.

    `unique(cycle, user)` on the participation table means a second reveal
    cannot quietly double-count or re-shuffle: it fails, and the transaction it
    is in takes the whole thing back.
    """
    reveal(retro, owner)
    positions = dict(cycle.cards.values_list("pk", "position"))

    Retrospective.objects.filter(pk=retro.pk).update(stage=Stage.DRAFT)
    forced = Retrospective.objects.get(pk=retro.pk)

    with pytest.raises(IntegrityError):
        advance_stage(owner, forced)

    assert dict(cycle.cards.values_list("pk", "position")) == positions
    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.DRAFT
    # Two members, two rows — the ones the first reveal wrote, and no more.
    assert CycleParticipation.objects.filter(cycle=cycle).count() == 2


# --------------------------------------------------------------------------
# The card that arrives while the reveal is running
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_writing_a_card_locks_the_cycle_row_before_it_reads_the_status(
    client: Client, cycle: FeedbackCycle, ada: User
) -> None:
    """The lock, asserted on the SQL, so it cannot be removed unnoticed.

    Reading `status` without a lock is reading a value another transaction has
    already changed and not yet committed. The threaded test below is what
    proves the consequence; this one is what fails the moment somebody
    simplifies the query and re-opens it.
    """
    log_in(client, ada)

    with CaptureQueriesContext(connection) as captured:
        client.post(
            reverse("card-create", args=[cycle.pk, Card.Category.START]),
            {"text": "a card written normally"},
        )

    locking = [
        query["sql"]
        for query in captured.captured_queries
        if "FOR UPDATE" in query["sql"].upper() and "cycles_feedbackcycle" in query["sql"]
    ]
    assert locking, [q["sql"] for q in captured.captured_queries]
    assert cycle.cards.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_card_written_at_the_instant_of_the_reveal_cannot_keep_its_author() -> None:
    """The narrowest window in the feature, held open on purpose and driven through.

    A member presses Add at the moment the facilitator presses Advance. The
    reveal has set the cycle to CLOSED inside its transaction and has not
    committed, so a submission that read the status without a lock would still
    see COLLECTING, accept the card, and commit it into a cycle that has just
    been revealed. Nothing would ever come back to null its author: the reveal
    happens once and has already been. That card would carry its author for
    good — the one outcome this issue exists to make impossible.

    The reveal is held open here between closing the cycle and touching the
    cards, which is exactly where the window is. Both transactions are real,
    on two connections, so what settles it is the database and not an ordering
    the test arranged.
    """
    owner = make_user("olive-race", "Olive Owner")
    ada = make_user("ada-race", "Ada Racer")
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    Membership.objects.create(project=project, user=ada, role=Membership.Role.MEMBER)
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=owner,
    )
    retro = Retrospective.objects.create(cycle=cycle)

    closed = threading.Event()
    posted = threading.Event()
    real_reveal = services.reveal_cycle

    def held_open(revealing: FeedbackCycle) -> None:
        """Stand in the window: the cycle is CLOSED, uncommitted, cards untouched."""
        closed.set()
        posted.wait(timeout=20)
        real_reveal(revealing)

    services.reveal_cycle = held_open

    def submit() -> None:
        try:
            closed.wait(timeout=20)
            writer = Client()
            writer.login(username=ada.username, password=PASSWORD)
            writer.post(
                reverse("card-create", args=[cycle.pk, Card.Category.START]),
                {"text": "slipped in during the reveal", "is_anonymous": "on"},
            )
        finally:
            posted.set()
            connection.close()

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        advance_stage(owner, retro)
    finally:
        services.reveal_cycle = real_reveal
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not cycle.cards.filter(is_anonymous=True, author__isnull=False).exists()
    # Whichever order the two transactions settled into, the cycle is coherent:
    # every card in it was seen by the reveal, or is not in it at all.
    assert not cycle.cards.filter(position=0).exists()


# --------------------------------------------------------------------------
# Nothing leaks: the sweep
# --------------------------------------------------------------------------


def member_urls(retro: Retrospective) -> list[str]:
    """Every URL a project member can reach with a GET, at any stage."""
    cycle = retro.cycle
    return [
        reverse("home"),
        reverse("project-list"),
        reverse("project-detail", args=[cycle.project_id]),
        reverse("cycle-detail", args=[cycle.pk]),
        reverse("cycle-cards", args=[cycle.pk]),
        reverse("retro-detail", args=[retro.pk]),
    ]


def advance_to(retro: Retrospective, facilitator: User, stage: str) -> None:
    while retro.stage != stage:
        advance_stage(facilitator, retro)


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_ada_is_named_on_no_page_another_member_can_reach_at_any_stage(
    stage: str,
    retro: Retrospective,
    owner: User,
    ada: User,
    bruno: User,
    cleo: User,
    client: Client,
    anonymous_card: Card,
    attributed_card: Card,
) -> None:
    """The sweep. Six URLs, six stages, the whole response body each time.

    Ada's card must appear on none of them: no screen renders another member's
    cards, before reveal or after it, and #11 and #14 will render them with no
    author attached. Ada's names must appear on none of them either, with one
    honest exception — the project page lists its members, which is what a
    member list is for, and is asserted here rather than excused, so that a
    card arriving on that page later fails this test.

    The body is searched, not the visible text. A name in a `title` attribute,
    a `data-` attribute, an element id, an `alt`, a comment or a `json_script`
    block is a leak a browser would not show and a reader would never find.
    """
    advance_to(retro, owner, stage)
    log_in(client, bruno)

    members_page = reverse("project-detail", args=[retro.cycle.project_id])

    for url in member_urls(retro):
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        body = response.content.decode()

        assert SECRET_TEXT not in body, url

        if url == members_page:
            # Ada is on the team and the team can see that. What must not be
            # here is anything she wrote — a name beside a card is the leak,
            # a name in a member list is the product.
            assert ADA_DISPLAY_NAME in body
            assert "anonymous" not in body.lower()
            continue

        for identifier in ADA_IDENTIFIERS:
            assert identifier not in body, (url, identifier)


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_no_card_endpoint_names_ada_to_anyone_at_any_stage(
    stage: str,
    retro: Retrospective,
    owner: User,
    ada: User,
    bruno: User,
    client: Client,
    anonymous_card: Card,
) -> None:
    """The card endpoints, driven as somebody who is not the author.

    `card-show` and `card-edit` load through `own_card_or_404`, so another
    member's card is a 404 and says nothing about whether the id exists. The
    body of the refusal is checked too: a 404 page that echoed the card or its
    author would leak just as well as a 200.
    """
    advance_to(retro, owner, stage)
    log_in(client, bruno)

    for url in (
        reverse("card-show", args=[anonymous_card.pk]),
        reverse("card-edit", args=[anonymous_card.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 404, (url, response.status_code)
        body = response.content.decode()
        assert SECRET_TEXT not in body
        for identifier in ADA_IDENTIFIERS:
            assert identifier not in body, (url, identifier)


@pytest.mark.django_db
def test_a_member_who_posts_at_the_card_endpoints_with_a_real_token_is_still_refused(
    retro: Retrospective, owner: User, ada: User, bruno: User, anonymous_card: Card
) -> None:
    """Refusal proved by attempting it, with CSRF enforced and a valid token.

    A test client with `enforce_csrf_checks` would pass a missing-token
    rejection off as a permission check. The token is fetched from a real page
    and sent, so what refuses the request is the rule and not the middleware.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, bruno)
    token = client.get(reverse("cycle-cards", args=[retro.cycle_id])).cookies["csrftoken"].value

    for url in (
        reverse("card-edit", args=[anonymous_card.pk]),
        reverse("card-delete", args=[anonymous_card.pk]),
    ):
        response = client.post(
            url, {"text": "rewritten by someone else", "csrfmiddlewaretoken": token}
        )
        assert response.status_code == 404, (url, response.status_code)

    stored = Card.objects.get(pk=anonymous_card.pk)
    assert stored.text == SECRET_TEXT
    assert stored.author_id == ada.pk


@pytest.mark.django_db
def test_after_the_reveal_not_even_ada_can_reach_her_own_anonymous_card(
    retro: Retrospective, owner: User, ada: User, bruno: User, anonymous_card: Card
) -> None:
    """The cost of the promise, stated as a test.

    Once the author is destroyed the card is nobody's. There is no one left to
    authorize an edit against — `_docs/decisions.md` item 1 — and the person
    who wrote it cannot be told apart from anyone else who claims to have. Her
    card list stops showing it and the endpoints that load a card of *her own*
    answer her with a 404.

    What she keeps is what every member keeps: she may read the card on the
    board, because it is the team's now. `can_view_card` gives her and Bruno
    the same answer, which is the shape the guarantee has to have — a rule that
    still treated her differently would be the link, expressed as a permission.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, ada)
    assert SECRET_TEXT in client.get(reverse("cycle-cards", args=[retro.cycle_id])).content.decode()

    reveal(retro, owner)

    listing = client.get(reverse("cycle-cards", args=[retro.cycle_id]))
    assert SECRET_TEXT not in listing.content.decode()
    assert list(listing.context["cards"]) == []

    token = listing.cookies["csrftoken"].value
    assert client.get(reverse("card-show", args=[anonymous_card.pk])).status_code == 404
    assert (
        client.post(
            reverse("card-delete", args=[anonymous_card.pk]),
            {"csrfmiddlewaretoken": token},
        ).status_code
        == 404
    )

    card = Card.objects.get(pk=anonymous_card.pk)
    assert can_edit_card(ada, card) is False
    assert can_delete_card(ada, card) is False
    assert can_view_card(ada, card) is can_view_card(bruno, card) is True


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_a_superuser_from_outside_the_project_learns_nothing_at_any_stage(
    stage: str,
    retro: Retrospective,
    owner: User,
    ada: User,
    root: User,
    client: Client,
    anonymous_card: Card,
) -> None:
    """No admin exception, at any stage, on any URL.

    `_docs/decisions.md` item 3 grants staff nothing, and #6 put that property
    in the predicates. This drives it through the views: a superuser who is not
    a member is answered exactly the way a stranger guessing at ids is, and no
    response carries the card or its author.
    """
    advance_to(retro, owner, stage)
    log_in(client, root)

    project_scoped = [
        reverse("project-detail", args=[retro.cycle.project_id]),
        reverse("cycle-detail", args=[retro.cycle_id]),
        reverse("cycle-cards", args=[retro.cycle_id]),
        reverse("retro-detail", args=[retro.pk]),
        reverse("card-show", args=[anonymous_card.pk]),
        reverse("card-edit", args=[anonymous_card.pk]),
    ]

    for url in project_scoped:
        response = client.get(url)
        assert response.status_code == 404, (url, response.status_code)

    for url in [*project_scoped, reverse("home"), reverse("project-list")]:
        body = client.get(url).content.decode()
        assert SECRET_TEXT not in body, url
        for identifier in ADA_IDENTIFIERS:
            assert identifier not in body, (url, identifier)

    assert can_view_card(root, Card.objects.get(pk=anonymous_card.pk)) is False


@pytest.mark.django_db
def test_an_outsider_is_told_nothing_either(
    retro: Retrospective, owner: User, outsider: User, client: Client, anonymous_card: Card
) -> None:
    reveal(retro, owner)
    log_in(client, outsider)

    for url in (
        reverse("cycle-cards", args=[retro.cycle_id]),
        reverse("retro-detail", args=[retro.pk]),
        reverse("card-show", args=[anonymous_card.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 404
        assert SECRET_TEXT not in response.content.decode()
