"""Build one realistic demo team, three weeks of history behind it.

`seed_demo()` is the whole surface. It is called from
`demo/management/commands/seed_demo.py` inside a `transaction.atomic()` block,
and it does two things: it deletes the demo data it owns (scoped to the fixed
demo usernames and the projects they own — never a table-wide delete), then it
rebuilds it from the literal content in this module.

Why it writes rows directly instead of driving the real services
----------------------------------------------------------------

The command constructs each retrospective in the stage it should end in rather
than calling `retro.services.advance_stage()` or `cycles.reveal.reveal_cycle()`.
Driving the real reveal would fire #22's OpenAI clustering call and shuffle the
positions with #10's `random.SystemRandom`, which no seed can reproduce — so the
demo would be non-deterministic and would need a worker and an API key. This is
the pattern `_docs/decisions.md` item 9 blesses: "construct the state you need
directly". The cost is that this module has to be kept in step with the models,
and `tests/test_seed_demo.py` walks the seeded invariants to catch a drift.

What it reproduces from the real reveal
---------------------------------------

The state a revealed cycle leaves behind, exactly:

- every anonymous card has `author` NULL — the reveal destroys the link
  (`_docs/decisions.md` items 3 and 3a), so a seeded anonymous card that kept an
  author would be a false demonstration of the product's central promise;
- every card in a revealed cycle has a distinct `position`, handed out in an
  order that is not submission order — the shuffle exists to destroy submission
  order, so the seed shuffles too, with the module-seeded `random.Random`;
- a `CycleParticipation` row exists for every member, including the ones who
  submitted nothing (`card_count=0`, `submitted_at` NULL).

A cycle that is still `COLLECTING` (cycle 3) has none of that: anonymity is
applied at reveal, not at write time (`_docs/decisions.md` item 8), so its
anonymous cards keep their authors and its cards keep `position=0`.

Determinism
-----------

Every date is derived from the current week's Monday at run time, and all
randomness comes from a `random.Random` seeded with `DEMO_SEED` (overridable
with `--seed`). Two runs in the same week therefore differ only in primary
keys. `auto_now_add` timestamps are backdated with a queryset `.update()` after
insert so the story spans the last three weeks rather than landing every row on
one microsecond.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime

from django.contrib.auth import get_user_model
from django.utils import timezone

from cycles.models import Card, CycleParticipation, FeedbackCycle, monday_of
from meetings.models import MeetingRecord, Transcript
from projects.models import Membership, Project
from retro.models import (
    ActionItem,
    Cluster,
    Decision,
    Note,
    Retrospective,
    Vote,
)

User = get_user_model()

# --------------------------------------------------------------------------
# Pinned identity — quoted in the README and the printed output, so it may not
# move. A demo whose logins change is worse than no demo.
# --------------------------------------------------------------------------

#: Shared by every demo user, `demo_admin` included. It clears the validators in
#: `config/settings.py` (long enough, not all-numeric, not a common password, not
#: similar to a username), so a person can retype it at a signup form. Overridden
#: by `--password`.
DEMO_PASSWORD = "retro-demo-2026"

#: Seeds the `random.Random` the shuffle draws from. A module constant so two
#: runs in the same week are identical; `--seed` overrides it for anyone who
#: wants a different-but-reproducible dataset. Never `random.SystemRandom`, and
#: never the module-level `random` functions.
DEMO_SEED = 20260722

#: Fixed so the printed join link is the same on every machine and can be pasted
#: into documentation.
PLATFORM_JOIN_TOKEN = uuid.UUID("11111111-1111-4111-8111-111111111111")
DESIGN_JOIN_TOKEN = uuid.UUID("22222222-2222-4222-8222-222222222222")

PLATFORM_TEAM = "Platform Team"
DESIGN_GUILD = "Design Guild"

ADMIN_USERNAME = "demo_admin"

#: (username, display name, Platform Team role). `demo_priya` owns both
#: projects. `Alex Novak` and `Alex Turner` share a first name on purpose: it is
#: what makes the ambiguous-owner draft (#23) visible on the review screen.
ROSTER: tuple[tuple[str, str, str], ...] = (
    ("demo_priya", "Priya Raman", Membership.Role.FACILITATOR),
    ("demo_mei", "Mei Lin", Membership.Role.FACILITATOR),
    ("demo_sam", "Sam Okafor", Membership.Role.MEMBER),
    ("demo_alex_n", "Alex Novak", Membership.Role.MEMBER),
    ("demo_alex_t", "Alex Turner", Membership.Role.MEMBER),
    ("demo_tom", "Tom Weber", Membership.Role.MEMBER),
)

#: Every row the command owns is found through these usernames and the projects
#: owned by them — never "every project" and never a heuristic on the name.
DEMO_USERNAMES: tuple[str, ...] = (*(u for u, _, _ in ROSTER), ADMIN_USERNAME)


# --------------------------------------------------------------------------
# Result of a build, for the command to print from.
# --------------------------------------------------------------------------


@dataclass
class SeedResult:
    platform: Project
    design: Project
    open_cycle: FeedbackCycle
    complete_retro: Retrospective
    discuss_retro: Retrospective
    password: str


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def seed_demo(*, seed: int = DEMO_SEED, password: str = DEMO_PASSWORD) -> SeedResult:
    """Delete the demo data this command owns and rebuild it. Must run in a transaction.

    The caller wraps this in one `transaction.atomic()` so a failure halfway
    through leaves the database as it was — no demo user, no demo project, no
    partial cycle.
    """
    _delete_demo_data()
    return _DemoBuilder(seed=seed, password=password).build()


def _delete_demo_data() -> None:
    """Remove exactly the demo rows, and nothing else.

    Projects owned by a demo user go first, which cascades their cycles, cards,
    participation, retrospectives, clusters, votes, notes, decisions, action
    items and meeting records. Only then are the users deleted — a cycle's
    `facilitator` and a meeting's `uploaded_by` are `PROTECT`, so a user cannot
    be removed while a cycle or meeting still points at them, and deleting the
    projects first is what clears those references.

    Scoped to `DEMO_USERNAMES` and the projects those users own. An unrelated
    user, project, cycle or card is named by none of this and survives.
    """
    demo_users = User.objects.filter(username__in=DEMO_USERNAMES)
    Project.objects.filter(owner__in=demo_users).delete()
    demo_users.delete()


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------


class _DemoBuilder:
    """Constructs the demo, deterministically, from the literal content below."""

    def __init__(self, *, seed: int, password: str) -> None:
        self.rng = random.Random(seed)
        self.password = password
        today = timezone.localdate()
        self.this_monday = monday_of(today)
        self.week1 = self.this_monday - timedelta(weeks=3)  # cycle 1, COMPLETE
        self.week2 = self.this_monday - timedelta(weeks=1)  # cycle 2, DISCUSS
        self.week3 = self.this_monday  # cycle 3, COLLECTING
        self.users: dict[str, User] = {}

    # -- orchestration ----------------------------------------------------

    def build(self) -> SeedResult:
        self._create_users()
        platform = self._create_platform_project()
        design = self._create_design_project()

        complete_retro = self._build_cycle_1(platform)
        discuss_retro = self._build_cycle_2(platform)
        open_cycle = self._build_cycle_3(platform)

        return SeedResult(
            platform=platform,
            design=design,
            open_cycle=open_cycle,
            complete_retro=complete_retro,
            discuss_retro=discuss_retro,
            password=self.password,
        )

    # -- people and projects ---------------------------------------------

    def _create_users(self) -> None:
        for username, display_name, _role in ROSTER:
            user = User(username=username, display_name=display_name, is_active=True)
            user.set_password(self.password)
            user.save()
            self.users[username] = user

        admin = User(
            username=ADMIN_USERNAME,
            display_name="Demo Admin",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        admin.set_password(self.password)
        admin.save()
        self.users[ADMIN_USERNAME] = admin

    def _create_platform_project(self) -> Project:
        project = Project.objects.create(
            name=PLATFORM_TEAM,
            owner=self.users["demo_priya"],
            join_token=PLATFORM_JOIN_TOKEN,
        )
        for username, _display, role in ROSTER:
            Membership.objects.create(project=project, user=self.users[username], role=role)
        self._backdate(Project, project.pk, "created_at", self._at(self.week1, 8, 0))
        Membership.objects.filter(project=project).update(joined_at=self._at(self.week1, 8, 0))
        return project

    def _create_design_project(self) -> Project:
        """`demo_priya` owns it, `demo_mei` is the only other member, no cycles.

        It is what makes #26's empty-state dashboard and #5's "more than one
        project" reachable. `demo_tom` opening it gets 404.
        """
        project = Project.objects.create(
            name=DESIGN_GUILD,
            owner=self.users["demo_priya"],
            join_token=DESIGN_JOIN_TOKEN,
        )
        Membership.objects.create(
            project=project,
            user=self.users["demo_priya"],
            role=Membership.Role.FACILITATOR,
        )
        Membership.objects.create(
            project=project, user=self.users["demo_mei"], role=Membership.Role.MEMBER
        )
        self._backdate(Project, project.pk, "created_at", self._at(self.week1, 8, 5))
        Membership.objects.filter(project=project).update(joined_at=self._at(self.week1, 8, 5))
        return project

    # -- cycle 1: three weeks ago, COMPLETE ------------------------------

    def _build_cycle_1(self, project: Project) -> Retrospective:
        cycle = self._create_cycle(
            project,
            week=self.week1,
            facilitator="demo_priya",
            status=FeedbackCycle.Status.CLOSED,
        )

        cards = CYCLE1_CARDS
        created = self._create_cards(cycle, cards)
        self._reveal(cycle, created, cards)

        retro = Retrospective.objects.create(
            cycle=cycle,
            stage=Retrospective.Stage.COMPLETE,
            started_at=self._at(self.week1 + timedelta(days=4), 17, 30),
            completed_at=self._at(self.week1 + timedelta(days=4), 18, 30),
            version=14,
        )
        self._backdate(
            Retrospective, retro.pk, "created_at", self._at(self.week1 + timedelta(days=4), 17, 0)
        )

        # Four clusters: two auto-generated, two hand-made; the four statuses the
        # summary has to render. Cards 0..14 as ordered above; the rest stay
        # ungrouped, because ungrouped is a normal state the summary must show.
        c1 = self._cluster(retro, "Protecting focus time", 1, True, Cluster.Status.DISCUSSED)
        c2 = self._cluster(retro, "CI and merge safety", 2, False, Cluster.Status.DISCUSSED)
        c3 = self._cluster(retro, "Environment drift", 3, False, Cluster.Status.SKIPPED)
        c4 = self._cluster(retro, "On-call load", 4, True, Cluster.Status.DEFERRED)
        self._group(created, {2: c1, 3: c1, 6: c1, 5: c2, 12: c2, 7: c3, 8: c4, 13: c4})
        # cards 0,1,4,9,10,11,14 stay ungrouped.

        # Three votes per member. `demo_priya` stacks all three on one cluster;
        # c1 and c4 tie on seven; c3 ends with none. All six members vote.
        self._votes(
            retro,
            [
                ("demo_priya", c1, 3),
                ("demo_mei", c1, 2),
                ("demo_mei", c2, 1),
                ("demo_sam", c1, 1),
                ("demo_sam", c2, 1),
                ("demo_sam", c4, 1),
                ("demo_alex_n", c2, 2),
                ("demo_alex_n", c4, 1),
                ("demo_alex_t", c1, 1),
                ("demo_alex_t", c4, 2),
                ("demo_tom", c4, 3),
            ],
        )

        self._notes(
            retro,
            self.week1,
            [
                (
                    "demo_priya",
                    c1,
                    "Agreed to trial no-meeting mornings for two weeks, review next retro.",
                ),
                (
                    "demo_sam",
                    c2,
                    "We will make the integration suite a required check, not an optional one.",
                ),
                (
                    "demo_mei",
                    c4,
                    "On-call assignment will be checked against the roster in sprint planning.",
                ),
                (
                    "demo_alex_t",
                    None,
                    "General point: the team wants fewer but longer focus blocks overall.",
                ),
            ],
        )

        self._meeting(
            retro,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            uploaded_by="demo_priya",
            week=self.week1,
            duration_seconds=None,
            transcript=CYCLE1_TRANSCRIPT,
        )

        # Three decisions, five action items, all confirmed, mixing MANUAL and
        # EXTRACTED. No draft anywhere: advancing to COMPLETE discards drafts.
        self._decision(
            retro,
            c1,
            "No-meeting mornings are a two-week trial starting Monday.",
            Decision.Source.MANUAL,
            "demo_priya",
        )
        self._decision(
            retro,
            c2,
            "The integration test suite becomes a required status check before merge.",
            Decision.Source.EXTRACTED,
            None,
            excerpt="We keep merging past the integration tests. Let us make them required.",
        )
        self._decision(
            retro,
            None,
            "The Wednesday architecture office hour continues.",
            Decision.Source.MANUAL,
            "demo_mei",
        )

        past = self.this_monday - timedelta(days=3)
        future = self.this_monday + timedelta(days=10)
        self._action(
            retro,
            c2,
            "Make the integration suite a required check in CI.",
            owner="demo_sam",
            status=ActionItem.Status.DONE,
            due_date=self.week1 + timedelta(days=4),
            source=ActionItem.Source.MANUAL,
        )
        self._action(
            retro,
            c4,
            "Document the on-call escalation path in the shared runbook.",
            owner=None,
            status=ActionItem.Status.OPEN,
            due_date=future,
            source=ActionItem.Source.EXTRACTED,
            excerpt="Someone should write the escalation path down; nobody could find it.",
        )
        self._action(
            retro,
            c1,
            "Set up calendar blocks for no-meeting mornings across the team.",
            owner="demo_alex_n",
            status=ActionItem.Status.OPEN,
            due_date=past,
            source=ActionItem.Source.MANUAL,
        )
        self._action(
            retro,
            None,
            "Draft the RFC summary-paragraph guideline.",
            owner="demo_priya",
            status=ActionItem.Status.OPEN,
            due_date=future,
            source=ActionItem.Source.MANUAL,
        )
        self._action(
            retro,
            c3,
            "Investigate automating the staging refresh from production config.",
            owner="demo_mei",
            status=ActionItem.Status.OPEN,
            due_date=None,
            source=ActionItem.Source.EXTRACTED,
            excerpt="Staging keeps drifting; we should rebuild it from prod config on a schedule.",
        )
        return retro

    # -- cycle 2: last week, DISCUSS with drafts waiting -----------------

    def _build_cycle_2(self, project: Project) -> Retrospective:
        # Facilitated by demo_sam, not demo_priya: #7 allows handing the role
        # over for a week, and nothing else in the demo shows it.
        cycle = self._create_cycle(
            project,
            week=self.week2,
            facilitator="demo_sam",
            status=FeedbackCycle.Status.CLOSED,
        )
        cards = CYCLE2_CARDS
        created = self._create_cards(cycle, cards)
        self._reveal(cycle, created, cards)

        retro = Retrospective.objects.create(
            cycle=cycle,
            stage=Retrospective.Stage.DISCUSS,
            started_at=self._at(self.week2 + timedelta(days=4), 17, 30),
            completed_at=None,
            version=8,
            extraction_summary=(
                "The team agreed to record decisions in the ticket rather than in chat, "
                "to give design review a longer lead time, and to deal with the flaky payment "
                "test. Ownership of the alert list was left open."
            ),
            # Still a draft: the facilitator has not confirmed it, which is why
            # #24's review screen has a summary to review and #25 shows nothing.
            extraction_summary_confirmed=False,
        )
        self._backdate(
            Retrospective, retro.pk, "created_at", self._at(self.week2 + timedelta(days=4), 17, 0)
        )

        cc1 = self._cluster(retro, "Decision records", 1, True, Cluster.Status.DISCUSSED)
        cc2 = self._cluster(retro, "Design review cadence", 2, False, Cluster.Status.DISCUSSED)
        cc3 = self._cluster(retro, "Flaky tests", 3, False, Cluster.Status.PENDING)
        self._group(created, {0: cc1, 3: cc1, 1: cc2, 7: cc2, 5: cc3})

        self._votes(
            retro,
            [
                ("demo_priya", cc1, 2),
                ("demo_priya", cc2, 1),
                ("demo_mei", cc2, 3),
                ("demo_sam", cc1, 1),
                ("demo_sam", cc3, 2),
                ("demo_alex_n", cc1, 3),
                ("demo_alex_t", cc3, 3),
                ("demo_tom", cc1, 2),
                ("demo_tom", cc2, 1),
            ],
        )

        self._notes(
            retro,
            self.week2,
            [
                ("demo_sam", cc1, "We will add a decisions section to the ticket template."),
                ("demo_mei", cc2, "Design review gets a two-day lead time from next sprint."),
            ],
        )

        self._meeting(
            retro,
            kind=MeetingRecord.Kind.AUDIO,
            uploaded_by="demo_sam",
            week=self.week2,
            duration_seconds=1837.0,
            transcript=CYCLE2_TRANSCRIPT,
        )

        # Everything here is an EXTRACTED draft: #24's review screen is exactly
        # this. No CONFIRMED row exists in this cycle, so the summary and the
        # dashboard show nothing from it.
        self._decision(
            retro,
            cc1,
            "Adopt a decisions section in the ticket template.",
            Decision.Source.EXTRACTED,
            None,
            status=Decision.Status.DRAFT,
            excerpt="Let us just put decisions in the ticket so they do not vanish.",
        )
        self._decision(
            retro,
            cc2,
            "Give design review a two-day lead time.",
            Decision.Source.EXTRACTED,
            None,
            status=Decision.Status.DRAFT,
            excerpt="Design review needs the same lead time as code review.",
        )
        self._decision(
            retro,
            cc3,
            "Quarantine the flaky payment test until it is fixed.",
            Decision.Source.EXTRACTED,
            None,
            status=Decision.Status.DRAFT,
            excerpt="Someone should just quarantine that flaky test.",
        )

        self._action(
            retro,
            cc1,
            "Update the ticket template with a decisions section.",
            owner="demo_sam",
            status=ActionItem.Status.OPEN,
            due_date=None,
            source=ActionItem.Source.EXTRACTED,
            review_status=ActionItem.ReviewStatus.DRAFT,
            excerpt="Sam said he would update the template.",
        )
        # The ambiguous-name case #23 leaves unresolved: two members are called
        # Alex, so extraction cannot pick one and leaves the owner NULL.
        self._action(
            retro,
            cc3,
            "Investigate and fix the flaky payment test.",
            owner=None,
            status=ActionItem.Status.OPEN,
            due_date=None,
            source=ActionItem.Source.EXTRACTED,
            review_status=ActionItem.ReviewStatus.DRAFT,
            excerpt="Alex will pick it up.",
        )
        self._action(
            retro,
            cc2,
            "Publish the new design-review lead-time policy.",
            owner="demo_mei",
            status=ActionItem.Status.OPEN,
            due_date=None,
            source=ActionItem.Source.EXTRACTED,
            review_status=ActionItem.ReviewStatus.DRAFT,
            excerpt="Mei will write it up.",
        )
        self._action(
            retro,
            cc1,
            "Draw up the alert ownership list.",
            owner=None,
            status=ActionItem.Status.OPEN,
            due_date=None,
            source=ActionItem.Source.EXTRACTED,
            review_status=ActionItem.ReviewStatus.DRAFT,
            excerpt="We need someone to own the alert list.",
        )
        return retro

    # -- cycle 3: this week, COLLECTING ----------------------------------

    def _build_cycle_3(self, project: Project) -> FeedbackCycle:
        cycle = self._create_cycle(
            project,
            week=self.week3,
            facilitator="demo_priya",
            status=FeedbackCycle.Status.COLLECTING,
        )
        # Some but not all members: demo_tom and demo_alex_t submit nothing, so
        # the dashboard's "not yet" column has content. No retrospective row, no
        # participation, positions stay 0 — a collecting cycle has not revealed.
        # One card is anonymous with its author still populated: anonymity is
        # applied at reveal, not at write time (#8).
        cards = CYCLE3_CARDS
        created = self._create_cards(cycle, cards)
        # Deliberately NOT revealed: no shuffle, no participation, no nulling.
        # `is_anonymous` cards keep their authors, positions stay at 0.
        self._backdate_card_times(cycle, created, cards, weekdays=3)
        return cycle

    # -- shared construction helpers -------------------------------------

    def _create_cycle(
        self, project: Project, *, week: date, facilitator: str, status: str
    ) -> FeedbackCycle:
        cycle = FeedbackCycle.objects.create(
            project=project,
            week_start=week,
            opens_at=self._at(week, 9, 0),
            closes_at=self._at(week + timedelta(days=4), 17, 0),
            facilitator=self.users[facilitator],
            status=status,
        )
        self._backdate(FeedbackCycle, cycle.pk, "created_at", self._at(week, 9, 0))
        return cycle

    def _create_cards(
        self, cycle: FeedbackCycle, specs: list[tuple[str, str, str, bool]]
    ) -> list[Card]:
        """Create the cards in submission order, authors intact.

        Anonymity is applied later by `_reveal` for a revealed cycle. Here every
        card keeps its writer, exactly as a card looks while the cycle is still
        collecting.
        """
        created: list[Card] = []
        for category, text, writer, is_anonymous in specs:
            card = Card.objects.create(
                cycle=cycle,
                category=category,
                text=text,
                author=self.users[writer],
                is_anonymous=is_anonymous,
                public_id=self._uuid(),
            )
            created.append(card)
        return created

    def _reveal(
        self,
        cycle: FeedbackCycle,
        created: list[Card],
        specs: list[tuple[str, str, str, bool]],
    ) -> None:
        """Reproduce the state the real reveal leaves, without driving it.

        Three things, in the order `cycles/reveal.py` does them:

        1. one `CycleParticipation` per member, counted from the intended
           writers before authorship is destroyed;
        2. a shuffled `position` on every card, drawn from the seeded RNG so the
           order is reproducible but is not submission order;
        3. `author` set to NULL for every anonymous card.
        """
        self._backdate_card_times(cycle, created, specs, weekdays=5)
        self._participation(cycle, created, specs)
        self._shuffle_positions(cycle, created)
        # Destroy anonymous authorship, exactly as the reveal does.
        anon_pks = [c.pk for c, spec in zip(created, specs, strict=True) if spec[3]]
        Card.objects.filter(pk__in=anon_pks).update(author=None)

    def _participation(
        self,
        cycle: FeedbackCycle,
        created: list[Card],
        specs: list[tuple[str, str, str, bool]],
    ) -> None:
        """One row per member, including the non-submitters (`card_count=0`).

        Counted per writer over every card, attributed and anonymous alike,
        because "how much did this person contribute" is a fact about the person,
        not about which box they ticked. `submitted_at` is truncated to the day
        of the member's first card, mirroring `cycles/reveal.py`.
        """
        counts: dict[str, int] = {}
        first_day: dict[str, datetime] = {}
        for card, (_cat, _text, writer, _anon) in zip(created, specs, strict=True):
            counts[writer] = counts.get(writer, 0) + 1
            when = card.created_at
            if writer not in first_day or when < first_day[writer]:
                first_day[writer] = when

        member_ids = Membership.objects.filter(project_id=cycle.project_id).values_list(
            "user_id", flat=True
        )
        for user_id in member_ids:
            username = next(u for u, obj in self.users.items() if obj.pk == user_id)
            count = counts.get(username, 0)
            submitted_at = self._start_of_day(first_day[username]) if count else None
            row = CycleParticipation.objects.create(
                cycle=cycle,
                user_id=user_id,
                card_count=count,
                submitted_at=submitted_at,
            )
            self._backdate(
                CycleParticipation,
                row.pk,
                "created_at",
                self._at(cycle.week_start + timedelta(days=4), 17, 30),
            )

    def _shuffle_positions(self, cycle: FeedbackCycle, created: list[Card]) -> None:
        """Positions 1..n in a shuffled order, from the seeded RNG.

        `cycles/reveal.py` numbers from `FIRST_POSITION = 1` and reserves 0 for
        "not revealed", so a revealed card is never 0. The shuffle guarantees the
        order is not submission order.
        """
        pks = [c.pk for c in created]
        order = list(pks)
        self.rng.shuffle(order)
        position_of = {pk: i for i, pk in enumerate(order, start=1)}
        for pk, position in position_of.items():
            Card.objects.filter(pk=pk).update(position=position)

    def _backdate_card_times(
        self,
        cycle: FeedbackCycle,
        created: list[Card],
        specs: list[tuple[str, str, str, bool]],
        *,
        weekdays: int,
    ) -> None:
        """Spread `created_at` across the collection week.

        `Card.created_at` is `auto_now_add`, so it is written on insert and then
        moved here with a per-row `update()` rather than left on one microsecond.
        Cards land Monday..(Monday+weekdays-1), inside 09:00..16:00 and always
        before the Friday 17:00 close.
        """
        for index, card in enumerate(created):
            day = cycle.week_start + timedelta(days=index % weekdays)
            hour = 9 + (index % 7)
            when = self._at(day, hour, (index * 7) % 60)
            Card.objects.filter(pk=card.pk).update(created_at=when)
            card.created_at = when

    def _cluster(
        self, retro: Retrospective, name: str, position: int, auto: bool, status: str
    ) -> Cluster:
        return Cluster.objects.create(
            retrospective=retro,
            name=name,
            position=position,
            is_auto_generated=auto,
            status=status,
        )

    def _group(self, created: list[Card], mapping: dict[int, Cluster]) -> None:
        """Put the named cards into clusters; everything else stays ungrouped."""
        for index, cluster in mapping.items():
            Card.objects.filter(pk=created[index].pk).update(cluster=cluster)

    def _votes(self, retro: Retrospective, rows: list[tuple[str, Cluster, int]]) -> None:
        for username, cluster, weight in rows:
            Vote.objects.create(
                retrospective=retro,
                cluster=cluster,
                user=self.users[username],
                weight=weight,
            )

    def _notes(
        self,
        retro: Retrospective,
        week: date,
        rows: list[tuple[str, Cluster | None, str]],
    ) -> None:
        for order, (username, cluster, text) in enumerate(rows):
            note = Note.objects.create(
                retrospective=retro,
                cluster=cluster,
                author=self.users[username],
                text=text,
            )
            when = self._at(week + timedelta(days=4), 17, 35 + order)
            self._backdate(Note, note.pk, "created_at", when)

    def _decision(
        self,
        retro: Retrospective,
        cluster: Cluster | None,
        text: str,
        source: str,
        created_by: str | None,
        *,
        status: str = Decision.Status.CONFIRMED,
        excerpt: str = "",
    ) -> None:
        Decision.objects.create(
            retrospective=retro,
            cluster=cluster,
            text=text,
            excerpt=excerpt,
            source=source,
            status=status,
            created_by=self.users[created_by] if created_by else None,
        )

    def _action(
        self,
        retro: Retrospective,
        cluster: Cluster | None,
        description: str,
        *,
        owner: str | None,
        status: str,
        due_date: date | None,
        source: str,
        review_status: str = ActionItem.ReviewStatus.CONFIRMED,
        excerpt: str = "",
    ) -> None:
        ActionItem.objects.create(
            retrospective=retro,
            cluster=cluster,
            description=description,
            excerpt=excerpt,
            owner=self.users[owner] if owner else None,
            due_date=due_date,
            status=status,
            source=source,
            review_status=review_status,
            created_by=None if source == ActionItem.Source.EXTRACTED else self.users["demo_priya"],
        )

    def _meeting(
        self,
        retro: Retrospective,
        *,
        kind: str,
        uploaded_by: str,
        week: date,
        duration_seconds: float | None,
        transcript: str,
    ) -> None:
        """A finished meeting: media gone, transcript kept.

        `temp_path` NULL and `media_deleted_at` set is the normal end state of
        every record (`_docs/decisions.md` item 6): the recording is deleted in
        a `finally` block and only the transcript outlives it.
        """
        record = MeetingRecord.objects.create(
            retrospective=retro,
            uploaded_by=self.users[uploaded_by],
            kind=kind,
            temp_path=None,
            original_filename=""
            if kind == MeetingRecord.Kind.PASTED_TEXT
            else "retro-recording.m4a",
            size_bytes=0 if kind == MeetingRecord.Kind.PASTED_TEXT else 42_000_000,
            status=MeetingRecord.Status.READY,
            attempts=1,
            media_deleted_at=self._at(week + timedelta(days=4), 18, 0),
        )
        self._backdate(
            MeetingRecord, record.pk, "created_at", self._at(week + timedelta(days=4), 17, 45)
        )
        t = Transcript.objects.create(
            record=record,
            text=transcript,
            language="",
            duration_seconds=duration_seconds,
        )
        self._backdate(Transcript, t.pk, "created_at", self._at(week + timedelta(days=4), 17, 50))

    # -- small utilities --------------------------------------------------

    def _uuid(self) -> uuid.UUID:
        """A UUID4 drawn from the seeded RNG, so two runs assign the same ones.

        `Card.public_id` defaults to `uuid.uuid4()`, which draws from the OS and
        would differ between runs. Drawing it here from `self.rng` keeps two runs
        identical down to the public id, so only primary keys differ.
        """
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def _at(self, day: date, hour: int, minute: int) -> datetime:
        """A timezone-aware datetime at `day` `hour:minute` in the active zone."""
        return timezone.make_aware(datetime.combine(day, dtime(hour, minute)))

    def _start_of_day(self, moment: datetime) -> datetime:
        local = timezone.localtime(moment) if timezone.is_aware(moment) else moment
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _backdate(self, model, pk: int, field: str, when: datetime) -> None:
        """Move an `auto_now_add` timestamp after insert, with a queryset update."""
        model.objects.filter(pk=pk).update(**{field: when})


# --------------------------------------------------------------------------
# Hand-written content. Realistic sentences about a software team's week — no
# lorem ipsum, no "Card 1", no real person or company. Each card is
# (category, text, writer, is_anonymous); the reveal nulls the author of every
# anonymous card in a closed cycle and leaves it on an open one.
# --------------------------------------------------------------------------

CYCLE1_CARDS: list[tuple[str, str, str, bool]] = [
    (
        "START",
        "Add a one-paragraph summary at the top of every RFC so reviewers can triage them.",
        "demo_priya",
        False,
    ),
    (
        "START",
        "Pair on the first ticket of any unfamiliar service instead of picking it up solo.",
        "demo_sam",
        False,
    ),
    (
        "START",
        "Block two focused hours each morning with no meetings; mornings are getting shredded.",
        "demo_alex_n",
        True,
    ),
    ("START", "Rotate who runs stand-up so it is not always the same person.", "demo_mei", False),
    (
        "START",
        "Say no to mid-sprint scope changes unless something is actually on fire.",
        "demo_sam",
        True,
    ),
    (
        "STOP",
        "Stop merging on a green run that skipped the slow integration tests; they matter.",
        "demo_alex_t",
        False,
    ),
    (
        "STOP",
        "Stop scheduling reviews at half past four on a Friday; nobody reads them properly.",
        "demo_mei",
        True,
    ),
    (
        "STOP",
        "Stop letting staging drift from production; half our bugs are config, not code.",
        "demo_priya",
        False,
    ),
    (
        "STOP",
        "Stop assigning tickets to people who are already on call that week.",
        "demo_alex_n",
        False,
    ),
    (
        "STOP",
        "Stop copying the same boilerplate into every new service; extract it once.",
        "demo_sam",
        False,
    ),
    (
        "CONTINUE",
        "Keep the Wednesday architecture office hour; it caught two bad designs this month.",
        "demo_mei",
        False,
    ),
    (
        "CONTINUE",
        "Keep being honest in postmortems; last week's blameless writeup was genuinely useful.",
        "demo_priya",
        True,
    ),
    (
        "CONTINUE",
        "Keep shipping small pull requests; the review turnaround has been much faster.",
        "demo_alex_t",
        False,
    ),
    (
        "CONTINUE",
        "Keep the shared on-call runbook; a new starter got through an incident unaided.",
        "demo_alex_n",
        False,
    ),
    (
        "CONTINUE",
        "Keep demoing on Fridays even when it is rough; it keeps everyone in the loop.",
        "demo_sam",
        False,
    ),
]

CYCLE2_CARDS: list[tuple[str, str, str, bool]] = [
    (
        "START",
        "Capture decisions in the ticket, not in chat threads that scroll away by Friday.",
        "demo_priya",
        False,
    ),
    ("START", "Give design review the same lead time we give code review.", "demo_mei", True),
    (
        "START",
        "Close the sprint board on Friday so Monday is not spent reconstructing it.",
        "demo_sam",
        False,
    ),
    (
        "START",
        "Write down who owns each alert before we add it to the dashboard.",
        "demo_tom",
        False,
    ),
    (
        "STOP",
        "Stop reopening tickets for follow-up; file a fresh one so the history stays clean.",
        "demo_alex_n",
        False,
    ),
    (
        "STOP",
        "Stop pretending the flaky payment test is someone else's problem.",
        "demo_alex_t",
        True,
    ),
    ("STOP", "Stop deploying on Fridays unless it is a genuine hotfix.", "demo_priya", False),
    (
        "CONTINUE",
        "Keep the design critique sessions; the last one saved us a redesign.",
        "demo_mei",
        False,
    ),
    ("CONTINUE", "Keep the incident channel calm and factual during outages.", "demo_sam", False),
    ("CONTINUE", "Keep the newcomer buddy scheme; it is working.", "demo_tom", True),
]

CYCLE3_CARDS: list[tuple[str, str, str, bool]] = [
    (
        "START",
        "Timebox spikes so research does not quietly become a whole week.",
        "demo_priya",
        False,
    ),
    (
        "START",
        "Acknowledge when a deadline slipped instead of quietly moving it.",
        "demo_mei",
        True,
    ),
    (
        "STOP",
        "Stop reviewing your own migration in a hurry right before stand-up.",
        "demo_sam",
        False,
    ),
    ("STOP", "Stop adding dashboards nobody has agreed to watch.", "demo_alex_n", False),
    ("CONTINUE", "Keep the Friday demo; it is the best part of the week.", "demo_priya", False),
    (
        "CONTINUE",
        "Keep writing migration notes in the pull-request description.",
        "demo_alex_n",
        False,
    ),
]

# Speaker-labelled transcripts, one turn per line, joined so each source line
# stays within the line-length cap.
CYCLE1_TRANSCRIPT = "\n".join(
    [
        "Speaker 1: Right, let us start with the focus-time topic, it had the most votes.",
        "Speaker 2: The mornings are the problem. By the time stand-up and the two syncs "
        "are done it is lunch and nobody has written a line.",
        "Speaker 3: Could we just block nine to eleven, no meetings, as a team default?",
        "Speaker 1: I like that. Two weeks as a trial, then we look at whether anything broke.",
        "Speaker 2: Nothing will break. Nobody needs me at half nine.",
        "Speaker 4: Agreed. Let us write it down as a decision so it does not evaporate.",
        "Speaker 1: Done. Next, CI and merge safety. Sam, this was yours.",
        "Speaker 2: We keep merging past the integration tests. Someone marks them optional "
        "to get unblocked and then they stay optional.",
        "Speaker 3: Make them a required check. If they are flaky we fix the flake, we do "
        "not route around them.",
        "Speaker 1: Action item: make the integration suite required in CI. Sam, own that?",
        "Speaker 2: Yes, I will have it done this week.",
        "Speaker 1: On-call load. We deferred this last time too.",
        "Speaker 4: The problem is we assign tickets to whoever is on call, so they do "
        "incident work and feature work at once.",
        "Speaker 3: Stop doing that. Check the roster in planning.",
        "Speaker 1: Let us defer the deeper fix but write the escalation path into the "
        "runbook now. That one is quick.",
        "Speaker 2: The staging drift topic we can skip; we know the answer, it needs doing.",
        "Speaker 1: Fair. Skipping it on the board, keeping the action item. Good retro.",
    ]
)

CYCLE2_TRANSCRIPT = "\n".join(
    [
        "Speaker 1: First topic is decision records, it came top.",
        "Speaker 2: Half our decisions live in a chat thread that scrolls away by Friday. "
        "Then we re-litigate them.",
        "Speaker 1: So put them in the ticket.",
        "Speaker 2: Let us just put decisions in the ticket so they do not vanish. I can "
        "add a section to the template.",
        "Speaker 1: Good. That is a decision and an action for you, Sam.",
        "Speaker 3: While we are on templates, design review needs the same lead time as "
        "code review. We keep getting mock-ups an hour before the meeting.",
        "Speaker 1: Two-day lead time from next sprint, agreed. Mei, will you write it up?",
        "Speaker 3: I will write it up.",
        "Speaker 1: The flaky payment test failed three times this week and each time "
        "someone just re-ran it.",
        "Speaker 4: Someone should just quarantine that flaky test until it is actually fixed.",
        "Speaker 1: Who picks up the fix?",
        "Speaker 2: Alex will pick it up.",
        "Speaker 1: There are two Alexes, so let us leave the owner open and sort it later.",
        "Speaker 4: The alert list also has no owner. We keep adding alerts nobody watches.",
        "Speaker 1: Right, we need someone to own the alert list. Another open action.",
    ]
)
