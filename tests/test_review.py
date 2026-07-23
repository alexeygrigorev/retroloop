"""Draft review and confirmation: the screen, its transitions, and its refusals.

Every test here maps to an acceptance criterion of issue #24. It keeps the
disciplines the outcomes tests (#17) established:

**A refusal is proved by attempting it, with a valid CSRF token.** A plain member
and a non-member post the review actions through a client that enforces CSRF,
carrying a token that works, so the 404 they get is the endpoint's own answer and
never a CSRF 403 standing in for it.

**A refusal is asserted as nothing changed** — the draft is re-read and shown to
be exactly what it was, not merely a status code.

**No card is touched.** A draft points at a `cluster` (an integer id) or nothing
and names an `owner` or nobody; its excerpt is transcript text. There is no card
author, no card `pk` and no anonymity flag on this screen — `_docs/decisions.md`
items 9 and 10 are about `Card`, and a draft is not one.

**The invisibility of a draft is asserted on the surface that renders it.** #17's
outcomes screen is the one built surface that lists decisions and action items;
a DRAFT row must not appear on it. The summary (#25) and the dashboard (#26) are
not built yet, so there is no surface of theirs to assert against here.
"""

from datetime import date

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

Stage = Retrospective.Stage


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


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
def outsider(db) -> User:
    """A member of a different project — a non-member here."""
    user = make_user("outsider", "Ora Outsider")
    elsewhere = Project.objects.create(name="Payments", owner=user)
    Membership.objects.create(project=elsewhere, user=user, role=Membership.Role.FACILITATOR)
    return user


def make_retro(project: Project, facilitator: User, *, stage: str) -> Retrospective:
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at="2026-07-20T09:00Z",
        closes_at="2026-07-24T17:00Z",
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )
    return Retrospective.objects.create(cycle=cycle, stage=stage)


@pytest.fixture
def retro(project: Project, owner: User) -> Retrospective:
    """A retrospective in DISCUSS — the meeting is done and the drafts are in review."""
    return make_retro(project, owner, stage=Stage.DISCUSS)


def draft_decision(retro: Retrospective, **extra) -> Decision:
    """One extracted decision, still in review — exactly what #23 writes."""
    fields = {
        "text": "Ship smaller pull requests.",
        "excerpt": "We kept blocking on huge PRs, so we should ship smaller ones.",
        "source": Decision.Source.EXTRACTED,
        "status": Decision.Status.DRAFT,
    }
    fields.update(extra)
    return Decision.objects.create(retrospective=retro, **fields)


def draft_action_item(retro: Retrospective, **extra) -> ActionItem:
    """One extracted action item, still in review, unassigned unless told otherwise."""
    fields = {
        "description": "Split the deploy PR before Thursday.",
        "excerpt": "Someone should split that deploy PR before Thursday.",
        "owner": None,
        "source": ActionItem.Source.EXTRACTED,
        "review_status": ActionItem.ReviewStatus.DRAFT,
        "status": ActionItem.Status.OPEN,
    }
    fields.update(extra)
    return ActionItem.objects.create(retrospective=retro, **fields)


def strict_client(user: User, get_url: str) -> tuple[Client, str]:
    """A CSRF-enforcing client, logged in, plus a token that works.

    The token comes from a GET of `get_url`, a page this user may open that
    renders `{% csrf_token %}` and so sets the cookie — so every refusal posted
    below carries a token the middleware accepts and a 403 can only come from the
    endpoint itself.
    """
    client = Client(enforce_csrf_checks=True)
    assert client.login(username=user.username, password=PASSWORD)
    assert client.get(get_url).status_code == 200
    return client, client.cookies["csrftoken"].value


def post(client: Client, token: str, url_name: str, args: list, body: dict | None = None):
    return client.post(reverse(url_name, args=args), body or {}, HTTP_X_CSRFTOKEN=token)


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_screen_lists_every_draft_with_its_excerpt(retro: Retrospective, owner: User) -> None:
    decision = draft_decision(retro)
    item = draft_action_item(retro)

    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert decision.text in body
    assert item.description in body
    # The supporting excerpt from the transcript is shown beside each draft.
    assert decision.excerpt in body
    assert item.excerpt in body


@pytest.mark.django_db
def test_only_extracted_drafts_appear_not_confirmed_or_manual_rows(
    retro: Retrospective, owner: User
) -> None:
    """The screen is for what is still in review, not the whole outcome list."""
    draft_decision(retro, text="A draft awaiting review.")
    Decision.objects.create(
        retrospective=retro,
        text="Already confirmed by hand.",
        source=Decision.Source.MANUAL,
        status=Decision.Status.CONFIRMED,
    )
    Decision.objects.create(
        retrospective=retro,
        text="Extracted and already accepted.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.CONFIRMED,
    )

    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert "A draft awaiting review." in body
    assert "Already confirmed by hand." not in body
    assert "Extracted and already accepted." not in body


