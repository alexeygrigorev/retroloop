"""Every screen renders styled, and the rules that keep the next one styled too.

Every test here maps to an acceptance criterion of issue #58.

The tests that matter most are the ones that walk. `templates/` is discovered by
walking the directory, never from a list written down here, so a screen added by
a later issue is covered the day it is added and nobody has to remember to come
back and edit this file. The same goes for the checks on the stylesheet: they ask
what the templates actually contain, not what they contained at grooming time.

They also assert absences. This project shipped three unstyled pages and a leaked
template comment past a green suite, because every test asked whether a required
string was present and none asked whether a forbidden one was gone.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from projects.models import Membership, Project

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
TEMPLATES_DIR = BASE_DIR / "templates"

EXTENDS = re.compile(r"{%\s*extends\s+[\"']([^\"']+)[\"']\s*%}")
INCLUDE = re.compile(r"{%\s*include\s+[\"']([^\"']+)[\"']")

LAYOUTS = ("base.html", "base_app.html")
DOCUMENT_SHELL = ("<!doctype", "<html", "<head", "<body")


def _templates() -> dict[str, str]:
    """Every template under `templates/`, by name, with its source."""
    return {
        path.relative_to(TEMPLATES_DIR).as_posix(): path.read_text()
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
    }


TEMPLATE_SOURCES = _templates()


def _fragments() -> set[str]:
    """Exempt: a layout another template extends, or a body only ever included.

    (A `{% partialdef %}` body is exempt by the same rule — it is not a file of
    its own, it lives inside the page template that defines it and is served
    through `name.html#partial`.)
    """
    exempt: set[str] = set()
    for source in TEMPLATE_SOURCES.values():
        exempt.update(EXTENDS.findall(source))
        exempt.update(INCLUDE.findall(source))
    return exempt


FULL_PAGE_TEMPLATES = sorted(set(TEMPLATE_SOURCES) - _fragments())


# --------------------------------------------------------------------------
# A. Every page is inside the layout
# --------------------------------------------------------------------------


def test_the_template_tree_is_discovered_by_walking_it() -> None:
    """Guards every parametrization below: an empty walk would prove nothing.

    It also pins the exemption rule in both directions — `base.html` is a layout
    and is not asked to extend anything, while the pages that extend it are.
    """
    assert TEMPLATES_DIR.is_dir()
    assert len(TEMPLATE_SOURCES) >= 12
    assert "base.html" not in FULL_PAGE_TEMPLATES
    assert "base_app.html" not in FULL_PAGE_TEMPLATES
    for name in ("home.html", "projects/project_list.html", "cycles/cycle_form.html"):
        assert name in FULL_PAGE_TEMPLATES, name


@pytest.mark.parametrize("name", FULL_PAGE_TEMPLATES)
def test_every_full_page_template_extends_the_base_layout(name: str) -> None:
    source = TEMPLATE_SOURCES[name]
    extended = EXTENDS.findall(source)

    assert extended, f"{name} renders a whole page but extends nothing"
    assert extended[0] in LAYOUTS, f"{name} extends {extended[0]}, not one of {LAYOUTS}"
    assert source.lstrip().startswith("{% extends"), (
        f"{name} has content before its `{{% extends %}}` tag"
    )


@pytest.mark.parametrize("name", FULL_PAGE_TEMPLATES)
def test_no_full_page_template_carries_a_document_shell_of_its_own(name: str) -> None:
    """The shell belongs to `base.html`. A page that repeats it escapes the layout."""
    source = TEMPLATE_SOURCES[name].lower()

    for tag in DOCUMENT_SHELL:
        assert tag not in source, f"{name} carries its own {tag}"


@pytest.mark.parametrize("name", FULL_PAGE_TEMPLATES)
def test_every_full_page_template_names_itself_in_the_browser_tab(name: str) -> None:
    assert re.search(r"{%\s*block\s+title\s*%}", TEMPLATE_SOURCES[name]), (
        f"{name} does not set a title block, so the tab says only the site name"
    )


def test_the_stale_plain_and_unstyled_comment_is_gone_everywhere() -> None:
    offenders = [
        name for name, source in TEMPLATE_SOURCES.items() if "Plain and unstyled" in source
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# A, rendered: the project pages reach the stylesheet and the navigation
# --------------------------------------------------------------------------

PASSWORD = "keel-haul-mizzen-41"


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="olive", password=PASSWORD, display_name="Olive Owner")


@pytest.fixture
def project(owner):
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def as_owner(client, owner):
    client.login(username="olive", password=PASSWORD)
    return client


@pytest.fixture
def project_urls(project):
    return ["/projects/", "/projects/new/", project.get_absolute_url()]


@pytest.mark.django_db
def test_every_project_page_reaches_the_stylesheet(as_owner, project_urls) -> None:
    """No stylesheet is exactly what the three hand-rolled documents were missing."""
    for url in project_urls:
        body = as_owner.get(url).content.decode()

        assert "css/app.css" in body, url
        assert body.count("<!doctype html>") == 1, url


@pytest.mark.django_db
def test_every_project_page_carries_the_same_navigation_as_the_homepage(
    as_owner, project_urls
) -> None:
    """A member on `/projects/` had no way back and no way to log out."""
    for url in project_urls:
        body = as_owner.get(url).content.decode()

        assert "<nav" in body, url
        assert "Olive Owner" in body, url
        assert "Your projects" in body, url
        assert "Change password" in body, url
        assert "Log out" in body, url
        assert f'action="{reverse("logout")}"' in body, url


@pytest.mark.django_db
def test_joining_reports_itself_through_the_base_flash_region(client, project, db) -> None:
    User.objects.create_user(username="newbie", password=PASSWORD, display_name="Newbie")
    client.login(username="newbie", password=PASSWORD)

    join = reverse("join-project", args=[project.join_token])
    body = client.get(join, follow=True).content.decode()

    assert "You have joined Platform." in body
    assert re.search(
        r'<li\b[^>]*data-message-level="success"[^>]*>\s*You have joined Platform\.', body
    ), "the join message did not come through the base flash region"


@pytest.mark.django_db
def test_replacing_the_join_link_reports_itself_through_the_base_flash_region(
    as_owner, project
) -> None:
    body = as_owner.post(
        reverse("project-rotate-link", args=[project.pk]), follow=True
    ).content.decode()

    assert re.search(
        r'<li\b[^>]*data-message-level="success"[^>]*>\s*The join link has been replaced', body
    ), "the rotation message did not come through the base flash region"


def test_no_template_loops_over_messages_itself() -> None:
    """One flash region, in base.html. A second one is an unstyled bullet list."""
    offenders = [
        name
        for name, source in TEMPLATE_SOURCES.items()
        if name != "base.html" and re.search(r"{%\s*if\s+messages\s*%}", source)
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# B. Form fields are visible, and the same on every screen
# --------------------------------------------------------------------------

STYLESHEET = (BASE_DIR / "assets" / "css" / "app.css").read_text()

FORM_BLOCK = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
CONTROL = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.IGNORECASE)
ATTRS = re.compile(r'([\w-]+)="([^"]*)"')
LABEL_FOR = re.compile(r'<label\b[^>]*\bfor="([^"]+)"')


def _attrs(fragment: str) -> dict[str, str]:
    return {name.lower(): value for name, value in ATTRS.findall(fragment)}


def _visible_controls(html: str) -> list[dict[str, str]]:
    controls = [_attrs(rest) for _tag, rest in CONTROL.findall(html)]
    return [c for c in controls if c.get("type") != "hidden"]


@pytest.fixture
def form_pages(as_owner, project, client):
    """Every screen in the app that renders a Django form, discovered per issue."""
    anonymous = type(client)()
    return [
        (anonymous, "/accounts/login/"),
        (anonymous, "/accounts/signup/"),
        (as_owner, "/accounts/password_change/"),
        (as_owner, "/projects/new/"),
        (as_owner, f"/projects/{project.pk}/cycles/new/"),
    ]


def test_no_template_renders_a_form_as_p() -> None:
    """`as_p` is what put the label, the control and the help text on one line."""
    offenders = [name for name, source in TEMPLATE_SOURCES.items() if "as_p" in source]

    assert offenders == []


def test_every_template_that_renders_a_form_uses_the_shared_treatment() -> None:
    offenders = []
    for name, source in TEMPLATE_SOURCES.items():
        if "{{ form." not in source:
            continue
        if "form.as_div" not in source or 'class="form-fields"' not in source:
            offenders.append(name)

    assert offenders == [], 'a form has to be `{{ form.as_div }}` inside `class="form-fields"`'


@pytest.mark.django_db
def test_every_rendered_form_puts_its_controls_inside_the_form_scope(form_pages) -> None:
    """A control outside `.form-fields` is a control nothing styles."""
    for page_client, url in form_pages:
        body = page_client.get(url).content.decode()

        for attributes, inner in FORM_BLOCK.findall(body):
            if not _visible_controls(inner):
                continue  # the log-out form: a hidden token and a button
            assert "form-fields" in _attrs(attributes).get("class", ""), url


@pytest.mark.django_db
def test_every_field_has_a_label_bound_to_its_control(form_pages) -> None:
    """Clicking the label text focuses the control, on every form screen."""
    for page_client, url in form_pages:
        body = page_client.get(url).content.decode()
        bound = set(LABEL_FOR.findall(body))

        for control in _visible_controls(body):
            assert control.get("id"), (url, control)
            assert control["id"] in bound, (url, control["id"])


def test_the_stylesheet_gives_every_control_a_border_a_background_padding_and_a_radius() -> None:
    """What made a password field invisible was preflight, not the browser."""
    scope = STYLESHEET.split(".form-fields input")[1].split("}")[0]

    assert "border border-slate-300" in scope
    assert "bg-white" in scope
    assert "rounded-md" in scope
    assert "px-3 py-2" in scope


def test_the_stylesheet_orders_a_field_label_control_help_then_errors() -> None:
    """Django emits label, help text, errors, control; `order` reads them back."""
    ordered = re.findall(r"(label|input|\.helptext|\.errorlist)[^{]*\{[^}]*order: (\d)", STYLESHEET)
    by_part = {part: int(index) for part, index in ordered}

    assert by_part["label"] < by_part["input"] < by_part[".helptext"] < by_part[".errorlist"]


def test_one_focus_ring_is_declared_for_everything_a_keyboard_reaches() -> None:
    rule = re.search(r":where\(([^)]*)\):focus-visible\s*\{([^}]*)\}", STYLESHEET)

    assert rule is not None, "no single focus-visible rule in the stylesheet"
    for element in ("a", "button", "input", "select", "textarea"):
        assert element in [part.strip() for part in rule.group(1).split(",")]
    assert "outline" in rule.group(2)
    assert "outline-offset" in rule.group(2)


def _assert_error_is_attached_to(body: str, field_id: str) -> None:
    """The error is that field's, not a bullet list floating above the form.

    Django names the list `<field>_error` and points the control at it through
    `aria-describedby`, so the attachment is checkable rather than a matter of
    where it happens to appear on screen.
    """
    errors = re.search(rf'<ul class="errorlist" id="{field_id}_error">(.*?)</ul>', body, re.DOTALL)
    assert errors is not None, f"no error list for {field_id}"
    assert errors.group(1).strip(), f"the error list for {field_id} is empty"

    control = re.search(rf"<(?:input|select|textarea)\b[^>]*id=\"{field_id}\"[^>]*>", body)
    assert control is not None, field_id
    assert f"{field_id}_error" in _attrs(control.group()).get("aria-describedby", ""), field_id


@pytest.mark.django_db
def test_a_rejected_signup_attaches_its_error_to_the_field(client, db) -> None:
    body = client.post(
        "/accounts/signup/",
        {
            "username": "newbie",
            "display_name": "Newbie",
            "password1": "keel-haul-mizzen-41",
            "password2": "a-completely-different-one-42",
        },
    ).content.decode()

    _assert_error_is_attached_to(body, "id_password2")
    assert "password fields didn" in body


@pytest.mark.django_db
def test_a_rejected_project_name_attaches_its_error_to_the_field(as_owner) -> None:
    body = as_owner.post("/projects/new/", {"name": ""}).content.decode()

    _assert_error_is_attached_to(body, "id_name")
    assert "This field is required." in body


def test_the_form_treatment_is_css_only() -> None:
    """The decision: no renderer, no template pack, no widget classes in Python.

    A form written by a later issue is styled the day it is written, and the
    styling lives in one file instead of in every form class.
    """
    assert not (BASE_DIR / "templates" / "django" / "forms").exists()

    for source in BASE_DIR.rglob("*.py"):
        if any(part in source.parts for part in (".venv", "node_modules", "migrations", "tests")):
            continue
        text = source.read_text()
        assert "FORM_RENDERER" not in text, source
        assert 'attrs={"class"' not in text, source
        assert 'attrs["class"]' not in text, source


# --------------------------------------------------------------------------
# C. One named set instead of copy-paste
# --------------------------------------------------------------------------

COMPONENTS = (
    ".btn-primary",
    ".btn-secondary",
    ".link",
    ".page-heading",
    ".panel",
    ".form-fields",
)

#: The strings the issue counted: 8, 4 and 5 copies across the templates.
COPY_PASTED = (
    "text-brand-700 underline hover:text-brand-600",
    "rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white",
    "text-2xl font-semibold tracking-tight",
)


@pytest.mark.parametrize("component", COMPONENTS)
def test_the_stylesheet_names_the_recurring_pieces(component: str) -> None:
    assert re.search(rf"^\s*{re.escape(component)}[\s,{{]", STYLESHEET, re.MULTILINE), component


def test_no_long_class_string_is_repeated_between_templates() -> None:
    """Four utilities in two templates is a component that was never named."""
    seen: dict[str, set[str]] = {}
    for name, source in TEMPLATE_SOURCES.items():
        for value in {" ".join(v.split()) for v in re.findall(r'class="([^"]*)"', source)}:
            if len(value.split()) > 3:
                seen.setdefault(value, set()).add(name)

    repeated = {value: sorted(names) for value, names in seen.items() if len(names) > 1}
    assert repeated == {}


@pytest.mark.parametrize("string", COPY_PASTED)
def test_the_copy_pasted_class_strings_are_gone(string: str) -> None:
    offenders = [name for name, source in TEMPLATE_SOURCES.items() if string in source]

    assert offenders == []


@pytest.mark.django_db
def test_the_log_out_and_replace_join_link_buttons_are_the_same_button(as_owner, project) -> None:
    body = as_owner.get(project.get_absolute_url()).content.decode()

    buttons = re.findall(r"<button\b[^>]*>\s*(Log out|Replace the join link)", body)
    assert sorted(buttons) == ["Log out", "Replace the join link"]

    classes = {
        _attrs(tag).get("class")
        for tag in re.findall(r"<button\b[^>]*>(?=\s*(?:Log out|Replace the join link))", body)
    }
    assert classes == {"btn-secondary"}


def test_no_template_uses_an_arbitrary_value_colour() -> None:
    """A new colour is a line in `@theme`, not a hex code in a class attribute."""
    arbitrary = re.compile(r"(?:bg|text|border|fill|stroke|ring|outline|accent)-\[[^\]]+\]")
    offenders = {
        name: arbitrary.findall(source)
        for name, source in TEMPLATE_SOURCES.items()
        if arbitrary.search(source)
    }

    assert offenders == {}


def test_every_colour_the_theme_declares_is_actually_used() -> None:
    """The block describes what exists: `--color-brand-50` was declared and unused."""
    declared = re.findall(r"--color-(brand-\d+):", STYLESHEET)
    assert declared, "no brand colours declared"

    used = STYLESHEET.split("}", 1)[-1] + "".join(TEMPLATE_SOURCES.values())
    for colour in declared:
        assert colour in used, f"--color-{colour} is declared and used nowhere"


# --------------------------------------------------------------------------
# D. The rule new screens follow
# --------------------------------------------------------------------------


def test_agents_md_states_the_styling_rule() -> None:
    agents = (BASE_DIR / "AGENTS.md").read_text()
    styling = agents.split("Styling", 1)

    assert len(styling) == 2, "AGENTS.md has no Styling entry"
    rule = styling[1].split("\n- ")[0]

    assert "base_app.html" in rule
    assert "assets/css/app.css" in rule
    assert "as_p" in rule
    assert "@theme" in rule
