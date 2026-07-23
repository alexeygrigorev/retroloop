"""Feedback cycles: opening one, closing one, and who may do either.

Every test here maps to an acceptance criterion of issue #7. Two themes run
through them. The first is that the database, not a form, is what makes "one
open cycle per project" true — the constraint tests insert straight through the
ORM to prove it. The second is that a control being hidden is never the rule:
where a member must not be able to do something, the test posts as that member
and asserts the server refused, and separately asserts the control is absent
from the page.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from cycles.models import FeedbackCycle, monday_of
from projects.models import Membership, Project

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

#: A Monday, and the Wednesday of the same week.
MONDAY = date(2026, 7, 20)
WEDNESDAY = date(2026, 7, 22)
MONDAY_LABEL = "20 July 2026"

OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def make_project(owner: User, name: str = "Platform") -> Project:
    project = Project.objects.create(name=name, owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


def make_cycle(project: Project, facilitator: User, week_start: date = MONDAY, **kwargs):
    return FeedbackCycle.objects.create(
        project=project,
        week_start=week_start,
        opens_at=kwargs.pop("opens_at", OPENS_AT),
        closes_at=kwargs.pop("closes_at", CLOSES_AT),
        facilitator=facilitator,
        **kwargs,
    )


def log_in(client: Client, user: User) -> None:
    client.login(username=user.username, password=PASSWORD)


def create_url(project: Project) -> str:
    return reverse("cycle-create", args=[project.pk])


def close_url(cycle: FeedbackCycle) -> str:
    return reverse("cycle-close", args=[cycle.pk])


def detail_url(cycle: FeedbackCycle) -> str:
    return reverse("cycle-detail", args=[cycle.pk])


def form_data(facilitator: User, **overrides) -> dict:
    data = {
        "week_start": "2026-07-20",
        "opens_at": "2026-07-20T09:00",
        "closes_at": "2026-07-24T17:00",
        "facilitator": str(facilitator.pk),
    }
    data.update(overrides)
    return data


@pytest.fixture
def owner(db) -> User:
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    return make_project(owner)


@pytest.fixture
def facilitator(project: Project) -> User:
    """A facilitator who is not the owner, so the two rules stay distinguishable."""
    user = make_user("facilitator", "Fay Facilitator")
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Mel Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_cycle_starts_out_collecting(project: Project, owner: User) -> None:
    cycle = make_cycle(project, owner)

    assert cycle.status == FeedbackCycle.Status.COLLECTING
    assert cycle.is_collecting is True
    assert cycle.facilitator == owner
    assert cycle.project == project


@pytest.mark.django_db
def test_a_project_cannot_hold_two_collecting_cycles(project: Project, owner: User) -> None:
    """The partial unique index, not a form check, is what refuses the second one."""
    make_cycle(project, owner, week_start=MONDAY)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_cycle(project, owner, week_start=MONDAY - timedelta(days=7))


@pytest.mark.django_db
def test_a_closed_cycle_leaves_room_for_the_next_one(project: Project, owner: User) -> None:
    """The index is partial: only `COLLECTING` rows collide."""
    first = make_cycle(project, owner, week_start=MONDAY - timedelta(days=7))
    first.status = FeedbackCycle.Status.CLOSED
    first.save(update_fields=["status"])

    second = make_cycle(project, owner, week_start=MONDAY)

    assert FeedbackCycle.objects.filter(project=project).count() == 2
    assert second.is_collecting


@pytest.mark.django_db
def test_one_project_open_cycle_does_not_block_another_project(owner: User) -> None:
    first = make_project(owner, "Platform")
    second = make_project(owner, "Payments")

    make_cycle(first, owner)
    make_cycle(second, owner)

    assert FeedbackCycle.objects.filter(status=FeedbackCycle.Status.COLLECTING).count() == 2


@pytest.mark.django_db
def test_a_project_cannot_hold_two_cycles_for_one_week(project: Project, owner: User) -> None:
    first = make_cycle(project, owner, week_start=MONDAY)
    first.status = FeedbackCycle.Status.CLOSED
    first.save(update_fields=["status"])

    with pytest.raises(IntegrityError), transaction.atomic():
        make_cycle(project, owner, week_start=MONDAY)


@pytest.mark.django_db
def test_week_start_is_stored_as_the_monday_of_that_week(project: Project, owner: User) -> None:
    cycle = make_cycle(project, owner, week_start=WEDNESDAY)
    cycle.refresh_from_db()

    assert cycle.week_start == MONDAY


def test_monday_of_leaves_a_monday_alone() -> None:
    assert monday_of(MONDAY) == MONDAY
    assert monday_of(date(2026, 7, 26)) == MONDAY  # the Sunday that ends the week


# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_owner_can_open_a_cycle(client: Client, project: Project, owner: User) -> None:
    log_in(client, owner)

    response = client.post(create_url(project), form_data(owner))

    cycle = FeedbackCycle.objects.get(project=project)
    assert response.status_code == 302
    assert response.headers["Location"] == detail_url(cycle)
    assert cycle.status == FeedbackCycle.Status.COLLECTING
    assert cycle.facilitator == owner


@pytest.mark.django_db
def test_a_facilitator_who_is_not_the_owner_can_open_a_cycle(
    client: Client, project: Project, facilitator: User
) -> None:
    log_in(client, facilitator)

    response = client.post(create_url(project), form_data(facilitator))

    assert response.status_code == 302
    assert FeedbackCycle.objects.filter(project=project, facilitator=facilitator).exists()


@pytest.mark.django_db
def test_a_plain_member_cannot_open_a_cycle(client: Client, project: Project, member: User) -> None:
    """The form page and a direct POST are both refused, and nothing is written."""
    log_in(client, member)

    assert client.get(create_url(project)).status_code == 403
    assert client.post(create_url(project), form_data(member)).status_code == 403
    assert not FeedbackCycle.objects.exists()


@pytest.mark.django_db
def test_a_plain_member_is_not_shown_the_open_a_cycle_link(
    client: Client, project: Project, member: User
) -> None:
    log_in(client, member)

    response = client.get(project.get_absolute_url())
    html = response.content.decode()

    assert response.status_code == 200
    assert "Open a feedback cycle" not in html
    assert create_url(project) not in html


@pytest.mark.django_db
def test_a_facilitator_is_shown_the_open_a_cycle_link(
    client: Client, project: Project, facilitator: User
) -> None:
    log_in(client, facilitator)

    html = client.get(project.get_absolute_url()).content.decode()

    # The dashboard (#26) labels the button for its context: "Open the first
    # cycle" on a brand-new project, "Open a feedback cycle" once the project
    # has cycles but none is collecting. Either way the facilitator is offered
    # the create form; the link to it is the guarantee this test protects.
    assert "Open the first cycle" in html or "Open a feedback cycle" in html
    assert create_url(project) in html


@pytest.mark.django_db
def test_a_non_member_gets_404_from_the_opening_form(
    client: Client, project: Project, outsider: User
) -> None:
    log_in(client, outsider)

    assert client.get(create_url(project)).status_code == 404


@pytest.mark.django_db
def test_the_form_defaults_the_facilitator_to_whoever_is_opening_the_cycle(
    client: Client, project: Project, facilitator: User, member: User, outsider: User
) -> None:
    log_in(client, facilitator)

    html = client.get(create_url(project)).content.decode()

    assert f'value="{facilitator.pk}" selected' in html
    # Any member may be picked instead — handing the role over for one week is
    # allowed — but nobody outside the project is on the list at all.
    assert f'value="{member.pk}"' in html
    assert f'value="{outsider.pk}"' not in html
    assert str(outsider) not in html


@pytest.mark.django_db
def test_the_facilitator_role_can_be_handed_to_another_member(
    client: Client, project: Project, owner: User, member: User
) -> None:
    log_in(client, owner)

    response = client.post(create_url(project), form_data(member))

    assert response.status_code == 302
    assert FeedbackCycle.objects.get(project=project).facilitator == member


@pytest.mark.django_db
def test_a_facilitator_from_outside_the_project_is_a_validation_error(
    client: Client, project: Project, owner: User, outsider: User
) -> None:
    """A rejected choice, not a 500 and not a saved row."""
    log_in(client, owner)

    response = client.post(create_url(project), form_data(outsider))

    assert response.status_code == 200
    assert response.context["form"].errors["facilitator"]
    assert not FeedbackCycle.objects.exists()


@pytest.mark.django_db
def test_opening_a_second_cycle_names_the_one_already_open(
    client: Client, project: Project, owner: User
) -> None:
    make_cycle(project, owner, week_start=MONDAY)
    log_in(client, owner)

    response = client.post(create_url(project), form_data(owner, week_start="2026-07-27"))
    html = response.content.decode()

    assert response.status_code == 200
    assert MONDAY_LABEL in html
    assert "already has an open cycle" in html
    assert FeedbackCycle.objects.count() == 1


@pytest.mark.django_db
def test_closing_before_opening_is_a_validation_error(
    client: Client, project: Project, owner: User
) -> None:
    log_in(client, owner)

    response = client.post(
        create_url(project),
        form_data(owner, opens_at="2026-07-24T17:00", closes_at="2026-07-20T09:00"),
    )

    assert response.status_code == 200
    assert response.context["form"].errors["closes_at"]
    assert not FeedbackCycle.objects.exists()


@pytest.mark.django_db
def test_a_week_named_by_any_of_its_days_is_saved_as_its_monday(
    client: Client, project: Project, owner: User
) -> None:
    log_in(client, owner)

    response = client.post(create_url(project), form_data(owner, week_start="2026-07-22"))

    assert response.status_code == 302
    assert FeedbackCycle.objects.get(project=project).week_start == MONDAY


@pytest.mark.django_db
def test_a_second_cycle_for_a_week_already_covered_is_a_validation_error(
    client: Client, project: Project, owner: User
) -> None:
    """The closed cycle for that week is in the way; the form says so, Postgres does not."""
    closed = make_cycle(project, owner, week_start=MONDAY)
    closed.status = FeedbackCycle.Status.CLOSED
    closed.save(update_fields=["status"])
    log_in(client, owner)

    response = client.post(create_url(project), form_data(owner, week_start="2026-07-22"))

    assert response.status_code == 200
    assert response.context["form"].errors["week_start"]
    assert FeedbackCycle.objects.count() == 1


# --------------------------------------------------------------------------
# Closing
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cycles_facilitator_closes_it(client: Client, project: Project, owner: User) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, owner)

    response = client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert response.status_code == 302
    assert cycle.status == FeedbackCycle.Status.CLOSED


@pytest.mark.django_db
def test_nobody_who_did_not_submit_blocks_the_close(
    client: Client, project: Project, owner: User, member: User
) -> None:
    """Decision 4: the facilitator's schedule, not full attendance."""
    cycle = make_cycle(project, owner)
    assert project.memberships.count() == 2  # and not one card between them
    log_in(client, owner)

    client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert cycle.status == FeedbackCycle.Status.CLOSED


