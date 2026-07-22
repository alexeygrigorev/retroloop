"""The retrospective row and the stage machine.

Every test here maps to an acceptance criterion of issue #9. Three themes run
through them.

The first is that a rule is only proved by attempting to break it. Where a
transition must not be possible, the test tries to make it and asserts the
refusal and that the row did not move — never by reading the code, and never by
being satisfied that a button is missing.

The second is absence. Where someone must not be shown a control, the test
asserts the control and its URL are not on the page, as well as posting to it
and being refused.

The third is that nothing here knows about cards. #8 has not landed, so what
`REVEAL` reveals and what `VOTE` freezes are hooks named after the issues that
fill them; the one side effect this task owns — reveal closes the cycle — is
tested through the cycle's own status.
"""

import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse

from cycles.models import FeedbackCycle
from projects.models import Membership, Project
from projects.permissions import can_advance_stage, can_start_retrospective
from retro import services
from retro.models import (
    STAGE_ORDER,
    Retrospective,
    is_legal_transition,
    next_stage_after,
)
from retro.services import (
    ConcurrentAdvance,
    InvalidTransition,
    StageError,
    advance_stage,
    bump_version,
    start_retrospective,
)

User = get_user_model()

PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage

#: The five moves that are allowed, written out rather than derived, so the
#: table below is an independent statement of the order and not a restatement
#: of the code it checks.
LEGAL_PAIRS = {
    (Stage.DRAFT, Stage.REVEAL),
    (Stage.REVEAL, Stage.CLUSTER),
    (Stage.CLUSTER, Stage.VOTE),
    (Stage.VOTE, Stage.DISCUSS),
    (Stage.DISCUSS, Stage.COMPLETE),
}

ALL_PAIRS = [(a, b) for a in STAGE_ORDER for b in STAGE_ORDER]
ILLEGAL_PAIRS = [pair for pair in ALL_PAIRS if pair not in LEGAL_PAIRS]


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def log_in(client: Client, user: User) -> None:
    client.login(username=user.username, password=PASSWORD)


def make_cycle(project: Project, facilitator: User, **kwargs) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=kwargs.pop("week_start", MONDAY),
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        **kwargs,
    )


def at_stage(retro: Retrospective, stage: str) -> Retrospective:
    """Put a retrospective straight into `stage`, without going through the machine.

    Forward-only is a product rule, so there is no `force` argument to lean on:
    a test that needs a later stage constructs it, which is also the only way to
    hold a state the machine itself would never produce.
    """
    Retrospective.objects.filter(pk=retro.pk).update(stage=stage)
    retro.refresh_from_db()
    return retro


def start_url(cycle: FeedbackCycle) -> str:
    return reverse("retro-start", args=[cycle.pk])


def detail_url(retro: Retrospective) -> str:
    return reverse("retro-detail", args=[retro.pk])


def advance_url(retro: Retrospective) -> str:
    return reverse("retro-advance", args=[retro.pk])


@pytest.fixture
def owner(db) -> User:
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def facilitator(project: Project) -> User:
    """This cycle's facilitator, who is not the project owner."""
    user = make_user("facilitator", "Fay Facilitator")
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Mel Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def other_facilitator(project: Project) -> User:
    """A project facilitator who is not this cycle's facilitator."""
    user = make_user("other", "Otto Other")
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


@pytest.fixture
def superuser_outsider(db) -> User:
    return User.objects.create_superuser(
        username="root", password=PASSWORD, display_name="Root Outsider"
    )


@pytest.fixture
def cycle(project: Project, facilitator: User) -> FeedbackCycle:
    return make_cycle(project, facilitator)


@pytest.fixture
def retro(cycle: FeedbackCycle) -> Retrospective:
    return Retrospective.objects.create(cycle=cycle)


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


def test_the_stages_are_exactly_these_six_in_this_order() -> None:
    assert STAGE_ORDER == ("DRAFT", "REVEAL", "CLUSTER", "VOTE", "DISCUSS", "COMPLETE")
    assert list(Stage.values) == list(STAGE_ORDER)


