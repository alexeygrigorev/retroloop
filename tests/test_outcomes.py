"""Decisions and action items: the models, the manual CRUD, and the freeze.

Every test here maps to an acceptance criterion of issue #17. It keeps the
board's disciplines:

**A refusal is proved by attempting it, with a valid CSRF token.** Every rejected
edit, delete and tick-off is posted through a client that enforces CSRF, carrying
a token that works, so a 403 from the middleware can never stand in for a refusal
the endpoint itself made.

**A refusal is asserted as nothing changed**, not merely as a status code: the
row is re-read and its text or status is shown to be exactly what it was.

**The frozen-after-COMPLETE boundary is proved on both sides.** After COMPLETE a
text edit and a delete are refused while a status flip succeeds, against the same
complete retrospective — because "frozen except this one field" is the kind of
rule that gets shipped as "frozen".

**No card is touched.** A decision and an action item reference a `cluster` (an
integer id the whole team made) and a user, never a `Card`, so there is no card
author, no card `pk` and no anonymity flag to leak — `_docs/decisions.md` items 9
and 10 are about `Card`, and these are not cards.
"""

from datetime import UTC, date, datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from cycles.models import FeedbackCycle
from projects.models import Membership, Project
from retro.models import ActionItem, Decision, Retrospective

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str, **extra) -> User:
    return User.objects.create_user(
        username=username, password=PASSWORD, display_name=display_name, **extra
    )


