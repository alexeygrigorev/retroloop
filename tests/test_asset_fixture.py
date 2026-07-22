"""What the built-asset fixtures do when the build is missing, broken or absent.

Every test here maps to an acceptance criterion of issue #54, widened to cover
both artefacts Node builds: the Tailwind stylesheet and the Vite island.

The defect was that a test which reads a built file skipped when the file was
not there, so a checkout that had never run the npm commands reported
`passed, skipped, exit 0` - a summary line that cannot be told apart from a run
which proved something. The fix is `tests/assets.py`: one registry of artefacts,
one builder, one skip.

Nothing here runs npm. `ensure_built` is driven with its two seams - the npm
lookup and the build itself - replaced, so the three outcomes (built, broken,
no Node at all) can each be provoked on any machine, including one where the
real build happens to work.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests import assets
from tests.assets import ARTEFACTS, ISLAND, NPM_MISSING, STYLESHEET, Artefact, ensure_built

BASE_DIR = assets.BASE_DIR

SOURCES = (
    BASE_DIR / "tests" / "test_layout.py",
    BASE_DIR / "tests" / "test_island.py",
    BASE_DIR / "tests" / "conftest.py",
    BASE_DIR / "tests" / "assets.py",
)


def completed(returncode: int, output: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["npm", "run", "build:css"], returncode=returncode, stdout=output, stderr=""
    )


@pytest.fixture
def missing(tmp_path: Path) -> Artefact:
    """An artefact whose file is not there, and whose build writes nothing."""
    return Artefact(label="stylesheet", script="build:css", path=tmp_path / "app.css")


@pytest.fixture
def no_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assets, "npm_executable", lambda: None)


@pytest.fixture
def npm_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(
        assets, "run_build", lambda artefact, npm: completed(1, "sh: tailwindcss: not found")
    )


# --------------------------------------------------------------------------
# Both artefacts, one registry
# --------------------------------------------------------------------------


def test_both_built_artefacts_are_registered() -> None:
    """The fix is general: a third artefact is one more entry, not another guard."""
    assert ARTEFACTS == (STYLESHEET, ISLAND)
    assert STYLESHEET.path == BASE_DIR / "static" / "css" / "app.css"
    assert ISLAND.path == BASE_DIR / "static" / "board" / "manifest.json"
    assert STYLESHEET.command == "npm run build:css"
    assert ISLAND.command == "npm run build:js"


def test_a_session_fixture_covers_every_registered_artefact() -> None:
    conftest = (BASE_DIR / "tests" / "conftest.py").read_text()
    named = {id(value): name for name, value in vars(assets).items() if isinstance(value, Artefact)}

    for artefact in ARTEFACTS:
        assert f"ensure_built({named[id(artefact)]})" in conftest
        assert artefact.relative in conftest


# --------------------------------------------------------------------------
# A missing artefact fails, and says what to run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("artefact", ARTEFACTS, ids=lambda a: a.script)
def test_the_failure_message_names_the_artefact_and_the_command_that_rebuilds_it(
    artefact: Artefact,
) -> None:
    message = assets.failure_message(artefact, completed(1, "some npm noise"))

    assert f"The {artefact.label} was not built" in message
    assert artefact.command in message
    assert artefact.relative in message
    assert "some npm noise" in message


def test_a_build_that_fails_fails_the_suite_instead_of_skipping_it(
    missing: Artefact, npm_that_fails: None
) -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(missing)

    assert "The stylesheet was not built" in str(failure.value)
    assert "npm run build:css" in str(failure.value)
    assert "tailwindcss: not found" in str(failure.value)


def test_a_build_that_reports_success_and_writes_nothing_still_fails(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `--output` path in package.json changing is exactly this case."""
    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", lambda artefact, npm: completed(0))

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(missing)

    assert "npm run build:css" in str(failure.value)


def test_a_missing_artefact_never_skips_when_npm_is_installed(
    missing: Artefact, npm_that_fails: None
) -> None:
    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(missing)

    assert not isinstance(failure.value, pytest.skip.Exception)


# --------------------------------------------------------------------------
# Building, once per session
# --------------------------------------------------------------------------


def test_the_fixture_builds_the_artefact_rather_than_expecting_a_developer_to(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = []

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        runs.append((artefact, npm))
        artefact.path.write_text("/* built */")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    assert ensure_built(missing) == missing.path
    assert runs == [(missing, "/usr/bin/npm")]


def test_a_stale_artefact_is_rebuilt_rather_than_trusted(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence is not proof: an old file from a build path that no longer exists."""
    missing.path.write_text("/* stale */")
    runs = []

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        runs.append(npm)
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    ensure_built(missing)

    assert runs == ["/usr/bin/npm"]


def test_each_artefact_is_built_once_per_session() -> None:
    """Session scope is what makes `once per session` true of the fixtures."""
    conftest = (BASE_DIR / "tests" / "conftest.py").read_text()

    definitions = re.findall(r'@pytest\.fixture\(scope="session"\)\ndef (\w+)', conftest)

    assert sorted(definitions) == ["built_island", "built_stylesheet"]


def test_the_real_build_commands_are_the_ones_package_json_defines() -> None:
    scripts = (BASE_DIR / "package.json").read_text()

    for artefact in ARTEFACTS:
        assert f'"{artefact.script}":' in scripts


# --------------------------------------------------------------------------
# The one honest skip
# --------------------------------------------------------------------------


def test_the_only_skip_left_is_npm_not_being_installed(missing: Artefact, no_npm: None) -> None:
    with pytest.raises(pytest.skip.Exception) as skipped:
        ensure_built(missing)

    assert str(skipped.value) == NPM_MISSING
    assert "npm is not installed" in NPM_MISSING


def test_that_skip_is_written_once_for_every_artefact() -> None:
    """Not once per artefact: one call, in the one place that builds them."""
    calls = [(source.name, source.read_text().count("pytest.skip(")) for source in SOURCES]

    assert calls == [
        ("test_layout.py", 0),
        ("test_island.py", 0),
        ("conftest.py", 0),
        ("assets.py", 1),
    ]


def test_an_artefact_that_is_already_there_runs_even_without_npm(
    missing: Artefact, no_npm: None
) -> None:
    """A machine with the build output but no Node has nothing to skip for."""
    missing.path.write_text("/* shipped in the image */")

    assert ensure_built(missing) == missing.path


def test_the_tests_that_read_a_built_file_ask_for_its_fixture() -> None:
    layout = (BASE_DIR / "tests" / "test_layout.py").read_text()
    island = (BASE_DIR / "tests" / "test_island.py").read_text()

    assert re.search(
        r"def test_collectstatic_picks_up_the_built_stylesheet\([^)]*built_stylesheet", layout
    )
    assert len(re.findall(r"def test_\w+\([^)]*built_island", island)) == 2