@pytest.mark.django_db
def test_with_nothing_extracted_the_page_says_so(retro: Retrospective, owner: User) -> None:
    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert 'data-empty="review"' in body


@pytest.mark.django_db
def test_an_unassigned_draft_is_flagged_and_offers_an_owner_dropdown(
    retro: Retrospective, owner: User, ada: User
) -> None:
    draft_action_item(retro, owner=None)

    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert 'data-unassigned="true"' in body
    assert "data-owner-picker" in body
    # Its options are the project members, by display name.
    assert "Ada Member" in body


# --------------------------------------------------------------------------
# Facilitator only: member, non-member and anonymous all get 404
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_plain_member_is_refused_the_screen_and_every_action(
    retro: Retrospective, owner: User, ada: User
) -> None:
    """Proved with a valid token, and asserted as nothing changed — not just a code."""
    decision = draft_decision(retro)
    item = draft_action_item(retro)
    # A page the member may open, to mint a valid CSRF token.
    client, token = strict_client(ada, reverse("retro-outcomes", args=[retro.pk]))

    assert client.get(reverse("retro-review", args=[retro.pk])).status_code == 404
    assert post(client, token, "review-decision-accept", [retro.pk, decision.pk]).status_code == 404
    assert post(client, token, "review-action-item-accept", [retro.pk, item.pk]).status_code == 404
    assert post(client, token, "review-accept-all", [retro.pk]).status_code == 404

    # Nothing moved: the drafts are still drafts.
    assert Decision.objects.get(pk=decision.pk).status == Decision.Status.DRAFT
    assert ActionItem.objects.get(pk=item.pk).review_status == ActionItem.ReviewStatus.DRAFT


@pytest.mark.django_db
def test_a_non_member_is_refused_the_screen_and_every_action(
    retro: Retrospective, outsider: User
) -> None:
    decision = draft_decision(retro)
    # The outsider mints a token from their own project's list page.
    client, token = strict_client(outsider, reverse("project-list"))

    assert client.get(reverse("retro-review", args=[retro.pk])).status_code == 404
    assert post(client, token, "review-decision-reject", [retro.pk, decision.pk]).status_code == 404
    assert Decision.objects.filter(pk=decision.pk).exists()


@pytest.mark.django_db
def test_an_anonymous_user_gets_404_not_a_login_redirect(retro: Retrospective) -> None:
    """`can_confirm_extraction` is False for an anonymous user, so the answer is 404."""
    draft_decision(retro)
    client = Client()

    assert client.get(reverse("retro-review", args=[retro.pk])).status_code == 404


