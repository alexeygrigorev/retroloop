"""Every template and partial, and the one context the sweeps render them with.

Two test modules import this one — `test_template_comments.py`, which renders
the tree looking for leaked comment source, and `test_template_urls.py`, which
renders it looking for empty URL attributes. The walk that finds the templates
and the context they are handed live here so that neither sweep grows a copy of
the other's, and so a third sweep costs nothing.

Why a context exists at all
---------------------------

`templates/cycles/card_list.html` used to reverse its URLs with
`{% url 'card-show' card.pk as show_url %}`, for one reason: the comment sweep
rendered every template and every `{% partialdef %}` with an EMPTY context, and
the plain tag raises `NoReverseMatch` when `card.pk` is not there. The `as var`
form does not raise. It swallows the failure and leaves the variable empty, so
a renamed route reached the browser as `hx-get=""` — a Cancel button that did
nothing, HTTP 200, no log line, and a suite that stayed green (issue #62).

So the tag is plain everywhere now, and the objects it reverses against are
built here instead. That is the trade this module exists to make: a little
setup in one place, in exchange for a wrong URL name being loud again.

Why there are two scenes
------------------------

A template is a set of branches, and a sweep only checks the branches it
renders. One scene has everything present and everything permitted; the other
has nothing permitted, an empty list wherever there was a list, a closed cycle
and a failed upload. Between them the `{% if %}` and the `{% else %}` of every
control on every page are rendered, which is the coverage the empty context
used to give for free on the negative half.

Both scenes carry the same objects, and differ only in status and in
permission. An object missing from a scene is not a state worth sweeping: it
would render an empty `href` from `{{ cycle.get_absolute_url }}` — the very
thing `test_template_urls.py` asserts against — and prove nothing about the
template.
"""

import re
from datetime import UTC, date, datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.template.loader import get_template
from django.test import RequestFactory

