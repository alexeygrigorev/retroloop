"""The one module that decides who may do what.

Every test here maps to an acceptance criterion of issue #6. Four themes run
through the file.

The first is that the rules are driven from a registry, not from a hand-written
list per test. `RULES` names every public predicate in the module and says which
object it takes and the world in which it says yes. A predicate added later
without an entry fails `test_every_public_name_in_the_module_is_exercised`
immediately, so a rule cannot ship untested.

The second is that a grant is proved as well as a refusal. Every rule is
asserted True for someone, in the same world where it is asserted False for an
anonymous user, a deactivated user, an outsider and a superuser from outside —
otherwise "returns False for everyone" would pass every refusal test.

The third is the stage table. A predicate that depends on the retrospective's
stage is walked through all six stages and its whole row of expected answers is
asserted, not only the stage it cares about.

The fourth is that consolidation is checked in the source as well as in the
behaviour: no second permissions module, no rule defined outside this one, and
the function-level import #7 needed to dodge a circular import is gone.
"""

import ast
import inspect
import itertools
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext

from cycles.models import Card, FeedbackCycle
from projects import permissions
from projects.models import Membership, Project
from projects.permissions import (
    can_add_card,
    can_advance_stage,
    can_cast_vote,
    can_close_cycle,
    can_confirm_extraction,
    can_create_cluster,
    can_delete_card,
    can_delete_cluster,
    can_edit_card,
    can_merge_cluster,
    can_move_card,
    can_open_cycle,
    can_rename_cluster,
    can_rotate_join_token,
    can_see_vote_totals,
    can_split_cluster,
    can_start_retrospective,
    can_upload_recording,
    can_view_card,
    can_view_project,
    can_view_summary,
)
from retro.models import STAGE_ORDER, Cluster, Retrospective

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage
Status = FeedbackCycle.Status

_SERIAL = itertools.count(1)


# --------------------------------------------------------------------------
# A world to ask the questions in
#
# `lead` is the one person every rule can say yes to: the project's owner, a
# FACILITATOR member, this cycle's facilitator and the card's author. That is
# what makes the refusal tests non-vacuous — the same call that returns True
# here returns False for everyone else.
# --------------------------------------------------------------------------


class World:
    """One project, one cycle, one card, and optionally a retrospective and cluster."""

    def __init__(self, *, stage: str | None, status: str) -> None:
        # A serial, so a test may build two worlds — an open cycle and a closed
        # one, say — without their people colliding on a username.
        serial = next(_SERIAL)
        self.lead = make_user(f"lead-{serial}")
        self.member = make_user(f"member-{serial}")
        self.other_lead = make_user(f"other-lead-{serial}")
        self.outsider = make_user(f"outsider-{serial}")
        self.superuser = make_user(f"root-{serial}", is_superuser=True, is_staff=True)
        self.anonymous = AnonymousUser()

        self.project = Project.objects.create(name="Platform", owner=self.lead)
        Membership.objects.create(
            project=self.project, user=self.lead, role=Membership.Role.FACILITATOR
        )
        Membership.objects.create(
            project=self.project, user=self.member, role=Membership.Role.MEMBER
        )
        Membership.objects.create(
            project=self.project, user=self.other_lead, role=Membership.Role.FACILITATOR
        )

        # A whole project of their own, so "not on this project" is tested
        # against a real user of the product rather than a stranger.
        other_project = Project.objects.create(name="Payments", owner=self.outsider)
        Membership.objects.create(
            project=other_project, user=self.outsider, role=Membership.Role.FACILITATOR
        )

        self.cycle = FeedbackCycle.objects.create(
            project=self.project,
            week_start=MONDAY,
            opens_at=OPENS_AT,
            closes_at=CLOSES_AT,
            facilitator=self.lead,
            status=status,
        )
        self.card = Card.objects.create(
            cycle=self.cycle,
            category=Card.Category.START,
            text="Pair on the parser",
            author=self.lead,
        )
        self.retro = None
        # A cluster exists exactly when a retrospective does: it hangs off the
        # retrospective, so a cycle that has not started one has no board to
        # hold clusters. The rules that take one are given a world with a stage.
        self.cluster = None
        if stage is not None:
            self.retro = Retrospective.objects.create(cycle=self.cycle, stage=stage)
            self.cluster = Cluster.objects.create(
                retrospective=self.retro, name="Deploys", position=1
            )

        self.refresh()

    def refresh(self) -> None:
        """Re-read every row, so no relation is answered from a stale cache."""
        self.project = Project.objects.get(pk=self.project.pk)
        self.cycle = FeedbackCycle.objects.get(pk=self.cycle.pk)
        self.card = Card.objects.select_related("cycle", "cycle__project").get(pk=self.card.pk)
        if self.retro is not None:
            self.retro = Retrospective.objects.select_related("cycle__project").get(
                pk=self.retro.pk
            )
        if self.cluster is not None:
            self.cluster = Cluster.objects.select_related("retrospective__cycle__project").get(
                pk=self.cluster.pk
            )

    def obj(self, kind: str):
        return {
            "project": self.project,
            "cycle": self.cycle,
            "card": self.card,
            "retro": self.retro,
            "cluster": self.cluster,
        }[kind]