# --------------------------------------------------------------------------
# Accepting promotes; source is not rewritten
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_accepting_a_decision_confirms_it_and_keeps_its_source(
    retro: Retrospective, owner: User
) -> None:
    decision = draft_decision(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    assert post(client, token, "review-decision-accept", [retro.pk, decision.pk]).status_code == 302

    decision.refresh_from_db()
    assert decision.status == Decision.Status.CONFIRMED
    assert decision.source == Decision.Source.EXTRACTED  # not rewritten by approving it


@pytest.mark.django_db
def test_accepting_an_action_item_confirms_it_and_leaves_it_open_and_extracted(
    retro: Retrospective, owner: User, ada: User
) -> None:
    item = draft_action_item(retro, owner=ada)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    assert post(client, token, "review-action-item-accept", [retro.pk, item.pk]).status_code == 302

    item.refresh_from_db()
    assert item.review_status == ActionItem.ReviewStatus.CONFIRMED
    assert item.source == ActionItem.Source.EXTRACTED
    assert item.status == ActionItem.Status.OPEN
    assert item.owner_id == ada.pk


# --------------------------------------------------------------------------
# Editing then accepting
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_editing_a_decision_changes_its_text_and_confirms_it(
    retro: Retrospective, owner: User
) -> None:
    decision = draft_decision(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(
        client, token, "review-decision-edit", [retro.pk, decision.pk], {"text": "Reworded call"}
    )

    assert response.status_code == 302
    decision.refresh_from_db()
    assert decision.text == "Reworded call"
    assert decision.status == Decision.Status.CONFIRMED
    assert decision.source == Decision.Source.EXTRACTED


@pytest.mark.django_db
def test_editing_an_action_item_changes_text_owner_and_due_then_confirms(
    retro: Retrospective, owner: User, ada: User
) -> None:
    """Editing sets text, owner and due date and accepts in one step."""
    item = draft_action_item(retro, owner=None)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(
        client,
        token,
        "review-action-item-edit",
        [retro.pk, item.pk],
        {"description": "Reworded task", "owner": ada.pk, "due_date": "2026-07-30"},
    )

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.description == "Reworded task"
    assert item.owner_id == ada.pk
    assert item.due_date == date(2026, 7, 30)
    assert item.review_status == ActionItem.ReviewStatus.CONFIRMED
    assert item.source == ActionItem.Source.EXTRACTED


# --------------------------------------------------------------------------
# Rejecting deletes; there is no archive
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_rejecting_a_decision_deletes_it(retro: Retrospective, owner: User) -> None:
    decision = draft_decision(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    assert post(client, token, "review-decision-reject", [retro.pk, decision.pk]).status_code == 302
    assert not Decision.objects.filter(pk=decision.pk).exists()


@pytest.mark.django_db
def test_rejecting_an_action_item_deletes_it(retro: Retrospective, owner: User) -> None:
    item = draft_action_item(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    assert post(client, token, "review-action-item-reject", [retro.pk, item.pk]).status_code == 302
    assert not ActionItem.objects.filter(pk=item.pk).exists()


# --------------------------------------------------------------------------
# The owner picked from the dropdown
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_accepting_with_an_owner_from_the_dropdown_assigns_it(
    retro: Retrospective, owner: User, ada: User
) -> None:
    """A NULL-owner draft gets its owner picked from the roster on accept."""
    item = draft_action_item(retro, owner=None)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(
        client, token, "review-action-item-accept", [retro.pk, item.pk], {"owner": ada.pk}
    )

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.owner_id == ada.pk
    assert item.review_status == ActionItem.ReviewStatus.CONFIRMED


@pytest.mark.django_db
def test_an_owner_off_the_roster_is_a_validation_error_and_not_stored(
    retro: Retrospective, owner: User, outsider: User
) -> None:
    item = draft_action_item(retro, owner=None)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(
        client, token, "review-action-item-accept", [retro.pk, item.pk], {"owner": outsider.pk}
    )

    # Refused, and the draft is untouched — still a draft, still unassigned.
    assert response.status_code == 302  # redirected back with an error message
    item.refresh_from_db()
    assert item.owner_id is None
    assert item.review_status == ActionItem.ReviewStatus.DRAFT


# --------------------------------------------------------------------------
# Accept everything at once
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_accept_all_confirms_every_draft_and_keeps_unowned_ones_unassigned(
    retro: Retrospective, owner: User, ada: User
) -> None:
    d1 = draft_decision(retro, text="First call")
    d2 = draft_decision(retro, text="Second call")
    owned = draft_action_item(retro, description="Owned task", owner=ada)
    unowned = draft_action_item(retro, description="Unowned task", owner=None)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    assert post(client, token, "review-accept-all", [retro.pk]).status_code == 302

    assert Decision.objects.get(pk=d1.pk).status == Decision.Status.CONFIRMED
    assert Decision.objects.get(pk=d2.pk).status == Decision.Status.CONFIRMED
    assert ActionItem.objects.get(pk=owned.pk).review_status == ActionItem.ReviewStatus.CONFIRMED
    # The unowned one is accepted unassigned, not blocked.
    unowned.refresh_from_db()
    assert unowned.review_status == ActionItem.ReviewStatus.CONFIRMED
    assert unowned.owner_id is None


# --------------------------------------------------------------------------
# Acting on a stale row: a readable message, never a 500
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_accepting_an_already_accepted_row_is_a_readable_message(
    retro: Retrospective, owner: User
) -> None:
    accepted = Decision.objects.create(
        retrospective=retro,
        text="Extracted, already accepted.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.CONFIRMED,
    )
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(client, token, "review-decision-accept", [retro.pk, accepted.pk])

    # A redirect with a message, not a 500 and not a wrongful re-promotion.
    assert response.status_code == 302
    assert Decision.objects.get(pk=accepted.pk).status == Decision.Status.CONFIRMED
    assert Decision.objects.count() == 1


@pytest.mark.django_db
def test_acting_on_a_row_someone_else_deleted_is_a_readable_message(
    retro: Retrospective, owner: User
) -> None:
    item = draft_action_item(retro)
    gone_pk = item.pk
    item.delete()
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(client, token, "review-action-item-accept", [retro.pk, gone_pk])

    assert response.status_code == 302  # redirect, not a 404 or a 500
    assert not ActionItem.objects.filter(pk=gone_pk).exists()


# --------------------------------------------------------------------------
# A draft is invisible until confirmed
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_draft_does_not_appear_on_the_outcomes_screen(
    retro: Retrospective, owner: User, ada: User
) -> None:
    """#17's outcomes screen is the one built surface that lists outcomes."""
    draft_decision(retro, text="Draft decision text")
    draft_action_item(retro, description="Draft action text")

    client = Client()
    assert client.login(username=ada.username, password=PASSWORD)
    body = client.get(reverse("retro-outcomes", args=[retro.pk])).content.decode()

    assert "Draft decision text" not in body
    assert "Draft action text" not in body
    # And with only drafts present, the outcomes screen reads as empty.
    assert 'data-empty="decisions"' in body
    assert 'data-empty="action-items"' in body


@pytest.mark.django_db
def test_a_draft_becomes_visible_on_the_outcomes_screen_once_accepted(
    retro: Retrospective, owner: User, ada: User
) -> None:
    decision = draft_decision(retro, text="Now confirmed decision")
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))
    post(client, token, "review-decision-accept", [retro.pk, decision.pk])

    member = Client()
    assert member.login(username=ada.username, password=PASSWORD)
    body = member.get(reverse("retro-outcomes", args=[retro.pk])).content.decode()

    assert "Now confirmed decision" in body


# --------------------------------------------------------------------------
# Advancing to COMPLETE discards outstanding drafts
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_advancing_to_complete_with_drafts_asks_first_and_names_the_count(
    retro: Retrospective, owner: User
) -> None:
    draft_decision(retro)
    draft_action_item(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(client, token, "retro-advance", [retro.pk], {"version": retro.version})

    # A confirmation page, not the transition: the stage has not moved and the
    # drafts are still there.
    assert response.status_code == 200
    body = response.content.decode()
    assert 'data-draft-count="2"' in body
    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.DISCUSS
    assert Decision.objects.filter(retrospective=retro).exists()
    assert ActionItem.objects.filter(retrospective=retro).exists()


@pytest.mark.django_db
def test_confirming_the_discard_completes_and_removes_the_drafts(
    retro: Retrospective, owner: User
) -> None:
    draft_decision(retro)
    draft_action_item(retro)
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(
        client,
        token,
        "retro-advance",
        [retro.pk],
        {"version": retro.version, "confirm_discard": "1"},
    )

    assert response.status_code == 302
    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.COMPLETE
    assert not Decision.objects.filter(retrospective=retro).exists()
    assert not ActionItem.objects.filter(retrospective=retro).exists()


@pytest.mark.django_db
def test_completing_keeps_confirmed_rows_and_discards_only_the_drafts(
    retro: Retrospective, owner: User
) -> None:
    kept = Decision.objects.create(
        retrospective=retro,
        text="Accepted before complete.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.CONFIRMED,
    )
    manual = Decision.objects.create(
        retrospective=retro,
        text="Typed by hand.",
        source=Decision.Source.MANUAL,
        status=Decision.Status.CONFIRMED,
    )
    thrown = draft_decision(retro, text="Never reviewed.")
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    post(
        client,
        token,
        "retro-advance",
        [retro.pk],
        {"version": retro.version, "confirm_discard": "1"},
    )

    assert Decision.objects.filter(pk=kept.pk).exists()
    assert Decision.objects.filter(pk=manual.pk).exists()
    assert not Decision.objects.filter(pk=thrown.pk).exists()


@pytest.mark.django_db
def test_advancing_to_complete_with_no_drafts_goes_straight_through(
    retro: Retrospective, owner: User
) -> None:
    """No drafts, no confirmation prompt — the transition happens at once."""
    client, token = strict_client(owner, reverse("retro-review", args=[retro.pk]))

    response = post(client, token, "retro-advance", [retro.pk], {"version": retro.version})

    assert response.status_code == 302
    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.COMPLETE


@pytest.mark.django_db
def test_review_is_a_pre_complete_activity(project: Project, owner: User) -> None:
    """After COMPLETE the drafts are gone, so the review screen reads as empty.

    Review interacts with the frozen-after-COMPLETE rule by happening before it:
    a draft accepted earlier is a frozen CONFIRMED record now, and one nobody
    reviewed was discarded on the way in, so there is nothing left to review.
    """
    retro = make_retro(project, owner, stage=Stage.COMPLETE)
    # No drafts survive COMPLETE, so none exist to show here.
    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert 'data-empty="review"' in body


# --------------------------------------------------------------------------
# Privacy: the excerpt is transcript text and carries no card handle
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_screen_shows_the_excerpt_and_leaks_no_card_handle(
    retro: Retrospective, owner: User
) -> None:
    """A draft references a cluster and an owner, never a card — nothing to leak."""
    draft_decision(retro, excerpt="A plain sentence from the transcript.")
    draft_action_item(retro, excerpt="Another line someone said in the meeting.")

    client = Client()
    assert client.login(username=owner.username, password=PASSWORD)
    body = client.get(reverse("retro-review", args=[retro.pk])).content.decode()

    assert "A plain sentence from the transcript." in body
    assert "Another line someone said in the meeting." in body
    # No card author, no card pk, no anonymity flag is anywhere on the screen.
    assert "public_id" not in body
    assert "is_anonymous" not in body
    assert "data-card" not in body
