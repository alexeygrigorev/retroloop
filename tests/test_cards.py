"""Feedback cards: writing them, changing them, and seeing only your own.

Every test here maps to an acceptance criterion of issue #8, plus the criterion
#8 inherited from #7 — see "Closed cycles" below.

Three themes run through the file.

The first is that a control being hidden is never the rule. Where a member must
not be able to do something, the test drives the endpoint as that member and
asserts the server refused, and separately asserts the control is absent from
the page.

The second is that absence is asserted, not assumed. A test that only checks
what *is* on a page is exactly how a two-line `{# #}` comment shipped to main:
everything it looked for was present. So where a criterion says something must
not appear — another member's card, a form on a closed cycle, a delete link —
the assertion is that it is not there.

The third is that "you see only your own" is a property of the query. The
cross-member tests assert on `response.context`, which is what the view handed
the template, and not only on the HTML the template happened to render.
"""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, migrations, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cycles import views as cycles_views
from cycles.models import CARD_TEXT_MAX_LENGTH, Card, FeedbackCycle
from projects.models import Membership, Project
from projects.permissions import can_add_card, can_delete_card, can_edit_card, can_view_card
from tests.template_render import template_sources

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

CATEGORIES = ["START", "STOP", "CONTINUE"]


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def log_in(client: Client, user: User) -> None:
    client.login(username=user.username, password=PASSWORD)


def cards_url(cycle: FeedbackCycle) -> str:
    return reverse("cycle-cards", args=[cycle.pk])


def create_url(cycle: FeedbackCycle, category: str = "START") -> str:
    return reverse("card-create", args=[cycle.pk, category])


def show_url(card: Card) -> str:
    return reverse("card-show", args=[card.pk])


def edit_url(card: Card) -> str:
    return reverse("card-edit", args=[card.pk])


def delete_url(card: Card) -> str:
    return reverse("card-delete", args=[card.pk])


def close(cycle: FeedbackCycle) -> FeedbackCycle:
    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])
    return cycle


@pytest.fixture
def owner(db) -> User:
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Mel Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def other_member(project: Project) -> User:
    user = make_user("other", "Otto Other")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


@pytest.fixture
def cycle(project: Project, owner: User) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=owner,
    )