def make_user(username: str, **extra) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, **extra)


def build(stage: str | None = None, status: str = Status.COLLECTING) -> World:
    return World(stage=stage, status=status)


# --------------------------------------------------------------------------
# The registry
#
# One entry per public predicate: the object it is handed, the world in which
# it says yes to `lead`, and what it says to an ordinary MEMBER in that same
# world. Nothing else in this file lists the rules, so adding one is one line.
# --------------------------------------------------------------------------


class Rule:
    def __init__(self, func, kind: str, *, stage: str | None, status: str, member: bool) -> None:
        self.func = func
        self.kind = kind
        self.stage = stage
        self.status = status
        self.member = member

    def world(self) -> World:
        return build(stage=self.stage, status=self.status)


RULES: dict[str, Rule] = {
    "can_view_project": Rule(
        can_view_project, "project", stage=None, status=Status.COLLECTING, member=True
    ),
    "can_rotate_join_token": Rule(
        can_rotate_join_token, "project", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_open_cycle": Rule(
        can_open_cycle, "project", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_close_cycle": Rule(
        can_close_cycle, "cycle", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_start_retrospective": Rule(
        can_start_retrospective, "cycle", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_add_card": Rule(can_add_card, "cycle", stage=None, status=Status.COLLECTING, member=True),
    "can_view_card": Rule(
        can_view_card, "card", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_edit_card": Rule(
        can_edit_card, "card", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_delete_card": Rule(
        can_delete_card, "card", stage=None, status=Status.COLLECTING, member=False
    ),
    "can_move_card": Rule(
        can_move_card, "card", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_create_cluster": Rule(
        can_create_cluster, "retro", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_rename_cluster": Rule(
        can_rename_cluster, "cluster", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_merge_cluster": Rule(
        can_merge_cluster, "cluster", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_split_cluster": Rule(
        can_split_cluster, "cluster", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_delete_cluster": Rule(
        can_delete_cluster, "cluster", stage=Stage.REVEAL, status=Status.CLOSED, member=True
    ),
    "can_advance_stage": Rule(
        can_advance_stage, "retro", stage=Stage.DRAFT, status=Status.COLLECTING, member=False
    ),
    "can_cast_vote": Rule(
        can_cast_vote, "retro", stage=Stage.VOTE, status=Status.CLOSED, member=True
    ),
    "can_see_vote_totals": Rule(
        can_see_vote_totals, "retro", stage=Stage.DISCUSS, status=Status.CLOSED, member=True
    ),
    "can_upload_recording": Rule(
        can_upload_recording, "retro", stage=Stage.DISCUSS, status=Status.CLOSED, member=False
    ),
    "can_confirm_extraction": Rule(
        can_confirm_extraction, "retro", stage=Stage.DISCUSS, status=Status.CLOSED, member=False
    ),
    "can_view_summary": Rule(
        can_view_summary, "retro", stage=Stage.COMPLETE, status=Status.CLOSED, member=True
    ),
}

NAMES = sorted(RULES)


def ask(name: str, user, world: World) -> bool:
    rule = RULES[name]
    return rule.func(user, world.obj(rule.kind))


# --------------------------------------------------------------------------
# The shape of the module
# --------------------------------------------------------------------------


def public_names() -> list[str]:
    """Every public callable the module exports — the rules, and nothing else."""
    return sorted(
        name
        for name, value in vars(permissions).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == permissions.__name__
    )


def test_every_public_name_in_the_module_is_exercised() -> None:
    """A predicate added later without a test is visible here, not in review."""
    assert public_names() == NAMES


def test_the_public_surface_is_exactly_the_rules() -> None:
    """Helpers are private, so what the module exports is the questions it answers."""
    for name in public_names():
        assert name.startswith("can_"), name

    helpers = [
        name
        for name, value in vars(permissions).items()
        if name.startswith("_") and inspect.isfunction(value) and not name.startswith("__")
    ]
    assert "_is_facilitator" in helpers


@pytest.mark.parametrize("name", NAMES)
def test_every_rule_takes_exactly_a_user_and_one_object(name: str) -> None:
    """`(user, obj)`. No request, no keyword-only extras, no **kwargs."""
    signature = inspect.signature(RULES[name].func)
    parameters = list(signature.parameters.values())

    assert len(parameters) == 2, name
    assert parameters[0].name == "user"
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in parameters), name
    assert "request" not in signature.parameters


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_rule_answers_a_bool(name: str) -> None:
    world = RULES[name].world()

    assert ask(name, world.lead, world) is True or ask(name, world.lead, world) is False


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_rule_says_yes_to_someone(name: str) -> None:
    """The grant case. Without it every refusal below would pass vacuously."""
    world = RULES[name].world()

    assert ask(name, world.lead, world) is True


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_rule_refuses_an_anonymous_user_rather_than_raising(name: str) -> None:
    world = RULES[name].world()

    assert ask(name, world.anonymous, world) is False


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_rule_refuses_a_deactivated_account(name: str) -> None:
    """The same person, the same world, with `is_active=False` — nothing is granted."""
    world = RULES[name].world()
    assert ask(name, world.lead, world) is True

    world.lead.is_active = False
    world.lead.save(update_fields=["is_active"])

    assert ask(name, world.lead, world) is False


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_every_rule_refuses_a_member_of_a_different_project(name: str) -> None:
    world = RULES[name].world()

    assert ask(name, world.outsider, world) is False


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_a_superuser_from_outside_the_project_is_granted_nothing(name: str) -> None:
    """No implicit grant, anywhere.

    Staff does not reveal another member's card and cannot recover an anonymous
    author: `_docs/decisions.md` item 3 has no admin exception, so this module
    has none either.
    """
    world = RULES[name].world()

    assert world.superuser.is_superuser is True
    assert ask(name, world.superuser, world) is False


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_an_ordinary_member_gets_the_member_rules_and_no_others(name: str) -> None:
    """The table that separates a member from a facilitator, rule by rule."""
    rule = RULES[name]
    world = rule.world()

    assert ask(name, world.member, world) is rule.member


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_no_rule_writes_anything(name: str) -> None:
    """A predicate answers a question. It never writes, and it never raises."""
    world = RULES[name].world()

    with CaptureQueriesContext(connection) as captured:
        for user in (world.lead, world.member, world.outsider, world.superuser, world.anonymous):
            ask(name, user, world)

    for query in captured.captured_queries:
        statement = query["sql"].strip().lower()
        assert statement.startswith("select"), statement


