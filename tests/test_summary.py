"""The retrospective summary — one record, the same for everyone who opens it.

Every test here maps to an acceptance criterion of issue #25, and the disciplines
are `_docs/decisions.md` item 10's, proven the way `tests/test_board.py` proves
them:

**Absence is asserted, not presence.** The card list is swept for every shape a
person-fact could take — a display name, a username, a first or last name, an
author id, an "Anonymous" label, an `is_anonymous`, a card handle — with members
whose identifiers are long and distinctive, and the sweep runs over the
`#feedback-cards` container alone, because the page legitimately carries names
elsewhere (note authors, action owners, the participation list). That the names
appear outside the container and not inside it is asserted, so the scoping is
shown to matter rather than assumed.

**An anonymous card is byte-identical to an attributed one.** The same category
and text, one anonymous and one not, render the same markup — proven by pulling
both card fragments out of the container and comparing them.

**No count splits the cards by anonymity, and the order is `position`.** The
team-wide totals are only the ones a reader could count off the page, and a
permutation that is not submission order is pinned and read back.

A refusal is proved by attempting it: a non-member and an anonymous visitor get
the 404 an unused id would, established by fetching the page as each.
"""

import itertools
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from cycles.models import Card, CycleParticipation, FeedbackCycle
from projects.models import Membership, Project
from retro.models import ActionItem, Cluster, Decision, Note, Retrospective, Vote

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

Stage = Retrospective.Stage

# Long, distinctive, and unlike anything else the page renders, so a leak of any
# of them into the card container is unambiguous — the style tests/test_board.py
# uses for the same reason.
AUTHOR_DISPLAY = "Zephyrina-Quillbottom-Authoress"
AUTHOR_USERNAME = "zephyrina_quillbottom_authoress_87"
AUTHOR_FIRST = "Zephyrina9174First"
AUTHOR_LAST = "Quillbottom9174Last"


def make_user(username: str, display_name: str, **extra) -> User:
    return User.objects.create_user(
        username=username, password=PASSWORD, display_name=display_name, **extra
    )


def make_project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


# A distinct week per cycle, so two cycles in one project never collide on the
# per-project-per-week unique constraint.
_WEEKS = itertools.count()


def make_cycle(project: Project, facilitator: User) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY + timedelta(weeks=next(_WEEKS)),
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )


def make_retro(project: Project, facilitator: User, stage: str = Stage.DISCUSS) -> Retrospective:
    cycle = make_cycle(project, facilitator)
    return Retrospective.objects.create(cycle=cycle, stage=stage)


def as_member(username: str) -> Client:
    client = Client()
    client.login(username=username, password=PASSWORD)
    return client


def summary_url(retro: Retrospective) -> str:
    return reverse("retro-summary", args=[retro.pk])


def feedback_container(html: str) -> str:
    """The `#feedback-cards` section and nothing after it.

    The container uses no nested `<section>`, so the first `</section>` after its
    start is its own — everything a person-fact must be absent from is between the
    two, and every name the page carries legitimately is outside them.
    """
    match = re.search(r'<section[^>]*id="feedback-cards".*?</section>', html, re.DOTALL)
    assert match, "the feedback-cards container was not rendered"
    return match.group(0)


def card_fragments(container: str) -> list[str]:
    """Every `<li class="feedback-card">…</li>` in the container, source and all."""
    return re.findall(r'<li class="feedback-card">.*?</li>', container, re.DOTALL)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def facilitator(db) -> User:
    return make_user("facilitator", "Fran Facilitator")


@pytest.fixture
def project(facilitator: User) -> Project:
    return make_project(facilitator)


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Morgan Member")
    Membership.objects.create(project=project, user=user)
    return user


# --------------------------------------------------------------------------
# Access and state
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_member_may_read_the_summary(project, facilitator, member) -> None:
    retro = make_retro(project, facilitator)
    response = as_member("member").get(summary_url(retro))
    assert response.status_code == 200


