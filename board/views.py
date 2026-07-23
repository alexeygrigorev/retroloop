"""The board's endpoints: one that reads it, and seven that change it.

Thin, like every other view here. A read view finds the row, refuses the people
who may not see it, and hands the serializer the rest. A write view does even
less: it names the operation and lets `board/mutations.py` take the lock, decide
and write. No view here decides who may act — `projects/permissions.py` does —
and no view writes a field.

Every write is a POST with a CSRF token, and every one of them answers with the
same full board state the read endpoint produces, so a client can replace its
state with the response and skip a poll. Nothing here is a GET: `require_POST`
answers 405 to one, which is what keeps "no GET mutates" true of the URLs
themselves rather than of a convention.

None of them is `@login_required`. A logged-out browser has to be answered the
way an id that was never used is answered, and a redirect to the login page
would confirm that this retrospective exists. `can_view_project` is False for an
anonymous user, so both fall through to the same 404.
"""

from django.http import Http404, HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from board import mutations
from board.mutations import BoardRejection, apply_mutation, members_who_spent_everything
from board.serializers import board_state, unchanged_state
from projects.permissions import can_advance_stage, can_view_project
from retro.models import Retrospective


@require_GET
def board_state_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`GET /retros/<pk>/state?v=<version>` — the whole state of one board.

    Two bodies, both documented in `board/serializers.py`: a small one saying
    the version has not moved, and the full state. `v` is what the client
    believes it already has.

    There is no `@login_required`. A logged-out browser has to be answered the
    same way an id that was never used is answered, and a redirect to the login
    page would confirm that this retrospective exists. `can_view_project` is
    False for an anonymous user, so both fall through to the same 404 below.

    GET only. The board's writes are on endpoints of their own, below, and a
    read that answered a POST would be an invitation to add one here.
    """
    # `.first()` rather than `get_object_or_404`, so that a retrospective which
    # does not exist and one this person may not see raise the same exception
    # from the same line, and produce responses that are identical byte for
    # byte. A 404 that carries "No Retrospective matches the given query" for
    # one of them and not the other is an existence oracle.
    retro = Retrospective.objects.select_related("cycle__project").filter(pk=pk).first()
    if retro is None or not can_view_project(request.user, retro.cycle.project):
        raise Http404

    if known_version(request.GET.get("v")) == retro.version:
        return JsonResponse(unchanged_state(retro))

    return JsonResponse(board_state(request.user, retro))


def known_version(raw: str | None) -> int | None:
    """The version the client says it holds, or None for "no known version".

    Absent, empty, non-numeric, negative, fractional and absurdly long values
    are all the same answer: this caller knows nothing, so send it everything.
    `int()` is asked rather than guessed at, because it is the thing that
    decides — `"²".isdigit()` is True and `int("²")` raises, and CPython
    refuses to convert a string of more than a few thousand digits at all.
    Either way the caller gets the full state instead of a 500.

    None is never equal to a stored version, so it can be compared directly
    without a second branch at the call site: `version` is a
    `PositiveIntegerField` and is never None.
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The seven writes
#
# One view per action the team can take, each of them the same three lines with
# a different operation named in the middle. They are separate URLs rather than
# one endpoint with an `action` field so that a client cannot reach a mutation
# it did not mean to, and so that the URLconf lists what the board can do.
# --------------------------------------------------------------------------


@require_POST
def card_move_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/cards/move` — `card` joins `cluster`.

    `card` is a card's `public_id`; `cluster` is a cluster's integer id. Both
    are read from the form body rather than from the URL, so an id that does not
    resolve is refused by this app's own rule and not by the URL resolver.
    """
    return _write(request, pk, mutations.move_card_to_cluster)


@require_POST
def card_ungroup_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/cards/ungroup` — `card` leaves its cluster."""
    return _write(request, pk, mutations.move_card_out)


@require_POST
def cluster_create_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/create` — a new empty cluster called `name`."""
    return _write(request, pk, mutations.create_cluster)


@require_POST
def cluster_rename_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/rename` — `cluster` is called `name` now."""
    return _write(request, pk, mutations.rename_cluster)


@require_POST
def cluster_merge_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/merge` — `source`'s cards join `target`."""
    return _write(request, pk, mutations.merge_clusters)


