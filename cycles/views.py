"""Feedback cycle and card views.

The access rules live at the top of this module as one-line predicates, in the
shape `projects/views.py` already uses. Issue #6 lifts `can_open_cycle`,
`can_close_cycle`, `can_add_card`, `can_view_card`, `can_edit_card` and
`can_delete_card` into `projects/permissions.py` unchanged and deletes them from
here; until then this module is the single place that decides who may open or
close a cycle and who may read or write a card. Templates only ever hide what a
view already refuses.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from cycles.forms import CardForm, FeedbackCycleForm
from cycles.models import CARD_TEXT_MAX_LENGTH, Card, FeedbackCycle, monday_of
from projects.models import Project
from projects.views import is_facilitator, is_member, member_or_404

# --------------------------------------------------------------------------
# Rules. One condition each, so #6 can lift them out as they are.
# --------------------------------------------------------------------------


def can_open_cycle(user, project: Project) -> bool:
    return project.owner_id == user.pk or is_facilitator(user, project)


def can_close_cycle(user, cycle: FeedbackCycle) -> bool:
    return cycle.facilitator_id == user.pk and cycle.status == FeedbackCycle.Status.COLLECTING


def can_add_card(user, cycle: FeedbackCycle) -> bool:
    return cycle.accepts_cards and is_member(user, cycle.project)


def can_view_card(user, card: Card) -> bool:
    return card.author_id is not None and card.author_id == user.pk


def can_edit_card(user, card: Card) -> bool:
    return can_view_card(user, card) and card.cycle.accepts_cards


def can_delete_card(user, card: Card) -> bool:
    return can_view_card(user, card) and card.cycle.accepts_cards


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@login_required
def cycle_create(request: HttpRequest, project_pk: int) -> HttpResponse:
    project = get_object_or_404(Project, pk=project_pk)
    member_or_404(request.user, project)
    if not can_open_cycle(request.user, project):
        raise PermissionDenied

    now = timezone.now()
    form = FeedbackCycleForm(
        request.POST or None,
        project=project,
        initial={
            "week_start": monday_of(timezone.localdate()),
            "opens_at": now,
            # A working week later, as a starting point the facilitator edits.
            # It is a deadline the team is shown, not a trigger: see cycle_close.
            "closes_at": now + timedelta(days=5),
            "facilitator": request.user,
        },
    )
    if request.method == "POST" and form.is_valid():
        cycle = form.save(commit=False)
        cycle.project = project
        cycle.save()
        messages.success(
            request,
            f"The cycle for the week of {date_format(cycle.week_start, 'j F Y')} is open.",
        )
        return redirect(cycle)

    return render(request, "cycles/cycle_form.html", {"form": form, "project": project})


@login_required
def cycle_detail(request: HttpRequest, pk: int) -> HttpResponse:
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project", "facilitator"), pk=pk)
    # A cycle is as private as its project, and answers a non-member the same
    # way an id that was never used does.
    member_or_404(request.user, cycle.project)

    return render(
        request,
        "cycles/cycle_detail.html",
        {
            "cycle": cycle,
            "project": cycle.project,
            "can_close": can_close_cycle(request.user, cycle),
        },
    )


@login_required
@require_POST
def cycle_close(request: HttpRequest, pk: int) -> HttpResponse:
    """Close a cycle, on a person's decision and nothing else.

    Nothing here waits for everyone to have submitted — `_docs/decisions.md`
    item 4 — and nothing closes a cycle because `closes_at` has passed: there is
    no scheduler in the MVP, and a cycle past its deadline stays `COLLECTING`
    until a human acts. There is no reopening: `can_close_cycle` is false for a
    cycle that is already `CLOSED`, so a second attempt is refused here rather
    than merely hidden in the template.
    """
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project"), pk=pk)
    member_or_404(request.user, cycle.project)
    if not can_close_cycle(request.user, cycle):
        raise PermissionDenied

    cycle.status = FeedbackCycle.Status.CLOSED
    cycle.save(update_fields=["status"])
    messages.success(
        request,
        "The cycle is closed. Nothing more can be submitted to it, and it cannot be reopened.",
    )
    return redirect(cycle)


# --------------------------------------------------------------------------
# Cards
#
# Every card query below is filtered by author before anything else happens to
# it. That filter is the security boundary — a member's own cards are the only
# rows a card view ever loads, so there is nothing for a template to leak and
# nothing for a forgotten `{% if %}` to expose. The predicates above are asked
# in addition, because they are what #6 lifts out and what says *why* an
# answer was refused.
# --------------------------------------------------------------------------


def own_cards(user, cycle: FeedbackCycle) -> QuerySet[Card]:
    """The cards `user` wrote into `cycle`, and no others."""
    return cycle.cards.filter(author=user).select_related("cycle")


def own_card_or_404(user, pk: int) -> Card:
    """One card of `user`'s own, by id.

    Another member's card, and a card whose author has been removed at reveal,
    are both 404 rather than 403: the answer says nothing about whether the id
    exists, let alone who wrote it.
    """
    return get_object_or_404(
        Card.objects.select_related("cycle", "cycle__project"), pk=pk, author=user
    )


def card_section(request: HttpRequest, cycle: FeedbackCycle, category: str, form=None) -> dict:
    """Everything one Start/Stop/Continue section needs to render itself.

    The same dictionary serves the full page and the htmx fragment, so a
    section swapped in after a create or a delete is built by the code that
    built it on the first load.
    """
    form = form if form is not None else CardForm()
    return {
        "cycle": cycle,
        "category": category,
        "category_label": Card.Category(category).label,
        "cards": own_cards(request.user, cycle).filter(category=category),
        "form": form,
        "remaining": CARD_TEXT_MAX_LENGTH - len(form.data.get("text", "") if form.is_bound else ""),
        "can_add": can_add_card(request.user, cycle),
    }


def category_or_404(category: str) -> str:
    if category not in Card.Category.values:
        raise Http404
    return category


@login_required
def card_list(request: HttpRequest, pk: int) -> HttpResponse:
    """The submission screen: the member's own cards, under three headings."""
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project"), pk=pk)
    # A non-member is not told the cycle exists, so the whole screen is a 404
    # for them rather than an empty one.
    member_or_404(request.user, cycle.project)

    cards = own_cards(request.user, cycle)
    return render(
        request,
        "cycles/card_list.html",
        {
            "cycle": cycle,
            "project": cycle.project,
            # The member's own cards, already filtered: the template never sees
            # anyone else's, so a test can assert on the context and not only
            # on the HTML.
            "cards": cards,
            "sections": [
                card_section(request, cycle, category) for category in Card.Category.values
            ],
            "can_add": can_add_card(request.user, cycle),
            "text_limit": CARD_TEXT_MAX_LENGTH,
        },
    )


