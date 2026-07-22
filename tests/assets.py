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
command that rebuilds it - unless the build exited 0, wrote *that path in this
run*, and wrote something that looks like build output rather than a stub.

The middle condition is the one a file's mere existence cannot answer. A build
whose output path has moved succeeds while writing somewhere else and leaves the
previous file behind, and an orphan on disk is the normal state of a developer's
checkout rather than an edge case. So the artefact is moved out of the way and
the build has to put it back: an empty path going in is what makes the file
coming out this run's work. `ensure_built` says why the two tidier-looking
checks - comparing contents, comparing timestamps - were measured against the
real toolchain and thrown away.

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

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
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

# The floor .github/workflows/ci.yml applies to both the stylesheet and the
# bundle, written down here so the suite and the workflow agree about what
# "produced nothing" means. A build that wrote a byte or two wrote a stub.
MIN_ASSET_BYTES = 1024


def nothing_to_complain_about(path: Path) -> str | None:
    """The default check: the build wrote it, and that is all that is asked."""
    return None


@dataclass(frozen=True)
class Artefact:
    """One file an npm script builds, and how to speak about it when it is absent."""

    label: str
    script: str
    path: Path
    # What the file has to look like to count as build output rather than a
    # stub. Returns a complaint, or None when there is nothing to say.
    verify: Callable[[Path], str | None] = nothing_to_complain_about

    @property
    def command(self) -> str:
        return f"npm run {self.script}"

    @property
    def relative(self) -> str:
        """How to name the file to someone standing in the checkout."""
        if self.path.is_relative_to(BASE_DIR):
            return self.path.relative_to(BASE_DIR).as_posix()
        return self.path.as_posix()


def a_compiled_stylesheet(path: Path) -> str | None:
    size = path.stat().st_size
    if size <= MIN_ASSET_BYTES:
        return f"it is {size} bytes, and a compiled stylesheet is not that small"
    return None


def a_manifest_naming_a_bundle(manifest: Path) -> str | None:
    """The same three questions CI asks of the manifest, asked of the same file.

    A manifest is a promise about a file the page will ask for: an unkept one is
    a build that produced something no browser can use, which is worth exactly as
    much as no build at all.
    """
    try:
        entries = json.loads(manifest.read_text())
    except json.JSONDecodeError as error:
        return f"it is not valid JSON ({error}), so nothing can read it"

    bundles = [
        entry["file"]
        for entry in entries.values()
        if isinstance(entry, dict) and entry.get("isEntry") and "file" in entry
    ]
    if not bundles:
        return "it names no entry bundle, so the page has no script to load"

    for name in bundles:
        bundle = manifest.parent / name
        if not bundle.is_file():
            return f"it names {name}, which is not there"
        size = bundle.stat().st_size
        if size <= MIN_ASSET_BYTES:
            return f"the bundle it names, {name}, is {size} bytes"
    return None


STYLESHEET = Artefact(
    label="stylesheet",
    script="build:css",
    path=BASE_DIR / "static" / "css" / "app.css",
    verify=a_compiled_stylesheet,
)

