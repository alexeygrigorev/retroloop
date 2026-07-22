"""The front-end assets Node builds, and how the suite gets hold of them.

Two artefacts in this repository are produced by npm rather than by Python:

- `static/css/app.css`, from `npm run build:css`
- `static/board/manifest.json` and the hashed bundle beside it, from
  `npm run build:js`

Every test that reads one of them used to guard itself with `pytest.skip` when
the file was absent, so a checkout that had never run the npm commands reported
a green summary line with a couple of skips in it - indistinguishable, at a
glance, from a run that proved something (#54).

Nothing here skips because an artefact is missing. `ensure_built` runs the npm
script that produces it, once per session per artefact, and fails - naming the
command that rebuilds it - when the build does not put the file where it is
expected. That is what turns a changed `--output` path, or a build that errors
out, into a red run instead of a skipped one.

The one honest skip left is `npm` itself not being installed. It is expressed
once, in `ensure_built`, for every artefact rather than once per artefact.

The registry below is the general form the fix takes: a third built artefact is
one more `Artefact(...)` and one more fixture in `tests/conftest.py`, not another
copy of a guard.

None of this touches `config/settings_test.py`'s `VITE_MANIFEST`, which points at
a checked-in fixture so the rest of the suite renders pages on a machine that has
never installed Node. The tests that need the real build point that setting back
at `static/board/` themselves.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent

# Generous: the builds take well under a second, and a hung build should still
# end the run with a message rather than sit there.
BUILD_TIMEOUT = 300

# How much of a failed build's output to quote back. Enough to see the error,
# not so much that the real assertion scrolls away.
OUTPUT_LINES = 20


@dataclass(frozen=True)
class Artefact:
    """One file an npm script builds, and how to speak about it when it is absent."""

    label: str
    script: str
    path: Path

    @property
    def command(self) -> str:
        return f"npm run {self.script}"

    @property
    def relative(self) -> str:
        """How to name the file to someone standing in the checkout."""
        if self.path.is_relative_to(BASE_DIR):
            return self.path.relative_to(BASE_DIR).as_posix()
        return self.path.as_posix()


STYLESHEET = Artefact(
    label="stylesheet",
    script="build:css",
    path=BASE_DIR / "static" / "css" / "app.css",
)

ISLAND = Artefact(
    label="island bundle",
    script="build:js",
    path=BASE_DIR / "static" / "board" / "manifest.json",
)

ARTEFACTS = (STYLESHEET, ISLAND)

# The only honest skip in this area, written once for both artefacts.
NPM_MISSING = (
    "npm is not installed, so the built front-end assets cannot be produced on this "
    "machine. This is the only case in which a test that reads one may skip: install "
    "Node, run `npm ci`, and it will run."
)


def npm_executable() -> str | None:
    """The npm on PATH, or None when Node is not installed here."""
    return shutil.which("npm")


def run_build(artefact: Artefact, npm: str) -> subprocess.CompletedProcess[str]:
    """Run the npm script that produces `artefact`, without raising on failure."""
    return subprocess.run(
        [npm, "run", artefact.script],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
        check=False,
    )


def failure_message(artefact: Artefact, result: subprocess.CompletedProcess[str]) -> str:
    """What a developer needs to read: what is missing, and what rebuilds it."""
    if artefact.path.is_file():
        headline = (
            f"The {artefact.label} was not built: `{artefact.command}` exited "
            f"{result.returncode}, so what is at {artefact.relative} is whatever an "
            f"earlier build left there."
        )
    else:
        headline = (
            f"The {artefact.label} was not built: `{artefact.command}` exited "
            f"{result.returncode} and wrote no {artefact.relative}."
        )
    message = f"{headline}\nRun `{artefact.command}` (after `npm ci`) and read what it says."
    output = "\n".join((result.stdout + result.stderr).splitlines()[-OUTPUT_LINES:]).strip()
    return f"{message}\n--- {artefact.command} ---\n{output}" if output else message


def ensure_built(artefact: Artefact) -> Path:
    """Return the built artefact, building it first; fail loudly if that does not work.

    Called once per session per artefact, through the fixtures in
    `tests/conftest.py`, so a fresh checkout does not have to remember the npm
    commands before running the suite.
    """
    npm = npm_executable()
    if npm is None:
        if artefact.path.is_file():
            # Nothing to build with, but the artefact is already here - an image
            # that ships the build output, say. The test can still run.
            return artefact.path
        pytest.skip(NPM_MISSING)

    result = run_build(artefact, npm)
    if result.returncode != 0 or not artefact.path.is_file():
        pytest.fail(failure_message(artefact, result), pytrace=False)
    return artefact.path
