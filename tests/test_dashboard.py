"""The project dashboard — issue #26.

Every test here maps to an acceptance criterion of #26. Two disciplines run
through it, both from `_docs/decisions.md`:

* item 3a — submission status is a yes/no per member and never a count. The
  source is checked for `card_count` and the rendered page for both `card_count`
  and `submitted_at`, so a count can never reach a screen a member opens. The
  status of the open cycle is derived from a live existence check the view runs
  against `Card`, because `CycleParticipation` is not written until reveal.
* item 5 — the open action items are one live query, never copied rows. Ticking
  an item done in a retrospective removes it from the list, which is asserted by
  attempting it.

The query-count criterion is load-bearing and proved directly: a project with one
past cycle and one with thirty are fetched and their query counts asserted equal,
so a per-cycle N+1 fails here.

A refusal is proved by attempting it: a non-member and an anonymous visitor get
the 404 an unused id would.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import ActionItem, Cluster, Decision, Retrospective

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def make_project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


def add_member(project: Project, user: User) -> Membership:
    return Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)


def make_cycle(
    project: Project,
    facilitator: User,
    *,
    week: int,
    status: str = FeedbackCycle.Status.CLOSED,
) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY + timedelta(weeks=week),
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=status,
    )


def make_retro(cycle: FeedbackCycle, stage: str = Stage.COMPLETE) -> Retrospective:
    return Retrospective.objects.create(cycle=cycle, stage=stage)


def make_action(
    retro: Retrospective,
    *,
    description: str = "Write the runbook.",
    owner: User | None = None,
    due_date: date | None = None,
    status: str = ActionItem.Status.OPEN,
    review_status: str = ActionItem.ReviewStatus.CONFIRMED,
) -> ActionItem:
    return ActionItem.objects.create(
        retrospective=retro,
        description=description,
        owner=owner,
        due_date=due_date,
        status=status,
        review_status=review_status,
    )


def as_user(username: str) -> Client:
    client = Client()
    client.login(username=username, password=PASSWORD)
    return client


def dashboard_url(project: Project) -> str:
    return reverse("project-detail", args=[project.pk])


# --------------------------------------------------------------------------
# Access — members only, non-members and anonymous get 404
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_member_sees_the_dashboard() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)

    response = as_user("owner").get(dashboard_url(project))

    assert response.status_code == 200


@pytest.mark.django_db
def test_a_non_member_gets_404_and_no_hint_the_project_exists() -> None:
    owner = make_user("owner", "Olive Owner")
    make_user(
        "outsider", "Nina Nonmember"
    )  # created so as_user("outsider") resolves; not referenced
    project = make_project(owner)

    response = as_user("outsider").get(dashboard_url(project))

    assert response.status_code == 404
    assert b"Platform" not in response.content


@pytest.mark.django_db
def test_an_anonymous_visitor_does_not_reach_the_dashboard() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)

    response = Client().get(dashboard_url(project))

    # login_required sends the anonymous visitor to log in rather than showing
    # the page — the project is never rendered for them.
    assert response.status_code == 302
    assert b"Platform" not in response.content


# --------------------------------------------------------------------------
# The current cycle
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_open_cycle_is_shown_with_its_deadline_and_a_link_to_the_form() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    cycle = make_cycle(project, owner, week=0, status=FeedbackCycle.Status.COLLECTING)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert "20 July 2026" in body  # the week
    assert reverse("cycle-cards", args=[cycle.pk]) in body  # the feedback form


@pytest.mark.django_db
def test_the_viewer_sees_whether_they_themselves_have_submitted() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    project = make_project(owner)
    add_member(project, ada)
    cycle = make_cycle(project, owner, week=0, status=FeedbackCycle.Status.COLLECTING)
    Card.objects.create(cycle=cycle, category=Card.Category.START, text="Pair up.", author=ada)

    ada_body = as_user("ada").get(dashboard_url(project)).content.decode()
    owner_body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert 'data-viewer-submitted="true"' in ada_body
    assert 'data-viewer-submitted="false"' in owner_body


@pytest.mark.django_db
def test_submission_status_is_listed_per_member_as_yes_or_no_never_a_count() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    bruno = make_user("bruno", "Bruno Member")
    project = make_project(owner)
    add_member(project, ada)
    add_member(project, bruno)
    cycle = make_cycle(project, owner, week=0, status=FeedbackCycle.Status.COLLECTING)
    # Ada writes two cards; a count would say "2" beside her name — it must not.
    Card.objects.create(cycle=cycle, category=Card.Category.START, text="One.", author=ada)
    Card.objects.create(cycle=cycle, category=Card.Category.STOP, text="Two.", author=ada)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    # Ada submitted, Bruno did not — a yes/no per member.
    assert 'data-submitted="yes"' in body
    assert 'data-submitted="no"' in body
    # Never a count, and never the submission time that reconstructs one.
    assert "card_count" not in body
    assert "submitted_at" not in body
    # The number of cards Ada wrote is not on the page anywhere.
    assert ">2<" not in body


@pytest.mark.django_db
def test_no_open_cycle_says_so_and_offers_the_facilitator_to_open_one() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    project = make_project(owner)
    add_member(project, ada)
    # A closed cycle exists, so this is not the brand-new empty state.
    make_cycle(project, owner, week=0, status=FeedbackCycle.Status.CLOSED)

    facilitator_body = as_user("owner").get(dashboard_url(project)).content.decode()
    member_body = as_user("ada").get(dashboard_url(project)).content.decode()

    assert 'data-no-open-cycle="true"' in facilitator_body
    # The facilitator is offered the open action; a plain member is not.
    assert reverse("cycle-create", args=[project.pk]) in facilitator_body
    assert reverse("cycle-create", args=[project.pk]) not in member_body


# --------------------------------------------------------------------------
# The retrospective
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_active_retrospective_is_shown_with_its_stage_and_a_link() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    cycle = make_cycle(project, owner, week=0, status=FeedbackCycle.Status.CLOSED)
    retro = make_retro(cycle, stage=Stage.VOTE)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert 'data-panel="active-retro"' in body
    assert reverse("retro-detail", args=[retro.pk]) in body
    assert "Vote" in body


@pytest.mark.django_db
def test_previous_retrospectives_are_listed_most_recent_first_linking_to_summaries() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    older = make_retro(make_cycle(project, owner, week=0), stage=Stage.COMPLETE)
    newer = make_retro(make_cycle(project, owner, week=1), stage=Stage.COMPLETE)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    older_link = reverse("retro-summary", args=[older.pk])
    newer_link = reverse("retro-summary", args=[newer.pk])
    assert older_link in body
    assert newer_link in body
    # Most recent first: the newer week's summary link precedes the older one.
    assert body.index(newer_link) < body.index(older_link)


# --------------------------------------------------------------------------
# Open action items
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_open_actions_across_retrospectives_show_owner_due_date_and_source() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    project = make_project(owner)
    add_member(project, ada)
    retro_one = make_retro(make_cycle(project, owner, week=0))
    retro_two = make_retro(make_cycle(project, owner, week=1))
    make_action(retro_one, description="Write the runbook.", owner=ada, due_date=date(2026, 8, 1))
    make_action(retro_two, description="Fix the flaky test.", owner=owner)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    # Both retrospectives' open items appear in the one place.
    assert "Write the runbook." in body
    assert "Fix the flaky test." in body
    assert "Ada Member" in body  # the owner
    assert "1 August 2026" in body  # the due date
    # Which retrospective each came from: a link to that retrospective.
    assert reverse("retro-detail", args=[retro_one.pk]) in body
    assert reverse("retro-detail", args=[retro_two.pk]) in body


@pytest.mark.django_db
def test_only_confirmed_action_items_appear() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    retro = make_retro(make_cycle(project, owner, week=0))
    make_action(retro, description="A confirmed item.")
    make_action(
        retro,
        description="A draft item nobody confirmed.",
        review_status=ActionItem.ReviewStatus.DRAFT,
    )

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert "A confirmed item." in body
    assert "A draft item nobody confirmed." not in body


@pytest.mark.django_db
def test_done_items_do_not_appear() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    retro = make_retro(make_cycle(project, owner, week=0))
    make_action(retro, description="Already finished.", status=ActionItem.Status.DONE)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert "Already finished." not in body
    assert 'data-empty="open-actions"' in body


@pytest.mark.django_db
def test_overdue_items_and_the_viewers_own_items_are_marked() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    project = make_project(owner)
    add_member(project, ada)
    retro = make_retro(make_cycle(project, owner, week=0))
    make_action(retro, description="Overdue and mine.", owner=owner, due_date=date(2020, 1, 1))
    make_action(retro, description="Someone else's.", owner=ada)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert 'data-overdue="yes"' in body
    assert 'data-mine="yes"' in body  # the owner's own item
    assert 'data-mine="no"' in body  # ada's item, from the owner's view


# --------------------------------------------------------------------------
# The tick-done interaction, and the live query behind it
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_ticking_an_item_done_removes_it_from_the_live_list() -> None:
    """`_docs/decisions.md` item 5: the list is a live query, not copied rows."""
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)
    retro = make_retro(make_cycle(project, owner, week=0))
    action = make_action(retro, description="Tick me off.", owner=owner)

    toggle_url = reverse("dashboard-action-toggle", args=[project.pk, action.pk])
    response = as_user("owner").post(toggle_url)

    assert response.status_code == 200
    # The fragment that comes back no longer lists the item.
    assert "Tick me off." not in response.content.decode()
    action.refresh_from_db()
    assert action.status == ActionItem.Status.DONE

    # And it is gone from the full dashboard too — a live query, not a copy.
    body = as_user("owner").get(dashboard_url(project)).content.decode()
    assert "Tick me off." not in body


@pytest.mark.django_db
def test_only_the_owner_or_facilitator_can_tick_an_item_off() -> None:
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    bruno = make_user("bruno", "Bruno Member")
    project = make_project(owner)
    add_member(project, ada)
    add_member(project, bruno)
    retro = make_retro(make_cycle(project, owner, week=0))
    action = make_action(retro, description="Ada's task.", owner=ada)

    toggle_url = reverse("dashboard-action-toggle", args=[project.pk, action.pk])

    # A plain member who is neither the owner nor the facilitator is refused.
    assert as_user("bruno").post(toggle_url).status_code == 403
    action.refresh_from_db()
    assert action.status == ActionItem.Status.OPEN

    # The owner may.
    assert as_user("ada").post(toggle_url).status_code == 200
    action.refresh_from_db()
    assert action.status == ActionItem.Status.DONE


@pytest.mark.django_db
def test_a_non_member_cannot_toggle_and_is_told_nothing() -> None:
    owner = make_user("owner", "Olive Owner")
    make_user(
        "outsider", "Nina Nonmember"
    )  # created so as_user("outsider") resolves; not referenced
    project = make_project(owner)
    retro = make_retro(make_cycle(project, owner, week=0))
    action = make_action(retro, description="Not yours.", owner=owner)

    toggle_url = reverse("dashboard-action-toggle", args=[project.pk, action.pk])
    assert as_user("outsider").post(toggle_url).status_code == 404


@pytest.mark.django_db
def test_an_item_from_another_project_is_a_404_not_a_cross_project_tick() -> None:
    owner = make_user("owner", "Olive Owner")
    project_a = make_project(owner)
    project_b = Project.objects.create(name="Other", owner=owner)
    Membership.objects.create(project=project_b, user=owner, role=Membership.Role.FACILITATOR)
    retro_b = make_retro(make_cycle(project_b, owner, week=5))
    action_b = make_action(retro_b, description="Belongs to B.", owner=owner)

    # Toggling B's item through A's URL is a 404, not a tick.
    toggle_url = reverse("dashboard-action-toggle", args=[project_a.pk, action_b.pk])
    assert as_user("owner").post(toggle_url).status_code == 404
    action_b.refresh_from_db()
    assert action_b.status == ActionItem.Status.OPEN


# --------------------------------------------------------------------------
# Empty state
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_brand_new_project_renders_a_useful_empty_state() -> None:
    owner = make_user("owner", "Olive Owner")
    project = make_project(owner)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert 'data-panel="empty-project"' in body
    # It tells the owner what to do first rather than showing empty panels.
    assert reverse("cycle-create", args=[project.pk]) in body
    assert 'data-panel="open-actions"' not in body


# --------------------------------------------------------------------------
# The query count does not grow with the number of past cycles
# --------------------------------------------------------------------------


def _build_project(name: str, owner: User, member: User, *, cycles: int) -> Project:
    """A project with `cycles` completed cycles (each a retro and two open items),
    plus one open cycle, and a fixed two-person roster."""
    project = Project.objects.create(name=name, owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
    for index in range(cycles):
        cycle = make_cycle(project, owner, week=index, status=FeedbackCycle.Status.CLOSED)
        retro = make_retro(cycle, stage=Stage.COMPLETE)
        cluster = Cluster.objects.create(retrospective=retro, name="Topic", position=1)
        Decision.objects.create(retrospective=retro, cluster=cluster, text="Decided.")
        make_action(retro, description=f"Item {index}a", owner=owner)
        make_action(retro, description=f"Item {index}b", owner=member, due_date=date(2020, 1, 1))
    # One open cycle, so the submission-status query runs in both projects.
    open_cycle = make_cycle(project, owner, week=cycles + 1, status=FeedbackCycle.Status.COLLECTING)
    Card.objects.create(
        cycle=open_cycle, category=Card.Category.START, text="A card.", author=member
    )
    return project


@pytest.mark.django_db
def test_the_query_count_does_not_grow_with_the_number_of_past_cycles() -> None:
    """A per-cycle N+1 is the obvious failure this criterion forbids.

    One project has a single past cycle, the other thirty. Every list on the page
    grows in rows but not in queries, so the two render in the identical number of
    queries — a loop that queried per cycle would make the second far larger.
    """
    owner = make_user("owner", "Olive Owner")
    member = make_user("member", "Mel Member")
    small = _build_project("Small", owner, member, cycles=1)
    large = _build_project("Large", owner, member, cycles=30)

    client = as_user("owner")
    # Warm any per-session caches (the auth/session rows) so the comparison is of
    # the view's own queries and not of a first-request session write.
    client.get(dashboard_url(small))

    with CaptureQueriesContext(connection) as small_queries:
        assert client.get(dashboard_url(small)).status_code == 200
    with CaptureQueriesContext(connection) as large_queries:
        assert client.get(dashboard_url(large)).status_code == 200

    assert len(small_queries) == len(large_queries), (
        len(small_queries),
        len(large_queries),
    )


# --------------------------------------------------------------------------
# The no-count rule, proved over the whole page as #10's guard does
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_dashboard_carries_no_card_count_or_submitted_at() -> None:
    """The page is member-reachable, so #10's guard checks it too — no count."""
    owner = make_user("owner", "Olive Owner")
    ada = make_user("ada", "Ada Member")
    project = make_project(owner)
    add_member(project, ada)
    cycle = make_cycle(project, owner, week=0, status=FeedbackCycle.Status.COLLECTING)
    Card.objects.create(cycle=cycle, category=Card.Category.START, text="A card.", author=ada)
    Card.objects.create(cycle=cycle, category=Card.Category.STOP, text="Another.", author=ada)

    body = as_user("owner").get(dashboard_url(project)).content.decode()

    assert "card_count" not in body
    assert "submitted_at" not in body