@pytest.mark.django_db
def test_a_non_member_gets_404(project, facilitator) -> None:
    make_user("outsider", "Otto Outsider")
    retro = make_retro(project, facilitator)
    response = as_member("outsider").get(summary_url(retro))
    assert response.status_code == 404


@pytest.mark.django_db
def test_an_anonymous_visitor_gets_404_not_a_login_redirect(project, facilitator) -> None:
    retro = make_retro(project, facilitator)
    response = Client().get(summary_url(retro))
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [Stage.DRAFT, Stage.REVEAL, Stage.CLUSTER, Stage.VOTE])
def test_the_summary_is_not_available_before_discuss(project, facilitator, member, stage) -> None:
    retro = make_retro(project, facilitator, stage=stage)
    response = as_member("member").get(summary_url(retro))
    assert response.status_code == 404


@pytest.mark.django_db
def test_the_summary_is_available_at_discuss_and_at_complete(project, facilitator, member) -> None:
    for stage in (Stage.DISCUSS, Stage.COMPLETE):
        retro = make_retro(project, facilitator, stage=stage)
        response = as_member("member").get(summary_url(retro))
        assert response.status_code == 200, stage


@pytest.mark.django_db
def test_a_live_view_before_complete_becomes_the_final_record_at_complete(
    project, facilitator, member
) -> None:
    live = make_retro(project, facilitator, stage=Stage.DISCUSS)
    final = make_retro(project, facilitator, stage=Stage.COMPLETE)

    live_body = as_member("member").get(summary_url(live)).content.decode()
    final_body = as_member("member").get(summary_url(final)).content.decode()

    assert 'data-summary-state="live"' in live_body
    assert 'data-summary-state="final"' in final_body


# --------------------------------------------------------------------------
# What it shows, and its order
# --------------------------------------------------------------------------


def _seed_full_retro(project: Project, facilitator: User) -> Retrospective:
    """A retrospective with every kind of content the summary renders."""
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    cycle = retro.cycle

    deploys = Cluster.objects.create(
        retrospective=retro, name="Deploys", position=1, status=Cluster.Status.DISCUSSED
    )
    testing = Cluster.objects.create(
        retrospective=retro, name="Testing", position=2, status=Cluster.Status.SKIPPED
    )
    # Testing outweighs Deploys, so agenda order is Testing then Deploys — not
    # position order — which the topic and card ordering must both follow.
    Vote.objects.create(retrospective=retro, cluster=deploys, user=facilitator, weight=1)
    Vote.objects.create(retrospective=retro, cluster=testing, user=facilitator, weight=3)

    Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Card in deploys.",
        cluster=deploys,
        position=1,
    )
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.STOP,
        text="Card in testing.",
        cluster=testing,
        position=2,
    )
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.CONTINUE,
        text="Ungrouped card.",
        position=3,
    )

    Note.objects.create(
        retrospective=retro, cluster=deploys, author=facilitator, text="A deploy note."
    )
    Note.objects.create(retrospective=retro, author=facilitator, text="A whole-retro note.")

    Decision.objects.create(
        retrospective=retro,
        text="Confirmed decision.",
        status=Decision.Status.CONFIRMED,
        created_by=facilitator,
    )
    ActionItem.objects.create(
        retrospective=retro,
        description="Confirmed action.",
        review_status=ActionItem.ReviewStatus.CONFIRMED,
        owner=facilitator,
        due_date=date(2026, 8, 1),
        status=ActionItem.Status.OPEN,
        created_by=facilitator,
    )

    CycleParticipation.objects.create(
        cycle=cycle, user=facilitator, card_count=3, submitted_at=SUBMITTED_AT
    )

    retro.extraction_summary = "The team talked about deploys and testing."
    retro.save(update_fields=["extraction_summary"])
    return retro