ISLAND = Artefact(
    label="island bundle",
    script="build:js",
    path=BASE_DIR / "static" / "board" / "manifest.json",
    verify=a_manifest_naming_a_bundle,
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


KEPT_PREFIX = "built-asset-"


@dataclass(frozen=True)
class SetAside:
    """A file moved out of the checkout for the duration of one build."""

    directory: Path
    path: Path


def set_aside(path: Path) -> SetAside | None:
    """Move `path` out of the way so the build has to write it, or None if absent.

    Moved rather than copied, and moved out of the tree rather than next to the
    file: `vite build` empties `static/board/` before it writes, so anything kept
    in there would be gone by the time it was needed. What is moved is either put
    back by `restore` or replaced by the build that was asked for, and `discard`
    takes the temp directory away afterwards - the caller does all three in a
    `try`/`finally`, so an interrupted run gets its file back too.
    """
    if not path.exists():
        return None
    directory = Path(tempfile.mkdtemp(prefix=KEPT_PREFIX))
    shutil.move(path, directory / path.name)
    return SetAside(directory=directory, path=directory / path.name)


def restore(kept: SetAside | None, path: Path) -> None:
    """Put back what `set_aside` moved, when the build did not replace it."""
    if kept is not None and kept.path.exists() and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(kept.path, path)


def discard(kept: SetAside | None) -> None:
    """Remove the temp directory `set_aside` made, and nothing else.

    Always after `restore`, never instead of it: by the time this runs, whatever
    was moved is either back in the checkout or superseded by the build's own
    output, so what is left here is a spare copy of a file one npm command
    reproduces.

    Two things have to be true of a directory before it is removed, and both are
    about this module having made it: it is the path `set_aside` recorded rather
    than one worked out again afterwards, and it carries the prefix and lives in
    the temp directory `set_aside` uses. Failing to clean up is not worth failing
    a test over, so errors are swallowed - the directory is a few kilobytes and
    the operating system clears it eventually.
    """
    if kept is None:
        return
    directory = kept.directory
    ours = directory.name.startswith(KEPT_PREFIX) and directory.parent == Path(
        tempfile.gettempdir()
    )
    if ours:
        shutil.rmtree(directory, ignore_errors=True)


def failure_message(
    artefact: Artefact, result: subprocess.CompletedProcess[str], because: str | None = None
) -> str:
    """What a developer needs to read: what went wrong, and what rebuilds it."""
    reason = because or (
        f"`{artefact.command}` exited {result.returncode} and wrote no {artefact.relative}"
    )
    message = (
        f"The {artefact.label} was not built: {reason}.\n"
        f"Run `{artefact.command}` (after `npm ci`) and read what it says."
    )
    output = "\n".join((result.stdout + result.stderr).splitlines()[-OUTPUT_LINES:]).strip()
    return f"{message}\n--- {artefact.command} ---\n{output}" if output else message


def stale_reason(artefact: Artefact, result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"`{artefact.command}` exited {result.returncode} and wrote no "
        f"{artefact.relative}. There was a file there before this run, left by an "
        f"earlier build, and it has been put back where it was - but it is not "
        f"evidence of anything: this build did not write it. An output path that "
        f"moved - `--output` in package.json, `outDir` in vite.config.js - looks "
        f"exactly like this, the build succeeding while it writes somewhere else"
    )


def broken_reason(artefact: Artefact, result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"`{artefact.command}` wrote {artefact.relative} and then exited "
        f"{result.returncode}, so it did not finish and what it left behind is "
        f"whatever it had got to"
    )


def ensure_built(artefact: Artefact) -> Path:
    """Return the artefact this run built; fail loudly when it is anything else.

    Called once per session per artefact, through the fixtures in
    `tests/conftest.py`, so a fresh checkout does not have to remember the npm
    commands before running the suite.

    Three things have to be true, and a file being there is only the first of
    them. The build must have exited 0, it must have written *this* path in
    *this* run, and what it wrote must look like build output rather than a stub.

    The middle one is the interesting one. A build that succeeds while writing
    somewhere else - `--output` in package.json, `outDir` in vite.config.js,
    changed and not noticed - leaves the previous file sitting there, and a check
    that only asks "is it there?" reads that orphan as proof of a build that
    never touched it. That is the harm this issue was filed about.

    So the artefact is moved out of the way before the build runs, and the build
    has to put it back. Nothing that survives is inherited: an empty path going
    in means the file coming out was written by this run, whatever the tool did
    or did not do internally. If the build does not produce it, what was moved is
    put back - the checkout is left as it was found - and the run goes red.

    Two tidier-looking designs were tried against the real toolchain first, and
    neither one holds:

    - Comparing the file's contents across the build calls a reproducible build
      stale. Run twice over unchanged sources - every second local run - and a
      build writes exactly the bytes already there, so demanding different bytes
      would fail a perfectly good build.
    - Comparing mtime, inode and size across the build is worse, because it is
      wrong in the direction that looks right. Two consecutive `npm run build:css`
      runs here leave all three identical: the Tailwind CLI does not rewrite its
      output when what it computed matches what is on disk. Vite does rewrite.
      A timestamp check therefore fails a good stylesheet build while passing a
      good island build, and - the fatal part - "the tool wrote nothing because
      the file was already correct" and "the tool wrote nothing because its
      output path moved" are the same observation. They stop being the same
      observation only when the file is not there to be already correct.

    The cost is that the artefact is rebuilt from nothing once per session rather
    than possibly not at all, which for these two builds is about a third of a
    second.

    Between the move and the build there is a window in which the checkout has no
    artefact in it, so everything after `set_aside` runs under `try`/`finally`:
    Ctrl-C during a build, a build that runs past its timeout, or any other way
    out of here puts the file back. `KeyboardInterrupt` is not an `Exception`,
    which is the whole reason the cleanup is a `finally` rather than an `except`.
    """
    npm = npm_executable()
    if npm is None:
        if artefact.path.is_file():
            # Nothing to build with, but the artefact is already here - an image
            # that ships the build output, say. Its freshness cannot be
            # established without a build, so it is taken as given; that it is
            # not a stub still can be, and is.
            return usable(artefact, artefact.path, no_build())
        pytest.skip(NPM_MISSING)

    kept = set_aside(artefact.path)
    try:
        return built_by_this_run(artefact, npm, kept)
    finally:
        restore(kept, artefact.path)
        discard(kept)


def built_by_this_run(artefact: Artefact, npm: str, kept: SetAside | None) -> Path:
    """Run the build over an empty path and judge what came back. See `ensure_built`."""
    result = run_build(artefact, npm)

    if not artefact.path.is_file():
        reason = stale_reason(artefact, result) if kept else None
        pytest.fail(failure_message(artefact, result, reason), pytrace=False)
    if result.returncode != 0:
        pytest.fail(
            failure_message(artefact, result, broken_reason(artefact, result)), pytrace=False
        )
    return usable(artefact, artefact.path, result)


def no_build() -> subprocess.CompletedProcess[str]:
    """A stand-in result for the one path that reaches a check without building."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def usable(artefact: Artefact, path: Path, result: subprocess.CompletedProcess[str]) -> Path:
    """Fail when what is on disk is not the kind of thing the build should write."""
    complaint = artefact.verify(path)
    if complaint:
        reason = (
            f"{artefact.relative} is there, but {complaint}. A build that writes a "
            f"stub is a build that failed quietly"
        )
        pytest.fail(failure_message(artefact, result, reason), pytrace=False)
    return path