@pytest.mark.django_db
def test_a_new_retrospective_is_a_draft_at_version_zero(cycle: FeedbackCycle) -> None:
    retro = Retrospective.objects.create(cycle=cycle)

    assert retro.stage == Stage.DRAFT
    assert retro.version == 0
    assert retro.started_at is None
    assert retro.completed_at is None
    assert retro.votes_per_member == 3
    assert retro.is_complete is False


@pytest.mark.django_db
def test_a_cycle_can_hold_only_one_retrospective(cycle: FeedbackCycle) -> None:
    """The one-to-one, not a view check, is what refuses the second one."""
    Retrospective.objects.create(cycle=cycle)

    with pytest.raises(IntegrityError), transaction.atomic():
        Retrospective.objects.create(cycle=cycle)


@pytest.mark.django_db
def test_two_cycles_each_have_their_own_retrospective(
    project: Project, facilitator: User, cycle: FeedbackCycle
) -> None:
    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])
    later = make_cycle(project, facilitator, week_start=date(2026, 7, 27))

    Retrospective.objects.create(cycle=cycle)
    Retrospective.objects.create(cycle=later)

    assert Retrospective.objects.count() == 2


@pytest.mark.django_db
def test_the_retrospective_is_reachable_from_its_cycle(cycle: FeedbackCycle) -> None:
    retro = Retrospective.objects.create(cycle=cycle)
    cycle.refresh_from_db()

    assert cycle.retrospective == retro


def test_next_stage_after_walks_the_order_and_stops_at_complete() -> None:
    assert next_stage_after(Stage.DRAFT) == Stage.REVEAL
    assert next_stage_after(Stage.REVEAL) == Stage.CLUSTER
    assert next_stage_after(Stage.CLUSTER) == Stage.VOTE
    assert next_stage_after(Stage.VOTE) == Stage.DISCUSS
    assert next_stage_after(Stage.DISCUSS) == Stage.COMPLETE
    assert next_stage_after(Stage.COMPLETE) is None


# --------------------------------------------------------------------------
# Every from-stage to to-stage pair
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("from_stage", "to_stage"), ALL_PAIRS)
def test_only_a_forward_single_step_pair_is_a_legal_transition(
    from_stage: str, to_stage: str
) -> None:
    """The table: all thirty-six pairs, five of them legal and thirty-one not."""
    assert is_legal_transition(from_stage, to_stage) is ((from_stage, to_stage) in LEGAL_PAIRS)


def test_the_transition_table_is_actually_thirty_six_pairs() -> None:
    """Guards the parametrization above: an empty list would make it vacuous."""
    assert len(ALL_PAIRS) == 36
    assert len(ILLEGAL_PAIRS) == 31


def test_a_stage_that_does_not_exist_is_not_a_legal_transition() -> None:
    assert is_legal_transition("DRAFT", "ARCHIVED") is False
    assert is_legal_transition("ARCHIVED", "REVEAL") is False


@pytest.mark.django_db
@pytest.mark.parametrize(("from_stage", "to_stage"), ILLEGAL_PAIRS)
def test_every_illegal_transition_is_refused_when_it_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
    retro: Retrospective,
    facilitator: User,
    from_stage: str,
    to_stage: str,
) -> None:
    """Each of the thirty-one illegal pairs, attempted rather than read about.

    `advance_stage()` derives its own target, so the only way to ask it for a
    backwards, skipped or standing-still move is to make the derivation return
    one — which is exactly what a future caller's bug would look like. The
    guard has to catch it, and the row has to be where it was.
    """
    at_stage(retro, from_stage)
    monkeypatch.setattr(services, "next_stage_after", lambda stage: to_stage)

    with pytest.raises(InvalidTransition):
        advance_stage(facilitator, retro)

    retro.refresh_from_db()
    assert retro.stage == from_stage
    assert retro.version == 0


