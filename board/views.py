"""The board's one read endpoint.

Thin, like every other view here: find the row, refuse the people who may not
see it, and hand the serializer the rest. It decides nothing about who may see
what — `projects/permissions.py` does — and it mutates nothing. The writes are
#12's, the polling that calls this is #14's.
"""

from django.http import Http404, HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

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

    GET only. The board's writes are #12's, on endpoints of their own, and a
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