@pytest.mark.django_db
@pytest.mark.parametrize("name", NAMES)
def test_no_rule_changes_the_object_it_is_handed(name: str) -> None:
    rule = RULES[name]
    world = rule.world()
    before = {
        "cycle": (world.cycle.status, world.cycle.facilitator_id),
        "card": (world.card.text, world.card.author_id),
        "stage": None if world.retro is None else (world.retro.stage, world.retro.version),
    }

    for user in (world.lead, world.member, world.outsider, world.anonymous):
        ask(name, user, world)

    world.refresh()
    after = {
        "cycle": (world.cycle.status, world.cycle.facilitator_id),
        "card": (world.card.text, world.card.author_id),
        "stage": None if world.retro is None else (world.retro.stage, world.retro.version),
    }
    assert after == before


# --------------------------------------------------------------------------
# Project
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_members_see_a_project() -> None:
    world = build()

    assert can_view_project(world.lead, world.project) is True
    assert can_view_project(world.member, world.project) is True
    assert can_view_project(world.outsider, world.project) is False
    assert can_view_project(world.superuser, world.project) is False
    assert can_view_project(world.anonymous, world.project) is False


@pytest.mark.django_db
def test_rotating_the_join_token_is_the_owner_or_a_facilitator() -> None:
    world = build()

    assert can_rotate_join_token(world.lead, world.project) is True
    assert can_rotate_join_token(world.other_lead, world.project) is True
    assert can_rotate_join_token(world.member, world.project) is False