@pytest.mark.django_db
def test_advancing_from_complete_is_refused_with_no_help_from_a_patch(
    retro: Retrospective, facilitator: User
) -> None:
    """COMPLETE is terminal through the real API, not only through the table."""
    at_stage(retro, Stage.COMPLETE)

    with pytest.raises(InvalidTransition) as rejection:
        advance_stage(facilitator, retro)

    retro.refresh_from_db()
    assert retro.stage == Stage.COMPLETE
    assert retro.version == 0
    assert "complete" in str(rejection.value).lower()


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_an_advance_never_lands_anywhere_but_the_next_stage(
    retro: Retrospective, facilitator: User, stage: str
) -> None:
    """From every stage, the only reachable outcome is one step forward or a refusal."""
    at_stage(retro, stage)
    expected = next_stage_after(stage)

    if expected is None:
        with pytest.raises(InvalidTransition):
            advance_stage(facilitator, retro)
    else:
        advance_stage(facilitator, retro)

    retro.refresh_from_db()
    assert retro.stage == (expected or stage)


# --------------------------------------------------------------------------
# The rules: who
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cycles_facilitator_may_start_and_advance(
    cycle: FeedbackCycle, facilitator: User
) -> None:
    assert can_start_retrospective(facilitator, cycle) is True

    retro = start_retrospective(facilitator, cycle)

    assert retro.stage == Stage.DRAFT
    assert can_advance_stage(facilitator, retro) is True


@pytest.mark.django_db
def test_a_cycle_that_already_has_a_retrospective_cannot_start_another(
    cycle: FeedbackCycle, facilitator: User
) -> None:
    start_retrospective(facilitator, cycle)
    cycle.refresh_from_db()

    assert can_start_retrospective(facilitator, cycle) is False
    with pytest.raises(PermissionDenied):
        start_retrospective(facilitator, cycle)
    assert Retrospective.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["member", "other_facilitator", "outsider", "superuser_outsider"])
def test_nobody_but_the_cycles_facilitator_may_start_a_retrospective(
    request: pytest.FixtureRequest, cycle: FeedbackCycle, who: str
) -> None:
    user = request.getfixturevalue(who)

    assert can_start_retrospective(user, cycle) is False
    with pytest.raises(PermissionDenied):
        start_retrospective(user, cycle)
    assert not Retrospective.objects.exists()


@pytest.mark.django_db
def test_an_anonymous_user_may_not_start_a_retrospective(cycle: FeedbackCycle) -> None:
    assert can_start_retrospective(AnonymousUser(), cycle) is False


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["member", "other_facilitator", "outsider", "superuser_outsider"])
def test_nobody_but_the_cycles_facilitator_may_advance(
    request: pytest.FixtureRequest, retro: Retrospective, who: str
) -> None:
    """A member, a non-member, and a superuser from outside are all refused."""
    user = request.getfixturevalue(who)

    assert can_advance_stage(user, retro) is False
    with pytest.raises(PermissionDenied):
        advance_stage(user, retro)

    retro.refresh_from_db()
    assert retro.stage == Stage.DRAFT
    assert retro.version == 0


@pytest.mark.django_db
def test_an_anonymous_user_may_not_advance(retro: Retrospective) -> None:
    anonymous = AnonymousUser()

    assert can_advance_stage(anonymous, retro) is False
    with pytest.raises(PermissionDenied):
        advance_stage(anonymous, retro)

    retro.refresh_from_db()
    assert retro.stage == Stage.DRAFT


@pytest.mark.django_db
def test_the_owner_of_the_project_is_not_automatically_this_cycles_facilitator(
    retro: Retrospective, owner: User
) -> None:
    """Authority over a retrospective is per cycle, as it is for the cycle itself."""
    assert can_advance_stage(owner, retro) is False


# --------------------------------------------------------------------------
# The legal path
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_full_path_from_draft_to_complete_bumps_the_version_each_step(
    retro: Retrospective, facilitator: User
) -> None:
    assert (retro.stage, retro.version) == (Stage.DRAFT, 0)

    seen = []
    for expected_version, expected_stage in enumerate(STAGE_ORDER[1:], start=1):
        advance_stage(facilitator, retro)
        stored = Retrospective.objects.get(pk=retro.pk)
        seen.append(stored.stage)

        assert stored.stage == expected_stage
        assert stored.version == expected_version
        # The caller's own instance is moved with the row, not left behind.
        assert (retro.stage, retro.version) == (expected_stage, expected_version)

    assert seen == list(STAGE_ORDER[1:])
    assert retro.version == 5