@pytest.mark.django_db
def test_the_sections_render_in_their_order(project, facilitator) -> None:
    retro = _seed_full_retro(project, facilitator)
    body = as_member("facilitator").get(summary_url(retro)).content.decode()

    order = [
        body.index(">Summary<"),
        body.index(">Discussion topics<"),
        body.index(">Notes<"),
        body.index(">Decisions<"),
        body.index(">Action items<"),
        body.index('id="feedback-cards"'),
        body.index(">Participation<"),
    ]
    assert order == sorted(order), "the summary sections are out of order"


@pytest.mark.django_db
def test_topics_are_in_agenda_order_with_weight_and_outcome(project, facilitator) -> None:
    retro = _seed_full_retro(project, facilitator)
    body = as_member("facilitator").get(summary_url(retro)).content.decode()

    # Testing (weight 3) outranks Deploys (weight 1) despite a later position.
    assert body.index("Testing") < body.index("Deploys")
    assert "3 votes — Skipped" in re.sub(r"\s+", " ", body)
    assert "1 vote — Discussed" in re.sub(r"\s+", " ", body)


@pytest.mark.django_db
def test_notes_carry_their_authors_and_sit_outside_the_card_container(project, facilitator) -> None:
    retro = _seed_full_retro(project, facilitator)
    body = as_member("facilitator").get(summary_url(retro)).content.decode()

    assert "A deploy note." in body
    assert "A whole-retro note." in body
    assert "Fran Facilitator" in body
    # A note's author name is never inside the card list.
    container = feedback_container(body)
    assert "Fran Facilitator" not in container


@pytest.mark.django_db
def test_the_extracted_summary_text_is_shown(project, facilitator) -> None:
    retro = _seed_full_retro(project, facilitator)
    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    assert "The team talked about deploys and testing." in body


# --------------------------------------------------------------------------
# Drafts are excluded
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_draft_decisions_and_action_items_are_absent(project, facilitator, member) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    Decision.objects.create(
        retrospective=retro, text="A confirmed decision.", status=Decision.Status.CONFIRMED
    )
    Decision.objects.create(
        retrospective=retro,
        text="A draft decision.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.DRAFT,
    )
    ActionItem.objects.create(
        retrospective=retro,
        description="A confirmed action.",
        review_status=ActionItem.ReviewStatus.CONFIRMED,
    )
    ActionItem.objects.create(
        retrospective=retro,
        description="A draft action.",
        source=ActionItem.Source.EXTRACTED,
        review_status=ActionItem.ReviewStatus.DRAFT,
    )

    body = as_member("member").get(summary_url(retro)).content.decode()
    assert "A confirmed decision." in body
    assert "A confirmed action." in body
    assert "A draft decision." not in body
    assert "A draft action." not in body


# --------------------------------------------------------------------------
# The card list — item 10
# --------------------------------------------------------------------------


def _seed_cards_by_authors(project: Project, facilitator: User) -> Retrospective:
    """A cycle whose cards are authored by members with distinctive identifiers."""
    author = make_user(
        AUTHOR_USERNAME, AUTHOR_DISPLAY, first_name=AUTHOR_FIRST, last_name=AUTHOR_LAST
    )
    Membership.objects.create(project=project, user=author)
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    cycle = retro.cycle
    # One card written openly and one written under the cloak, both revealed. The
    # texts carry none of the words the sweep looks for, so a match is a leak.
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Ship the deploy runbook.",
        author=author,
        is_anonymous=False,
        position=1,
    )
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.STOP,
        text="Skipping code review under pressure.",
        author=None,
        is_anonymous=True,
        position=2,
    )
    CycleParticipation.objects.create(
        cycle=cycle, user=author, card_count=2, submitted_at=SUBMITTED_AT
    )
    return retro


