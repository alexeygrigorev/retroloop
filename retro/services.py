"""The stage machine: the one place a retrospective's stage ever changes.

`advance_stage()` is the whole public surface. It moves a retrospective forward
by exactly one stage, under a row-level lock, with that transition's side
effects inside the same transaction as the stage write — so a side effect that
raises leaves the stage where it was rather than half-moving the board.

The access rules live at the top of this module as one-line predicates, in the
shape `projects/views.py` and `cycles/views.py` already use. Issue #6 lifts
`can_start_retrospective` and `can_advance_stage` into
`projects/permissions.py` unchanged and deletes them from here; until then this
module is the single place that decides who may start a retrospective or move
it on. There is deliberately no `retro/permissions.py`: one module for the
whole application is the rule, and these are a temporary shape with #6 as their
deletion date.

The division of labour is fixed: `can_advance_stage` answers *who*, and is
handed no target stage. Forward-only, single-step and `COMPLETE` being terminal
belong to `advance_stage()`, which is the only place both the from-stage and
the to-stage are known.
"""

from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.utils import timezone

from cycles.models import FeedbackCycle
from retro.models import Retrospective, is_legal_transition, next_stage_after

# --------------------------------------------------------------------------
# Rules. One condition each, so #6 can lift them out as they are.
# --------------------------------------------------------------------------


def can_start_retrospective(user, cycle: FeedbackCycle) -> bool:
    return cycle.facilitator_id == user.pk and not hasattr(cycle, "retrospective")


def can_advance_stage(user, retro: Retrospective) -> bool:
    return retro.cycle.facilitator_id == user.pk


# --------------------------------------------------------------------------
# Rejections
#
# A rejection is an exception rather than a False, because every caller has to
# deal with it: a view turns one into a message, a later API into a 409. Who
# may act is the one exception Django already models, so it stays
# PermissionDenied and a view that does nothing returns 403.
# --------------------------------------------------------------------------


class StageError(Exception):
    """The transition was refused. The stage is unchanged."""


class InvalidTransition(StageError):
    """The move is not forward by one step, or the retrospective is complete."""


class ConcurrentAdvance(StageError):
    """Someone else advanced the board first, so this caller acted on a stale view of it."""


# --------------------------------------------------------------------------
# The board version
# --------------------------------------------------------------------------