@pytest.mark.django_db
def test_a_plain_member_cannot_close_the_cycle(
    client: Client, project: Project, owner: User, member: User
) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, member)

    response = client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert response.status_code == 403
    assert cycle.status == FeedbackCycle.Status.COLLECTING


@pytest.mark.django_db
def test_a_project_facilitator_who_is_not_this_cycles_facilitator_cannot_close_it(
    client: Client, project: Project, owner: User, facilitator: User
) -> None:
    """Authority over a cycle is per cycle, not read off the Membership row."""
    cycle = make_cycle(project, owner)
    log_in(client, facilitator)

    response = client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert response.status_code == 403
    assert cycle.status == FeedbackCycle.Status.COLLECTING


@pytest.mark.django_db
def test_a_non_member_gets_404_from_the_close_endpoint(
    client: Client, project: Project, owner: User, outsider: User
) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, outsider)

    response = client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert response.status_code == 404
    assert cycle.status == FeedbackCycle.Status.COLLECTING


@pytest.mark.django_db
def test_a_closed_cycle_cannot_be_closed_again_or_reopened(
    client: Client, project: Project, owner: User
) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, owner)
    client.post(close_url(cycle))

    second_attempt = client.post(close_url(cycle))
    cycle.refresh_from_db()

    assert second_attempt.status_code == 403
    assert cycle.status == FeedbackCycle.Status.CLOSED
    # There is no route back either: no reopen endpoint exists to be posted to.
    with pytest.raises(NoReverseMatch):
        reverse("cycle-reopen", args=[cycle.pk])


