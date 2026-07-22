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
from board.mutations import BoardRejection, apply_mutation
from board.serializers import board_state, unchanged_state
from projects.permissions import can_view_project
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
