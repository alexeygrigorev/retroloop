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

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
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


EARLIER_BUILD = "/* written by an earlier build */"

# A timestamp from well before this test run. A file left by yesterday's build is
# the state these tests are about, and stamping it explicitly keeps them off the
# filesystem clock: nothing here depends on how finely mtimes are recorded, or on
# how quickly the fake build runs.
EARLIER = 1_700_000_000_000_000_000


def from_an_earlier_build(path: Path, content: str = EARLIER_BUILD) -> None:
    """Leave `path` looking exactly like output of a build that ran long ago."""
    path.write_text(content)
    os.utime(path, ns=(EARLIER, EARLIER))


def record(order: list[str], name: str, original: Callable[..., object]) -> Callable[..., object]:
    """Wrap `original` so the order it is called in can be asserted on."""

    def wrapper(*args: object, **kwargs: object) -> object:
        order.append(name)
        return original(*args, **kwargs)

    return wrapper


@pytest.fixture
def kept_directories(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every temp directory `set_aside` makes during a test, so it can be looked for."""
    made: list[Path] = []
    set_aside = assets.set_aside

    def spy(path: Path) -> assets.SetAside | None:
        kept = set_aside(path)
        if kept is not None:
            made.append(kept.directory)
        return kept

    monkeypatch.setattr(assets, "set_aside", spy)
    return made


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


def test_the_build_runs_even_when_the_artefact_is_already_present(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence is not a reason to skip the build. Whether it is *proof* is below."""
    from_an_earlier_build(missing.path)
    runs = []

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        runs.append(npm)
        artefact.path.write_text("/* built now */")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    ensure_built(missing)

    assert runs == ["/usr/bin/npm"]


# --------------------------------------------------------------------------
# A stale artefact is not evidence of a build
# --------------------------------------------------------------------------


def test_a_build_that_writes_somewhere_else_leaves_the_stale_artefact_red(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA's case: `--output` changed, the build exits 0, the orphan stays behind.

    The harm #54's "Why it matters later" names. Nothing about the run is
    unusual - the build succeeds, the file is there - except that the file is
    not what this build wrote.
    """
    from_an_earlier_build(missing.path)
    renamed = missing.path.with_name("app.RENAMED.css")

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        renamed.write_text("/* the output path moved; the build went here */")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(missing)

    assert "npm run build:css" in str(failure.value)
    assert "earlier build" in str(failure.value)
    assert missing.path.read_text() == EARLIER_BUILD, "the stale file was read, not rewritten"


def test_a_build_that_exits_zero_and_touches_nothing_leaves_the_artefact_red(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_an_earlier_build(missing.path)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", lambda artefact, npm: completed(0))

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(missing)

    assert "npm run build:css" in str(failure.value)


def test_a_rebuild_that_writes_byte_identical_output_is_not_red(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check may not lean on the bytes changing: a build is meant to be reproducible.

    Twice in a row from unchanged sources - which is what every second CI run
    and every second local run is - the build writes exactly what is already
    there, and that has to stay green.
    """
    from_an_earlier_build(missing.path)

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        artefact.path.write_text(EARLIER_BUILD)
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    assert ensure_built(missing) == missing.path
    assert missing.path.read_text() == EARLIER_BUILD


# --------------------------------------------------------------------------
# The window where the artefact is out of the checkout
# --------------------------------------------------------------------------


def test_an_interrupted_build_puts_the_artefact_back(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch, kept_directories: list[Path]
) -> None:
    """Ctrl-C during a build must not cost a developer their build output.

    `KeyboardInterrupt` is not an `Exception`, so nothing short of a `finally`
    catches this one.
    """
    from_an_earlier_build(missing.path)

    def interrupted(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        assert not artefact.path.exists(), "the build should run over an empty path"
        raise KeyboardInterrupt

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", interrupted)

    with pytest.raises(KeyboardInterrupt):
        ensure_built(missing)

    assert missing.path.read_text() == EARLIER_BUILD
    assert kept_directories and not kept_directories[0].exists()


def test_a_build_that_runs_past_its_timeout_puts_the_artefact_back(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_an_earlier_build(missing.path)

    def timed_out(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="npm run build:css", timeout=assets.BUILD_TIMEOUT)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", timed_out)

    with pytest.raises(subprocess.TimeoutExpired):
        ensure_built(missing)

    assert missing.path.read_text() == EARLIER_BUILD


def test_the_artefact_is_back_before_the_temp_directory_goes(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch, kept_directories: list[Path]
) -> None:
    """Cleanup after restore, never instead of it: the order is what makes it safe."""
    from_an_earlier_build(missing.path)
    order = []

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", lambda artefact, npm: completed(0))
    monkeypatch.setattr(assets, "restore", record(order, "restore", assets.restore))
    monkeypatch.setattr(assets, "discard", record(order, "discard", assets.discard))

    with pytest.raises(pytest.fail.Exception):
        ensure_built(missing)

    assert order == ["restore", "discard"]
    assert missing.path.read_text() == EARLIER_BUILD
    assert not kept_directories[0].exists()


# --------------------------------------------------------------------------
# The temp directory does not outlive the build
# --------------------------------------------------------------------------


def test_a_finished_build_leaves_no_temp_directory_behind(
    missing: Artefact, monkeypatch: pytest.MonkeyPatch, kept_directories: list[Path]
) -> None:
    from_an_earlier_build(missing.path)

    def build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        artefact.path.write_text("/* built now */")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    ensure_built(missing)

    assert kept_directories and not kept_directories[0].exists()
    assert missing.path.read_text() == "/* built now */"


def test_cleanup_removes_only_a_directory_this_module_made(tmp_path: Path) -> None:
    """Scoped by what `set_aside` recorded, and by where and how it makes it."""
    someone_elses = tmp_path / "not-ours"
    someone_elses.mkdir()
    (someone_elses / "precious.txt").write_text("not the fixture's to remove")

    assets.discard(assets.SetAside(directory=someone_elses, path=someone_elses / "app.css"))

    assert (someone_elses / "precious.txt").is_file()


def test_cleanup_of_nothing_set_aside_does_nothing() -> None:
    assert assets.discard(None) is None


def test_what_set_aside_makes_is_what_cleanup_accepts(tmp_path: Path) -> None:
    """The two ends of the scoping rule, checked against each other."""
    artefact = tmp_path / "app.css"
    artefact.write_text("/* built */")

    kept = assets.set_aside(artefact)

    assert kept is not None
    assert not artefact.exists(), "the build has to run over an empty path"
    assert kept.directory.name.startswith(assets.KEPT_PREFIX)
    assert kept.directory.parent == Path(tempfile.gettempdir())

    assets.restore(kept, artefact)
    assets.discard(kept)

    assert artefact.read_text() == "/* built */"
    assert not kept.directory.exists()


# --------------------------------------------------------------------------
# A stub is not a build either
# --------------------------------------------------------------------------


def test_a_stub_written_where_the_stylesheet_belongs_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh, and still not a stylesheet. The floor is CI's own: 1 KB."""
    artefact = replace(STYLESHEET, path=tmp_path / "app.css")

    def build(built: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        built.path.write_text("/* stub */")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(artefact)

    assert "npm run build:css" in str(failure.value)
    assert "bytes" in str(failure.value)


def test_a_stub_manifest_that_names_no_bundle_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artefact = replace(ISLAND, path=tmp_path / "manifest.json")

    def build(built: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        built.path.write_text("{}")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(artefact)

    assert "npm run build:js" in str(failure.value)


def test_a_manifest_naming_a_bundle_that_is_not_there_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is a promise about a file; an unkept one is a broken build."""
    artefact = replace(ISLAND, path=tmp_path / "manifest.json")

    def build(built: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        built.path.write_text(
            '{"assets/js/board.jsx": {"file": "assets/board-Deadbeef.js", "isEntry": true}}'
        )
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(artefact)

    assert "board-Deadbeef.js" in str(failure.value)
    assert "npm run build:js" in str(failure.value)


def test_a_manifest_nothing_can_read_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artefact = replace(ISLAND, path=tmp_path / "manifest.json")

    def build(built: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
        built.path.write_text("not json at all")
        return completed(0)

    monkeypatch.setattr(assets, "npm_executable", lambda: "/usr/bin/npm")
    monkeypatch.setattr(assets, "run_build", build)

    with pytest.raises(pytest.fail.Exception) as failure:
        ensure_built(artefact)

    assert "npm run build:js" in str(failure.value)


def test_what_the_real_builds_produce_satisfies_those_checks(
    built_stylesheet: Path, built_island: Path
) -> None:
    """The checks above are worth nothing if the real output does not pass them."""
    assert STYLESHEET.verify(built_stylesheet) is None
    assert ISLAND.verify(built_island) is None


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