@login_required
@require_POST
def card_create(request: HttpRequest, pk: int, category: str) -> HttpResponse:
    """Add one card to one section, and answer with that section.

    The cycle's state is read here, on the request, and not trusted from the
    page the form was rendered on: a cycle closed while the member was typing
    refuses the POST that follows, which is the half of #7's "once CLOSED, no
    card may be created" that only an endpoint can prove.
    """
    cycle = get_object_or_404(FeedbackCycle.objects.select_related("project"), pk=pk)
    member_or_404(request.user, cycle.project)
    category = category_or_404(category)
    if not can_add_card(request.user, cycle):
        raise PermissionDenied

    form = CardForm(request.POST)
    if form.is_valid():
        card = form.save(commit=False)
        card.cycle = cycle
        card.category = category
        # On every card, including the anonymous ones. Anonymity is applied at
        # reveal by #10 — `_docs/decisions.md` item 3 — and a card with no
        # author before then is a card nobody could edit or delete.
        card.author = request.user
        card.save()
        # A fresh, empty form: the section comes back ready for the next card.
        form = None

    return render(
        request,
        "cycles/card_list.html#card_section",
        {
            "section": card_section(request, cycle, category, form),
            "text_limit": CARD_TEXT_MAX_LENGTH,
        },
    )


@login_required
def card_show(request: HttpRequest, pk: int) -> HttpResponse:
    """One card, read-only. What Cancel swaps back in over an edit form."""
    card = own_card_or_404(request.user, pk)
    member_or_404(request.user, card.cycle.project)
    if not can_view_card(request.user, card):
        raise Http404

    return render(request, "cycles/card_list.html#card", {"card": card, "cycle": card.cycle})


@login_required
def card_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """The edit form on GET, the re-worded card on POST.

    The anonymous checkbox is part of this form, so it can still be changed
    while the cycle is collecting.
    """
    card = own_card_or_404(request.user, pk)
    member_or_404(request.user, card.cycle.project)
    if not can_edit_card(request.user, card):
        raise PermissionDenied

    form = CardForm(request.POST if request.method == "POST" else None, instance=card)
    if request.method == "POST" and form.is_valid():
        form.save()
        return render(request, "cycles/card_list.html#card", {"card": card, "cycle": card.cycle})

    return render(
        request,
        "cycles/card_list.html#card_edit_form",
        {
            "card": card,
            "cycle": card.cycle,
            "form": form,
            "remaining": CARD_TEXT_MAX_LENGTH - len(card.text),
            "text_limit": CARD_TEXT_MAX_LENGTH,
        },
    )


@login_required
@require_POST
def card_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove one of your own cards, and answer with the section it was in.

    POST only — `require_POST` makes a GET a 405 — so no link, no crawler and
    no prefetch can delete a card, and the form that does carries a CSRF token.
    """
    card = own_card_or_404(request.user, pk)
    member_or_404(request.user, card.cycle.project)
    if not can_delete_card(request.user, card):
        raise PermissionDenied

    cycle, category = card.cycle, card.category
    card.delete()

    return render(
        request,
        "cycles/card_list.html#card_section",
        {"section": card_section(request, cycle, category), "text_limit": CARD_TEXT_MAX_LENGTH},
    )
