"""The React island: the Vite build, the manifest tag, and what crosses into it.

Every test here maps to an acceptance criterion of issue #13.

Three themes run through the file.

The first is the leak. The island mounts on the real retrospective page, so
anything the bootstrap carries is in the page source of a page every member of
the project can open. The criterion is that it carries the retrospective's id,
its stage, its version and the viewer's own cards and nothing else, so the tests
assert another member's card text is *absent* — from the bootstrap and from the
whole document — at every one of the six stages, and not merely that the
viewer's own cards are present. #10 and #11 exist to keep that text off this
page; a placeholder is not allowed to undo them first.

The second is that the island is offline in this task. It has no endpoint to
poll: #14 wires it to #11's state endpoint and owns the poll loop. So the tests
assert on absence again — no `fetch`, no `XMLHttpRequest`, no timer in the
island's source, and no script on the page beyond the bundle and the
`json_script` block.

The third is that the build must fail loudly. A missing manifest renders no
`<script>` at all: it raises, naming the command to run. That is asserted by
driving the page with the manifest pointed at a file that is not there.

The suite itself runs without Node — `config/settings_test.py` points
`VITE_MANIFEST` at a checked-in fixture, the same way the suite does not need
`npm run build:css` to have been run. The tests that need the real build point
the setting back at `static/board/` and skip when it has not been built.
"""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.template import Context, Template
from django.test import Client, override_settings
from django.urls import reverse

from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.models import Retrospective
from retro.templatetags import vite
from retro.templatetags.vite import ManifestError
from retro.views import board_bootstrap

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

ENTRY = "assets/js/board.jsx"
ISLAND_SOURCE = (BASE_DIR / ENTRY).read_text()
VITE_CONFIG = (BASE_DIR / "vite.config.js").read_text()
RETRO_TEMPLATE = (BASE_DIR / "templates" / "retro" / "retro_detail.html").read_text()

#: Where the real build writes, as opposed to the fixture the suite runs on.
BUILD_DIR = BASE_DIR / "static" / settings.VITE_BUILD_SUBDIR
BUILT_MANIFEST = BUILD_DIR / "manifest.json"

#: The two ids the bundle reads, taken from the bundle's own source rather than
#: written down again here — that is what makes this a contract test and not two
#: files agreeing by luck.
MOUNT_ID = re.search(r'const MOUNT_ID = "([^"]+)"', ISLAND_SOURCE).group(1)
BOOTSTRAP_ID = re.search(r'const BOOTSTRAP_ID = "([^"]+)"', ISLAND_SOURCE).group(1)

BOOTSTRAP_BLOCK = re.compile(
    rf'<script id="{BOOTSTRAP_ID}" type="application/json">(.*?)</script>', re.DOTALL
)
MOUNT_ELEMENT = re.compile(rf'<div id="{MOUNT_ID}"\s*>\s*</div>')
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL)
BUNDLE_SCRIPT = re.compile(r'<script type="module" src="([^"]+)"></script>')


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forget_cached_manifests():
    """The tag caches by path when DEBUG is off; no test inherits another's."""
    vite._manifests.clear()
    yield
    vite._manifests.clear()


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


@pytest.fixture
def viewer(db):
    return make_user("alexey", "Alexey G")


@pytest.fixture
def other(db):
    return make_user("mira", "Mira M")


@pytest.fixture
def project(viewer, other):
    project = Project.objects.create(name="Platform", owner=viewer)
    Membership.objects.create(project=project, user=viewer, role=Membership.Role.FACILITATOR)
    Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)
    return project


@pytest.fixture
def cycle(project, viewer):
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=viewer,
    )


@pytest.fixture
def retro(cycle):
    return Retrospective.objects.create(cycle=cycle)


@pytest.fixture
def as_viewer(client: Client, viewer) -> Client:
    client.login(username="alexey", password=PASSWORD)
    return client


def write_card(cycle: FeedbackCycle, author: User, text: str, category: str = "START") -> Card:
    return Card.objects.create(cycle=cycle, author=author, text=text, category=category)


