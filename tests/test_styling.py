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