@pytest.mark.django_db
def test_an_owner_who_left_the_membership_table_still_owns_the_project() -> None:
    """The owner is authorized as the owner, not by their membership row."""
    world = build()
    Membership.objects.filter(project=world.project, user=world.lead).delete()

    assert can_rotate_join_token(world.lead, world.project) is True
    assert can_open_cycle(world.lead, world.project) is True
    # Seeing the project is membership and nothing else, so it does not follow.
    assert can_view_project(world.lead, world.project) is False


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_plain_member_cannot_open_a_cycle() -> None:
    world = build()

    assert can_open_cycle(world.lead, world.project) is True
    assert can_open_cycle(world.other_lead, world.project) is True
    assert can_open_cycle(world.member, world.project) is False


@pytest.mark.django_db
def test_closing_is_this_cycles_facilitator_while_it_is_collecting() -> None:
    world = build()

    assert can_close_cycle(world.lead, world.cycle) is True
    # A facilitator of the project who is not this week's facilitator.
    assert can_close_cycle(world.other_lead, world.cycle) is False
    assert can_close_cycle(world.member, world.cycle) is False


@pytest.mark.django_db
def test_a_closed_cycle_cannot_be_closed_again_by_anyone() -> None:
    """What makes closing twice impossible rather than merely hidden."""
    world = build(status=Status.CLOSED)

    assert can_close_cycle(world.lead, world.cycle) is False
    assert can_close_cycle(world.other_lead, world.cycle) is False
    assert can_close_cycle(world.member, world.cycle) is False


@pytest.mark.django_db
def test_adding_a_card_wants_a_member_and_a_collecting_cycle() -> None:
    world = build()

    assert can_add_card(world.lead, world.cycle) is True
    assert can_add_card(world.member, world.cycle) is True
    assert can_add_card(world.outsider, world.cycle) is False

    closed = build(status=Status.CLOSED)
    assert can_add_card(closed.lead, closed.cycle) is False
    assert can_add_card(closed.member, closed.cycle) is False


@pytest.mark.django_db
def test_starting_a_retrospective_is_the_facilitator_and_only_once() -> None:
    world = build()

    assert can_start_retrospective(world.lead, world.cycle) is True
    assert can_start_retrospective(world.other_lead, world.cycle) is False
    assert can_start_retrospective(world.member, world.cycle) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_a_cycle_that_has_a_retrospective_cannot_start_another_at_any_stage(stage: str) -> None:
    world = build(stage=stage, status=Status.CLOSED)

    assert can_start_retrospective(world.lead, world.cycle) is False


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_who_sees_a_card_at_every_stage(stage: str) -> None:
    """The author always; everyone else in the project from REVEAL on."""
    world = build(stage=stage, status=Status.CLOSED)
    revealed = STAGE_ORDER.index(stage) >= STAGE_ORDER.index(Stage.REVEAL)

    assert can_view_card(world.lead, world.card) is True
    assert can_view_card(world.member, world.card) is revealed
    assert can_view_card(world.other_lead, world.card) is revealed
    # Never, at any stage, for someone who is not on the project.
    assert can_view_card(world.outsider, world.card) is False
    assert can_view_card(world.superuser, world.card) is False
    assert can_view_card(world.anonymous, world.card) is False