@pytest.fixture
def owner(db) -> User:
    """The project owner and this cycle's facilitator."""
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def ada(project: Project) -> User:
    user = make_user("ada", "Ada Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def bruno(project: Project) -> User:
    user = make_user("bruno", "Bruno Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    user = make_user("outsider", "Ora Outsider")
    elsewhere = Project.objects.create(name="Payments", owner=user)
    Membership.objects.create(project=elsewhere, user=user, role=Membership.Role.FACILITATOR)
    return user


def make_retro(project: Project, facilitator: User, *, stage: str) -> Retrospective:
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )
    return Retrospective.objects.create(cycle=cycle, stage=stage)


@pytest.fixture
def retro(project: Project, owner: User) -> Retrospective:
    """A retrospective in DISCUSS — the meeting is happening, outcomes are written."""
    return make_retro(project, owner, stage=Stage.DISCUSS)


def strict_client(user: User, retro: Retrospective) -> tuple[Client, str]:
    """A CSRF-enforcing client, logged in, plus a token that works.

    Getting the outcomes page sets the token cookie, so every refusal posted
    below carries a token the middleware accepts and a 403 can only come from the
    endpoint.
    """
    client = Client(enforce_csrf_checks=True)
    assert client.login(username=user.username, password=PASSWORD)
    assert client.get(reverse("retro-outcomes", args=[retro.pk])).status_code == 200
    return client, client.cookies["csrftoken"].value


def post(client: Client, token: str, url_name: str, pk: int, body: dict | None = None):
    return client.post(reverse(url_name, args=[pk]), body or {}, HTTP_X_CSRFTOKEN=token)


# --------------------------------------------------------------------------
# Models and defaults
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_manual_decision_is_manual_and_lands_confirmed(retro: Retrospective, owner: User) -> None:
    """Anything typed by hand is source=MANUAL and status=CONFIRMED — no review step."""
    client, token = strict_client(owner, retro)

    response = post(client, token, "decision-create", retro.pk, {"text": "Adopt trunk-based dev"})

    assert response.status_code == 302
    decision = Decision.objects.get()
    assert decision.source == Decision.Source.MANUAL
    assert decision.status == Decision.Status.CONFIRMED
    assert decision.created_by_id == owner.pk
    assert decision.text == "Adopt trunk-based dev"


@pytest.mark.django_db
def test_a_manual_action_item_is_manual_and_confirmed_and_open(
    retro: Retrospective, owner: User, ada: User
) -> None:
    client, token = strict_client(owner, retro)

    response = post(
        client,
        token,
        "action-item-create",
        retro.pk,
        {"description": "Write the runbook", "owner": ada.pk, "due_date": "2026-07-30"},
    )

    assert response.status_code == 302
    action = ActionItem.objects.get()
    assert action.source == ActionItem.Source.MANUAL
    assert action.review_status == ActionItem.ReviewStatus.CONFIRMED
    assert action.status == ActionItem.Status.OPEN
    assert action.owner_id == ada.pk
    assert action.created_by_id == owner.pk
    assert action.due_date == date(2026, 7, 30)


@pytest.mark.django_db
def test_any_project_member_can_write_a_decision_and_an_action_item(
    retro: Retrospective, ada: User
) -> None:
    """Not only the facilitator — any member records outcomes by hand."""
    client, token = strict_client(ada, retro)

    assert post(client, token, "decision-create", retro.pk, {"text": "A call"}).status_code == 302
    assert (
        post(client, token, "action-item-create", retro.pk, {"description": "A task"}).status_code
        == 302
    )
    assert Decision.objects.count() == 1
    assert ActionItem.objects.count() == 1


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_whitespace_only_text_is_rejected(retro: Retrospective, owner: User) -> None:
    client, token = strict_client(owner, retro)

    decision = post(client, token, "decision-create", retro.pk, {"text": "   "})
    action = post(client, token, "action-item-create", retro.pk, {"description": "  \n "})

    assert decision.status_code == 400
    assert action.status_code == 400
    assert Decision.objects.count() == 0
    assert ActionItem.objects.count() == 0


@pytest.mark.django_db
def test_an_owner_who_is_not_a_project_member_is_a_validation_error(
    retro: Retrospective, owner: User, outsider: User
) -> None:
    """An owner off the roster is refused before it becomes a row, not stored."""
    client, token = strict_client(owner, retro)

    response = post(
        client,
        token,
        "action-item-create",
        retro.pk,
        {"description": "Write the runbook", "owner": outsider.pk},
    )

    assert response.status_code == 400
    assert ActionItem.objects.count() == 0


@pytest.mark.django_db
def test_an_action_item_with_no_owner_is_allowed_and_shown(
    retro: Retrospective, owner: User
) -> None:
    """Unassigned is a legitimate state — displayed, never hidden."""
    client, token = strict_client(owner, retro)

    response = post(client, token, "action-item-create", retro.pk, {"description": "Unowned task"})

    assert response.status_code == 302
    action = ActionItem.objects.get()
    assert action.owner_id is None
    assert action.is_unassigned is True

    body = client.get(reverse("retro-outcomes", args=[retro.pk])).content.decode()
    assert 'data-unassigned="true"' in body
    assert "Unowned task" in body


@pytest.mark.django_db
def test_a_due_date_in_the_past_is_accepted_and_marked_overdue(
    retro: Retrospective, owner: User
) -> None:
    """Recording a date that has already slipped is legitimate; an open past item is overdue."""
    client, token = strict_client(owner, retro)

    response = post(
        client,
        token,
        "action-item-create",
        retro.pk,
        {"description": "Overdue thing", "due_date": "2020-01-01"},
    )

    assert response.status_code == 302
    action = ActionItem.objects.get()
    assert action.due_date == date(2020, 1, 1)
    assert action.is_overdue is True

    body = client.get(reverse("retro-outcomes", args=[retro.pk])).content.decode()
    assert 'data-overdue="true"' in body


# --------------------------------------------------------------------------
# Seeing
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_outcomes_are_visible_to_every_member_with_owner_due_and_status(
    retro: Retrospective, owner: User, ada: User, bruno: User
) -> None:
    Decision.objects.create(retrospective=retro, text="Adopt trunk-based dev", created_by=owner)
    ActionItem.objects.create(
        retrospective=retro,
        description="Write the runbook",
        owner=ada,
        due_date=date(2026, 7, 30),
        created_by=owner,
    )

    client = Client()
    assert client.login(username=bruno.username, password=PASSWORD)
    body = client.get(reverse("retro-outcomes", args=[retro.pk])).content.decode()

    assert "Adopt trunk-based dev" in body
    assert "Write the runbook" in body
    assert "Ada Member" in body  # owner display name
    assert "30 July 2026" in body  # due date
    assert "Open" in body  # status


@pytest.mark.django_db
def test_a_non_member_is_refused_the_outcomes_page_like_an_unused_id(
    retro: Retrospective, outsider: User
) -> None:
    client = Client()
    assert client.login(username=outsider.username, password=PASSWORD)

    assert client.get(reverse("retro-outcomes", args=[retro.pk])).status_code == 404


# --------------------------------------------------------------------------
# The tick box: owner or facilitator, and nobody else
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_owner_flips_their_own_action_item(retro: Retrospective, ada: User) -> None:
    action = ActionItem.objects.create(
        retrospective=retro, description="Write the runbook", owner=ada
    )
    client, token = strict_client(ada, retro)

    assert post(client, token, "action-item-toggle", action.pk).status_code == 302
    assert ActionItem.objects.get(pk=action.pk).status == ActionItem.Status.DONE

    assert post(client, token, "action-item-toggle", action.pk).status_code == 302
    assert ActionItem.objects.get(pk=action.pk).status == ActionItem.Status.OPEN


@pytest.mark.django_db
def test_the_facilitator_flips_any_action_item(
    retro: Retrospective, owner: User, ada: User
) -> None:
    action = ActionItem.objects.create(
        retrospective=retro, description="Write the runbook", owner=ada
    )
    client, token = strict_client(owner, retro)

    assert post(client, token, "action-item-toggle", action.pk).status_code == 302
    assert ActionItem.objects.get(pk=action.pk).status == ActionItem.Status.DONE


@pytest.mark.django_db
def test_a_member_who_is_neither_owner_nor_facilitator_cannot_flip_it(
    retro: Retrospective, ada: User, bruno: User
) -> None:
    """Proved with a valid token, and asserted as nothing changed — not just a code."""
    action = ActionItem.objects.create(
        retrospective=retro, description="Write the runbook", owner=ada
    )
    client, token = strict_client(bruno, retro)

    response = post(client, token, "action-item-toggle", action.pk)

    assert response.status_code == 403
    assert ActionItem.objects.get(pk=action.pk).status == ActionItem.Status.OPEN


# --------------------------------------------------------------------------
# Editing and deleting: author or facilitator, while not COMPLETE
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_author_edits_their_own_decision(retro: Retrospective, ada: User) -> None:
    decision = Decision.objects.create(retrospective=retro, text="First wording", created_by=ada)
    client, token = strict_client(ada, retro)

    response = post(client, token, "decision-edit", decision.pk, {"text": "Second wording"})

    assert response.status_code == 302
    assert Decision.objects.get(pk=decision.pk).text == "Second wording"


@pytest.mark.django_db
def test_a_member_who_is_not_the_author_cannot_edit_a_decision(
    retro: Retrospective, ada: User, bruno: User
) -> None:
    decision = Decision.objects.create(retrospective=retro, text="First wording", created_by=ada)
    client, token = strict_client(bruno, retro)

    response = post(client, token, "decision-edit", decision.pk, {"text": "Tampered"})

    assert response.status_code == 403
    assert Decision.objects.get(pk=decision.pk).text == "First wording"


@pytest.mark.django_db
def test_the_facilitator_deletes_any_manual_entry(
    retro: Retrospective, owner: User, ada: User
) -> None:
    decision = Decision.objects.create(retrospective=retro, text="A call", created_by=ada)
    action = ActionItem.objects.create(retrospective=retro, description="A task", created_by=ada)
    client, token = strict_client(owner, retro)

    assert post(client, token, "decision-delete", decision.pk).status_code == 302
    assert post(client, token, "action-item-delete", action.pk).status_code == 302
    assert Decision.objects.count() == 0
    assert ActionItem.objects.count() == 0


# --------------------------------------------------------------------------
# The freeze at COMPLETE: text frozen, tick box open
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_after_complete_decision_text_is_frozen(project: Project, owner: User) -> None:
    """A text edit and a delete are refused after COMPLETE — proved with a valid token."""
    retro = make_retro(project, owner, stage=Stage.COMPLETE)
    decision = Decision.objects.create(retrospective=retro, text="Settled", created_by=owner)
    client, token = strict_client(owner, retro)

    edit = post(client, token, "decision-edit", decision.pk, {"text": "Changed"})
    delete = post(client, token, "decision-delete", decision.pk)

    assert edit.status_code == 403
    assert delete.status_code == 403
    assert Decision.objects.get(pk=decision.pk).text == "Settled"


@pytest.mark.django_db
def test_after_complete_action_item_text_is_frozen_but_the_tick_box_is_not(
    project: Project, owner: User, ada: User
) -> None:
    """The subtle rule: a text edit is refused after COMPLETE, a status flip is not."""
    retro = make_retro(project, owner, stage=Stage.COMPLETE)
    action = ActionItem.objects.create(
        retrospective=retro, description="Write the runbook", owner=ada
    )
    client, token = strict_client(ada, retro)

    edit = post(client, token, "action-item-edit", action.pk, {"description": "Reworded"})
    delete = post(client, token, "action-item-delete", action.pk)

    assert edit.status_code == 403
    assert delete.status_code == 403
    assert ActionItem.objects.get(pk=action.pk).description == "Write the runbook"

    # The tick box outlives the retrospective: the owner still flips it.
    toggle = post(client, token, "action-item-toggle", action.pk)
    assert toggle.status_code == 302
    assert ActionItem.objects.get(pk=action.pk).status == ActionItem.Status.DONE