@pytest.mark.django_db
def test_the_closed_cycle_page_offers_neither_closing_nor_reopening(
    client: Client, project: Project, owner: User
) -> None:
    cycle = make_cycle(project, owner, status=FeedbackCycle.Status.CLOSED)
    log_in(client, owner)

    html = client.get(detail_url(cycle)).content.decode()

    assert "Close the cycle" not in html
    assert "Reopen" not in html
    assert close_url(cycle) not in html


@pytest.mark.django_db
def test_a_member_who_is_not_the_facilitator_is_not_shown_the_close_button(
    client: Client, project: Project, owner: User, member: User
) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, member)

    html = client.get(detail_url(cycle)).content.decode()

    assert "Close the cycle" not in html
    assert close_url(cycle) not in html


@pytest.mark.django_db
def test_the_facilitator_is_shown_the_close_button(
    client: Client, project: Project, owner: User
) -> None:
    cycle = make_cycle(project, owner)
    log_in(client, owner)

    html = client.get(detail_url(cycle)).content.decode()

    assert "Close the cycle" in html
    assert close_url(cycle) in html


@pytest.mark.django_db
def test_a_cycle_past_its_deadline_is_still_collecting(
    client: Client, project: Project, owner: User
) -> None:
    """There is no scheduler: `closes_at` is a deadline shown to people, not a timer."""
    now = timezone.now()
    cycle = make_cycle(
        project,
        owner,
        opens_at=now - timedelta(days=10),
        closes_at=now - timedelta(days=3),
    )
    log_in(client, owner)

    html = client.get(detail_url(cycle)).content.decode()
    cycle.refresh_from_db()

    assert cycle.status == FeedbackCycle.Status.COLLECTING
    assert 'data-cycle-status="COLLECTING"' in html
    # And a human still has something to click, because only a human closes it.
    assert "Close the cycle" in html