def bump_version(retro: Retrospective) -> int:
    """Record that this transaction mutated the board, and return the new version.

    This is the single helper #12, #15 and #16 call, so the counter cannot be
    forgotten by whoever writes the next mutation. It updates in the database
    with an F expression rather than from a value read earlier, so two
    transactions that both mutate cannot land on the same number.

    It refuses to run outside a transaction: a version bump that commits apart
    from the change it describes is worse than no bump at all, because a poller
    would then be told to re-read a board that has not changed yet.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "bump_version() must run inside the transaction that mutates the board, "
            "so the counter and the change commit together."
        )

    Retrospective.objects.filter(pk=retro.pk).update(version=models.F("version") + 1)
    retro.version = Retrospective.objects.values_list("version", flat=True).get(pk=retro.pk)
    return retro.version


# --------------------------------------------------------------------------
# Transition hooks
#
# One per arriving stage, so a later issue fills its own in without touching
# the machine. They run inside advance_stage()'s transaction, after the stage
# is written and before it commits: raising from one of these rolls the stage
# back with everything else.
# --------------------------------------------------------------------------


def _on_reveal(retro: Retrospective) -> None:
    """Entering REVEAL.

    Closing the cycle is the one part that is this task's own: reveal is the
    moment collection ends, so there is no state where cards are revealed and
    the submission form is still open. It happens in this transaction, so the
    two can never disagree.

    Not yet implemented, and no-ops until they are:
    - #10 destroys anonymous authorship and shuffles the positions;
    - #22 enqueues the clustering job.
    """
    if retro.cycle.status == FeedbackCycle.Status.COLLECTING:
        retro.cycle.status = FeedbackCycle.Status.CLOSED
        retro.cycle.save(update_fields=["status"])


def _on_cluster(retro: Retrospective) -> None:
    """Entering CLUSTER.

    #9 names no side effect for this transition — the clustering job is
    enqueued on the way into REVEAL (#22) and the board does the rest (#12).
    The hook exists anyway, so that every arriving stage has one and a later
    issue has somewhere obvious to put its work.
    """


def _on_vote(retro: Retrospective) -> None:
    """Entering VOTE. A no-op until #12 freezes cluster membership here."""


def _on_discuss(retro: Retrospective) -> None:
    """Entering DISCUSS.

    A no-op until #15 computes the ranked agenda and #16 unhides the vote
    totals.
    """


def _on_complete(retro: Retrospective) -> None:
    """Entering COMPLETE. A no-op until #25 locks the board."""


#: Keyed by the stage being entered. Every stage after DRAFT has an entry, so a
#: missing hook is a KeyError at the transition rather than a silent nothing.
TRANSITION_HOOKS = {
    Retrospective.Stage.REVEAL: _on_reveal,
    Retrospective.Stage.CLUSTER: _on_cluster,
    Retrospective.Stage.VOTE: _on_vote,
    Retrospective.Stage.DISCUSS: _on_discuss,
    Retrospective.Stage.COMPLETE: _on_complete,
}


# --------------------------------------------------------------------------
# The service functions
# --------------------------------------------------------------------------


def start_retrospective(user, cycle: FeedbackCycle) -> Retrospective:
    """Create this cycle's retrospective, in DRAFT.

    The one-to-one is what makes "at most one per cycle" true; the predicate
    above only saves the caller from being shown a button that would fail.
    """
    if not can_start_retrospective(user, cycle):
        raise PermissionDenied(
            "Only this cycle's facilitator can start its retrospective, and only once."
        )

    return Retrospective.objects.create(cycle=cycle)


def advance_stage(user, retro: Retrospective) -> Retrospective:
    """Move `retro` on by one stage. The only way the stage ever changes.

    Refuses, leaving the stage untouched, when the caller is not the cycle's
    facilitator, when the retrospective is already COMPLETE, and when the board
    moved on since the caller read it. Backwards and skipping are not
    parameters a caller can pass: the target is derived here, and checked
    against `is_legal_transition` before anything is written.

    On success the passed instance is updated in place, so a caller holding it
    can advance again without re-reading.
    """
    if not can_advance_stage(user, retro):
        raise PermissionDenied("Only this cycle's facilitator can advance the retrospective.")

    with transaction.atomic():
        # The lock is taken on the row, not on the copy the caller is holding,
        # and everything below is decided from the locked row. A second
        # advance waits here and then finds the version it was given is stale.
        locked = Retrospective.objects.select_for_update().select_related("cycle").get(pk=retro.pk)
        if locked.version != retro.version:
            raise ConcurrentAdvance(
                f"The retrospective moved to {locked.get_stage_display()} while you were "
                f"looking at it. Reload the page and try again."
            )

        target = next_stage_after(locked.stage)
        if target is None or not is_legal_transition(locked.stage, target):
            raise InvalidTransition(
                f"A retrospective in {locked.get_stage_display()} cannot advance"
                + (f" to {target}." if target else ": it is complete.")
            )

        now = timezone.now()
        if target == Retrospective.Stage.REVEAL:
            locked.started_at = now
        if target == Retrospective.Stage.COMPLETE:
            locked.completed_at = now
        locked.stage = target
        locked.save(update_fields=["stage", "started_at", "completed_at"])

        # Inside the transaction on purpose: a hook that raises takes the stage
        # write above with it.
        TRANSITION_HOOKS[target](locked)
        bump_version(locked)

    retro.stage = locked.stage
    retro.started_at = locked.started_at
    retro.completed_at = locked.completed_at
    retro.version = locked.version
    return retro