def make_card(cycle: FeedbackCycle, author: User, text: str = "Pair on the tricky bits", **kwargs):
    return Card.objects.create(
        cycle=cycle,
        author=author,
        category=kwargs.pop("category", Card.Category.START),
        text=text,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_card_carries_a_cycle_a_category_an_author_and_a_body(
    cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Start writing the deploy notes down")

    assert card.cycle == cycle
    assert card.category == Card.Category.START
    assert card.author == member
    assert card.text == "Start writing the deploy notes down"
    assert card.is_anonymous is False
    assert card.position == 0
    assert card.created_at is not None


@pytest.mark.django_db
def test_the_three_categories_are_start_stop_and_continue() -> None:
    assert Card.Category.values == CATEGORIES


@pytest.mark.django_db
def test_a_card_can_have_no_author_at_all(cycle: FeedbackCycle, member: User) -> None:
    """What #10 does at reveal, done directly: the column takes NULL today."""
    card = make_card(cycle, member)

    card.author = None
    card.save(update_fields=["author"])
    card.refresh_from_db()

    assert card.author is None


def test_author_is_nullable_in_the_migration_that_creates_the_table() -> None:
    """Nullable from the first migration, not retrofitted — decisions.md item 3.

    Read off the migration rather than the model, because the point of the
    criterion is that no later migration has to alter a populated column.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    creations = [
        (name, operation)
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == "cycles"
        for operation in migration.operations
        if isinstance(operation, migrations.CreateModel) and operation.name == "Card"
    ]

    assert len(creations) == 1
    _name, operation = creations[0]
    fields = dict(operation.fields)
    assert fields["author"].null is True

    # And nothing anywhere alters it afterwards.
    alterations = [
        operation
        for (app_label, _name), migration in loader.disk_migrations.items()
        if app_label == "cycles"
        for operation in migration.operations
        if isinstance(operation, migrations.AlterField)
        and operation.model_name.lower() == "card"
        and operation.name == "author"
    ]
    assert alterations == []


@pytest.mark.django_db
def test_nothing_sorts_by_position(cycle: FeedbackCycle, member: User) -> None:
    """#10 writes `position` at reveal. Until then it means nothing, so nothing reads it."""
    first = make_card(cycle, member, "written first", position=99)
    second = make_card(cycle, member, "written second", position=1)

    assert Card._meta.ordering == ["created_at", "id"]
    assert "position" not in Card._meta.ordering
    assert list(Card.objects.all()) == [first, second]


@pytest.mark.django_db
def test_the_text_column_stops_at_the_cap(cycle: FeedbackCycle, member: User) -> None:
    assert Card._meta.get_field("text").max_length == CARD_TEXT_MAX_LENGTH
    assert CARD_TEXT_MAX_LENGTH == 500


@pytest.mark.django_db
def test_the_database_refuses_a_card_of_nothing_but_space(
    cycle: FeedbackCycle, member: User
) -> None:
    """The form says it politely; this is the same rule where no form is involved."""
    with pytest.raises(IntegrityError), transaction.atomic():
        make_card(cycle, member, "   \n  ")


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_screen_shows_three_sections(client: Client, cycle: FeedbackCycle, member: User):
    log_in(client, member)

    response = client.get(cards_url(cycle))
    html = response.content.decode()

    assert response.status_code == 200
    assert [section["category"] for section in response.context["sections"]] == CATEGORIES
    for category, label in zip(CATEGORIES, ["Start", "Stop", "Continue"], strict=True):
        assert f'data-card-section="{category}"' in html
        assert f"Add to {label}" in html


@pytest.mark.django_db
def test_the_screen_renders_inside_the_application_layout(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """base_app.html, so the page has the navigation every other page has."""
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert html.count("<!doctype html>") == 1
    assert "<nav" in html
    assert "Your projects" in html
    assert "Log out" in html


def test_the_card_template_has_no_document_shell_of_its_own() -> None:
    source = (BASE_DIR / "templates" / "cycles" / "card_list.html").read_text()

    assert "<!doctype" not in source.lower()
    assert "<html" not in source.lower()
    assert source.lstrip().startswith('{% extends "base_app.html" %}')


@pytest.mark.django_db
def test_a_member_can_add_a_card_under_each_heading(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    for category in CATEGORIES:
        response = client.post(create_url(cycle, category), {"text": f"one for {category}"})
        assert response.status_code == 200

    assert [card.category for card in cycle.cards.all()] == CATEGORIES
    assert cycle.cards.count() == 3


@pytest.mark.django_db
def test_several_cards_fit_under_one_heading(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    client.post(create_url(cycle, "STOP"), {"text": "Stop the Friday deploys"})
    client.post(create_url(cycle, "STOP"), {"text": "Stop the standup overrunning"})

    texts = list(cycle.cards.filter(category="STOP").values_list("text", flat=True))
    assert texts == ["Stop the Friday deploys", "Stop the standup overrunning"]


@pytest.mark.django_db
def test_the_author_is_set_on_an_anonymous_card(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """Anonymity is applied at reveal by #10, never at write time."""
    log_in(client, member)

    client.post(create_url(cycle), {"text": "Say the quiet part", "is_anonymous": "on"})

    card = cycle.cards.get()
    assert card.is_anonymous is True
    assert card.author == member


@pytest.mark.django_db
def test_the_author_is_set_on_an_ordinary_card(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    client.post(create_url(cycle), {"text": "Say it with your name on it"})

    card = cycle.cards.get()
    assert card.is_anonymous is False
    assert card.author == member


@pytest.mark.django_db
@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"])
def test_a_card_of_nothing_is_rejected_without_a_row(
    client: Client, cycle: FeedbackCycle, member: User, text: str
) -> None:
    log_in(client, member)

    response = client.post(create_url(cycle), {"text": text})

    assert response.status_code == 200
    assert not Card.objects.exists()
    assert "A card needs some words on it." in response.content.decode() or "required" in (
        response.content.decode()
    )


@pytest.mark.django_db
def test_the_text_is_stored_without_its_surrounding_space(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    client.post(create_url(cycle), {"text": "  Trim me  "})

    assert cycle.cards.get().text == "Trim me"


@pytest.mark.django_db
def test_the_cap_is_enforced_by_the_server_not_only_by_the_browser(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """The POST goes straight past `maxlength`, which is where a real one would go."""
    log_in(client, member)

    response = client.post(create_url(cycle), {"text": "x" * (CARD_TEXT_MAX_LENGTH + 1)})

    assert response.status_code == 200
    assert not Card.objects.exists()
    assert "500 characters" in response.content.decode()


@pytest.mark.django_db
def test_a_card_of_exactly_the_cap_is_accepted(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    client.post(create_url(cycle), {"text": "x" * CARD_TEXT_MAX_LENGTH})

    assert cycle.cards.get().text == "x" * CARD_TEXT_MAX_LENGTH


@pytest.mark.django_db
def test_the_form_counts_down_the_characters_left_as_you_type(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert f'maxlength="{CARD_TEXT_MAX_LENGTH}"' in html
    assert 'x-data="{ remaining: 500 }"' in html
    assert 'x-text="remaining"' in html
    assert "characters left" in html
    assert "@input=" in html


@pytest.mark.django_db
def test_every_card_gets_its_own_anonymous_checkbox(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    checkboxes = [
        tag
        for tag in re.findall(r"<input[^>]*>", html)
        if 'name="is_anonymous"' in tag and 'type="checkbox"' in tag
    ]
    assert len(checkboxes) == len(CATEGORIES)


@pytest.mark.django_db
def test_the_checkbox_says_the_author_goes_permanently_and_cannot_come_back(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert "Post this anonymously" in html
    assert "your name is removed from this card permanently" in html
    assert "cannot be undone" in html


@pytest.mark.django_db
def test_the_empty_state_says_what_to_do(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert 'id="cards-empty"' in html
    assert "You have not written anything for this week yet." in html
    assert "Add a card under Start," in html


@pytest.mark.django_db
def test_the_empty_state_goes_once_there_is_a_card(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    make_card(cycle, member)
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert 'id="cards-empty"' not in html
    assert "You have not written anything for this week yet." not in html


# --------------------------------------------------------------------------
# Only your own
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_screen_holds_only_the_looking_members_cards(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    """Asserted on the context, because the filter has to be on the queryset."""
    mine = make_card(cycle, member, "Mine to see")
    theirs = make_card(cycle, other_member, "Theirs to keep")
    log_in(client, member)

    response = client.get(cards_url(cycle))
    html = response.content.decode()

    assert list(response.context["cards"]) == [mine]
    assert theirs not in list(response.context["cards"])
    for section in response.context["sections"]:
        assert theirs not in list(section["cards"])
    assert "Mine to see" in html
    assert "Theirs to keep" not in html
    assert str(theirs.pk) not in re.findall(r'data-card="(\d+)"', html)


@pytest.mark.django_db
def test_another_members_card_is_absent_from_every_section_of_a_full_board(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    for category in CATEGORIES:
        make_card(cycle, other_member, f"theirs under {category}", category=category)
    log_in(client, member)

    response = client.get(cards_url(cycle))
    html = response.content.decode()

    assert list(response.context["cards"]) == []
    for category in CATEGORIES:
        assert f"theirs under {category}" not in html
    # And the page tells this member they have written nothing, rather than
    # showing them a full board that is not theirs.
    assert 'id="cards-empty"' in html


@pytest.mark.django_db
@pytest.mark.parametrize("url_for", [show_url, edit_url])
def test_getting_another_members_card_is_a_404(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User, url_for
) -> None:
    theirs = make_card(cycle, other_member, "Not yours")
    log_in(client, member)

    assert client.get(url_for(theirs)).status_code == 404


@pytest.mark.django_db
def test_editing_another_members_card_is_a_404_and_changes_nothing(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    theirs = make_card(cycle, other_member, "Not yours")
    log_in(client, member)

    response = client.post(edit_url(theirs), {"text": "Mine now"})
    theirs.refresh_from_db()

    assert response.status_code == 404
    assert theirs.text == "Not yours"


@pytest.mark.django_db
def test_deleting_another_members_card_is_a_404_and_the_card_stays(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    theirs = make_card(cycle, other_member, "Not yours")
    log_in(client, member)

    response = client.post(delete_url(theirs))

    assert response.status_code == 404
    assert Card.objects.filter(pk=theirs.pk).exists()


@pytest.mark.django_db
def test_a_card_with_no_author_left_belongs_to_nobody(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """The state #10 creates at reveal: nobody can then edit or delete the card."""
    card = make_card(cycle, member)
    card.author = None
    card.save(update_fields=["author"])
    log_in(client, member)

    assert client.get(show_url(card)).status_code == 404
    assert client.get(edit_url(card)).status_code == 404
    assert client.post(edit_url(card), {"text": "mine again"}).status_code == 404
    assert client.post(delete_url(card)).status_code == 404
    assert Card.objects.filter(pk=card.pk).exists()


@pytest.mark.django_db
def test_a_non_member_gets_404_for_the_whole_screen(
    client: Client, cycle: FeedbackCycle, member: User, outsider: User
) -> None:
    make_card(cycle, member, "Members only")
    log_in(client, outsider)

    response = client.get(cards_url(cycle))

    assert response.status_code == 404
    assert "Members only" not in response.content.decode()


@pytest.mark.django_db
def test_a_non_member_cannot_post_a_card_into_the_cycle(
    client: Client, cycle: FeedbackCycle, outsider: User
) -> None:
    log_in(client, outsider)

    response = client.post(create_url(cycle), {"text": "Let me in"})

    assert response.status_code == 404
    assert not Card.objects.exists()


@pytest.mark.django_db
def test_an_unknown_category_is_a_404(client: Client, cycle: FeedbackCycle, member: User) -> None:
    log_in(client, member)

    response = client.post(create_url(cycle, "PONDER"), {"text": "Not a section"})

    assert response.status_code == 404
    assert not Card.objects.exists()


@pytest.mark.django_db
def test_every_card_url_needs_a_login(client: Client, cycle: FeedbackCycle, member: User) -> None:
    card = make_card(cycle, member)

    for url in (cards_url(cycle), create_url(cycle), show_url(card), edit_url(card)):
        response = client.get(url)
        assert response.status_code == 302, url
        assert response.headers["Location"].startswith("/accounts/login/")

    response = client.post(delete_url(card))
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/accounts/login/")
    assert Card.objects.filter(pk=card.pk).exists()


# --------------------------------------------------------------------------
# Editing and deleting over htmx
# --------------------------------------------------------------------------


def assert_is_a_fragment(html: str) -> None:
    assert "<html" not in html.lower()
    assert "<!doctype" not in html.lower()
    assert "<nav" not in html.lower()
    assert "Log out" not in html


@pytest.mark.django_db
def test_creating_a_card_answers_with_the_affected_section_only(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    response = client.post(
        create_url(cycle, "STOP"),
        {"text": "Stop the Friday deploys"},
        headers={"hx-request": "true"},
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert_is_a_fragment(html)
    assert 'data-card-section="STOP"' in html
    assert "Stop the Friday deploys" in html
    # Only the section that changed, so the other two are not in the answer.
    assert 'data-card-section="START"' not in html
    assert 'data-card-section="CONTINUE"' not in html


@pytest.mark.django_db
def test_the_page_wires_create_edit_and_delete_to_htmx(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member)
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert f'hx-post="{create_url(cycle, "START")}"' in html
    assert 'hx-target="#section-START"' in html
    assert f'hx-get="{edit_url(card)}"' in html
    assert f'hx-post="{delete_url(card)}"' in html
    assert f'hx-target="#card-{card.pk}"' in html
    # The exact strings the closed-cycle test asserts the absence of, so that
    # test cannot quietly pass because the markup was reworded underneath it.
    assert ">Edit</button>" in html
    assert ">Delete</button>" in html


@pytest.mark.django_db
def test_the_edit_form_arrives_as_a_fragment_with_the_card_in_it(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Pair on the tricky bits")
    log_in(client, member)

    response = client.get(edit_url(card), headers={"hx-request": "true"})
    html = response.content.decode()

    assert response.status_code == 200
    assert_is_a_fragment(html)
    assert f'data-card-editing="{card.pk}"' in html
    assert "Pair on the tricky bits" in html
    assert "characters left" in html


@pytest.mark.django_db
def test_saving_an_edit_answers_with_the_card_alone(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Pair on the tricky bits")
    log_in(client, member)

    response = client.post(edit_url(card), {"text": "Pair on the hard bits"})
    html = response.content.decode()
    card.refresh_from_db()

    assert response.status_code == 200
    assert_is_a_fragment(html)
    assert card.text == "Pair on the hard bits"
    assert f'data-card="{card.pk}"' in html
    assert "Pair on the hard bits" in html
    # The form is gone: what comes back is the card, read-only again.
    assert f'data-card-editing="{card.pk}"' not in html


@pytest.mark.django_db
def test_an_edit_can_turn_the_anonymous_checkbox_on_and_off_again(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """Changeable for as long as the cycle is collecting."""
    card = make_card(cycle, member, "Say the quiet part")
    log_in(client, member)

    client.post(edit_url(card), {"text": card.text, "is_anonymous": "on"})
    card.refresh_from_db()
    assert card.is_anonymous is True
    assert card.author == member

    client.post(edit_url(card), {"text": card.text})
    card.refresh_from_db()
    assert card.is_anonymous is False
    assert card.author == member


@pytest.mark.django_db
def test_an_edit_to_nothing_is_rejected_and_the_card_keeps_its_words(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Pair on the tricky bits")
    log_in(client, member)

    response = client.post(edit_url(card), {"text": "   "})
    card.refresh_from_db()

    assert response.status_code == 200
    assert card.text == "Pair on the tricky bits"
    assert f'data-card-editing="{card.pk}"' in response.content.decode()


@pytest.mark.django_db
def test_cancelling_an_edit_brings_the_card_back(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Pair on the tricky bits")
    log_in(client, member)

    response = client.get(show_url(card), headers={"hx-request": "true"})
    html = response.content.decode()

    assert response.status_code == 200
    assert_is_a_fragment(html)
    assert f'data-card="{card.pk}"' in html
    assert f'data-card-editing="{card.pk}"' not in html


@pytest.mark.django_db
def test_deleting_a_card_answers_with_the_section_it_left(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "Delete me", category="CONTINUE")
    kept = make_card(cycle, member, "Keep me", category="START")
    log_in(client, member)

    response = client.post(delete_url(card), headers={"hx-request": "true"})
    html = response.content.decode()

    assert response.status_code == 200
    assert_is_a_fragment(html)
    assert not Card.objects.filter(pk=card.pk).exists()
    assert Card.objects.filter(pk=kept.pk).exists()
    assert 'data-card-section="CONTINUE"' in html
    assert "Delete me" not in html
    assert "Nothing under this heading yet." in html


@pytest.mark.django_db
def test_delete_is_a_post_with_a_token_and_never_a_link(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member)
    log_in(client, member)
    html = client.get(cards_url(cycle)).content.decode()

    assert f'<a href="{delete_url(card)}"' not in html
    assert delete_url(card) not in re.findall(r'<a\b[^>]*href="([^"]+)"', html)
    form = re.search(
        rf'<form[^>]*action="{re.escape(delete_url(card))}"[^>]*>(.*?)</form>',
        html,
        re.DOTALL,
    )
    assert form is not None
    assert 'name="csrfmiddlewaretoken"' in form.group(1)

    # And a GET at the endpoint deletes nothing, whatever a page might link to.
    assert client.get(delete_url(card)).status_code == 405
    assert Card.objects.filter(pk=card.pk).exists()


@pytest.mark.django_db
def test_a_delete_without_a_csrf_token_is_refused(cycle: FeedbackCycle, member: User) -> None:
    card = make_card(cycle, member)
    client = Client(enforce_csrf_checks=True)
    client.login(username=member.username, password=PASSWORD)

    response = client.post(delete_url(card), headers={"hx-request": "true"})

    assert response.status_code == 403
    assert Card.objects.filter(pk=card.pk).exists()


# --------------------------------------------------------------------------
# Closed cycles
#
# This section is the criterion #8 inherited from #7. #7 could only express
# "once CLOSED, no card may be created" as `FeedbackCycle.accepts_cards`,
# because no endpoint existed to post at; QA accepted that on condition that #8
# proved the whole thing end to end. So every test below drives the real
# endpoint and asserts on rows in the table — none of them asserts on
# `accepts_cards`.
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_post_to_a_closed_cycle_is_refused_by_the_server(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    close(cycle)
    log_in(client, member)

    response = client.post(create_url(cycle), {"text": "Sneaking one in"})

    assert response.status_code == 403
    assert not Card.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("category", CATEGORIES)
def test_no_section_of_a_closed_cycle_takes_a_card(
    client: Client, cycle: FeedbackCycle, member: User, category: str
) -> None:
    close(cycle)
    log_in(client, member)

    response = client.post(create_url(cycle, category), {"text": "Sneaking one in"})

    assert response.status_code == 403
    assert not Card.objects.exists()


@pytest.mark.django_db
def test_a_cycle_that_closes_while_the_form_is_open_refuses_the_post_that_follows(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """The window between rendering the form and posting it.

    The page is fetched while the cycle is collecting, so the member has a real
    form in front of them; the facilitator closes the cycle; the POST that was
    already on its way is refused, and no row is written. The state is read on
    the request, never trusted from the page it came from.
    """
    log_in(client, member)
    rendered = client.get(cards_url(cycle)).content.decode()
    assert f'action="{create_url(cycle)}"' in rendered  # the form really was there

    close(cycle)

    response = client.post(create_url(cycle), {"text": "Typed before the close"})

    assert response.status_code == 403
    assert not Card.objects.exists()


@pytest.mark.django_db
def test_a_cycle_that_closes_while_a_card_is_being_edited_refuses_the_save(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "As it was")
    log_in(client, member)
    assert client.get(edit_url(card)).status_code == 200  # the edit form really was served

    close(cycle)

    response = client.post(edit_url(card), {"text": "As it never became"})
    card.refresh_from_db()

    assert response.status_code == 403
    assert card.text == "As it was"


@pytest.mark.django_db
def test_a_cycle_that_closes_while_the_page_is_open_refuses_the_delete(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member)
    log_in(client, member)
    assert f'hx-post="{delete_url(card)}"' in client.get(cards_url(cycle)).content.decode()

    close(cycle)

    response = client.post(delete_url(card))

    assert response.status_code == 403
    assert Card.objects.filter(pk=card.pk).exists()


@pytest.mark.django_db
def test_a_closed_cycle_shows_your_own_cards_read_only_and_says_why(
    client: Client, cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    mine = make_card(cycle, member, "What I wrote")
    make_card(cycle, other_member, "What they wrote")
    close(cycle)
    log_in(client, member)

    response = client.get(cards_url(cycle))
    html = response.content.decode()

    assert response.status_code == 200
    assert list(response.context["cards"]) == [mine]
    assert "What I wrote" in html
    assert "What they wrote" not in html
    assert 'data-cards-readonly="true"' in html
    assert "This cycle is closed, so your cards are read-only." in html


@pytest.mark.django_db
def test_a_closed_cycle_offers_no_control_that_would_change_anything(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member, "What I wrote")
    close(cycle)
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert "<textarea" not in html
    assert 'name="is_anonymous"' not in html
    assert "Add to Start" not in html
    assert "characters left" not in html
    assert create_url(cycle) not in html
    assert edit_url(card) not in html
    assert delete_url(card) not in html
    assert ">Edit</button>" not in html
    assert ">Delete</button>" not in html


@pytest.mark.django_db
def test_a_closed_cycle_with_no_cards_says_that_instead_of_offering_a_form(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    close(cycle)
    log_in(client, member)

    html = client.get(cards_url(cycle)).content.decode()

    assert "You did not add any cards to this cycle" in html
    assert "<textarea" not in html


# --------------------------------------------------------------------------
# One window, written once
#
# Issue #66. The card partial used to hide Edit and Delete on
# `cycle.accepts_cards` — the status half of `can_edit_card`, written a second
# time in a template with nothing pinning the two together. The controls now
# come from per-card flags the view attaches from the predicates themselves, so
# moving the window in `_docs/decisions.md` item 1 moves the buttons with it.
#
# Every test below stubs a predicate rather than closing the cycle: closing one
# would satisfy the old template as well, and prove nothing about which of the
# two the page is reading.
# --------------------------------------------------------------------------


def refuse(monkeypatch, name: str) -> None:
    """Make one predicate say no, where `cycles/views.py` asks it."""
    monkeypatch.setattr(cycles_views, name, lambda user, card: False)


@pytest.mark.django_db
def test_the_edit_control_is_gone_when_can_edit_card_says_no(
    client: Client, cycle: FeedbackCycle, member: User, monkeypatch
) -> None:
    """The card is the member's own and the cycle is collecting, so the status
    the old template read still says yes. Only the predicate says no."""
    card = make_card(cycle, member, "What I wrote")
    log_in(client, member)
    refuse(monkeypatch, "can_edit_card")

    html = client.get(cards_url(cycle)).content.decode()

    assert cycle.status == FeedbackCycle.Status.COLLECTING
    assert "What I wrote" in html
    assert ">Edit</button>" not in html
    assert edit_url(card) not in html
    # And the endpoint is the same answer, not merely a hidden button.
    assert client.get(edit_url(card)).status_code == 403


@pytest.mark.django_db
def test_the_delete_control_is_gone_when_can_delete_card_says_no(
    client: Client, cycle: FeedbackCycle, member: User, monkeypatch
) -> None:
    card = make_card(cycle, member, "What I wrote")
    log_in(client, member)
    refuse(monkeypatch, "can_delete_card")

    html = client.get(cards_url(cycle)).content.decode()

    assert ">Delete</button>" not in html
    assert delete_url(card) not in html
    assert client.post(delete_url(card)).status_code == 403
    assert Card.objects.filter(pk=card.pk).exists()


@pytest.mark.django_db
def test_each_control_follows_its_own_predicate(
    client: Client, cycle: FeedbackCycle, member: User, monkeypatch
) -> None:
    """Two rules, two flags: refusing one leaves the other's control standing."""
    card = make_card(cycle, member)
    log_in(client, member)
    refuse(monkeypatch, "can_edit_card")

    html = client.get(cards_url(cycle)).content.decode()

    assert ">Edit</button>" not in html
    assert ">Delete</button>" in html
    assert delete_url(card) in html


@pytest.mark.django_db
def test_a_refused_card_keeps_the_rest_of_the_page(
    client: Client, cycle: FeedbackCycle, member: User, monkeypatch
) -> None:
    """The flags are per card. A card nobody may change is still shown, and the
    section it sits in still takes new cards, because `can_add_card` is a
    different question."""
    make_card(cycle, member, "Still readable")
    log_in(client, member)
    refuse(monkeypatch, "can_edit_card")
    refuse(monkeypatch, "can_delete_card")

    html = client.get(cards_url(cycle)).content.decode()

    assert "Still readable" in html
    assert create_url(cycle) in html
    assert "Add to Start" in html
    assert 'data-cards-readonly="true"' not in html
    assert ">Edit</button>" not in html
    assert ">Delete</button>" not in html


@pytest.mark.django_db
@pytest.mark.parametrize("predicate", ["can_edit_card", "can_delete_card"])
def test_the_htmx_fragments_carry_the_same_flags_as_the_page(
    client: Client, cycle: FeedbackCycle, member: User, monkeypatch, predicate: str
) -> None:
    """A card swapped in after a save or a cancel is drawn by the same partial,
    so it has to arrive with the same answers on it — otherwise the controls
    come back, or vanish, depending on which route drew the card last."""
    card = make_card(cycle, member, "As it was")
    log_in(client, member)

    saved = client.post(edit_url(card), {"text": "As it became"}).content.decode()
    cancelled = client.get(show_url(card)).content.decode()
    assert ">Edit</button>" in saved
    assert ">Delete</button>" in saved
    assert ">Edit</button>" in cancelled
    assert ">Delete</button>" in cancelled

    refuse(monkeypatch, predicate)
    control = ">Edit</button>" if predicate == "can_edit_card" else ">Delete</button>"

    assert control not in client.get(show_url(card)).content.decode()


@pytest.mark.django_db
def test_a_hidden_control_is_a_refusal_a_valid_token_cannot_get_round(
    cycle: FeedbackCycle, member: User
) -> None:
    """The template is fail-safe, and this is what proves it.

    The same POST is made twice with a CSRF token the server itself issued: it
    is accepted while the cycle collects, so the token is genuine and the
    request is well formed, and refused once the cycle is closed. What changed
    is the rule, not the markup.
    """
    card = make_card(cycle, member, "As it was")
    client = Client(enforce_csrf_checks=True)
    client.login(username=member.username, password=PASSWORD)
    html = client.get(cards_url(cycle)).content.decode()
    token = client.cookies["csrftoken"].value
    assert edit_url(card) in html

    accepted = client.post(edit_url(card), {"text": "As it became", "csrfmiddlewaretoken": token})
    card.refresh_from_db()
    assert accepted.status_code == 200
    assert card.text == "As it became"

    close(cycle)
    html = client.get(cards_url(cycle)).content.decode()
    refused = client.post(
        edit_url(card), {"text": "As it never became", "csrfmiddlewaretoken": token}
    )
    card.refresh_from_db()

    assert edit_url(card) not in html
    assert ">Edit</button>" not in html
    assert refused.status_code == 403
    assert card.text == "As it became"


@pytest.mark.django_db
def test_the_card_page_costs_the_same_queries_however_many_cards_are_on_it(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    """The flags are computed per card, so this is the test that says they cost
    nothing per card. Both predicates read the card's author and the status of
    its cycle, and the cycle comes back with the card in one query.
    """
    make_card(cycle, member, "The only one")
    log_in(client, member)

    with CaptureQueriesContext(connection) as one_card:
        assert client.get(cards_url(cycle)).status_code == 200

    for index in range(30):
        make_card(cycle, member, f"Card {index}", category=CATEGORIES[index % 3])

    with CaptureQueriesContext(connection) as many_cards:
        assert client.get(cards_url(cycle)).status_code == 200

    assert len(many_cards) == len(one_card), (
        f"{len(one_card)} queries for one card and {len(many_cards)} for thirty-one: "
        f"the page is asking the database something per card"
    )


def test_accepts_cards_is_read_by_no_template() -> None:
    """It stays on the model as a description of the row — #7's tests read it —
    but nothing renders from it any more."""
    readers = [name for name, source in template_sources().items() if "accepts_cards" in source]

    assert readers == []


def test_accepts_cards_is_read_by_no_application_code() -> None:
    """Its only appearance outside tests is the property itself."""
    packages = ("accounts", "ai", "board", "config", "cycles", "meetings", "projects", "retro")
    readers = [
        str(path.relative_to(BASE_DIR))
        for package in packages
        for path in sorted((BASE_DIR / package).rglob("*.py"))
        if "accepts_cards" in path.read_text()
    ]

    assert readers == ["cycles/models.py"]
    assert cycles_models_source().count("accepts_cards") == 1


def cycles_models_source() -> str:
    return (BASE_DIR / "cycles" / "models.py").read_text()


# --------------------------------------------------------------------------
# The rules themselves
#
# They were one-line predicates at the top of cycles/views.py until #6 lifted
# them into projects/permissions.py, which is where they are imported from now.
# The tests below pin the names and the answers; the tests above prove the
# views actually ask them.
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_can_add_card_wants_a_member_and_an_open_cycle(
    cycle: FeedbackCycle, member: User, outsider: User
) -> None:
    assert can_add_card(member, cycle) is True
    assert can_add_card(outsider, cycle) is False

    close(cycle)
    assert can_add_card(member, cycle) is False


@pytest.mark.django_db
def test_the_card_rules_are_author_and_collecting(
    cycle: FeedbackCycle, member: User, other_member: User
) -> None:
    card = make_card(cycle, member)

    assert can_view_card(member, card) is True
    assert can_edit_card(member, card) is True
    assert can_delete_card(member, card) is True

    assert can_view_card(other_member, card) is False
    assert can_edit_card(other_member, card) is False
    assert can_delete_card(other_member, card) is False

    close(cycle)
    card.refresh_from_db()
    # Reading your own card outlives the cycle; changing it does not.
    assert can_view_card(member, card) is True
    assert can_edit_card(member, card) is False
    assert can_delete_card(member, card) is False


@pytest.mark.django_db
def test_a_card_without_an_author_is_nobodys_to_read_or_change(
    cycle: FeedbackCycle, member: User
) -> None:
    card = make_card(cycle, member)
    card.author = None

    assert can_view_card(member, card) is False
    assert can_edit_card(member, card) is False
    assert can_delete_card(member, card) is False


def test_the_card_rules_live_in_the_one_permissions_module() -> None:
    """One module for the application's rules — #6 consolidated them there.

    The `# Rules` banner this app carried had #6 as its deletion date. What
    replaces it is `projects/permissions.py`; a `permissions.py` in this app
    would be the second permissions module the whole arrangement exists to
    prevent.
    """
    rules = (BASE_DIR / "projects" / "permissions.py").read_text()
    views = (BASE_DIR / "cycles" / "views.py").read_text()
    names = ["can_add_card", "can_view_card", "can_edit_card", "can_delete_card"]

    for name in names:
        assert f"def {name}(" in rules
        # This app asks them; it no longer defines them.
        assert f"def {name}(" not in views
        assert name in views

    assert "# Rules." not in views
    assert not (BASE_DIR / "cycles" / "permissions.py").exists()


# --------------------------------------------------------------------------
# Getting there
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cycle_page_links_to_your_cards(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cycle.get_absolute_url()).content.decode()

    assert cards_url(cycle) in html
    assert "Your cards for this week" in html
