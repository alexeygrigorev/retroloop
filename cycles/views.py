"""Feedback cycle and card views.

Who may open or close a cycle, and who may read or write a card, is decided in
`projects/permissions.py` and asked here. This module holds the enforcement,
and the templates only ever hide what a view already refuses.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from cycles.forms import CardForm, FeedbackCycleForm
from cycles.models import CARD_TEXT_MAX_LENGTH, Card, FeedbackCycle, monday_of
from projects.models import Project
from projects.permissions import (
    can_add_card,
    can_close_cycle,
    can_delete_card,
    can_edit_card,
    can_open_cycle,
    can_start_retrospective,
    can_view_card,
)
from projects.views import member_or_404

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
            # The retrospective that follows this week, and whether this person
            # may start it. The rule itself lives in projects/permissions.py;
            # this view only asks it, the same way the template only shows what
            # the view already decided.
            "retro": getattr(cycle, "retrospective", None),
            "can_start_retro": can_start_retrospective(request.user, cycle),
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

    Under the same lock as the card endpoints and the reveal, for the same
    reason: "already closed" read without one is a value that another
    transaction has already changed and not yet committed, so two facilitators
    clicking together would both be told they closed it. It writes no card, so
    nothing here could leak an author — it is locked because it is the fourth
    path that reads this row's state and writes based on it, and leaving one
    unlocked is how the first three came to differ.
    """
    with transaction.atomic():
        cycle = get_object_or_404(
            FeedbackCycle.objects.select_for_update(of=("self",)).select_related("project"), pk=pk
        )
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
# nothing for a forgotten `{% if %}` to expose. The predicates in
# projects/permissions.py are asked in addition, because they are what says
# *why* an answer was refused.
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


def lock_the_cycle_of(pk: int) -> None:
    """Take the cycle's row lock before writing to one of its cards.

    Every path that writes to `cycles_card` goes through this or through
    `card_create`'s own locking load, and `cycles/reveal.py` takes the same row
    on the way into REVEAL. That is what makes "the cycle is still COLLECTING"
    a fact for the rest of the request instead of a value that was true when it
    was read: a reveal in flight has already set the cycle to CLOSED and not
    yet committed, so an unlocked reader sees COLLECTING, passes the permission
    check, and applies its write on top of a reveal that has already happened
    and will not happen again.

    The lock is always taken on the cycle first and the card afterwards — the
    same order as `card_create` and as the reveal — so the acquisition order is
    the same everywhere and no two of them can wait on each other in a circle.
    `advance_stage` goes retrospective, then cycle, then cards; nothing here
    ever locks the retrospective, so that order cannot be reversed either.

    The card is read once, unlocked, only to find which cycle it belongs to. A
    card never moves between cycles, so that value cannot go stale.
    """
    cycle_id = Card.objects.filter(pk=pk).values_list("cycle_id", flat=True).first()
    if cycle_id is None:
        raise Http404
    FeedbackCycle.objects.select_for_update().get(pk=cycle_id)


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

    The row is locked while that is decided, and the card is written under the
    same lock. Reading the status without one is not enough: a reveal in
    flight has already set the cycle to CLOSED but not yet committed, so this
    request would still see COLLECTING, accept the card, and commit it into a
    cycle that has just been revealed — where nothing would ever null its
    author. That card would carry its author for good, which is the one defect
    #10 exists to make impossible. `cycles/reveal.py` takes the same lock, so
    the two orders are the only two possible: the card lands before the reveal
    counts and anonymises it, or the reveal wins and the card is refused.
    """
    form = CardForm(request.POST)

    with transaction.atomic():
        cycle = get_object_or_404(
            FeedbackCycle.objects.select_for_update(of=("self",)).select_related("project"), pk=pk
        )
        member_or_404(request.user, cycle.project)
        category = category_or_404(category)
        if not can_add_card(request.user, cycle):
            raise PermissionDenied

        if form.is_valid():
            card = form.save(commit=False)
            card.cycle = cycle
            card.category = category
            # On every card, including the anonymous ones. Anonymity is applied
            # at reveal by #10 — `_docs/decisions.md` item 3 — and a card with
            # no author before then is a card nobody could edit or delete.
            card.author = request.user
            card.save()
            # A fresh, empty form: the section comes back ready for the next
            # card.
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

    Two separate things stop this endpoint restoring an author the reveal has
    destroyed, because one of them is a lock and locks are easy to lose:

    - the cycle is locked before its status is read, so an edit racing the
      reveal waits for it and is then refused rather than allowed through on a
      value that was true a moment ago;
    - the save names its fields. A ModelForm's `save()` writes the whole row
      back from the instance it loaded, so a stale copy would carry the
      pre-reveal `author_id` and `position` with it. `text` and `is_anonymous`
      are the only two columns this form owns, and now the only two it can
      write — the `UPDATE` cannot mention `author_id` at all.
    """
    with transaction.atomic():
        lock_the_cycle_of(pk)
        card = own_card_or_404(request.user, pk)
        member_or_404(request.user, card.cycle.project)
        if not can_edit_card(request.user, card):
            raise PermissionDenied

        form = CardForm(request.POST if request.method == "POST" else None, instance=card)
        saved = request.method == "POST" and form.is_valid()
        if saved:
            # The form's own field list, so the two cannot drift apart.
            form.save(commit=False).save(update_fields=list(CardForm.Meta.fields))

    if saved:
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

    Locked like the other two. A delete that lands after the reveal has counted
    the card and given it a position takes the card out from under both, so the
    participation row counts a card that is gone and the positions are left with
    a hole in them — `_docs/decisions.md` item 1 frozen by timing rather than by
    the rule.
    """
    with transaction.atomic():
        lock_the_cycle_of(pk)
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