@pytest.mark.django_db
def test_a_cycle_with_no_retrospective_is_before_reveal() -> None:
    """The state every cycle starts in: only the author sees the card."""
    world = build()
    assert world.retro is None

    assert can_view_card(world.lead, world.card) is True
    assert can_view_card(world.member, world.card) is False
    assert can_view_card(world.other_lead, world.card) is False
    assert can_view_card(world.superuser, world.card) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_editing_and_deleting_a_card_end_when_collection_does(stage: str) -> None:
    open_cycle = build(stage=stage, status=Status.COLLECTING)
    assert can_edit_card(open_cycle.lead, open_cycle.card) is True
    assert can_delete_card(open_cycle.lead, open_cycle.card) is True

    closed = build(stage=stage, status=Status.CLOSED)
    assert can_edit_card(closed.lead, closed.card) is False
    assert can_delete_card(closed.lead, closed.card) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_a_card_whose_author_is_gone_is_nobodys_to_change(stage: str) -> None:
    """A destroyed anonymous author can never be matched — decisions.md item 3."""
    world = build(stage=stage, status=Status.COLLECTING)
    Card.objects.filter(pk=world.card.pk).update(author=None)
    world.refresh()

    for user in (world.lead, world.member, world.other_lead, world.superuser, world.anonymous):
        assert can_edit_card(user, world.card) is False
        assert can_delete_card(user, world.card) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_moving_a_card_is_open_in_reveal_and_cluster_and_frozen_after(stage: str) -> None:
    """The `-> VOTE` transition freezes cluster membership, so the rule stops there."""
    world = build(stage=stage, status=Status.CLOSED)
    movable = stage in {Stage.REVEAL, Stage.CLUSTER}

    assert can_move_card(world.lead, world.card) is movable
    assert can_move_card(world.member, world.card) is movable
    assert can_move_card(world.outsider, world.card) is False
    assert can_move_card(world.superuser, world.card) is False
    assert can_move_card(world.anonymous, world.card) is False


@pytest.mark.django_db
def test_a_card_with_no_retrospective_cannot_be_moved() -> None:
    world = build()

    assert can_move_card(world.lead, world.card) is False
    assert can_move_card(world.member, world.card) is False


# --------------------------------------------------------------------------
# Clusters
# --------------------------------------------------------------------------

#: The five rules #12 added, all of them the same window as `can_move_card`.
#: The four that take a cluster are walked together; creating takes the
#: retrospective, because there is no cluster to ask about yet.
CLUSTER_RULES = (can_rename_cluster, can_merge_cluster, can_split_cluster, can_delete_cluster)


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_changing_a_cluster_is_open_in_reveal_and_cluster_and_frozen_after(stage: str) -> None:
    """The board's shape is the team's, until the `-> VOTE` transition freezes it.

    The same window as `can_move_card`, asserted rule by rule rather than
    inferred from the fact that they share a helper: a cluster that could still
    be merged after the votes were cast would move the votes with it.
    """
    world = build(stage=stage, status=Status.CLOSED)
    changeable = stage in {Stage.REVEAL, Stage.CLUSTER}

    for rule in CLUSTER_RULES:
        assert rule(world.lead, world.cluster) is changeable, rule.__name__
        assert rule(world.member, world.cluster) is changeable, rule.__name__
        assert rule(world.outsider, world.cluster) is False, rule.__name__
        assert rule(world.superuser, world.cluster) is False, rule.__name__
        assert rule(world.anonymous, world.cluster) is False, rule.__name__


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_creating_a_cluster_is_open_in_the_same_two_stages(stage: str) -> None:
    world = build(stage=stage, status=Status.CLOSED)
    changeable = stage in {Stage.REVEAL, Stage.CLUSTER}

    assert can_create_cluster(world.lead, world.retro) is changeable
    assert can_create_cluster(world.member, world.retro) is changeable
    assert can_create_cluster(world.outsider, world.retro) is False
    assert can_create_cluster(world.superuser, world.retro) is False
    assert can_create_cluster(world.anonymous, world.retro) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [Stage.REVEAL, Stage.CLUSTER])
