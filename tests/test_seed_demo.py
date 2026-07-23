"""`manage.py seed_demo` — the guard, the idempotency, and the seeded invariants.

The command constructs the demo state directly instead of driving the real
services (`_docs/decisions.md` item 9, and issue #28's grooming). That trade
buys determinism and a demo that needs no worker and no API key; it costs a test
that walks the seeded invariants, because a model change that the services would
have enforced can now slip into the seed silently. `test_invariant_walk` is that
test.

Every test that seeds runs under `override_settings(DEBUG=True)`, because the
suite's settings have `DEBUG` off. `test_refuses_when_debug_is_off` is the one
that must not, and it proves the production guard with the suite's own settings.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from cycles.models import Card, CycleParticipation, FeedbackCycle, monday_of
from demo import seed as seed_module
from demo.seed import DEMO_PASSWORD, DEMO_USERNAMES, PLATFORM_TEAM
from meetings.models import MeetingRecord
from projects.models import Membership, Project
from retro.models import ActionItem, Cluster, Decision, Note, Retrospective, Vote

User = get_user_model()

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _run(**kwargs) -> None:
    call_command("seed_demo", **kwargs)


def _platform_cards():
    return Card.objects.filter(cycle__project__name=PLATFORM_TEAM)


def _dump() -> dict:
    """A normalised, primary-key-free view of the whole seeded Platform Team.

    Every list is keyed by natural values (usernames, texts, week starts) and
    sorted, so two builds that differ only in primary keys produce an equal
    dump. `public_id` is drawn from the seeded RNG, so it is stable too and is
    included; the row primary keys are the only thing left out.
    """
    week = lambda c: c.week_start.isoformat()  # noqa: E731

    cards = sorted(
        (
            week(c.cycle),
            c.category,
            c.text,
            c.is_anonymous,
            c.author.username if c.author else None,
            c.cluster.name if c.cluster else None,
            c.position,
            str(c.public_id),
        )
        for c in _platform_cards().select_related("author", "cluster", "cycle")
    )
    participation = sorted(
        (week(p.cycle), p.user.username, p.card_count, p.submitted_at is not None)
        for p in CycleParticipation.objects.filter(
            cycle__project__name=PLATFORM_TEAM
        ).select_related("cycle", "user")
    )
    clusters = sorted(
        (week(cl.retrospective.cycle), cl.name, cl.position, cl.is_auto_generated, cl.status)
        for cl in Cluster.objects.filter(
            retrospective__cycle__project__name=PLATFORM_TEAM
        ).select_related("retrospective__cycle")
    )
    votes = sorted(
        (week(v.retrospective.cycle), v.cluster.name, v.user.username, v.weight)
        for v in Vote.objects.filter(
            retrospective__cycle__project__name=PLATFORM_TEAM
        ).select_related("retrospective__cycle", "cluster", "user")
    )
    notes = sorted(
        (
            week(n.retrospective.cycle),
            n.author.username,
            n.cluster.name if n.cluster else None,
            n.text,
        )
        for n in Note.objects.filter(
            retrospective__cycle__project__name=PLATFORM_TEAM
        ).select_related("retrospective__cycle", "author", "cluster")
    )
    decisions = sorted(
        (week(d.retrospective.cycle), d.text, d.source, d.status, d.excerpt)
        for d in Decision.objects.filter(
            retrospective__cycle__project__name=PLATFORM_TEAM
        ).select_related("retrospective__cycle")
    )
    actions = sorted(
        (
            week(a.retrospective.cycle),
            a.description,
            a.source,
            a.status,
            a.review_status,
            a.owner.username if a.owner else None,
            a.due_date.isoformat() if a.due_date else None,
            a.excerpt,
        )
        for a in ActionItem.objects.filter(
            retrospective__cycle__project__name=PLATFORM_TEAM
        ).select_related("retrospective__cycle", "owner")
    )
    return {
        "cards": cards,
        "participation": participation,
        "clusters": clusters,
        "votes": votes,
        "notes": notes,
        "decisions": decisions,
        "actions": actions,
    }


# --------------------------------------------------------------------------
# The production guard
# --------------------------------------------------------------------------


def test_refuses_when_debug_is_off() -> None:
    """The suite runs with DEBUG off, so this needs no setup. It must not run."""
    assert settings.DEBUG is False

    with pytest.raises(CommandError) as exc:
        _run()

    assert "DEBUG" in str(exc.value)
    assert "development" in str(exc.value).lower()
    assert not User.objects.filter(username__in=DEMO_USERNAMES).exists()
    assert not Project.objects.exists()


def test_no_force_flag_exists_to_override_the_debug_guard() -> None:
    """There is no --force and no override argument at all.

    A switch to run this in production is the thing that gets used in production,
    so the command defines none: passing one is rejected as an unknown option.
    The only custom arguments are --seed and --password.
    """
    with pytest.raises(TypeError, match="force"):
        _run(force=True)


# --------------------------------------------------------------------------
# Seeding an empty database
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_seeds_on_empty_database() -> None:
    _run()

    assert User.objects.filter(username__in=DEMO_USERNAMES).count() == 7
    assert set(Project.objects.values_list("name", flat=True)) == {PLATFORM_TEAM, "Design Guild"}

    platform = Project.objects.get(name=PLATFORM_TEAM)
    assert platform.owner.username == "demo_priya"
    assert platform.memberships.count() == 6
    assert FeedbackCycle.objects.filter(project=platform).count() == 3
    assert (
        FeedbackCycle.objects.filter(
            project=platform, status=FeedbackCycle.Status.COLLECTING
        ).count()
        == 1
    )

    design = Project.objects.get(name="Design Guild")
    assert design.cycles.count() == 0
    assert set(design.memberships.values_list("user__username", flat=True)) == {
        "demo_priya",
        "demo_mei",
    }


@override_settings(DEBUG=True)
def test_admin_is_a_superuser_on_no_project() -> None:
    _run()
    admin = User.objects.get(username="demo_admin")
    assert admin.is_superuser
    assert admin.is_staff
    assert not Membership.objects.filter(user=admin).exists()


@override_settings(DEBUG=True)
def test_every_demo_user_can_log_in_with_the_documented_password() -> None:
    _run()
    client = Client()
    response = client.post(
        reverse("login"),
        {"username": "demo_priya", "password": DEMO_PASSWORD},
    )
    # A successful login redirects; a failed one re-renders the form with 200.
    assert response.status_code == 302
    assert User.objects.get(username="demo_priya").is_active


@override_settings(DEBUG=True)
def test_pinned_join_token_is_stable() -> None:
    _run()
    platform = Project.objects.get(name=PLATFORM_TEAM)
    assert str(platform.join_token) == "11111111-1111-4111-8111-111111111111"


# --------------------------------------------------------------------------
# Re-running it
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_two_runs_leave_identical_content() -> None:
    _run()
    first = _dump()
    _run()
    second = _dump()
    assert first == second


@override_settings(DEBUG=True)
def test_two_runs_differ_only_in_primary_keys() -> None:
    """Determinism: same cards, clusters, votes, reveal order — only pks move."""
    _run()
    first_dump = _dump()
    first_pks = set(_platform_cards().values_list("pk", flat=True))

    _run()
    second_dump = _dump()
    second_pks = set(_platform_cards().values_list("pk", flat=True))

    # Content, including the RNG-drawn public_ids, is identical...
    assert first_dump == second_dump
    # ...while the primary keys are new rows, because the second run deleted and
    # rebuilt.
    assert first_pks.isdisjoint(second_pks)


@override_settings(DEBUG=True)
def test_leaves_unrelated_rows_untouched() -> None:
    """The delete is scoped to demo usernames and the projects they own."""
    outsider = User.objects.create_user(username="real_person", password="not-the-demo-2026")
    outsider.display_name = "Real Person"
    outsider.save()
    project = Project.objects.create(name="Real Project", owner=outsider)
    Membership.objects.create(project=project, user=outsider, role=Membership.Role.FACILITATOR)
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=monday_of(date(2026, 1, 5)),
        opens_at=timezone.now(),
        closes_at=timezone.now(),
        facilitator=outsider,
    )
    card = Card.objects.create(
        cycle=cycle, category="START", text="A real card that is not demo data.", author=outsider
    )

    outsider_pk, project_pk, cycle_pk, card_pk = outsider.pk, project.pk, cycle.pk, card.pk

    _run()
    _run()

    # Every one of those rows is still there, unchanged.
    assert User.objects.filter(pk=outsider_pk, username="real_person").exists()
    assert Project.objects.filter(pk=project_pk, name="Real Project", owner_id=outsider_pk).exists()
    assert FeedbackCycle.objects.filter(pk=cycle_pk, project_id=project_pk).exists()
    survived = Card.objects.get(pk=card_pk)
    assert survived.text == "A real card that is not demo data."
    assert survived.author_id == outsider_pk


# --------------------------------------------------------------------------
# No network, no queue
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_constructs_no_openai_client_and_enqueues_no_task() -> None:
    with (
        mock.patch("openai.OpenAI") as openai_client,
        mock.patch("django.tasks.backends.immediate.ImmediateBackend.enqueue") as enqueue,
    ):
        _run()

    openai_client.assert_not_called()
    enqueue.assert_not_called()


# --------------------------------------------------------------------------
# Anonymity, both directions
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_revealed_anonymous_cards_have_null_author() -> None:
    """A revealed cycle carries anonymous cards, and every one has author NULL."""
    _run()
    for cycle in FeedbackCycle.objects.filter(
        project__name=PLATFORM_TEAM, status=FeedbackCycle.Status.CLOSED
    ):
        anon = cycle.cards.filter(is_anonymous=True)
        assert anon.count() >= 3
        # The invariant: no anonymous card in a revealed cycle keeps an author.
        assert not cycle.cards.filter(is_anonymous=True, author__isnull=False).exists()


@override_settings(DEBUG=True)
def test_collecting_cycle_anonymous_card_keeps_its_author() -> None:
    """Anonymity is applied at reveal, not at write time (#8): the collecting
    cycle has an anonymous card whose author is still populated."""
    _run()
    collecting = FeedbackCycle.objects.get(
        project__name=PLATFORM_TEAM, status=FeedbackCycle.Status.COLLECTING
    )
    assert collecting.cards.filter(is_anonymous=True, author__isnull=False).exists()
    # And no participation and no reveal has happened here.
    assert (
        not hasattr(collecting, "retrospective")
        or not Retrospective.objects.filter(cycle=collecting).exists()
    )
    assert not CycleParticipation.objects.filter(cycle=collecting).exists()


# --------------------------------------------------------------------------
# Rollback on failure
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_a_failure_partway_through_leaves_nothing_behind(monkeypatch) -> None:
    """The whole seed is one transaction: a failure near the end rolls it all back."""

    def boom(self, *args, **kwargs):
        raise RuntimeError("forced failure near the end of the build")

    # Cycles 1 and 2 are already written when cycle 3 blows up.
    monkeypatch.setattr(seed_module._DemoBuilder, "_build_cycle_3", boom)

    with pytest.raises(RuntimeError):
        _run()

    assert not User.objects.filter(username__in=DEMO_USERNAMES).exists()
    assert not Project.objects.exists()
    assert not FeedbackCycle.objects.exists()
    assert not Card.objects.exists()


# --------------------------------------------------------------------------
# The invariant walk — what catches a model change breaking the seed
# --------------------------------------------------------------------------


@override_settings(DEBUG=True)
def test_invariant_walk() -> None:
    """Walk the seeded rows and assert every real invariant the services enforce."""
    _run()
    platform = Project.objects.get(name=PLATFORM_TEAM)
    member_ids = set(platform.memberships.values_list("user_id", flat=True))
    assert len(member_ids) == 6

    revealed = FeedbackCycle.objects.filter(project=platform, status=FeedbackCycle.Status.CLOSED)
    collecting = FeedbackCycle.objects.get(project=platform, status=FeedbackCycle.Status.COLLECTING)

    # -- revealed cycles --------------------------------------------------
    for cycle in revealed:
        retro = Retrospective.objects.get(cycle=cycle)

        cards = list(cycle.cards.all())
        # A revealed card has a real position: 1..n, distinct, none is the "not
        # revealed" sentinel 0.
        positions = sorted(c.position for c in cards)
        assert positions == list(range(1, len(cards) + 1)), cycle.week_start
        assert 0 not in positions

        # Anonymity destroyed: no anonymous card keeps an author.
        for c in cards:
            if c.is_anonymous:
                assert c.author_id is None
            # A card's cluster belongs to this cycle's retrospective.
            if c.cluster_id is not None:
                assert c.cluster.retrospective_id == retro.pk

        # Participation for every member, consistent both halves.
        parts = {p.user_id: p for p in CycleParticipation.objects.filter(cycle=cycle)}
        assert set(parts) == member_ids
        for p in parts.values():
            if p.card_count == 0:
                assert p.submitted_at is None
            else:
                assert p.submitted_at is not None

        # Votes: one row per (cluster, member); weight within budget; every
        # voting member spent exactly the budget.
        spent: dict[int, int] = {}
        for v in Vote.objects.filter(retrospective=retro):
            assert v.retrospective_id == v.cluster.retrospective_id
            assert 1 <= v.weight <= retro.votes_per_member
            spent[v.user_id] = spent.get(v.user_id, 0) + v.weight
        assert spent, cycle.week_start
        for user_id, total in spent.items():
            assert total == retro.votes_per_member, (cycle.week_start, user_id, total)

        # Every note is attributed and belongs to this retro.
        for n in Note.objects.filter(retrospective=retro):
            assert n.author_id is not None
            if n.cluster_id is not None:
                assert n.cluster.retrospective_id == retro.pk

        # The meeting finished: media gone, transcript kept.
        record = MeetingRecord.objects.get(retrospective=retro)
        assert record.status == MeetingRecord.Status.READY
        assert record.temp_path is None
        assert record.media_deleted_at is not None
        assert record.transcript.text.strip()

    # -- the COMPLETE cycle: confirmed outcomes, no drafts ----------------
    this_monday = monday_of(timezone.localdate())
    complete = revealed.get(week_start=this_monday - timedelta(weeks=3))
    complete_retro = complete.retrospective
    assert complete_retro.stage == Retrospective.Stage.COMPLETE
    assert complete_retro.completed_at is not None and complete_retro.version > 0
    assert Decision.objects.filter(retrospective=complete_retro).exists()
    assert not Decision.objects.filter(
        retrospective=complete_retro, status=Decision.Status.DRAFT
    ).exists()
    assert not ActionItem.objects.filter(
        retrospective=complete_retro, review_status=ActionItem.ReviewStatus.DRAFT
    ).exists()

    # -- the DISCUSS cycle: extracted drafts, nothing confirmed -----------
    discuss = revealed.get(week_start=this_monday - timedelta(weeks=1))
    discuss_retro = discuss.retrospective
    assert discuss_retro.stage == Retrospective.Stage.DISCUSS
    assert Decision.objects.filter(retrospective=discuss_retro).exists()
    assert not Decision.objects.filter(
        retrospective=discuss_retro, status=Decision.Status.CONFIRMED
    ).exists()
    assert not ActionItem.objects.filter(
        retrospective=discuss_retro, review_status=ActionItem.ReviewStatus.CONFIRMED
    ).exists()
    # The ambiguous-owner draft #23 leaves unresolved.
    assert ActionItem.objects.filter(
        retrospective=discuss_retro, owner__isnull=True, excerpt__icontains="Alex will pick it up"
    ).exists()

    # -- the COLLECTING cycle: no reveal has happened ---------------------
    assert not Retrospective.objects.filter(cycle=collecting).exists()
    assert not CycleParticipation.objects.filter(cycle=collecting).exists()
    assert all(c.position == 0 for c in collecting.cards.all())
    assert collecting.cards.filter(is_anonymous=True, author__isnull=False).exists()


@override_settings(DEBUG=True)
def test_action_items_cover_every_display_case() -> None:
    """The completed cycle's actions exercise overdue, done, unassigned and more."""
    _run()
    retro = Retrospective.objects.get(
        cycle__project__name=PLATFORM_TEAM, stage=Retrospective.Stage.COMPLETE
    )
    items = list(ActionItem.objects.filter(retrospective=retro))
    assert any(i.owner_id is None for i in items)
    assert any(i.status == ActionItem.Status.DONE for i in items)
    assert any(i.is_overdue for i in items)
    assert any(
        i.status == ActionItem.Status.OPEN and i.due_date and not i.is_overdue for i in items
    )
    assert any(i.due_date is None for i in items)
    assert {i.source for i in items} == {ActionItem.Source.MANUAL, ActionItem.Source.EXTRACTED}


@override_settings(DEBUG=True)
def test_content_has_no_placeholder_text() -> None:
    """Hand-written sentences only: no lorem ipsum, no "Card 1", within the cap."""
    _run()
    for card in _platform_cards():
        assert card.text.strip()
        assert len(card.text) <= 500
        assert "lorem" not in card.text.lower()
        assert not card.text.lower().startswith("card ")