@require_POST
def cluster_split_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/split` — the `cards` leave `cluster` for a new one.

    `cards` is repeated once per card, each value a `public_id`. `name` is
    optional and defaults to the name of the cluster being split.
    """
    return _write(request, pk, mutations.split_cluster)


@require_POST
def cluster_delete_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/delete` — `cluster` goes, its cards are ungrouped."""
    return _write(request, pk, mutations.delete_cluster)


# --------------------------------------------------------------------------
# Votes
#
# Casting and withdrawing are two more writes, in the same style: a POST with a
# CSRF token, answered with the same full board state the read endpoint and the
# seven cluster writes produce, so a voter's own updated votes and remaining
# budget come straight back in the response. They are separate URLs, so a client
# cannot reach one meaning the other, and neither is a GET.
# --------------------------------------------------------------------------


@require_POST
def vote_cast_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/votes/cast` — add `weight` votes to `cluster`.

    `cluster` is a cluster's integer id; `weight` is optional and defaults to
    one. Refused past the member's budget, and refused outside the VOTE stage.
    """
    return _write(request, pk, mutations.cast_vote)


@require_POST
def vote_withdraw_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/votes/withdraw` — take `weight` votes back off `cluster`.

    The votes return to the member's remaining budget. Refused outside VOTE, and
    refused when the member has fewer votes on the cluster than they ask to take.
    """
    return _write(request, pk, mutations.withdraw_vote)


@require_GET
def vote_progress_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`GET /retros/<pk>/votes/progress` — how many members have spent every vote.

    A facilitator-only count, and nothing else: it tells them when to close
    voting without telling them who voted for what. `_docs/decisions.md` item 10
    and #15's secrecy criteria — never which members, never partial progress per
    person, only how many have placed their whole budget.

    Gated by `can_advance_stage`, which is exactly "this cycle's facilitator":
    #15 defines no access rule of its own, so it reuses the predicate that
    already means facilitator rather than adding one, and the facilitator is the
    person this count exists for — it is what they watch to know when to advance.
    Everyone else — a member who is not the facilitator, a non-member, an
    anonymous browser — gets the same 404 an unused id gets, so the endpoint
    confirms nothing to anyone it is not for.
    """
    retro = Retrospective.objects.select_related("cycle__project").filter(pk=pk).first()
    if retro is None or not can_advance_stage(request.user, retro):
        raise Http404

    return JsonResponse({"finished": members_who_spent_everything(retro)})


# --------------------------------------------------------------------------
# Discussion — #16
#
# Four more writes, the same style: a POST with a CSRF token, answered with the
# same full board state, so a client can replace its state and skip a poll. They
# belong to the DISCUSS stage; `board/mutations.py` refuses one outside it with a
# 409 and one the caller may not make with a 403, and this file stays as thin as
# it is for the other writes. Separate URLs, so a client cannot reach one meaning
# another, and none of them is a GET.
# --------------------------------------------------------------------------


@require_POST
def cluster_status_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/clusters/status` — `cluster` moves to `status`.

    `cluster` is a cluster's integer id; `status` is one of the four
    `Cluster.Status` values. The facilitator's call, during DISCUSS: a member's
    direct POST is a 403, and a request outside DISCUSS is a 409.
    """
    return _write(request, pk, mutations.set_cluster_status)


@require_POST
def note_add_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/notes/add` — a new attributed note by the caller.

    `text` is the note; `cluster` is optional and names the cluster under
    discussion, absent for a note about the retrospective as a whole. Any member
    may add one during DISCUSS.
    """
    return _write(request, pk, mutations.add_note)


@require_POST
def note_edit_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/notes/edit` — `note`'s text becomes `text`.

    `note` is a note's integer id. The author's own, during DISCUSS: another
    member editing it is a 403, and everyone gets a 409 once the stage is COMPLETE.
    """
    return _write(request, pk, mutations.edit_note)


@require_POST
def note_delete_view(request: HttpRequest, pk: int) -> JsonResponse:
    """`POST /retros/<pk>/notes/delete` — `note` is removed.

    Its author or this cycle's facilitator, during DISCUSS. A member who is
    neither is a 403; once the stage is COMPLETE the notes are read-only and this
    is a 409.
    """
    return _write(request, pk, mutations.delete_note)


def _write(request: HttpRequest, pk: int, change) -> JsonResponse:
    """Run one operation and answer with the board, or with why it was refused.

    The refusal carries a status the client can act on and a sentence it can
    show — 409 for a board that has moved past clustering, 400 for a request
    that cannot be carried out as written — never a 200 with the board
    unchanged, which a client would apply as success.

    `Http404` is not caught: a retrospective, card or cluster that does not
    resolve is Django's own 404, byte for byte the same as one that was never
    used, and the same answer a non-member gets.
    """
    try:
        state = apply_mutation(request.user, pk, request.POST, change)
    except BoardRejection as rejection:
        return JsonResponse({"error": str(rejection)}, status=rejection.status)

    return JsonResponse(state)