def test_a_suggested_cluster_is_governed_by_exactly_the_same_rules(stage: str) -> None:
    """#22's rows are not privileged and not protected — the flag is wording only."""
    world = build(stage=stage, status=Status.CLOSED)
    Cluster.objects.filter(pk=world.cluster.pk).update(is_auto_generated=True)
    world.refresh()

    assert world.cluster.is_auto_generated is True
    for rule in CLUSTER_RULES:
        assert rule(world.member, world.cluster) is True, rule.__name__
        assert rule(world.outsider, world.cluster) is False, rule.__name__


@pytest.mark.django_db
def test_a_cluster_rule_reaches_its_project_through_its_retrospective() -> None:
    """A cluster on another team's board is nobody's here, at any stage."""
    ours = build(stage=Stage.CLUSTER, status=Status.CLOSED)
    theirs = build(stage=Stage.CLUSTER, status=Status.CLOSED)

    for rule in CLUSTER_RULES:
        assert rule(ours.member, ours.cluster) is True, rule.__name__
        assert rule(ours.member, theirs.cluster) is False, rule.__name__
        assert rule(theirs.member, ours.cluster) is False, rule.__name__


# --------------------------------------------------------------------------
# Retrospective
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_advancing_answers_who_and_not_which_transition(stage: str) -> None:
    """True for the facilitator at every stage, COMPLETE included.

    Forward-only, single-step and COMPLETE being terminal are `advance_stage()`'s
    rules, because it is the only place both stages are known. The predicate is
    handed no target stage and does not pretend to answer that.
    """
    world = build(stage=stage, status=Status.CLOSED)

    assert can_advance_stage(world.lead, world.retro) is True
    assert can_advance_stage(world.other_lead, world.retro) is False
    assert can_advance_stage(world.member, world.retro) is False
    assert can_advance_stage(world.outsider, world.retro) is False
    assert can_advance_stage(world.superuser, world.retro) is False
    assert can_advance_stage(world.anonymous, world.retro) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_voting_is_open_only_during_vote(stage: str) -> None:
    world = build(stage=stage, status=Status.CLOSED)
    voting = stage == Stage.VOTE

    assert can_cast_vote(world.lead, world.retro) is voting
    assert can_cast_vote(world.member, world.retro) is voting
    assert can_cast_vote(world.outsider, world.retro) is False
    assert can_cast_vote(world.superuser, world.retro) is False
    assert can_cast_vote(world.anonymous, world.retro) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_totals_stay_hidden_until_voting_is_over(stage: str) -> None:
    """Hidden during VOTE, which is what makes a vote reassignable — item 2."""
    world = build(stage=stage, status=Status.CLOSED)
    past_vote = STAGE_ORDER.index(stage) > STAGE_ORDER.index(Stage.VOTE)

    assert can_see_vote_totals(world.lead, world.retro) is past_vote
    assert can_see_vote_totals(world.member, world.retro) is past_vote
    assert can_see_vote_totals(world.outsider, world.retro) is False
    assert can_see_vote_totals(world.superuser, world.retro) is False
    assert can_see_vote_totals(world.anonymous, world.retro) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_the_recording_and_its_extraction_are_the_facilitators(stage: str) -> None:
    world = build(stage=stage, status=Status.CLOSED)

    for rule in (can_upload_recording, can_confirm_extraction):
        assert rule(world.lead, world.retro) is True
        assert rule(world.other_lead, world.retro) is False
        assert rule(world.member, world.retro) is False
        assert rule(world.outsider, world.retro) is False
        assert rule(world.superuser, world.retro) is False
        assert rule(world.anonymous, world.retro) is False


@pytest.mark.django_db
@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_the_summary_is_the_whole_teams_at_every_stage(stage: str) -> None:
    world = build(stage=stage, status=Status.CLOSED)

    assert can_view_summary(world.lead, world.retro) is True
    assert can_view_summary(world.member, world.retro) is True
    assert can_view_summary(world.outsider, world.retro) is False
    assert can_view_summary(world.superuser, world.retro) is False
    assert can_view_summary(world.anonymous, world.retro) is False