@pytest.mark.django_db
def test_started_at_is_set_on_entering_reveal_and_never_moved_again(
    retro: Retrospective, facilitator: User
) -> None:
    assert retro.started_at is None

    advance_stage(facilitator, retro)
    started = Retrospective.objects.get(pk=retro.pk).started_at
    assert started is not None

    advance_stage(facilitator, retro)
    assert Retrospective.objects.get(pk=retro.pk).started_at == started


@pytest.mark.django_db
def test_completed_at_is_set_on_entering_complete_and_not_before(
    retro: Retrospective, facilitator: User
) -> None:
    at_stage(retro, Stage.DISCUSS)
    assert retro.completed_at is None

    advance_stage(facilitator, retro)
    stored = Retrospective.objects.get(pk=retro.pk)

    assert stored.stage == Stage.COMPLETE
    assert stored.completed_at is not None
    assert stored.is_complete is True


# --------------------------------------------------------------------------
# Reveal ends collection
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_advancing_to_reveal_closes_a_cycle_that_is_still_collecting(
    retro: Retrospective, cycle: FeedbackCycle, facilitator: User
) -> None:
    assert cycle.status == FeedbackCycle.Status.COLLECTING

    advance_stage(facilitator, retro)
    cycle.refresh_from_db()

    assert cycle.status == FeedbackCycle.Status.CLOSED
    # There is no state where the cards are revealed and the form is still open.
    assert cycle.accepts_cards is False


@pytest.mark.django_db
def test_advancing_to_reveal_leaves_an_already_closed_cycle_closed(
    retro: Retrospective, cycle: FeedbackCycle, facilitator: User
) -> None:
    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])

    advance_stage(facilitator, retro)
    cycle.refresh_from_db()

    assert cycle.status == FeedbackCycle.Status.CLOSED
    assert Retrospective.objects.get(pk=retro.pk).stage == Stage.REVEAL


@pytest.mark.django_db
def test_a_later_transition_does_not_touch_the_cycle(
    retro: Retrospective, cycle: FeedbackCycle, facilitator: User
) -> None:
    at_stage(retro, Stage.CLUSTER)

    advance_stage(facilitator, retro)
    cycle.refresh_from_db()

    assert cycle.status == FeedbackCycle.Status.COLLECTING


# --------------------------------------------------------------------------
# The hooks, and the transaction they run in
# --------------------------------------------------------------------------


def test_every_stage_after_draft_has_a_hook() -> None:
    """A missing hook must be a KeyError at the transition, not a silent nothing."""
    assert set(services.TRANSITION_HOOKS) == set(STAGE_ORDER[1:])


@pytest.mark.django_db
def test_a_side_effect_that_raises_leaves_the_stage_where_it_was(
    monkeypatch: pytest.MonkeyPatch, retro: Retrospective, cycle: FeedbackCycle, facilitator: User
) -> None:
    """The whole transition is one transaction: the hook fails, nothing moves."""

    def boom(retro: Retrospective) -> None:
        raise RuntimeError("the clustering job could not be enqueued")

    monkeypatch.setitem(services.TRANSITION_HOOKS, Stage.REVEAL, boom)

    with pytest.raises(RuntimeError):
        advance_stage(facilitator, retro)

    stored = Retrospective.objects.get(pk=retro.pk)
    cycle.refresh_from_db()
    assert stored.stage == Stage.DRAFT
    assert stored.version == 0
    assert stored.started_at is None
    # The cycle is the side effect this task owns, and it rolled back too.
    assert cycle.status == FeedbackCycle.Status.COLLECTING


