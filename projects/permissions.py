"""Who may do what. The one module in the application that decides.

Every rule is a predicate `(user, obj) -> bool`. It answers a question and does
nothing else: it never raises, never writes, never redirects, never enqueues a
task and never returns a response. Enforcement — a 404, a `PermissionDenied`, a
hidden button — stays at the call site, which is what gets reviewed.

Three properties hold for every rule here, so no caller has to remember them:

- an `AnonymousUser`, and a user whose account has been deactivated, is refused
  rather than crashing the predicate;
- a superuser gets nothing extra. Being staff does not reveal another member's
  card and cannot undo an anonymous author — `_docs/decisions.md` item 3 has no
  admin exception, so this module has none either;
- authority over a cycle and its retrospective is per cycle, not per project.
  The project's owner is not automatically the facilitator of this week.

One module, not one per app, even though it guards objects from `cycles/` and
`retro/`. Splitting it per app is what produced the scattered rules #5, #7, #8
and #9 carried inline until this file existed. A new rule is a new function
here; a second `permissions.py` anywhere is the thing this file exists to
prevent.

Not here yet, deliberately: `can_update_action_item` waits for `ActionItem`
(#17), and the rules guarding `Cluster` and `Note` wait for those models (#12,
#16). Each of those issues adds its predicate to this file.
"""

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import STAGE_ORDER, Retrospective

# --------------------------------------------------------------------------
# Helpers. Not rules, so they are private and the public surface of this
# module is exactly the questions it answers.
# --------------------------------------------------------------------------


def _is_active_user(user) -> bool:
    """Whether `user` is someone who can be granted anything at all.

    Anonymous and deactivated users stop here, so the rules below never have to
    say so themselves and never hand an `AnonymousUser` to a query.
    """
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.pk is not None
    )


def _is_member(user, project: Project) -> bool:
    return Membership.objects.filter(project=project, user=user).exists()


def _is_facilitator(user, project: Project) -> bool:
    return Membership.objects.filter(
        project=project, user=user, role=Membership.Role.FACILITATOR
    ).exists()


def _leads_project(user, project: Project) -> bool:
    """The project's owner, or a member whose role is FACILITATOR."""
    return project.owner_id == user.pk or _is_facilitator(user, project)


def _leads_cycle(user, cycle: FeedbackCycle) -> bool:
    """This cycle's facilitator, and nobody else — authority is per cycle."""
    return cycle.facilitator_id == user.pk


def _retrospective_of(cycle: FeedbackCycle) -> Retrospective | None:
    """The cycle's retrospective, or None when it has not been started.

    A cycle with no retrospective row is the state every cycle starts in, so it
    is a normal answer rather than a missing one.
    """
    return getattr(cycle, "retrospective", None)


def _stage_reached(retro: Retrospective | None, stage: str) -> bool:
    """Whether `retro` is at `stage` or past it. No retrospective is "not yet"."""
    if retro is None:
        return False
    return STAGE_ORDER.index(retro.stage) >= STAGE_ORDER.index(stage)


def _stage_past(retro: Retrospective | None, stage: str) -> bool:
    if retro is None:
        return False
    return STAGE_ORDER.index(retro.stage) > STAGE_ORDER.index(stage)


# --------------------------------------------------------------------------
# Project
# --------------------------------------------------------------------------


def can_view_project(user, project: Project) -> bool:
    """Project members only. Everyone else is not told the project exists."""
    return _is_active_user(user) and _is_member(user, project)


def can_rotate_join_token(user, project: Project) -> bool:
    return _is_active_user(user) and _leads_project(user, project)


# --------------------------------------------------------------------------
# Cycle
# --------------------------------------------------------------------------


def can_open_cycle(user, project: Project) -> bool:
    return _is_active_user(user) and _leads_project(user, project)


def can_close_cycle(user, cycle: FeedbackCycle) -> bool:
    """This cycle's facilitator, while it is still COLLECTING.

    A CLOSED cycle is False for everyone, which is what makes closing twice
    impossible rather than merely hidden.
    """
    return (
        _is_active_user(user)
        and _leads_cycle(user, cycle)
        and cycle.status == FeedbackCycle.Status.COLLECTING
    )


def can_start_retrospective(user, cycle: FeedbackCycle) -> bool:
    """This cycle's facilitator, and only while there is no retrospective yet."""
    return _is_active_user(user) and _leads_cycle(user, cycle) and _retrospective_of(cycle) is None


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


def can_add_card(user, cycle: FeedbackCycle) -> bool:
    return (
        _is_active_user(user)
        and _is_member(user, cycle.project)
        and cycle.status == FeedbackCycle.Status.COLLECTING
    )


def can_view_card(user, card: Card) -> bool:
    """The author always; everyone else in the project only from REVEAL on.

    A cycle whose retrospective has not been started is "before reveal", so
    only the author sees the card. That is the state every cycle starts in.
    """
    if not _is_active_user(user):
        return False
    if card.author_id is not None and card.author_id == user.pk:
        return True
    return _stage_reached(_retrospective_of(card.cycle), Retrospective.Stage.REVEAL) and _is_member(
        user, card.cycle.project
    )


def can_edit_card(user, card: Card) -> bool:
    """The author, while the cycle is COLLECTING — `_docs/decisions.md` item 1.

    A card whose author is NULL is nobody's: a destroyed anonymous author can
    never be matched, so it is False for everyone including the person who
    wrote it.
    """
    return (
        _is_active_user(user)
        and card.author_id is not None
        and card.author_id == user.pk
        and card.cycle.status == FeedbackCycle.Status.COLLECTING
    )


def can_delete_card(user, card: Card) -> bool:
    return can_edit_card(user, card)


def can_move_card(user, card: Card) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER.

    Frozen from VOTE onward, because the move into VOTE freezes cluster
    membership — #12 enforces this and says the same two stages.
    """
    retro = _retrospective_of(card.cycle)
    return (
        _is_active_user(user)
        and retro is not None
        and retro.stage in {Retrospective.Stage.REVEAL, Retrospective.Stage.CLUSTER}
        and _is_member(user, card.cycle.project)
    )


# --------------------------------------------------------------------------
# Retrospective
# --------------------------------------------------------------------------


def can_advance_stage(user, retro: Retrospective) -> bool:
    """This cycle's facilitator. Which transition is legal is not this question.

    The predicate is handed no target stage, so it cannot answer forward-only.
    That stays in `advance_stage()`, the one place both stages are known.
    """
    return _is_active_user(user) and _leads_cycle(user, retro.cycle)


def can_cast_vote(user, retro: Retrospective) -> bool:
    return (
        _is_active_user(user)
        and retro.stage == Retrospective.Stage.VOTE
        and _is_member(user, retro.cycle.project)
    )


def can_see_vote_totals(user, retro: Retrospective) -> bool:
    """Project members, once voting is over — totals stay hidden during VOTE.

    `_docs/decisions.md` item 2: votes are reassignable while the stage is
    VOTE, which is only safe while nobody can see the running totals.
    """
    return (
        _is_active_user(user)
        and _stage_past(retro, Retrospective.Stage.VOTE)
        and _is_member(user, retro.cycle.project)
    )


def can_upload_recording(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _leads_cycle(user, retro.cycle)


def can_confirm_extraction(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _leads_cycle(user, retro.cycle)


def can_view_summary(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _is_member(user, retro.cycle.project)