@pytest.mark.django_db
def test_the_card_container_carries_no_author_and_no_anonymity_mark(project, facilitator) -> None:
    retro = _seed_cards_by_authors(project, facilitator)
    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    container = feedback_container(body)

    # The cards are there.
    assert "Ship the deploy runbook." in container
    assert "Skipping code review under pressure." in container

    # No person-fact of any kind is beside a card.
    for needle in (AUTHOR_DISPLAY, AUTHOR_USERNAME, AUTHOR_FIRST, AUTHOR_LAST):
        assert needle not in container, needle
    # No anonymity distinction, in any spelling.
    for needle in ("Anonymous", "anonymous", "is_anonymous", "data-author", "author"):
        assert needle not in container, needle

    # But the author's name is on the page — in the participation list — which is
    # exactly why the sweep is scoped to the container and not the document.
    assert AUTHOR_DISPLAY in body


@pytest.mark.django_db
def test_no_card_handle_reaches_the_container(project, facilitator) -> None:
    retro = _seed_cards_by_authors(project, facilitator)
    cards = list(Card.objects.filter(cycle=retro.cycle))
    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    container = feedback_container(body)

    for card in cards:
        assert str(card.public_id) not in container, "a public_id leaked into the card list"
        assert f'data-card="{card.pk}"' not in container
    assert "public_id" not in container
    assert "public-id" not in container


@pytest.mark.django_db
def test_an_anonymous_card_is_byte_identical_to_an_attributed_one(project, facilitator) -> None:
    """Same category and text, one anonymous and one not, render the same markup."""
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    author = make_user("twin_author", "Twin Author")
    Membership.objects.create(project=project, user=author)
    cycle = retro.cycle
    # Identical category and text; one attributed, one anonymous. Both ungrouped,
    # so they land in one group and can be compared side by side.
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Exactly the same words.",
        author=author,
        is_anonymous=False,
        position=1,
    )
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Exactly the same words.",
        author=None,
        is_anonymous=True,
        position=2,
    )

    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    fragments = card_fragments(feedback_container(body))
    assert len(fragments) == 2, "expected two card fragments"
    assert fragments[0] == fragments[1], (
        "the anonymous card and the attributed card rendered different markup"
    )


@pytest.mark.django_db
def test_no_count_splits_the_cards_by_anonymity(project, facilitator) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    author = make_user("counter", "Cara Counter")
    Membership.objects.create(project=project, user=author)
    cycle = retro.cycle
    # Two attributed, one anonymous.
    Card.objects.create(
        cycle=cycle, category=Card.Category.START, text="One.", author=author, position=1
    )
    Card.objects.create(
        cycle=cycle, category=Card.Category.START, text="Two.", author=author, position=2
    )
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.STOP,
        text="Three.",
        author=None,
        is_anonymous=True,
        position=3,
    )

    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    flat = re.sub(r"\s+", " ", body).lower()

    # No anonymity split anywhere on the page.
    assert "anonymous" not in flat
    # But the allowed totals — one a reader could count off the page — are here.
    assert "3 cards in total" in flat
    assert "start: 2" in flat
    assert "stop: 1" in flat


@pytest.mark.django_db
def test_cards_render_in_position_order_not_submission_order(project, facilitator) -> None:
    """A permutation that is not submission order is pinned and read back."""
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    cycle = retro.cycle
    # Created first-to-last: alpha, beta, gamma. Positions permute them so
    # submission order (created_at, id) and reveal order (position) disagree.
    alpha = Card.objects.create(cycle=cycle, category=Card.Category.START, text="ALPHA")
    beta = Card.objects.create(cycle=cycle, category=Card.Category.START, text="BETA")
    gamma = Card.objects.create(cycle=cycle, category=Card.Category.START, text="GAMMA")
    Card.objects.filter(pk=alpha.pk).update(position=3)
    Card.objects.filter(pk=beta.pk).update(position=1)
    Card.objects.filter(pk=gamma.pk).update(position=2)

    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    container = feedback_container(body)
    # Reveal order is BETA, GAMMA, ALPHA — not the submission order.
    assert container.index("BETA") < container.index("GAMMA") < container.index("ALPHA")