@pytest.mark.django_db
def test_a_closed_cycle_takes_no_cards(project: Project, owner: User) -> None:
    """The status is the whole rule #8's card views ask about, and #6 later lifts."""
    cycle = make_cycle(project, owner)
    assert cycle.accepts_cards is True

    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])
    cycle.refresh_from_db()

    assert cycle.accepts_cards is False


@pytest.mark.django_db
def test_the_closed_cycle_page_says_nothing_more_can_be_added(
    client: Client, project: Project, owner: User
) -> None:
    cycle = make_cycle(project, owner, status=FeedbackCycle.Status.CLOSED)
    log_in(client, owner)

    html = client.get(detail_url(cycle)).content.decode()

    assert "This cycle is closed." in html


# --------------------------------------------------------------------------
# Seeing
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_project_page_lists_its_cycles_most_recent_first(
    client: Client, project: Project, owner: User
) -> None:
    older = make_cycle(project, owner, week_start=MONDAY - timedelta(days=7))
    older.status = FeedbackCycle.Status.CLOSED
    older.save(update_fields=["status"])
    make_cycle(project, owner, week_start=MONDAY)
    log_in(client, owner)

    html = client.get(project.get_absolute_url()).content.decode()

    assert "Collecting" in html
    assert "Closed" in html
    assert MONDAY_LABEL in html
    assert "13 July 2026" in html
    assert html.index(MONDAY_LABEL) < html.index("13 July 2026")


@pytest.mark.django_db
def test_a_project_without_cycles_says_so(client: Client, project: Project, owner: User) -> None:
    log_in(client, owner)

    html = client.get(project.get_absolute_url()).content.decode()

    assert "No cycle has been opened for this project yet." in html


@pytest.mark.django_db
def test_a_member_can_see_a_cycle(client: Client, project: Project, owner: User, member: User):
    cycle = make_cycle(project, owner)
    log_in(client, member)

    response = client.get(detail_url(cycle))

    assert response.status_code == 200
    assert MONDAY_LABEL in response.content.decode()


@pytest.mark.django_db
def test_a_non_member_gets_404_for_a_cycle(
    client: Client, project: Project, owner: User, outsider: User
) -> None:
    """404 and not 403, so the answer says nothing about whether the cycle exists."""
    cycle = make_cycle(project, owner)
    log_in(client, outsider)

    assert client.get(detail_url(cycle)).status_code == 404


@pytest.mark.django_db
def test_the_cycle_pages_need_a_login(client: Client, project: Project, owner: User) -> None:
    cycle = make_cycle(project, owner)

    for url in (create_url(project), detail_url(cycle), close_url(cycle)):
        response = client.get(url)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/accounts/login/")