@pytest.mark.django_db
def test_a_side_effect_that_raises_mid_path_leaves_the_earlier_stages_alone(
    monkeypatch: pytest.MonkeyPatch, retro: Retrospective, facilitator: User
) -> None:
    advance_stage(facilitator, retro)
    advance_stage(facilitator, retro)
    assert retro.stage == Stage.CLUSTER

    def boom(retro: Retrospective) -> None:
        raise RuntimeError("#12 could not freeze the clusters")

    monkeypatch.setitem(services.TRANSITION_HOOKS, Stage.VOTE, boom)

    with pytest.raises(RuntimeError):
        advance_stage(facilitator, retro)

    stored = Retrospective.objects.get(pk=retro.pk)
    assert stored.stage == Stage.CLUSTER
    assert stored.version == 2


# --------------------------------------------------------------------------
# The version counter
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_bump_version_is_the_helper_a_later_mutation_calls(retro: Retrospective) -> None:
    with transaction.atomic():
        assert bump_version(retro) == 1
        assert bump_version(retro) == 2

    assert Retrospective.objects.get(pk=retro.pk).version == 2
    assert retro.version == 2


@pytest.mark.django_db(transaction=True)
def test_bump_version_refuses_to_run_outside_a_transaction(retro: Retrospective) -> None:
    """A counter that commits apart from the change it describes is worse than none.

    The test is transactional so that there is genuinely no transaction open
    around it — the usual test wrapper would supply one and prove nothing.
    """
    with pytest.raises(RuntimeError):
        bump_version(retro)

    assert Retrospective.objects.get(pk=retro.pk).version == 0