from cycles.forms import CardForm
from cycles.models import CARD_TEXT_MAX_LENGTH, Card, FeedbackCycle
from cycles.views import card_section
from meetings.models import MeetingRecord
from meetings.uploads import (
    AUDIO_EXTENSIONS,
    MAX_UPLOAD_LABEL,
    TRANSCRIPT_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from projects.models import Membership, Project
from retro.forms import ActionItemForm, DecisionForm
from retro.models import ActionItem, Cluster, Decision, Retrospective
from retro.views import board_bootstrap

BASE_DIR = Path(settings.BASE_DIR)
TEMPLATES_DIR = BASE_DIR / "templates"

PARTIALDEF = re.compile(r"{%\s*partialdef\s+([\w-]+)")

#: The two scenes, by name. They are parametrization ids as well as arguments,
#: so a failure reads "…[card_list.html-refused]" and says which half broke.
PERMITTED = "permitted"
REFUSED = "refused"
SCENES = (PERMITTED, REFUSED)

WEEK_START = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

PASSWORD = "keel-haul-mizzen-41"


def template_sources() -> dict[str, str]:
    """Every template under `templates/`, by name, with its source."""
    return {
        path.relative_to(TEMPLATES_DIR).as_posix(): path.read_text()
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
    }


def template_names() -> list[str]:
    """Every template in `templates/`, plus every partial defined inside one.

    Discovered by walking the directory, never from a list written down: a
    screen added by a later issue is swept the day it is added, and a partial
    added to an existing screen the moment it is defined.
    """
    names = []
    for name, source in template_sources().items():
        names.append(name)
        names += [f"{name}#{partial}" for partial in PARTIALDEF.findall(source)]
    return names


def _request(user) -> HttpRequest:
    request = RequestFactory().get("/")
    request.user = user
    return request


def scene(name: str) -> tuple[dict, HttpRequest]:
    """The context and request for one scene, built from real rows.

    Real objects rather than stand-ins, because half of what these sweeps read
    is a URL a model produced: `{{ cycle.get_absolute_url }}` is in an `href` on
    four pages, and a stand-in carrying a hardcoded string would assert that the
    string is not empty and nothing more.

    Needs the database, so every test that calls this is a `django_db` test.
    """
    permitted = name == PERMITTED
    user = get_user_model().objects.create_user(
        username="facilitator",
        password=PASSWORD,
        display_name="Robin Facilitator",
    )
    project = Project.objects.create(name="Platform", owner=user)
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=WEEK_START,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=user,
        # Drives the cards the sections carry: `card_section()` below asks
        # `can_add_card` and `can_edit_card` about this cycle, and a closed one
        # answers no to both.
        status=FeedbackCycle.Status.COLLECTING if permitted else FeedbackCycle.Status.CLOSED,
    )
    card = Card.objects.create(
        cycle=cycle,
        category=Card.Category.START,
        text="Pair on the deploy script before Thursday.",
        author=user,
    )
    # The per-card flags the view attaches, set here rather than computed, like
    # every other permission flag in this scene: what the sweeps need is that
    # both sides of each `{% if %}` are rendered somewhere. Which answer
    # `can_edit_card` gives this person is the subject of tests/test_cards.py.
    card.can_edit = permitted
    card.can_delete = permitted
    retro = Retrospective.objects.create(
        cycle=cycle,
        stage=Retrospective.Stage.DISCUSS if permitted else Retrospective.Stage.COMPLETE,
    )
    record = MeetingRecord.objects.create(
        retrospective=retro,
        uploaded_by=user,
        kind=MeetingRecord.Kind.AUDIO,
        original_filename="retro.m4a",
        size_bytes=4_200_000,
        # Uploaded is still moving, so the fragment polls; failed is done, so it
        # does not and the failure paragraph renders instead.
        status=MeetingRecord.Status.UPLOADED if permitted else MeetingRecord.Status.FAILED,
        error_message="" if permitted else "The transcription service refused the file.",
    )

    # #17's outcomes. A cluster to attach them to, one decision and one action
    # item, each carrying the per-viewer flags the view sets on the row so both
    # sides of every control's `{% if %}` are rendered across the two scenes. The
    # action item is unassigned and overdue in the refused scene, so the marked
    # branches render too.
    cluster = Cluster.objects.create(retrospective=retro, name="Deploys", position=1)
    decision = Decision.objects.create(
        retrospective=retro, cluster=cluster, text="Pair on the deploy script.", created_by=user
    )
    decision.can_edit = permitted
    action_item = ActionItem.objects.create(
        retrospective=retro,
        cluster=cluster,
        description="Write the runbook.",
        owner=user if permitted else None,
        due_date=None if permitted else date(2020, 1, 1),
        status=ActionItem.Status.OPEN,
        created_by=user,
    )
    action_item.can_edit = permitted
    action_item.can_update = permitted

    # #24's review screen. An extracted draft of each kind, still in review, so the
    # accept/edit/reject controls and the owner dropdown both render; the draft
    # action item is unassigned so the "pick an owner" branch is swept too. These
    # rows are EXTRACTED/DRAFT, so they never appear on #17's outcomes lists above,
    # which the view filters to CONFIRMED.
    draft_decision = Decision.objects.create(
        retrospective=retro,
        cluster=cluster,
        text="Ship smaller pull requests.",
        excerpt="We kept blocking on huge PRs, let's ship smaller ones.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.DRAFT,
    )
    draft_action_item = ActionItem.objects.create(
        retrospective=retro,
        cluster=cluster,
        description="Split the deploy PR before Thursday.",
        excerpt="Someone should split that deploy PR before Thursday.",
        owner=None,
        source=ActionItem.Source.EXTRACTED,
        review_status=ActionItem.ReviewStatus.DRAFT,
        status=ActionItem.Status.OPEN,
    )

    # The sections are the view's own, so what is swept is what a browser gets:
    # `card_section()` fills in the form, the remaining characters and `can_add`
    # from the cycle it is given. The refused scene asks for the category the
    # card is not in, which is how the empty-section line gets rendered too.
    owner_request = _request(user)
    sections = [card_section(owner_request, cycle, category) for category in Card.Category.values]
    section = sections[0] if permitted else card_section(owner_request, cycle, "STOP")

    context = {
        # Objects. Present in both scenes: see the module docstring.
        "project": project,
        "cycle": cycle,
        "card": card,
        "retro": retro,
        "record": record,
        "section": section,
        "form": CardForm(),
        "decision_form": DecisionForm(retrospective=retro),
        "action_item_form": ActionItemForm(retrospective=retro, project=project),
        "remaining": CARD_TEXT_MAX_LENGTH,
        "stages": Retrospective.Stage.choices,
        "next_stage_label": Retrospective.Stage.COMPLETE.label,
        "board_bootstrap": board_bootstrap(user, retro),
        "join_url": f"http://testserver{project.join_path()}",
        "served_at": "14:03:11",
        "next": "/projects/",
        "audio_extensions": ", ".join(AUDIO_EXTENSIONS),
        "video_extensions": ", ".join(VIDEO_EXTENSIONS),
        "transcript_extensions": ", ".join(TRANSCRIPT_EXTENSIONS),
        "max_upload_label": MAX_UPLOAD_LABEL,
        # Lists, so that both the populated branch and the empty-state branch of
        # every page are rendered across the two scenes.
        "projects": [project] if permitted else [],
        "cycles": [cycle] if permitted else [],
        "decisions": [decision],
        "action_items": [action_item],
        # #24's review screen: the drafts, the roster its owner dropdowns are built
        # from, the extracted summary it shows, and the count the discard-on-
        # complete confirmation names. Present in both scenes, like every other
        # object here, so both the populated and the empty branch are swept.
        "draft_decisions": [draft_decision],
        "draft_action_items": [draft_action_item],
        "members": get_user_model()
        .objects.filter(memberships__project=project)
        .order_by("username"),
        "summary": "The team agreed to ship smaller pull requests.",
        "draft_count": 2,
        "memberships": project.memberships.all() if permitted else [],
        "cards": [card] if permitted else [],
        "sections": sections if permitted else [],
        # Permission flags, set rather than computed: what a sweep needs is that
        # both sides of each `{% if %}` are rendered somewhere. Whether the
        # predicate behind the flag says yes to this person is the subject of
        # tests/test_permissions.py.
        "can_add": permitted,
        "can_close": permitted,
        "can_open_cycle": permitted,
        "can_rotate": permitted,
        "can_start_retro": permitted,
        "can_advance": permitted,
        "can_review": permitted,
        "can_hand_over_meeting": permitted,
        "upload_is_open": permitted,
        "can_upload": permitted,
        "polling": permitted,
        "failed": not permitted,
    }
    # Signed in for one scene and signed out for the other, because the account
    # controls in `base_app.html` are two different sets of links.
    return context, _request(user if permitted else AnonymousUser())


def render(name: str, scene_name: str) -> str:
    """Render one template or partial in one scene."""
    context, request = scene(scene_name)
    return get_template(name).render(context, request)