def detail_url(retro: Retrospective) -> str:
    return reverse("retro-detail", args=[retro.pk])


def bootstrap_of(body: str) -> dict:
    """The parsed contents of the `json_script` block on a rendered page."""
    block = BOOTSTRAP_BLOCK.search(body)
    assert block is not None, "no bootstrap block on the page"
    return json.loads(block.group(1))


def write_manifest(path: Path, file: str) -> Path:
    path.write_text(json.dumps({ENTRY: {"file": file, "src": ENTRY, "isEntry": True}}))
    return path


def render_tag() -> str:
    return Template('{% load vite %}{% vite_bundle "' + ENTRY + '" %}').render(Context())


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------


def test_the_island_has_one_entry_point_and_the_build_writes_into_static() -> None:
    assert (BASE_DIR / ENTRY).is_file()
    assert f'input: "{ENTRY}"' in VITE_CONFIG
    assert 'outDir: "static/board"' in VITE_CONFIG
    assert 'manifest: "manifest.json"' in VITE_CONFIG
    # The manifest goes beside the bundle, inside a directory collectstatic
    # walks — not into Vite's default hidden .vite/, which collectstatic skips.
    assert BUILT_MANIFEST.parent == BUILD_DIR
    assert BASE_DIR / "static" in settings.STATICFILES_DIRS


def test_one_command_builds_the_island_and_one_watches_it() -> None:
    scripts = json.loads((BASE_DIR / "package.json").read_text())["scripts"]
    agents = (BASE_DIR / "AGENTS.md").read_text()
    readme = (BASE_DIR / "README.md").read_text()

    assert "--watch" not in scripts["build:js"]
    assert "--watch" in scripts["watch:js"]
    for document in (agents, readme):
        assert "npm run build:js" in document
        assert "npm run watch:js" in document


def test_the_built_bundle_is_git_ignored() -> None:
    ignored = (BASE_DIR / ".gitignore").read_text().splitlines()

    assert "/static/board/" in ignored


def test_collectstatic_picks_up_the_bundle_and_the_manifest(tmp_path: Path) -> None:
    if not BUILT_MANIFEST.is_file():
        pytest.skip("island not built yet — run `npm run build:js`")

    with override_settings(STATIC_ROOT=tmp_path / "staticfiles"):
        call_command("collectstatic", "--noinput", verbosity=0)

    collected = tmp_path / "staticfiles" / settings.VITE_BUILD_SUBDIR
    built = json.loads(BUILT_MANIFEST.read_text())[ENTRY]["file"]

    assert (collected / "manifest.json").is_file()
    assert (collected / built).is_file(), built
    # The hash is what makes the filename safe to cache forever.
    assert re.search(r"-[A-Za-z0-9_-]{8,}\.js$", built), built


@pytest.mark.parametrize("name", ["Dockerfile", "compose.yaml"])
def test_the_application_runs_with_no_node_runtime_installed(name: str) -> None:
    """Node is a build-time tool: the image ships the build output, not the toolchain."""
    text = (BASE_DIR / name).read_text().lower()

    assert "node" not in text
    assert "npm" not in text
    assert "vite" not in text


def test_the_docker_build_context_excludes_the_toolchain_but_not_its_output() -> None:
    """The image gets the bundle; it never gets what built the bundle."""
    ignored = [line.strip() for line in (BASE_DIR / ".dockerignore").read_text().splitlines()]

    assert "node_modules/" in ignored
    assert not any(line.startswith(("static/", "/static", "static/board")) for line in ignored)


# --------------------------------------------------------------------------
# Resolving the bundle
# --------------------------------------------------------------------------


