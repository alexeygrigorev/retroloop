"""A wrong URL name in a template or a partial fails here, loudly.

Every test in this file maps to an acceptance criterion of issue #62.

What went wrong. `templates/cycles/card_list.html` reversed its routes with
`{% url 'card-show' card.pk as show_url %}`. The `as var` form does not raise a
`NoReverseMatch`; it swallows it and leaves the variable empty. QA renamed the
`card-show` route while verifying #8 and the whole suite still reported 277
passed, while the live page served an edit form whose Cancel button carried
`hx-get=""` — HTTP 200, no error, no log line. Breaking `card-delete` did fail
three tests, but only because those tests happened to assert on URL strings. The
safety net was luck.

Two nets replace it, because each covers what the other cannot.

* The source scan reads every `{% url %}` tag in every template — including the
  ones inside a `{% partialdef %}` body, and the ones inside a branch no context
  reaches — and asks the URLconf whether that name exists and takes that many
  arguments. It is what makes "breaking any URL name fails the suite" true of
  *any* name, rather than of the names some test happens to render.

* The render sweep renders every template and every partial, in both scenes from
  `tests/template_render.py`, and asserts that no `href`, `action` or `hx-*`
  attribute came out empty. It is what catches a URL that is not a `{% url %}`
  tag at all — a `{{ cycle.get_absolute_url }}` in an `href`, a variable a view
  forgot to pass — and what would catch an `as var` coming back.

Both walk `templates/`. Neither has a list of templates, of partials or of URL
names written down in it, so a screen added by #12 or #14 is covered on the day
it is added.
"""

import re

import pytest
from django.template.loader import get_template
from django.urls import get_resolver, reverse
from django.utils.text import smart_split

from tests.template_render import PERMITTED, SCENES, render, scene, template_names, template_sources

TEMPLATE_SOURCES = template_sources()
TEMPLATE_NAMES = template_names()

#: A `{% url ... %}` tag, with everything between the tag name and the closing
#: brace. Nothing inside a tag may contain `%}`, so the lazy match ends where
#: the tag does.
URL_TAG = re.compile(r"{%\s*url\s+(?P<arguments>.*?)%}", re.DOTALL)

COMMENT_BLOCK = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
HASH_COMMENT = re.compile(r"{#.*?#}")

#: The attributes an empty value silently breaks. The first five are the ones
#: #62 names; `hx-put` and `hx-patch` are the other two htmx verbs, added here
#: because the board mutation endpoints of #12 are what arrives next.
URL_ATTRIBUTES = ("href", "action", "hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")

