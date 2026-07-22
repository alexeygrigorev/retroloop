"""The base layout, the asset pipeline, and the htmx round trip.

Every test here maps to an acceptance criterion of issue #3.

The module doubles as a URLconf: `_flash` is a view that exists only to put
messages in the session and redirect, so the flash region can be checked on a
real next-page-load rather than by rendering a template with a fake context.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import path, reverse

import config.urls

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

HOME_URL = "/"
FRAGMENT_URL = "/frontend-check/"


def _flash(request: HttpRequest) -> HttpResponse:
    messages.success(request, "The cycle is open.")
    messages.error(request, "That cycle is already closed.")
    return redirect("home")


urlpatterns = [*config.urls.urlpatterns, path("flash/", _flash, name="flash")]

with_flash_urls = override_settings(ROOT_URLCONF="tests.test_layout")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="alexey", password=PASSWORD, display_name="Alexey G")


# --------------------------------------------------------------------------
# The layout
# --------------------------------------------------------------------------


def test_home_renders_inside_the_base_layout(client: Client) -> None:
    body = client.get(HOME_URL).content.decode()

    assert body.count("<!doctype html>") == 1
    assert "<nav" in body
    assert "<main" in body
    assert "<title>Weekly Team Feedback</title>" in body


def test_home_template_no_longer_carries_its_own_document_shell() -> None:
    source = (BASE_DIR / "templates" / "home.html").read_text()

    assert "<!doctype" not in source.lower()
    assert source.lstrip().startswith('{% extends "base_app.html" %}')


def test_base_defines_the_four_blocks() -> None:
    source = (BASE_DIR / "templates" / "base.html").read_text()

    for block in ("title", "content", "extra_head", "nav_actions"):
        assert re.search(rf"{{%\s*block\s+{block}\s*%}}", source), block


def test_nav_actions_is_empty_in_base_itself() -> None:
    """base.html is the seam; the account controls live in base_app.html."""
    source = (BASE_DIR / "templates" / "base.html").read_text()

    assert re.search(r"{%\s*block\s+nav_actions\s*%}\s*{%\s*endblock\s*%}", source)

    rendered = render_to_string("base.html")
    assert "Log out" not in rendered
    assert "Log in" not in rendered


def test_a_page_can_override_the_title(client: Client) -> None:
    assert b"<title>Log in</title>" in client.get("/accounts/login/").content


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/", "/accounts/login/", "/accounts/signup/"])
def test_every_page_shows_the_account_controls_to_a_visitor(client: Client, url: str) -> None:
    body = client.get(url).content.decode()

    assert "Log in" in body
    assert "Sign up" in body


@pytest.mark.parametrize("url", ["/", "/accounts/password_change/"])
def test_every_page_shows_the_account_controls_to_a_member(
    client: Client, member, url: str
) -> None:
    client.login(username="alexey", password=PASSWORD)

    body = client.get(url).content.decode()

    assert "Alexey G" in body
    assert reverse("password_change") in body
    assert f'action="{reverse("logout")}"' in body
    assert 'name="csrfmiddlewaretoken"' in body


@with_flash_urls
def test_messages_appear_in_the_flash_region_on_the_next_page_load(client: Client) -> None:
    response = client.get("/flash/", follow=True)
    body = response.content.decode()

    assert response.redirect_chain[-1] == (HOME_URL, 302)
    assert "The cycle is open." in body
    assert "That cycle is already closed." in body


@with_flash_urls
def test_success_and_error_messages_are_styled_differently(client: Client) -> None:
    body = client.get("/flash/", follow=True).content.decode()

    success = re.search(r'<li\b[^>]*data-message-level="success"[^>]*>', body)
    error = re.search(r'<li\b[^>]*data-message-level="error"[^>]*>', body)

    assert success and error
    assert success.group() != error.group()
    assert "emerald" in success.group()
    assert "red" in error.group()


def test_the_navigation_wraps_instead_of_overflowing_a_narrow_viewport(client: Client) -> None:
    """375px is the check; `flex-wrap` with no fixed width is what makes it pass."""
    body = client.get(HOME_URL).content.decode()
    nav = re.search(r"<nav\b[^>]*>", body)

    assert nav is not None
    assert "flex-wrap" in nav.group()
    assert 'name="viewport"' in body
    assert "width=device-width" in body


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------


def test_no_tailwind_config_file_exists_anywhere() -> None:
    offenders = [
        str(path.relative_to(BASE_DIR))
        for path in BASE_DIR.rglob("tailwind.config.*")
        if "node_modules" not in path.parts and ".venv" not in path.parts
    ]

    assert offenders == []


def test_tailwind_is_configured_css_first() -> None:
    source = (BASE_DIR / "assets" / "css" / "app.css").read_text()

    assert '@import "tailwindcss"' in source
    assert "@source" in source


def test_package_json_pins_the_tailwind_cli_and_nothing_else() -> None:
    package = json.loads((BASE_DIR / "package.json").read_text())

    assert set(package["devDependencies"]) == {"@tailwindcss/cli", "tailwindcss"}
    assert "dependencies" not in package
    for version in package["devDependencies"].values():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    assert package["devDependencies"]["tailwindcss"].startswith("4.3.")


def test_one_command_builds_the_css_and_one_watches_it() -> None:
    scripts = json.loads((BASE_DIR / "package.json").read_text())["scripts"]
    agents = (BASE_DIR / "AGENTS.md").read_text()

    assert "--watch" not in scripts["build:css"]
    assert "--watch" in scripts["watch:css"]
    assert "npm run build:css" in agents
    assert "npm run watch:css" in agents


def test_generated_css_is_git_ignored() -> None:
    ignored = (BASE_DIR / ".gitignore").read_text().splitlines()

    assert "/static/css/app.css" in ignored
    assert "/node_modules/" in ignored


def test_readme_says_assets_are_built_on_the_host() -> None:
    readme = (BASE_DIR / "README.md").read_text().lower()

    assert "built on the host" in readme
    assert "npm run build:css" in readme


def test_htmx_and_alpine_are_vendored_at_pinned_versions() -> None:
    vendor = BASE_DIR / "static" / "vendor"
    htmx = vendor / "htmx-2.0.10.min.js"
    alpine = vendor / "alpine-3.15.12.min.js"

    assert htmx.is_file() and htmx.stat().st_size > 10_000
    assert alpine.is_file() and alpine.stat().st_size > 10_000

    base = (BASE_DIR / "templates" / "base.html").read_text()
    assert "vendor/htmx-2.0.10.min.js" in base
    assert "vendor/alpine-3.15.12.min.js" in base


def test_no_page_requests_a_third_party_origin(client: Client) -> None:
    for url in ("/", "/accounts/login/", "/accounts/signup/"):
        body = client.get(url).content.decode()

        assert "//cdn." not in body
        for source in re.findall(r'(?:src|href)="([^"]+)"', body):
            assert not source.startswith(("http://", "https://", "//")), source


def test_every_template_loads_its_assets_from_static() -> None:
    for template in (BASE_DIR / "templates").rglob("*.html"):
        source = template.read_text()

        assert "cdn.jsdelivr" not in source
        assert "unpkg.com" not in source
        for asset in re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="([^"]+)"', source):
            assert asset.startswith("{% static "), (template.name, asset)


def test_collectstatic_picks_up_the_built_stylesheet(tmp_path: Path) -> None:
    if not (BASE_DIR / "static" / "css" / "app.css").is_file():
        pytest.skip("stylesheet not built yet — run `npm run build:css`")

    with override_settings(STATIC_ROOT=tmp_path / "staticfiles"):
        call_command("collectstatic", "--noinput", verbosity=0)

    collected = tmp_path / "staticfiles"
    assert (collected / "css" / "app.css").is_file()
    assert (collected / "vendor" / "htmx-2.0.10.min.js").is_file()
    assert (collected / "vendor" / "alpine-3.15.12.min.js").is_file()


# --------------------------------------------------------------------------
# htmx
# --------------------------------------------------------------------------


def test_home_carries_a_control_that_triggers_an_htmx_request(client: Client) -> None:
    body = client.get(HOME_URL).content.decode()

    assert f'hx-post="{FRAGMENT_URL}"' in body
    assert 'hx-target="#frontend-check-panel"' in body
    assert 'id="frontend-check-panel"' in body


def test_the_fragment_endpoint_returns_no_layout(client: Client) -> None:
    response = client.post(FRAGMENT_URL)
    body = response.content.decode()

    assert response.status_code == 200
    assert "<html" not in body
    assert "<!doctype" not in body.lower()
    assert "<nav" not in body
    assert "no page reload" in body


def test_the_fragment_comes_from_a_template_partial() -> None:
    """No separate file per fragment: the partial lives in the page it serves."""
    home = (BASE_DIR / "templates" / "home.html").read_text()

    assert re.search(r"{%\s*partialdef\s+frontend_check\s*%}", home)
    assert "home.html#frontend_check" in (BASE_DIR / "config" / "views.py").read_text()


def test_the_fragment_is_alpine_markup_so_it_initialises_after_a_swap(client: Client) -> None:
    body = client.post(FRAGMENT_URL).content.decode()

    assert "x-data=" in body
    assert "x-text=" in body
    assert "@click=" in body


def test_htmx_requests_carry_the_csrf_token(client: Client) -> None:
    body = client.get(HOME_URL).content.decode()

    match = re.search(r"hx-headers='([^']+)'", body)
    assert match is not None

    headers = json.loads(match.group(1))
    assert headers["X-CSRFToken"]
    assert headers["X-CSRFToken"] != "NOTPROVIDED"


def test_an_htmx_post_without_a_csrf_token_is_rejected() -> None:
    client = Client(enforce_csrf_checks=True)
    client.get(HOME_URL)

    response = client.post(FRAGMENT_URL, headers={"hx-request": "true"})

    assert response.status_code == 403


def test_an_htmx_post_with_the_csrf_token_is_accepted() -> None:
    client = Client(enforce_csrf_checks=True)
    client.get(HOME_URL)
    token = client.cookies["csrftoken"].value

    response = client.post(
        FRAGMENT_URL,
        headers={"hx-request": "true", "x-csrftoken": token},
    )

    assert response.status_code == 200
    assert "<html" not in response.content.decode()


def test_the_fragment_endpoint_refuses_a_get(client: Client) -> None:
    assert client.get(FRAGMENT_URL).status_code == 405