@pytest.mark.django_db
def test_ungrouped_cards_come_last_in_a_group_of_their_own(project, facilitator) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    cycle = retro.cycle
    cluster = Cluster.objects.create(retrospective=retro, name="A cluster", position=1)
    Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Grouped one.",
        cluster=cluster,
        position=1,
    )
    Card.objects.create(cycle=cycle, category=Card.Category.STOP, text="Loose one.", position=2)

    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    container = feedback_container(body)
    assert container.index("Grouped one.") < container.index("Loose one.")
    assert container.index("A cluster") < container.index("Ungrouped")


# --------------------------------------------------------------------------
# One record, the same for everyone
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_two_members_who_both_wrote_cards_get_the_same_card_list(project, facilitator) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    ada = make_user("ada", "Ada Writer")
    bob = make_user("bob", "Bob Writer")
    Membership.objects.create(project=project, user=ada)
    Membership.objects.create(project=project, user=bob)
    cycle = retro.cycle
    Card.objects.create(
        cycle=cycle, category=Card.Category.START, text="Ada's card.", author=ada, position=1
    )
    Card.objects.create(
        cycle=cycle, category=Card.Category.STOP, text="Bob's card.", author=bob, position=2
    )

    ada_container = feedback_container(as_member("ada").get(summary_url(retro)).content.decode())
    bob_container = feedback_container(as_member("bob").get(summary_url(retro)).content.decode())

    # Identical for both — the summary does not mark either reader's own cards.
    assert ada_container == bob_container
    assert "mine" not in ada_container
    assert "data-mine" not in ada_container


# --------------------------------------------------------------------------
# Participation
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_participation_lists_submitters_and_non_submitters_without_counts(
    project, facilitator
) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    submitter = make_user("sam", "Sam Submitter")
    quiet = make_user("quinn", "Quinn Quiet")
    Membership.objects.create(project=project, user=submitter)
    Membership.objects.create(project=project, user=quiet)
    CycleParticipation.objects.create(
        cycle=retro.cycle, user=submitter, card_count=2, submitted_at=SUBMITTED_AT
    )
    CycleParticipation.objects.create(
        cycle=retro.cycle, user=quiet, card_count=0, submitted_at=None
    )

    body = as_member("sam").get(summary_url(retro)).content.decode()
    assert "Sam Submitter" in body
    assert "Quinn Quiet" in body
    # No per-member count, and no submitted-at timestamp.
    assert "card_count" not in body
    assert "2026-07-21" not in body
    flat = re.sub(r"\s+", " ", body)
    assert "Sam Submitter (2" not in flat


@pytest.mark.django_db
def test_participation_keeps_a_member_who_has_since_left_the_project(project, facilitator) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    # A participation row for someone who is no longer a member — they wrote a
    # card into this cycle and left. Listing current membership would drop them.
    departed = make_user("departed", "Dana Departed")
    CycleParticipation.objects.create(
        cycle=retro.cycle, user=departed, card_count=1, submitted_at=SUBMITTED_AT
    )

    body = as_member("facilitator").get(summary_url(retro)).content.decode()
    assert "Dana Departed" in body


# --------------------------------------------------------------------------
# The empty retrospective
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_empty_retrospective_says_so_without_scaffolding(project, facilitator, member) -> None:
    retro = make_retro(project, facilitator, stage=Stage.DISCUSS)
    body = as_member("member").get(summary_url(retro)).content.decode()

    assert "produced no recorded outcomes" in body
    # No empty section scaffolding for the outcomes.
    assert ">Decisions<" not in body
    assert ">Action items<" not in body
    assert ">Notes<" not in body
    assert 'id="feedback-cards"' not in body
    # Participation still renders — it is not one of the outcome sections.
    assert ">Participation<" in body