@pytest.mark.django_db
def test_authority_over_a_retrospective_is_per_cycle_not_per_project() -> None:
    """A facilitator of the project is not this week's facilitator."""
    world = build(stage=Stage.DRAFT, status=Status.COLLECTING)

    assert can_advance_stage(world.other_lead, world.retro) is False
    assert can_upload_recording(world.other_lead, world.retro) is False
    assert can_confirm_extraction(world.other_lead, world.retro) is False


# --------------------------------------------------------------------------
# One module, and no rule outside it
# --------------------------------------------------------------------------

APP_PACKAGES = ("accounts", "projects", "cycles", "retro", "board")

PERMISSIONS_PATH = BASE_DIR / "projects" / "permissions.py"


def app_sources() -> list[Path]:
    return [
        path
        for package in APP_PACKAGES
        for path in sorted((BASE_DIR / package).rglob("*.py"))
        if "migrations" not in path.parts
    ]


def test_exactly_one_permissions_module_exists() -> None:
    found = [
        str(path.relative_to(BASE_DIR))
        for package in APP_PACKAGES
        for path in (BASE_DIR / package).rglob("permissions.py")
    ]

    assert found == ["projects/permissions.py"]


def test_no_rule_is_defined_outside_the_permissions_module() -> None:
    """The `# Rules` banners #5, #7, #8 and #9 carried are gone, not copied."""
    for path in app_sources():
        if path == PERMISSIONS_PATH:
            continue
        source = path.read_text()
        for name in NAMES:
            assert f"def {name}(" not in source, f"{path} defines {name}"
        assert "# Rules." not in source, path


def test_the_old_names_are_gone_with_no_alias_left_behind() -> None:
    """`is_member` became `can_view_project`; one name per rule, everywhere.

    `is_facilitator` survives only as the private helper `_is_facilitator`, and
    the membership lookup only as `_is_member`. Neither is a name a caller can
    reach, so there is still exactly one public name per rule.
    """
    for path in app_sources():
        source = path.read_text()
        assert "def is_member(" not in source, path
        assert "def is_facilitator(" not in source, path

    assert not hasattr(permissions, "is_member")
    assert not hasattr(permissions, "is_facilitator")
    assert public_names() == NAMES


def test_no_access_comparison_survives_outside_the_module() -> None:
    """Who someone is, is decided in one file.

    A stage or status comparison that expresses a business rule — reveal closing
    the cycle, the forward-only transition table — is not an access check and is
    left where it is, which is why this looks for the identity comparisons.
    """
    for path in app_sources():
        if path == PERMISSIONS_PATH:
            continue
        source = path.read_text()
        for marker in ("request.user ==", "owner_id ==", "facilitator_id ==", "role =="):
            assert marker not in source, f"{path} decides access with {marker!r}"


def test_the_permission_module_imports_nothing_from_a_view() -> None:
    """It sits under the views, so no view ever has to import it lazily."""
    tree = ast.parse(PERMISSIONS_PATH.read_text())
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]

    assert imported, "the module imports the models it reads"
    for module in imported:
        assert not module.endswith("views"), module
        assert not module.endswith("services"), module


def test_projects_views_imports_the_rules_at_module_level() -> None:
    """#7's carrying condition: the function-level import is gone.

    `projects.views` imported `can_open_cycle` inside `project_detail` to dodge
    a circular import with `cycles.views`. Consolidating removed the cycle, so
    the import is a plain one at the top and every import in the module is.
    """
    path = BASE_DIR / "projects" / "views.py"
    tree = ast.parse(path.read_text())

    top_level = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert any(
        node.module == "projects.permissions"
        and {alias.name for alias in node.names} >= {"can_open_cycle", "can_view_project"}
        for node in top_level
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            nested = [
                child for child in ast.walk(node) if isinstance(child, ast.Import | ast.ImportFrom)
            ]
            assert nested == [], f"{node.name} imports inside its body"

    assert "from cycles.views import" not in path.read_text()


def test_advancing_a_stage_still_bumps_the_version_exactly_once() -> None:
    """#9's carrying condition: this task added no second bump."""
    source = (BASE_DIR / "retro" / "services.py").read_text()

    assert source.count("bump_version(locked)") == 1
    assert source.count("def bump_version(") == 1