ATTRIBUTE = re.compile(
    r"\b(?P<name>" + "|".join(URL_ATTRIBUTES) + r")\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)


def _uncommented(source: str) -> str:
    """The template with its comments removed.

    A `{% url %}` written inside a `{% comment %}` block is prose about a tag
    and not a tag — this file's own subject matter gets discussed in template
    comments, so without this the scan below would trip over the explanation of
    itself.
    """
    return HASH_COMMENT.sub("", COMMENT_BLOCK.sub("", source))


class UrlTag:
    """One `{% url %}` tag, in the terms the URLconf would have to answer it."""

    def __init__(self, template: str, arguments: str) -> None:
        self.template = template
        self.source = f"{{% url {arguments.strip()} %}}"
        bits = list(smart_split(arguments))
        # `{% url 'name' a b as var %}` — the target variable is not an argument
        # and neither is the `as`.
        self.target = None
        if len(bits) >= 2 and bits[-2] == "as":
            self.target = bits[-1]
            bits = bits[:-2]
        self.quoted = bool(bits) and bits[0][:1] in "\"'"
        self.name = bits[0].strip("\"'") if bits else ""
        rest = bits[1:]
        self.keywords = sorted(bit.split("=", 1)[0] for bit in rest if "=" in bit)
        self.positional = [bit for bit in rest if "=" not in bit]

    def __str__(self) -> str:
        return f"{self.template}: {self.source}"


def _url_tags() -> list[UrlTag]:
    """Every `{% url %}` tag in the template tree, partial bodies included.

    Read from the source rather than from a render, on purpose: a tag inside an
    `{% if %}` that no scene reaches is a tag a browser reaches one day.
    """
    return [
        UrlTag(name, match.group("arguments"))
        for name, source in TEMPLATE_SOURCES.items()
        for match in URL_TAG.finditer(_uncommented(source))
    ]


URL_TAGS = _url_tags()


def _possibilities(name: str) -> list[list[str]]:
    """The parameter list of every route registered under `name`.

    An empty list means no route is registered under that name at all — which
    is what a typo, a rename and a deleted route all look like from a template.
    """
    entries = get_resolver().reverse_dict.getlist(name)
    return [sorted(parameters) for possibilities, *_ in entries for _, parameters in possibilities]


def _page_body_of_card_list() -> str:
    """`card_list.html` up to its first `{% partialdef %}` — the page itself."""
    return TEMPLATE_SOURCES["cycles/card_list.html"].split("{% partialdef")[0]


# --------------------------------------------------------------------------
# A. Every URL name a template uses is a route that exists
# --------------------------------------------------------------------------


def test_the_url_tags_are_actually_discovered() -> None:
    """Guards every parametrization below: an empty walk would prove nothing.

    The names asserted are the ones #62 was reported against, and one of them —
    `card-show` — is written nowhere but inside a `{% partialdef %}` body, so
    finding it is what says partial bodies are scanned as well as pages.
    """
    assert len(TEMPLATE_SOURCES) >= 12
    assert len(URL_TAGS) >= 15

    found = {tag.name for tag in URL_TAGS}
    for name in ("card-show", "card-edit", "card-delete", "card-create", "cycle-cards"):
        assert name in found, f"{name} is in no template, so nothing below checks it"

    assert "card-show" not in _page_body_of_card_list(), (
        "card-show is expected to be written only inside a partialdef body. If "
        "that has changed, this guard no longer proves partial bodies are read."
    )


@pytest.mark.parametrize("tag", URL_TAGS, ids=str)
def test_every_url_name_in_a_template_is_a_route_that_exists(tag: UrlTag) -> None:
    """The check the `as var` form used to swallow.

    Asked of the URLconf rather than of a render, so it holds for a tag in a
    branch no test renders as much as for one on the front page.
    """
    assert tag.quoted, f"{tag} names its route with a variable, so nothing can check it"
    assert _possibilities(tag.name), (
        f"{tag} names a route that does not exist. Either the template has the "
        f"wrong name, or the route was renamed and this template was not."
    )


@pytest.mark.parametrize("tag", URL_TAGS, ids=str)
def test_every_url_tag_passes_the_arguments_its_route_takes(tag: UrlTag) -> None:
    """Wrong arguments fail the same way a wrong name does — `NoReverseMatch`,
    swallowed by `as var` just as silently."""
    possibilities = _possibilities(tag.name)
    assert possibilities, f"{tag} names a route that does not exist"

    if tag.keywords:
        assert tag.keywords in possibilities, (
            f"{tag} passes {tag.keywords}, and no route registered under "
            f"{tag.name!r} takes those: {possibilities}"
        )
        return

    arities = {len(parameters) for parameters in possibilities}
    assert len(tag.positional) in arities, (
        f"{tag} passes {len(tag.positional)} argument(s), and the route(s) "
        f"registered under {tag.name!r} take {sorted(arities)}"
    )


def test_no_template_reverses_a_url_into_a_variable() -> None:
    """`{% url ... as var %}` is the form that swallows, so it is not used here.

    The rule is written in AGENTS.md; this is what enforces it. The plain tag
    raises where the `as var` form renders an empty attribute and a 200.
    """
    offenders = [str(tag) for tag in URL_TAGS if tag.target]

    assert offenders == [], (
        "`{% url ... as var %}` swallows a NoReverseMatch and leaves the "
        "variable empty, which reaches the browser as an empty attribute and no "
        "error at all. Use the plain `{% url %}` tag, and give the sweep in "
        "tests/template_render.py whatever context it needs to reverse "
        "against.\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------
# B. Nothing renders an empty URL attribute
# --------------------------------------------------------------------------


def _empty_attributes(rendered: str) -> list[str]:
    return [
        f"{match.group('name')}={match.group('quote')}{match.group('quote')}"
        for match in ATTRIBUTE.finditer(rendered)
        if not match.group("value").strip()
    ]


def test_the_attribute_scan_reads_the_attributes_it_checks() -> None:
    """Guards the sweep below: a regex that matched nothing would always pass."""
    empty = '<a href="">x</a><form action=" "><button hx-post=\'\'>y</button></form>'
    assert _empty_attributes(empty) == ['href=""', 'action=""', "hx-post=''"]

    filled = "<a href=\"/cards/1/\">x</a><button hx-get='/cards/1/edit/'>y</button>"
    assert _empty_attributes(filled) == []


@pytest.mark.django_db
@pytest.mark.parametrize("scene_name", SCENES)
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_renders_an_empty_url_attribute(name: str, scene_name: str) -> None:
    rendered = render(name, scene_name)

    assert rendered.strip(), f"{name} rendered nothing, so the check below proves nothing"
    empty = _empty_attributes(rendered)
    assert empty == [], (
        f"{name} rendered {', '.join(empty)} in the {scene_name} scene. An empty "
        f"href, action or hx- attribute is a control that does nothing and says "
        f"nothing: the link goes to the page it is already on, the button posts "
        f"nowhere, and the response is still a 200."
    )


@pytest.mark.django_db
@pytest.mark.parametrize("scene_name", SCENES)
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_renders_a_python_value_as_a_url(name: str, scene_name: str) -> None:
    """The same defect wearing a different hat: `href="None"` is what a missing
    object renders as once someone gives the attribute a default."""
    rendered = render(name, scene_name)

    wrong = [
        f'{match.group("name")}="{value}"'
        for match in ATTRIBUTE.finditer(rendered)
        if (value := match.group("value").strip()) in {"None", "False", "True"}
    ]
    assert wrong == [], f"{name} rendered {', '.join(wrong)} in the {scene_name} scene"


# --------------------------------------------------------------------------
# C. The render sweep really does reach those attributes
#
# Section B asserts an absence, and an absence is satisfied by a fragment that
# renders no controls at all. These say the permitted scene puts the controls on
# the page: each case renders one template and looks for the address the route
# actually reverses to, so a rename fails here as well as in section A.
# --------------------------------------------------------------------------

RENDERED_URLS = [
    ("cycles/card_list.html#card", "card-edit", lambda context: [context["card"].pk]),
    ("cycles/card_list.html#card", "card-delete", lambda context: [context["card"].pk]),
    ("cycles/card_list.html#card_edit_form", "card-show", lambda context: [context["card"].pk]),
    (
        "cycles/card_list.html#card_section",
        "card-create",
        lambda context: [context["section"]["cycle"].pk, context["section"]["category"]],
    ),
    ("cycles/cycle_detail.html", "cycle-cards", lambda context: [context["cycle"].pk]),
    ("cycles/cycle_detail.html", "cycle-close", lambda context: [context["cycle"].pk]),
    ("projects/project_detail.html", "cycle-create", lambda context: [context["project"].pk]),
    (
        "projects/project_detail.html",
        "project-rotate-link",
        lambda context: [context["project"].pk],
    ),
    ("retro/retro_detail.html", "retro-advance", lambda context: [context["retro"].pk]),
    ("retro/retro_detail.html", "meeting-upload", lambda context: [context["retro"].pk]),
    (
        "meetings/meeting_status.html",
        "meeting-record-status",
        lambda context: [context["record"].pk],
    ),
    ("home.html", "frontend_check", lambda context: []),
]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "route", "arguments"),
    RENDERED_URLS,
    ids=[f"{name}-{route}" for name, route, _ in RENDERED_URLS],
)
def test_the_permitted_scene_renders_the_url_tags_it_is_meant_to(
    name: str, route: str, arguments
) -> None:
    context, request = scene(PERMITTED)
    rendered = get_template(name).render(context, request)

    expected = reverse(route, args=arguments(context))
    assert f'"{expected}"' in rendered, (
        f"{name} did not render {route} ({expected}) in the permitted scene, so "
        f"the sweep above is reading a fragment with its controls missing"
    )