@pytest.mark.django_db
def test_a_caller_holding_a_stale_version_is_refused(
    retro: Retrospective, facilitator: User
) -> None:
    """The version is the sync mechanism: acting on an old read is rejected."""
    stale = Retrospective.objects.get(pk=retro.pk)
    advance_stage(facilitator, retro)

    with pytest.raises(ConcurrentAdvance):
        advance_stage(facilitator, stale)

    stored = Retrospective.objects.get(pk=retro.pk)
    assert stored.stage == Stage.REVEAL
    assert stored.version == 1


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_advances_give_one_success_and_one_rejection(
    retro: Retrospective, facilitator: User
) -> None:
    """The row-level lock, exercised with two concurrent transactions.

    Both threads read the retrospective before either acts, so both hold
    version 0. One takes the lock and commits; the other waits on the same row
    and then finds the board moved under it. A double advance would leave the
    stage at CLUSTER, which is what this asserts against.
    """
    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, str] = {}

    def advance(name: str) -> None:
        try:
            user = User.objects.get(pk=facilitator.pk)
            mine = Retrospective.objects.get(pk=retro.pk)
            assert mine.version == 0
            barrier.wait()
            advance_stage(user, mine)
            results[name] = "advanced"
        except StageError:
            results[name] = "rejected"
        finally:
            connection.close()

    threads = [threading.Thread(target=advance, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    stored = Retrospective.objects.get(pk=retro.pk)
    assert sorted(results.values()) == ["advanced", "rejected"]
    assert stored.stage == Stage.REVEAL
    assert stored.version == 1


# --------------------------------------------------------------------------
# Nothing else writes the stage
# --------------------------------------------------------------------------


def test_no_view_assigns_a_stage_directly() -> None:
    """`advance_stage()` is the only way the stage changes, in the source as well."""
    views = sorted(Path(settings.BASE_DIR).glob("*/views.py"))
    assert len(views) >= 4

    for path in views:
        source = path.read_text()
        assert re.search(r"\.stage\s*=[^=]", source) is None, f"{path.name} assigns a stage"


# --------------------------------------------------------------------------
# Starting one, through the views
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_facilitator_starts_the_retrospective_from_the_cycle(
    client: Client, cycle: FeedbackCycle, facilitator: User
) -> None:
    log_in(client, facilitator)

    response = client.post(start_url(cycle))

    retro = Retrospective.objects.get(cycle=cycle)
    assert response.status_code == 302
    assert response.headers["Location"] == detail_url(retro)
    assert retro.stage == Stage.DRAFT


@pytest.mark.django_db
def test_a_plain_member_cannot_start_the_retrospective(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    response = client.post(start_url(cycle))

    assert response.status_code == 403
    assert not Retrospective.objects.exists()


@pytest.mark.django_db
def test_a_plain_member_is_not_shown_the_start_control(
    client: Client, cycle: FeedbackCycle, member: User
) -> None:
    log_in(client, member)

    html = client.get(cycle.get_absolute_url()).content.decode()

    assert "Start the retrospective" not in html
    assert start_url(cycle) not in html


@pytest.mark.django_db
def test_the_facilitator_is_shown_the_start_control(
    client: Client, cycle: FeedbackCycle, facilitator: User
) -> None:
    log_in(client, facilitator)

    html = client.get(cycle.get_absolute_url()).content.decode()

    assert "Start the retrospective" in html
    assert start_url(cycle) in html


@pytest.mark.django_db
def test_the_start_control_is_gone_once_a_retrospective_exists(
    client: Client, cycle: FeedbackCycle, facilitator: User, retro: Retrospective
) -> None:
    log_in(client, facilitator)

    html = client.get(cycle.get_absolute_url()).content.decode()

    assert "Start the retrospective" not in html
    assert start_url(cycle) not in html
    assert detail_url(retro) in html


@pytest.mark.django_db
def test_starting_a_second_retrospective_for_one_cycle_is_refused(
    client: Client, cycle: FeedbackCycle, facilitator: User, retro: Retrospective
) -> None:
    log_in(client, facilitator)

    response = client.post(start_url(cycle))

    assert response.status_code == 403
    assert Retrospective.objects.count() == 1


@pytest.mark.django_db
def test_a_non_member_gets_404_from_starting_one(
    client: Client, cycle: FeedbackCycle, outsider: User
) -> None:
    log_in(client, outsider)

    assert client.post(start_url(cycle)).status_code == 404
    assert not Retrospective.objects.exists()


@pytest.mark.django_db
def test_starting_one_needs_a_post(client: Client, cycle: FeedbackCycle, facilitator: User) -> None:
    log_in(client, facilitator)

    assert client.get(start_url(cycle)).status_code == 405
    assert not Retrospective.objects.exists()


# --------------------------------------------------------------------------
# The retrospective page
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_page_renders_inside_the_application_layout(
    client: Client, retro: Retrospective, member: User
) -> None:
    """One document, from base_app.html, with the navigation on it."""
    log_in(client, member)

    html = client.get(detail_url(retro)).content.decode()

    assert html.count("<!doctype html>") == 1
    assert "Your projects" in html
    assert "Log out" in html


@pytest.mark.django_db
def test_the_page_shows_the_stage_and_the_votes_each_member_gets(
    client: Client, retro: Retrospective, member: User
) -> None:
    log_in(client, member)

    html = client.get(detail_url(retro)).content.decode()

    assert 'data-retro-stage="DRAFT"' in html
    assert 'data-votes-per-member="3"' in html
    # A field with a default and no UI in this task: nothing on the page edits it.
    assert 'name="votes_per_member"' not in html


@pytest.mark.django_db
def test_a_non_member_gets_404_from_the_retrospective_page(
    client: Client, retro: Retrospective, outsider: User
) -> None:
    log_in(client, outsider)

    assert client.get(detail_url(retro)).status_code == 404


@pytest.mark.django_db
def test_a_superuser_from_outside_the_project_gets_404_too(
    client: Client, retro: Retrospective, superuser_outsider: User
) -> None:
    log_in(client, superuser_outsider)

    assert client.get(detail_url(retro)).status_code == 404


@pytest.mark.django_db
def test_a_plain_member_is_not_shown_the_advance_control(
    client: Client, retro: Retrospective, member: User
) -> None:
    log_in(client, member)

    html = client.get(detail_url(retro)).content.decode()

    assert "Advance to" not in html
    assert advance_url(retro) not in html


@pytest.mark.django_db
def test_the_facilitator_is_shown_the_advance_control(
    client: Client, retro: Retrospective, facilitator: User
) -> None:
    log_in(client, facilitator)

    html = client.get(detail_url(retro)).content.decode()

    assert "Advance to Reveal" in html
    assert advance_url(retro) in html


@pytest.mark.django_db
def test_a_complete_retrospective_offers_the_facilitator_no_advance_control(
    client: Client, retro: Retrospective, facilitator: User
) -> None:
    """Not even to the person who may advance one: there is nowhere left to go."""
    at_stage(retro, Stage.COMPLETE)
    log_in(client, facilitator)

    html = client.get(detail_url(retro)).content.decode()

    assert "Advance to" not in html
    assert advance_url(retro) not in html
    assert "This retrospective is complete" in html


# --------------------------------------------------------------------------
# Advancing, through the views
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_facilitator_advances_the_retrospective(
    client: Client, retro: Retrospective, facilitator: User
) -> None:
    log_in(client, facilitator)

    response = client.post(advance_url(retro), {"version": retro.version})
    retro.refresh_from_db()

    assert response.status_code == 302
    assert response.headers["Location"] == detail_url(retro)
    assert retro.stage == Stage.REVEAL
    assert retro.version == 1


@pytest.mark.django_db
def test_a_plain_member_posting_to_advance_is_refused(
    client: Client, retro: Retrospective, member: User
) -> None:
    """The hidden control is a courtesy; this is the rule."""
    log_in(client, member)

    response = client.post(advance_url(retro), {"version": retro.version})
    retro.refresh_from_db()

    assert response.status_code == 403
    assert retro.stage == Stage.DRAFT
    assert retro.version == 0


@pytest.mark.django_db
def test_a_non_member_posting_to_advance_gets_404(
    client: Client, retro: Retrospective, outsider: User
) -> None:
    log_in(client, outsider)

    response = client.post(advance_url(retro), {"version": retro.version})
    retro.refresh_from_db()

    assert response.status_code == 404
    assert retro.stage == Stage.DRAFT


@pytest.mark.django_db
def test_an_anonymous_visitor_posting_to_advance_is_sent_to_the_login_page(
    client: Client, retro: Retrospective
) -> None:
    response = client.post(advance_url(retro), {"version": retro.version})
    retro.refresh_from_db()

    assert response.status_code == 302
    assert reverse("login") in response.headers["Location"]
    assert retro.stage == Stage.DRAFT


@pytest.mark.django_db
def test_advancing_a_complete_retrospective_through_the_view_says_so_and_changes_nothing(
    client: Client, retro: Retrospective, facilitator: User
) -> None:
    at_stage(retro, Stage.COMPLETE)
    completed_before = Retrospective.objects.get(pk=retro.pk).completed_at
    log_in(client, facilitator)

    response = client.post(advance_url(retro), {"version": retro.version}, follow=True)
    html = response.content.decode()
    retro.refresh_from_db()

    assert response.status_code == 200
    assert "cannot advance" in html
    assert retro.stage == Stage.COMPLETE
    assert retro.version == 0
    assert retro.completed_at == completed_before


@pytest.mark.django_db
def test_a_second_click_on_a_page_that_has_gone_stale_does_not_advance_twice(
    client: Client, retro: Retrospective, facilitator: User
) -> None:
    """The version the page was rendered from is what the second post carries."""
    log_in(client, facilitator)
    rendered_version = retro.version

    first = client.post(advance_url(retro), {"version": rendered_version})
    second = client.post(advance_url(retro), {"version": rendered_version}, follow=True)
    retro.refresh_from_db()

    assert first.status_code == 302
    assert "Reload the page" in second.content.decode()
    assert retro.stage == Stage.REVEAL
    assert retro.version == 1


@pytest.mark.django_db
def test_advancing_needs_a_post(client: Client, retro: Retrospective, facilitator: User) -> None:
    log_in(client, facilitator)

    assert client.get(advance_url(retro)).status_code == 405
    retro.refresh_from_db()
    assert retro.stage == Stage.DRAFT
