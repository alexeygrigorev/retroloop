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
(#17). Each of those issues adds its predicate to this file. The `Cluster` rules
arrived with #12 and are below, beside `can_move_card`; the discussion rules —
setting a cluster's status, and editing or deleting a note — arrived with #16,
which is where `Cluster.status` and `Note` first exist, and are at the foot of
this file.
"""

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import STAGE_ORDER, ActionItem, Cluster, Decision, Note, Retrospective

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


#: The two stages in which the board's shape may be changed at all. The
#: `-> VOTE` transition freezes cluster membership, so the window closes there
#: and never reopens: a board that has been voted on is not reshaped underneath
#: the votes.
_BOARD_SHAPING_STAGES = frozenset({Retrospective.Stage.REVEAL, Retrospective.Stage.CLUSTER})


def _may_shape_board(user, retro: Retrospective | None, project: Project) -> bool:
    """The one window every board mutation shares: a member, in REVEAL or CLUSTER.

    Written once because `can_move_card` and the five cluster rules are the same
    sentence about the same board. A cycle with no retrospective has no board to
    reshape, so it is False rather than an attribute error.
    """
    return (
        _is_active_user(user)
        and retro is not None
        and retro.stage in _BOARD_SHAPING_STAGES
        and _is_member(user, project)
    )


def _shaping_a_cluster(user, cluster: Cluster) -> bool:
    """`_may_shape_board` for a rule that is handed a cluster."""
    retro = cluster.retrospective
    return _may_shape_board(user, retro, retro.cycle.project)


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
    return _may_shape_board(user, _retrospective_of(card.cycle), card.cycle.project)


# --------------------------------------------------------------------------
# Clusters
#
# The board's shape — which cards are grouped, and into what — is one rule with
# five more names, all of them the same window as `can_move_card`: a project
# member, while the stage is REVEAL or CLUSTER. They are written out one per
# action rather than collapsed into a single `can_change_cluster`, because a
# call site that asks the question it means is what makes a later divergence —
# #16 letting only the facilitator delete a discussed cluster, say — a change to
# one predicate instead of a new one and a search for its call sites.
# --------------------------------------------------------------------------


def can_create_cluster(user, retro: Retrospective) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER.

    Handed the retrospective rather than a cluster: there is no cluster to ask
    about yet. Creating one on a board that is frozen is refused for the same
    reason moving a card into one is.
    """
    return _may_shape_board(user, retro, retro.cycle.project)


def can_rename_cluster(user, cluster: Cluster) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER.

    `is_auto_generated` is not consulted here or in any rule below. A cluster
    #22 suggested is renamed, merged, split and deleted exactly like a hand-made
    one; the flag changes the wording a screen uses and nothing else.
    """
    return _shaping_a_cluster(user, cluster)


def can_merge_cluster(user, cluster: Cluster) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER.

    Asked of both clusters a merge names — the source that disappears and the
    target that grows — so neither side is authorized by the other's stage.
    """
    return _shaping_a_cluster(user, cluster)


def can_split_cluster(user, cluster: Cluster) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER."""
    return _shaping_a_cluster(user, cluster)


def can_delete_cluster(user, cluster: Cluster) -> bool:
    """Project members, while the board is in REVEAL or CLUSTER.

    Deleting a cluster is a change to the board's shape and nothing more: its
    cards return to ungrouped and no card is deleted, so this is the same
    window as moving one card out by hand, which is what it amounts to.
    """
    return _shaping_a_cluster(user, cluster)


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


# --------------------------------------------------------------------------
# Discussion — #16
#
# The two rules the discussion needs that #6 could not write, because
# `Cluster.status` and `Note` did not exist when it ran, plus a third for the
# note edit path. They live here, in the one module, and not in `retro/` or
# `board/`: from #10 onward every app imports its rules from this file and adds
# no second permissions module.
#
# `can_set_cluster_status` is the facilitator, at any stage: whether the board is
# in the stage where a status may be set is not a question about *who* the caller
# is, so — like `can_advance_stage` — the predicate answers only the who, and the
# DISCUSS window is enforced at the mutation, which is the one place the action
# and the stage are both known.
#
# `can_delete_note` and `can_edit_note` do carry the stage, because their answer
# genuinely differs by it: a note is the author's to edit and the author's or the
# facilitator's to delete *while the stage is DISCUSS*, and read-only for everyone
# once it is COMPLETE. Editing is the author's alone — a facilitator may remove a
# note but never rewrite one and leave someone else's name on the new words, which
# is why editing is a rule of its own rather than `can_delete_note` reused.
# --------------------------------------------------------------------------


def can_set_cluster_status(user, cluster: Cluster) -> bool:
    """This cycle's facilitator, and nobody else. A member's direct POST is refused.

    Answers only *who*: the DISCUSS window in which a status may be set is checked
    at the mutation, the same division of labour `can_advance_stage` keeps. A
    project member who is not this week's facilitator gets False here and a 403 at
    the endpoint; a non-member never reaches this predicate at all.
    """
    return _is_active_user(user) and _leads_cycle(user, cluster.retrospective.cycle)


def can_edit_note(user, note: Note) -> bool:
    """The note's author, while the stage is DISCUSS. Nobody else edits it.

    A member edits their own notes and only their own — rewriting another
    member's attributed note would put words in a mouth that is not theirs, which
    is the one thing "always attributed" rules out. The facilitator's power over a
    note is to delete it, not to edit it, so this is deliberately narrower than
    `can_delete_note` and not the same rule reused.

    Read-only once the retrospective is COMPLETE, which is why the stage is part
    of the answer rather than left to the endpoint.
    """
    return (
        _is_active_user(user)
        and note.author_id == user.pk
        and note.retrospective.stage == Retrospective.Stage.DISCUSS
    )


def can_delete_note(user, note: Note) -> bool:
    """The note's author or the cycle's facilitator, while the stage is DISCUSS.

    Wider than `can_edit_note`: a facilitator working the agenda may clear any
    note, but only the author may change what a note says. Read-only for everyone
    once the retrospective is COMPLETE, so the stage is part of the answer.
    """
    return (
        _is_active_user(user)
        and (note.author_id == user.pk or _leads_cycle(user, note.retrospective.cycle))
        and note.retrospective.stage == Retrospective.Stage.DISCUSS
    )


# --------------------------------------------------------------------------
# Decisions and action items — #17
#
# The retrospective's structured outcomes. Two kinds of rule, and the split is
# the whole subtlety of the freeze at COMPLETE:
#
# - editing or deleting a manual entry — its *text* — is the author's or this
#   cycle's facilitator's, and only *while the retrospective is not COMPLETE*.
#   The stage is part of the answer, exactly as it is for `can_edit_note`,
#   because a decision or an action item's words are frozen once the
#   retrospective closes;
# - flipping an action item between OPEN and DONE is the owner's or the
#   facilitator's, at *any* stage, COMPLETE included. Work agreed one week is
#   finished in another, so the tick box outlives the retrospective while its
#   description does not.
#
# Both take the object, not the retrospective, because who may act depends on
# who wrote it (`created_by`) or who owns it (`owner`) — a fact of the row. The
# creation rule needs no predicate of its own: any project member may write an
# outcome, and membership is already established by `can_view_project` at the
# view, the same division `add_note` uses.
#
# `can_delete_decision` and `can_delete_action_item` are `can_edit_*` reused:
# for these two the delete and the edit answer the same question — author or
# facilitator, while not COMPLETE — so the delete site asks the question it means
# and a later divergence is a change to one predicate, the pattern `can_delete_card`
# follows.
# --------------------------------------------------------------------------


def can_edit_decision(user, decision: Decision) -> bool:
    """The decision's author or this cycle's facilitator, while not COMPLETE.

    Read-only for everyone once the retrospective is COMPLETE — a decision's text
    is frozen then — so the stage is part of the answer rather than left to the
    view. An author whose account is gone (`created_by` NULL, as on #23's
    extracted rows) is matched by nobody, so only the facilitator may edit those.
    """
    retro = decision.retrospective
    return (
        _is_active_user(user)
        and retro.stage != Retrospective.Stage.COMPLETE
        and (
            (decision.created_by_id is not None and decision.created_by_id == user.pk)
            or _leads_cycle(user, retro.cycle)
        )
    )


def can_delete_decision(user, decision: Decision) -> bool:
    return can_edit_decision(user, decision)


def can_edit_action_item(user, action: ActionItem) -> bool:
    """The item's author or this cycle's facilitator, while not COMPLETE.

    This is the *text* rule — the description is frozen at COMPLETE, exactly like
    a decision's. Ticking the item off is a different question with a different
    answer: see `can_update_action_item`, which stays True after COMPLETE.
    """
    retro = action.retrospective
    return (
        _is_active_user(user)
        and retro.stage != Retrospective.Stage.COMPLETE
        and (
            (action.created_by_id is not None and action.created_by_id == user.pk)
            or _leads_cycle(user, retro.cycle)
        )
    )


def can_delete_action_item(user, action: ActionItem) -> bool:
    return can_edit_action_item(user, action)


def can_update_action_item(user, action: ActionItem) -> bool:
    """Flip an action item between OPEN and DONE: its owner or the cycle's facilitator.

    The predicate #6 could not write, because `ActionItem` did not exist yet. It
    answers *who*, and it does not consult the stage: unlike editing the text,
    ticking an item off is allowed after the retrospective is COMPLETE, because
    the work outlives the meeting. Nobody else — a plain member who is neither the
    owner nor this week's facilitator gets False here and a 403 at the view. An
    item with no owner (`owner` NULL) is nobody's to tick but the facilitator's.
    """
    return _is_active_user(user) and (
        (action.owner_id is not None and action.owner_id == user.pk)
        or _leads_cycle(user, action.retrospective.cycle)
    )


def can_upload_recording(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _leads_cycle(user, retro.cycle)


def can_confirm_extraction(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _leads_cycle(user, retro.cycle)


def can_view_summary(user, retro: Retrospective) -> bool:
    return _is_active_user(user) and _is_member(user, retro.cycle.project)