def test_the_tag_renders_the_hashed_filename_the_build_produced(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json", "assets/board-D3adB33f.js")

    with override_settings(VITE_MANIFEST=manifest):
        rendered = render_tag()

    assert rendered == (
        '<script type="module" src="/static/board/assets/board-D3adB33f.js"></script>'
    )


def test_no_template_names_a_built_file_directly() -> None:
    """The hash changes on every build, so a template that names one is stale by lunch."""
    for path in (BASE_DIR / "templates").rglob("*.html"):
        source = path.read_text()

        assert f"{settings.VITE_BUILD_SUBDIR}/assets/" not in source, path.name
        for reference in re.findall(r"[\"']([^\"']*\.js)[\"']", source):
            assert reference.startswith("vendor/"), (path.name, reference)

    assert f'{{% vite_bundle "{ENTRY}" %}}' in RETRO_TEMPLATE


def test_the_manifest_is_read_once_and_cached_when_debug_is_off(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json", "assets/board-first.js")

    with override_settings(VITE_MANIFEST=manifest, DEBUG=False):
        first = render_tag()
        write_manifest(manifest, "assets/board-second.js")
        second = render_tag()

    assert "board-first.js" in first
    assert second == first, "the manifest was re-read with DEBUG off"


def test_the_manifest_is_re_read_on_every_render_when_debug_is_on(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json", "assets/board-first.js")

    with override_settings(VITE_MANIFEST=manifest, DEBUG=True):
        first = render_tag()
        write_manifest(manifest, "assets/board-second.js")
        second = render_tag()

    assert "board-first.js" in first
    assert "board-second.js" in second, "a rebuild was not picked up with DEBUG on"


def test_a_missing_manifest_raises_and_names_the_build_command(tmp_path: Path) -> None:
    with override_settings(VITE_MANIFEST=tmp_path / "never-built.json"):
        with pytest.raises(ManifestError) as failure:
            render_tag()

    assert "npm run build:js" in str(failure.value)
    assert "never-built.json" in str(failure.value)


def test_an_unparseable_manifest_raises_and_names_the_build_command(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{ this is not json")

    with override_settings(VITE_MANIFEST=manifest):
        with pytest.raises(ManifestError) as failure:
            render_tag()

    assert "npm run build:js" in str(failure.value)


def test_a_manifest_without_the_entry_raises_rather_than_rendering_nothing(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets/js/somethingelse.jsx": {"file": "assets/other.js"}}))

    with override_settings(VITE_MANIFEST=manifest):
        with pytest.raises(ManifestError) as failure:
            render_tag()

    assert ENTRY in str(failure.value)
    assert "npm run build:js" in str(failure.value)


@pytest.mark.django_db
def test_the_page_fails_loudly_instead_of_serving_a_broken_script_tag(
    as_viewer, retro, tmp_path: Path
) -> None:
    """The failure a person must not be able to miss: no build, no page."""
    with override_settings(VITE_MANIFEST=tmp_path / "never-built.json"):
        with pytest.raises(ManifestError) as failure:
            as_viewer.get(detail_url(retro))

    assert "npm run build:js" in str(failure.value)


@pytest.mark.django_db
def test_the_page_renders_a_script_tag_matching_the_built_manifest_entry(as_viewer, retro) -> None:
    if not BUILT_MANIFEST.is_file():
        pytest.skip("island not built yet — run `npm run build:js`")

    built = json.loads(BUILT_MANIFEST.read_text())[ENTRY]["file"]

    with override_settings(VITE_MANIFEST=BUILT_MANIFEST):
        body = as_viewer.get(detail_url(retro)).content.decode()

    sources = BUNDLE_SCRIPT.findall(body)

    assert sources == [f"/static/{settings.VITE_BUILD_SUBDIR}/{built}"]
    assert (BUILD_DIR / built).is_file()


# --------------------------------------------------------------------------
# Mounting
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_page_carries_the_mount_element_and_the_bootstrap_block(as_viewer, retro) -> None:
    """Under the ids the bundle reads, which are read out of the bundle's source."""
    body = as_viewer.get(detail_url(retro)).content.decode()

    assert f'<div id="{MOUNT_ID}"' in body
    assert f'<script id="{BOOTSTRAP_ID}" type="application/json">' in body


@pytest.mark.django_db
def test_the_mount_element_is_empty_in_the_page_source(as_viewer, retro, viewer, cycle) -> None:
    """What a person compares against the browser to see the island is alive."""
    write_card(cycle, viewer, "Start pairing on the deploy script")

    body = as_viewer.get(detail_url(retro)).content.decode()

    assert MOUNT_ELEMENT.search(body), "the mount element is not empty in the page source"


@pytest.mark.django_db
def test_there_is_exactly_one_mount_point_in_the_application(as_viewer, retro) -> None:
    templates = [path.read_text() for path in (BASE_DIR / "templates").rglob("*.html")]

    assert sum(source.count(f'id="{MOUNT_ID}"') for source in templates) == 1
    assert ISLAND_SOURCE.count("createRoot(") == 1
    assert len(list((BASE_DIR / "assets" / "js").glob("*.jsx"))) == 1


@pytest.mark.django_db
def test_the_bootstrap_carries_the_id_the_stage_the_version_and_nothing_else(
    as_viewer, retro, cycle, viewer
) -> None:
    write_card(cycle, viewer, "Continue the Friday demo", category="CONTINUE")
    retro.version = 7
    retro.save(update_fields=["version"])

    payload = bootstrap_of(as_viewer.get(detail_url(retro)).content.decode())

    assert set(payload) == {"id", "stage", "version", "cards"}
    assert payload["id"] == retro.pk
    assert payload["stage"] == Retrospective.Stage.DRAFT
    assert payload["version"] == 7
    assert [card["text"] for card in payload["cards"]] == ["Continue the Friday demo"]
    assert set(payload["cards"][0]) == {"id", "category", "text"}


@pytest.mark.django_db
def test_the_bootstrap_carries_every_card_the_viewer_wrote(as_viewer, retro, cycle, viewer) -> None:
    for index in range(3):
        write_card(cycle, viewer, f"Mine number {index}")

    payload = bootstrap_of(as_viewer.get(detail_url(retro)).content.decode())

    assert [card["text"] for card in payload["cards"]] == [
        "Mine number 0",
        "Mine number 1",
        "Mine number 2",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("stage", [stage for stage, _label in Retrospective.Stage.choices])
def test_another_members_card_is_absent_from_the_bootstrap_at_every_stage(
    as_viewer, retro, cycle, viewer, other, stage: str
) -> None:
    """The leak this issue exists not to introduce.

    Written so it would fail if the bootstrap were built from the cycle's cards
    rather than from the viewer's: the other member's card is in the same cycle,
    it is checked at all six stages, and it is looked for in the whole document
    and not only in the payload.
    """
    secret = "Mira thinks the standup is theatre"
    write_card(cycle, other, secret, category="STOP")
    mine = write_card(cycle, viewer, "Start writing the runbook")
    retro.stage = stage
    retro.save(update_fields=["stage"])

    body = as_viewer.get(detail_url(retro)).content.decode()
    payload = bootstrap_of(body)

    assert secret not in body, f"another member's card text is in the page at {stage}"
    assert secret not in json.dumps(payload)
    assert [card["text"] for card in payload["cards"]] == [mine.text]
    assert payload["stage"] == stage


@pytest.mark.django_db
def test_the_bootstrap_names_no_other_member(as_viewer, retro, cycle, other) -> None:
    """Not the card text and not who wrote one: the payload has no author at all."""
    write_card(cycle, other, "Stop the Wednesday sync", category="STOP")

    payload = bootstrap_of(as_viewer.get(detail_url(retro)).content.decode())

    assert payload["cards"] == []
    assert "Mira" not in json.dumps(payload)
    assert "author" not in json.dumps(payload)


@pytest.mark.django_db
def test_the_bootstrap_is_not_reachable_by_someone_who_is_not_a_member(
    client: Client, retro, cycle, viewer
) -> None:
    write_card(cycle, viewer, "Start pairing on the deploy script")
    make_user("stranger", "A Stranger")
    client.login(username="stranger", password=PASSWORD)

    response = client.get(detail_url(retro))

    assert response.status_code == 404
    assert "Start pairing" not in response.content.decode()


@pytest.mark.django_db
def test_a_card_containing_a_closing_script_tag_does_not_break_the_page(
    as_viewer, retro, cycle, viewer
) -> None:
    """`json_script` escapes it; `|safe` or string interpolation would not."""
    hostile = "Stop </script><script>alert('x')</script> \"quoted\" & 'single'"
    write_card(cycle, viewer, hostile, category="STOP")

    body = as_viewer.get(detail_url(retro)).content.decode()
    block = BOOTSTRAP_BLOCK.search(body)

    assert block is not None, "the bootstrap block did not survive the card text"
    assert "</script>" not in block.group(1)
    assert "\\u003C" in block.group(1)
    assert json.loads(block.group(1))["cards"][0]["text"] == hostile
    # The injected tag is not markup anywhere on the page.
    assert "<script>alert(" not in body


def test_the_bootstrap_is_rendered_with_json_script_and_never_with_safe() -> None:
    assert f'|json_script:"{BOOTSTRAP_ID}"' in RETRO_TEMPLATE
    assert "|safe" not in RETRO_TEMPLATE


@pytest.mark.django_db
def test_the_view_builds_the_bootstrap_from_the_viewer_not_from_the_cycle(
    retro, cycle, viewer, other
) -> None:
    """The rule at the source, so it holds for any template that ever calls it."""
    write_card(cycle, other, "Theirs")
    write_card(cycle, viewer, "Mine")

    assert [card["text"] for card in board_bootstrap(viewer, retro)["cards"]] == ["Mine"]
    assert [card["text"] for card in board_bootstrap(other, retro)["cards"]] == ["Theirs"]


# --------------------------------------------------------------------------
# What the island does not do
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["fetch(", "XMLHttpRequest", "EventSource", "WebSocket", "setInterval", "setTimeout"],
)
def test_the_island_makes_no_request_of_its_own(forbidden: str) -> None:
    """#14 wires the board to #11's state endpoint. This one is offline."""
    assert forbidden not in ISLAND_SOURCE


def test_the_island_invents_no_endpoint() -> None:
    """No URL is guessed here for #11 or #12 to inherit or break."""
    assert "/retros/" not in ISLAND_SOURCE
    assert "/retrospectives/" not in ISLAND_SOURCE
    assert not re.search(r"https?://", ISLAND_SOURCE)


def test_the_island_renders_what_it_was_handed_and_toggles_it_from_its_own_state() -> None:
    """The two things a person checks on screen, held to the source that does them."""
    assert "useState" in ISLAND_SOURCE
    assert "stage" in ISLAND_SOURCE and "version" in ISLAND_SOURCE
    assert "Hide my cards" in ISLAND_SOURCE and "Show my cards" in ISLAND_SOURCE
    assert "onClick" in ISLAND_SOURCE


def test_the_island_styles_itself_with_the_named_components_only() -> None:
    """`assets/css/app.css` scans templates, so a raw utility in .jsx is never built."""
    named = {"section-heading", "list-rows", "btn-secondary", "panel", "link", "btn-primary"}

    for value in re.findall(r'className="([^"]+)"', ISLAND_SOURCE):
        for component in value.split():
            assert component in named, component


@pytest.mark.django_db
def test_the_page_carries_no_inline_script_body_and_no_inline_event_handler(
    as_viewer, retro, cycle, viewer
) -> None:
    """#27 adds a CSP; it must not have to make an exception for this page."""
    write_card(cycle, viewer, "Start pairing on the deploy script")

    body = as_viewer.get(detail_url(retro)).content.decode()

    for attributes, content in SCRIPT.findall(body):
        if 'type="application/json"' in attributes:
            assert f'id="{BOOTSTRAP_ID}"' in attributes
            continue
        assert "src=" in attributes, attributes
        assert content.strip() == "", "an inline script body on the page"

    assert not re.search(r"<[^>]+\son[a-z]+=", body), "an inline event handler on the page"


@pytest.mark.django_db
def test_the_retrospective_page_loads_nothing_from_a_third_party_origin(as_viewer, retro) -> None:
    body = as_viewer.get(detail_url(retro)).content.decode()

    assert "//cdn." not in body
    for source in re.findall(r'(?:src|href)="([^"]+)"', body):
        assert not source.startswith(("http://", "https://", "//")), source
