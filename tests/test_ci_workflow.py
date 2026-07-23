"""The CI workflow, read as text and checked against what it has to guarantee.

Every test here maps to an acceptance criterion of issue #30.

The workflow is read as text rather than parsed as YAML, the way the suite
already reads `package.json`, `AGENTS.md` and `.gitignore`: parsing it would
need a YAML library, and issue #30 adds no dependency. Text is also the right
altitude for what is being asserted — that a command is present, that one
command runs before another, and that certain strings are *absent*.

The absences matter as much as the presences. A workflow that grew a
`paths-ignore`, a bare `uv sync`, a fourth marketplace action or a hand-written
dotenv file would still be a valid workflow and would still go green; it would
just no longer be checking what this project needs checked. This project has
already shipped defects past a suite where every test asked whether a required
string was present and none asked whether a forbidden one was gone.

Nothing here skips. A skipped test fails CI (see the gate this file asserts on),
and a file that guards the gate is the last place to opt out of it.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from functools import cache
from pathlib import Path

from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
WORKFLOW_DIR = BASE_DIR / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "ci.yml"

#: The three actions issue #30 allows, and nothing else from the marketplace.
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-node", "astral-sh/setup-uv"}

USES = re.compile(r"uses:\s*(\S+)")

#: The two gates that read the JUnit report, by step name.
COUNT_GATE = "Fail if the number of tests that ran is not the expected count"
SKIP_GATE = "Fail if any test was skipped"

#: The gate that collects the suite itself and pins a count per test file (#72).
#: Runs as a job step, not a collected test, so dropping the guard file cannot
#: disable it.
PERFILE_GATE = "Fail if any test file's collected count is not its committed count"


def source() -> str:
    """The workflow as written, comments and all."""
    assert WORKFLOW.is_file(), f"{WORKFLOW} does not exist"
    return WORKFLOW.read_text()


def commands() -> str:
    """The workflow with every comment line removed.

    Comments explain why a step is shaped the way it is, and they quote the
    things they warn against — "never a bare `uv sync`", "no dotenv file". An
    assertion about what the job *does* has to look at the lines that run.
    """
    return "\n".join(line for line in source().splitlines() if not line.lstrip().startswith("#"))


def index_of(needle: str) -> int:
    text = commands()
    position = text.find(needle)
    assert position != -1, f"{needle!r} does not appear in {WORKFLOW.name}"
    return position


# --------------------------------------------------------------------------
# Running a gate rather than reading it
#
# Issue #63's QA mutation-tested the assertions in this file and found three
# that survived the behaviour being broken, because they matched a string
# inside the step's own `::error::` text rather than the thing they claimed to
# check. The gates added for #67 are therefore not read, they are *run*: the
# step's script is lifted out of the workflow and executed against a JUnit
# report written for the occasion. Break the gate and these fail.
# --------------------------------------------------------------------------


def step(name: str) -> str:
    """One step of the job, as written, comments and all."""
    text = source()
    marker = f"- name: {name}\n"
    start = text.find(marker)
    assert start != -1, f"{WORKFLOW.name} has no step named {name!r}"

    rest = text[start + len(marker) :]
    following = re.search(r"\n {6}- (?:name|uses):", rest)
    return marker + (rest[: following.start()] if following else rest)


def gate_script(name: str) -> str:
    """The python program a named step feeds to its interpreter."""
    heredoc = re.search(r"<<'PY'\n(.*?)\n *PY *$", step(name), re.S | re.M)
    assert heredoc, f"the {name!r} step runs no python heredoc"
    return textwrap.dedent(heredoc.group(1))


def junit_report(tests: int, skips: tuple[tuple[str, str], ...] = ()) -> str:
    """A JUnit report of the shape pytest writes, with `tests` cases in it."""
    cases = [
        f'<testcase classname="tests.test_made_up" name="test_{number}" time="0.01" />'
        for number in range(tests - len(skips))
    ]
    cases += [
        f'<testcase classname="{node.rpartition("::")[0]}" name="{node.rpartition("::")[2]}"'
        f' time="0.01"><skipped type="pytest.skip" message="{reason}">{reason}</skipped></testcase>'
        for node, reason in skips
    ]
    body = "\n".join(cases)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" errors="0" failures="0"'
        f' skipped="{len(skips)}" tests="{tests}" time="1.0">\n{body}\n'
        "</testsuite></testsuites>\n"
    )


def run_gate(name: str, tmp_path: Path, report: str | None) -> subprocess.CompletedProcess[str]:
    """Run a gate's own script against a report, the way the job runs it."""
    location = tmp_path / "junit.xml"
    if report is not None:
        location.write_text(report)

    environment = dict(os.environ, JUNIT_REPORT=str(location))
    if "TEST_COUNTS" in gate_script(name):
        environment["TEST_COUNTS"] = counts_block()

    return subprocess.run(
        [sys.executable, "-c", gate_script(name)],
        capture_output=True,
        text=True,
        env=environment,
    )


def counts_block() -> str:
    """The committed `TEST_COUNTS` block scalar, verbatim, read out of the workflow."""
    match = re.search(r"\n {6}TEST_COUNTS: \|\n((?: {8}\S.*\n)+)", source())
    assert match, "TEST_COUNTS is not declared as a block scalar in the job env"
    return textwrap.dedent(match.group(1))


def committed_counts() -> dict[str, int]:
    """The committed `{basename: count}` map - the one source of truth."""
    counts: dict[str, int] = {}
    for line in counts_block().splitlines():
        line = line.strip()
        if line:
            name, count = line.split()
            counts[name] = int(count)
    assert counts, "TEST_COUNTS is empty"
    return counts


def expected_tests() -> int:
    """The committed number of tests - derived, the sum of the per-file map."""
    return sum(committed_counts().values())


# --------------------------------------------------------------------------
# Running the per-file gate — issue #72, third round
#
# A single whole-suite total, however measured, cannot see a subset dropped from
# one file while the total is lowered to match - QA beat every version of that
# with a collection hook (collect_ignore, modifyitems, and finally
# pytest_pycollect_makeitem, which suppresses during the tree-walk before any
# per-item counter fires). The gate now pins a count per file and checks each
# file's collected count against it: dropping tests from one file fails on that
# file whatever the total does and whatever hook did it. These helpers lift its
# plugin and its checker out of ci.yml and run them against collections built
# for the occasion - the behaviour is executed, never string-matched.
# --------------------------------------------------------------------------


def plugin_script(name: str) -> str:
    """The pytest plugin a named step writes to disk before collecting."""
    heredoc = re.search(r"<<'PLUGIN'\n(.*?)\n *PLUGIN *$", step(name), re.S | re.M)
    assert heredoc, f"the {name!r} step writes no PLUGIN heredoc"
    return textwrap.dedent(heredoc.group(1))


def collection(counts: dict[str, int], raw: int | None = None) -> str:
    """A collection result of the shape the per-file gate's plugin writes."""
    items = sum(counts.values())
    return json.dumps(
        {
            "raw": items if raw is None else raw,
            "items": items,
            "counts": dict(counts),
        }
    )


def run_perfile_gate(
    tmp_path: Path,
    *,
    actual: dict[str, int],
    committed: dict[str, int] | None = None,
    on_disk: list[str] | None = None,
    raw: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the per-file gate's checker against a collection and a tests/ directory.

    ``actual`` is the per-file collected count the plugin would report;
    ``committed`` is the map in the workflow (the real one by default);
    ``on_disk`` is the set of ``test_*.py`` files the checker globs (the keys of
    ``actual`` plus ``committed`` by default), fabricated as empty files so the
    on-disk comparison can be driven without moving real files.
    """
    committed = committed_counts() if committed is None else committed
    if on_disk is None:
        on_disk = sorted(set(actual) | set(committed))

    tmp_path.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=tmp_path))
    tests_dir = scratch / "tests"
    tests_dir.mkdir()
    for name in on_disk:
        (tests_dir / name).touch()

    out = scratch / "collect.json"
    out.write_text(collection(actual, raw=raw))

    counts_text = "".join(f"{name} {count}\n" for name, count in sorted(committed.items()))
    environment = dict(
        os.environ,
        CI_COLLECT_OUT=str(out),
        TEST_COUNTS=counts_text,
        TESTS_DIR=str(tests_dir),
    )
    return subprocess.run(
        [sys.executable, "-c", gate_script(PERFILE_GATE)],
        capture_output=True,
        text=True,
        env=environment,
    )


# --------------------------------------------------------------------------
# Counting the tests there actually are — issue #72
#
# The number in the workflow is only worth anything if something checks it
# against reality. #67 checked it against the count of `def test_` lines - 578 of
# them against a suite of over 1,300, the rest being parametrized cases - so
# `MINIMUM_TESTS: "600"` passed the whole workflow test file with three test
# files' worth of room to spare. The number is therefore compared against a real
# pytest collection: the same machinery that produces the run, including
# `addopts`, including `tests/conftest.py` and any `collect_ignore` in it.
#
# Collection runs in a subprocess and does not touch the database, so this is a
# few seconds and no fixtures. PYTEST_* variables are dropped from its
# environment: PYTEST_ADDOPTS with a `-k` in it would otherwise let whoever ran
# the suite decide what the suite is.
# --------------------------------------------------------------------------

#: Collects the suite and prints one line per collected test. `pytest.main` in a
#: subprocess rather than a file on disk, so nothing is written anywhere.
COLLECTOR = """
import os
import sys
from pathlib import Path

import pytest


class Report:
    def pytest_collection_finish(self, session):
        for item in session.items:
            print("collected " + item.nodeid)


class IgnoreOneFile:
    # Exactly what a `collect_ignore` entry in a conftest does: pytest's own
    # pytest_ignore_collect hook is what reads `collect_ignore`, and this is
    # that hook.
    def __init__(self, path):
        self.path = Path(path).resolve()

    def pytest_ignore_collect(self, collection_path, config):
        return True if collection_path == self.path else None


plugins = [Report()]
ignore = os.environ.get("COLLECT_IGNORE")
if ignore:
    plugins.append(IgnoreOneFile(ignore))

sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider"], plugins=plugins))
"""


@cache
def collected(ignore: str | None = None) -> tuple[str, ...]:
    """Every test pytest collects, as node ids, optionally with one file ignored."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    if ignore is not None:
        environment["COLLECT_IGNORE"] = str(BASE_DIR / ignore)

    result = subprocess.run(
        [sys.executable, "-c", COLLECTOR],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    prefix = "collected "
    ids = tuple(
        line[len(prefix) :] for line in result.stdout.splitlines() if line.startswith(prefix)
    )
    assert ids, result.stdout + result.stderr
    return ids


def collected_by_file(ignore: str | None = None) -> dict[str, int]:
    """The real collection grouped by file basename - `{basename: count}`."""
    counts: dict[str, int] = {}
    for node_id in collected(ignore=ignore):
        name = node_id.partition("::")[0].rpartition("/")[2]
        counts[name] = counts.get(name, 0) + 1
    return counts


# --------------------------------------------------------------------------
# The workflow exists and runs
# --------------------------------------------------------------------------


def test_ci_yml_is_the_only_workflow() -> None:
    assert WORKFLOW.is_file(), f"{WORKFLOW} does not exist"

    assert sorted(path.name for path in WORKFLOW_DIR.iterdir()) == ["ci.yml"]


def test_it_triggers_on_every_push_on_pull_requests_to_main_and_by_hand() -> None:
    text = commands()

    assert "\n  push:" in text
    assert "\n  pull_request:" in text
    assert "branches: [main]" in text
    assert "\n  workflow_dispatch:" in text

    # No path filter: several tests read AGENTS.md, README.md, package.json and
    # .gitignore as fixtures, so a documentation-only commit can break the suite.
    assert "paths-ignore" not in text
    assert "paths:" not in text


def test_one_job_on_ubuntu_with_a_timeout_and_no_matrix() -> None:
    text = commands()

    assert "runs-on: ubuntu-latest" in text
    assert "timeout-minutes: 30" in text
    assert "strategy:" not in text
    assert "matrix:" not in text

    # One deployment target, one pinned Python, one OS: a second job or a matrix
    # would multiply minutes and produce nothing this project can act on.
    jobs = text.split("\njobs:\n", 1)[1]
    assert len(re.findall(r"^  [a-z][\w-]*:$", jobs, re.M)) == 1
    assert text.count("runs-on:") == 1


def test_it_is_read_only_and_references_no_secret() -> None:
    text = source()

    assert "permissions:" in text
    assert "contents: read" in text
    assert "secrets." not in text


def test_superseded_runs_are_cancelled_rather_than_run_twice() -> None:
    text = commands()

    assert "concurrency:" in text
    assert "github.workflow" in text
    assert "github.ref" in text
    assert "cancel-in-progress: true" in text


# --------------------------------------------------------------------------
# Python, Postgres, and how DATABASE_URL gets there
# --------------------------------------------------------------------------


def test_postgres_is_a_service_container_on_the_image_compose_uses() -> None:
    text = commands()

    assert "services:" in text
    assert "image: postgres:18" in text
    assert "pg_isready" in text

    # Compose builds the application image to get a database, and brings up web
    # and worker with it. None of that belongs in a test job.
    assert "docker compose" not in text
    assert "docker-compose" not in text


def test_the_database_is_feedback_ci_and_arrives_as_a_real_environment_variable() -> None:
    text = commands()

    assert "POSTGRES_DB: feedback_ci" in text
    assert "DATABASE_URL: postgres://postgres:postgres@localhost:5432/feedback_ci" in text

    # The settings line the job logs is proof the variable arrived: the fallback
    # in config/settings.py ends in /feedback, so feedback_ci cannot be it.
    assert "import config.settings_test as s; print(s.DATABASES['default']['NAME'])" in text


def test_no_step_fabricates_a_dotenv_file() -> None:
    # config/settings.py seeds os.environ from a dotenv file with setdefault, so
    # a real environment variable wins. CI has to travel that path, which is the
    # production path, rather than writing a file no deployment writes.
    #
    # `.env` as a filename, which is why the lookahead is here: `os.environ` in
    # the skip gate contains the same four characters and is not a file.
    dotenv = re.search(r"\.env(?![a-z])", commands())

    assert dotenv is None, f"the workflow mentions a dotenv file: {dotenv}"


def test_the_interpreter_version_is_logged() -> None:
    text = commands()

    assert "import sys; print(sys.version)" in text
    assert "3.14." in text


# --------------------------------------------------------------------------
# Dependencies are the locked ones
# --------------------------------------------------------------------------


def test_python_dependencies_install_from_the_lock_file() -> None:
    text = commands()

    assert "uv sync --locked" in text
    assert re.search(r"uv sync(?! --locked)", text) is None, "bare `uv sync` in the workflow"


def test_uv_run_never_re_resolves() -> None:
    assert 'UV_FROZEN: "1"' in commands()


def test_the_resolver_window_is_the_one_the_lock_file_was_resolved_with() -> None:
    """`uv sync --locked` fails on an untouched lock file without this.

    uv.lock records the exclude-newer span it was resolved under. That span
    comes from a uv config on the developer's machine, and a runner has none, so
    uv sees the window removed and re-resolves - and then reports the lock as
    out of date when nothing in the project changed. The two values have to stay
    in step, in both directions: a lock that stops recording a span means the
    workflow should stop setting the variable.
    """
    lock = (BASE_DIR / "uv.lock").read_text()
    span = re.search(r'exclude-newer-span = "P(\d+)D"', lock)

    if span:
        assert f'UV_EXCLUDE_NEWER: "{span.group(1)} days"' in commands()
    else:
        assert "UV_EXCLUDE_NEWER" not in commands()


def test_npm_dependencies_install_from_the_committed_lock_file() -> None:
    text = commands()

    assert "npm ci" in text
    assert "npm install" not in text


def test_both_caches_are_keyed_on_a_lock_file() -> None:
    text = commands()

    assert "enable-cache: true" in text
    assert "cache-dependency-glob: uv.lock" in text
    assert "cache: npm" in text
    assert "cache-dependency-path: package-lock.json" in text


def test_only_the_three_agreed_actions_are_used_and_each_is_pinned() -> None:
    used = USES.findall(commands())

    assert used, "the workflow uses no actions at all"
    for reference in used:
        name, _, version = reference.partition("@")
        assert name in ALLOWED_ACTIONS, f"{name} is not one of the three agreed actions"
        # An explicit major-version tag, never a branch or a floating ref.
        assert re.fullmatch(r"v\d+", version), f"{reference} is not pinned to a major tag"


# --------------------------------------------------------------------------
# The asset build cannot be skipped past
# --------------------------------------------------------------------------


def test_the_stylesheet_is_built_before_the_suite_runs() -> None:
    assert index_of("npm run build:css") < index_of("uv run pytest")


def test_a_missing_stylesheet_fails_the_job_and_names_the_command() -> None:
    text = commands()

    assert "static/css/app.css" in text
    # Larger than 1 KB: an empty file written by a build that produced nothing
    # is not a stylesheet.
    assert "1024" in text

    # The failure message has to say what to run, not only what is missing.
    errors = [line for line in text.splitlines() if "::error::" in line]
    assert any("npm run build:css" in line for line in errors), errors


def test_the_island_bundle_is_built_after_npm_ci_and_before_the_suite() -> None:
    """Issue #63: the second built artefact gets the same treatment as the first.

    `npm run build:js` is the production build AGENTS.md documents. It has to
    run inside this job, from the install `npm ci` already did, and before the
    suite - a bundle built after the tests would prove nothing about them.
    """
    assert index_of("npm ci") < index_of("npm run build:js")
    assert index_of("npm run build:js") < index_of("uv run pytest")

    # One install for both builds; a second one would be a slower job saying
    # the same thing.
    assert commands().count("npm ci") == 1


def test_a_missing_bundle_or_manifest_fails_the_job_and_names_the_command() -> None:
    text = commands()

    # The path is read out of the settings module the application reads, not
    # written down again in the workflow - and out of production settings,
    # because config/settings_test.py points VITE_MANIFEST at a checked-in
    # fixture that exists whether or not anything was built.
    assert "import config.settings as settings" in text
    assert "settings.VITE_MANIFEST" in text
    # Derived, never a literal: a build output path renamed in vite.config.js
    # and config/settings.py together must not leave this job checking the old
    # one and passing.
    assert "static/board" not in text

    # A manifest naming a bundle that is not there, or an empty file where a
    # bundle should be, is as broken as no manifest at all.
    assert 'entry.get("isEntry")' in text
    assert "1024" in text

    errors = [line for line in text.splitlines() if "::error::" in line]
    named = [line for line in errors if "npm run build:js" in line]
    assert len(named) >= 3, errors


def test_collectstatic_runs_against_the_built_assets() -> None:
    """The step that proves the build output is where Django's finders look."""
    text = commands()

    assert "uv run manage.py collectstatic --noinput" in text
    assert index_of("npm run build:css") < index_of("uv run manage.py collectstatic --noinput")
    assert index_of("npm run build:js") < index_of("uv run manage.py collectstatic --noinput")


def test_the_bundle_the_suite_reads_is_the_one_this_job_builds() -> None:
    """Why building it matters: two tests read the real manifest, not the fixture.

    They take the `built_island` fixture, which builds the island and fails -
    never skips - when the build does not produce the file (#54). So the build
    step is not decoration: it is the same artefact, produced by the same
    command, that those two tests refuse to run without.
    """
    island = (BASE_DIR / "tests" / "test_island.py").read_text()

    # The fixture lives in #54's files, not this one. What this test owns is the
    # other half of the bargain: whatever those tests depend on, this job builds.
    assert island.count("built_island: Path") == 2
    assert "npm run build:js" in island
    assert 'BASE_DIR / "static" / settings.VITE_BUILD_SUBDIR' in island

    assert index_of("npm run build:js") < index_of("uv run pytest")


def test_node_stays_pinned_to_a_major_version() -> None:
    text = commands()

    assert 'node-version: "24"' in text
    # Never `node-version-file` or a floating `lts/*`: the version the build
    # runs on is a fact of this file.
    assert "node-version-file" not in text
    assert "lts/*" not in text


def test_ffmpeg_comes_from_the_image_the_dockerfile_uses() -> None:
    text = commands()

    assert "mwader/static-ffmpeg:8.1" in text
    assert "ffmpeg -version" in text
    assert "ffprobe" in text
    assert index_of("mwader/static-ffmpeg:8.1") < index_of("uv run pytest")


def test_a_skipped_test_fails_the_job() -> None:
    text = commands()

    assert "-rs" in text
    assert "--junitxml" in text
    # The standard library, because issue #30 adds no dependency.
    assert "xml.etree.ElementTree" in text
    assert 'findall("skipped")' in text
    assert index_of("--junitxml") < index_of("xml.etree.ElementTree")


def test_the_skip_gate_reports_the_node_id_and_the_reason() -> None:
    text = commands()

    assert 'case.get("classname"' in text
    assert 'case.get("name"' in text
    assert 'skip.get("message"' in text
    assert "sys.exit(1)" in text


# --------------------------------------------------------------------------
# A test removed from collection is red too — issues #67 and #72
#
# The skip gate reads `<skipped>` elements. A test that is never collected
# writes no `<testcase>` at all, so it is invisible to that gate: QA put
# `--ignore=tests/test_audio.py` in addopts and got a green run with a fifth of
# the suite gone. The count of tests that ran is checked as well.
#
# #67 checked it against a floor. #72 makes it an exact count, because a floor
# is only ever wrong in the direction nobody feels: the run that would tell you
# to raise it is green and the run that tempts you to lower it is red. An exact
# count moves in the commit that changes the suite or the build is red.
# --------------------------------------------------------------------------


def test_the_test_count_gate_runs_after_the_suite_and_reads_the_same_report() -> None:
    assert index_of("uv run pytest") < index_of(COUNT_GATE)
    assert "JUNIT_REPORT" in step(COUNT_GATE)
    # The count comes off the report the suite wrote, not out of the pytest log,
    # and it is the root element's own total rather than a tally of elements the
    # gate happened to find.
    assert 'get("tests"' in gate_script(COUNT_GATE)


def test_the_committed_counts_are_the_one_source_of_truth() -> None:
    text = source()
    job_env = text.split("\n    steps:\n", 1)[0]

    # One committed fact, the per-file map, declared once as a block scalar in
    # the job env. The count is derived from it (the sum), not committed a second
    # time - so there is nothing to drift.
    block = re.findall(r"\n {6}TEST_COUNTS: \|\n(?: {8}\S.*\n)+", text)
    assert len(block) == 1, block
    assert block[0] in job_env

    # The single-total facts it replaced are gone as committed values - no
    # anonymous number survives that a subset drop could hide behind. Their names
    # may still appear in prose (the comment explains that the count is now the
    # derived sum), but not as a declared env key.
    assert "MINIMUM_TESTS" not in text
    assert re.search(r"\n +EXPECTED_TESTS:", text) is None
    assert re.search(r"\n +TEST_FILES:", text) is None


def test_the_committed_counts_are_the_real_per_file_collection() -> None:
    """The map is checked against a real collection, per file, not against text.

    This is what makes the number un-lowerable (criterion 3): a committed count
    that the suite does not actually produce fails here, one step before CI. It
    is per file, so it also catches the class QA rode all the way to a
    `pytest_pycollect_makeitem` hook - a subset dropped from one file, the total
    lowered to match - because the drop shows up as that one file's number.
    """
    actual = collected_by_file()

    assert actual == committed_counts(), (
        "the per-file collection is not TEST_COUNTS in .github/workflows/ci.yml. "
        "Regenerate the block with the command in AGENTS.md and commit it in the "
        f"same change. Collected: {dict(sorted(actual.items()))}"
    )
    # The derived total agrees with the collection, so the JUnit gate's number
    # (the sum) is honest too.
    assert expected_tests() == len(collected())


def test_a_subset_dropped_from_one_file_shows_up_on_that_file() -> None:
    """The move a single total cannot see, and the per-file map can.

    `tests/test_audio.py` leaves collection through the hook a `collect_ignore`
    feeds, and its tests are assumed to reappear elsewhere so the total is
    restored. A single number is blind to that; the per-file map is not - the
    audio line has moved even though the sum has not.
    """
    masked = collected_by_file(ignore="tests/test_audio.py")

    # The total can be made to match again by cases added elsewhere...
    restored_total = len(collected()) - committed_counts()["test_audio.py"]
    assert sum(masked.values()) + committed_counts()["test_audio.py"] == expected_tests()
    assert restored_total == sum(masked.values())

    # ...but test_audio.py's own count has collapsed, and that is what the gate
    # reads. No balancing act elsewhere restores this line.
    assert masked.get("test_audio.py", 0) == 0
    assert masked != committed_counts()


def test_the_conftest_carries_no_collection_shrinking_hook(tmp_path: Path) -> None:
    """#54 gave the project a `tests/conftest.py`, the natural home for a hide.

    Four hooks can shrink a collection from here - `collect_ignore`,
    `pytest_collection_modifyitems`, `pytest_pycollect_makeitem`,
    `pytest_generate_tests` - and QA used three of them in turn. The grep is
    documentation that none is present today; what actually catches any of them
    at run time is the per-file gate, which reads each file's collected count
    from `session.items` and so sees suppression at every stage. The last line
    proves that: the audio file thinned to nothing is red against the real map.
    """
    conftest = BASE_DIR / "tests" / "conftest.py"
    body = conftest.read_text()

    assert conftest.is_file(), "tests/conftest.py is gone; collection no longer travels it"
    for hook in (
        "collect_ignore",
        "pytest_collection_modifyitems",
        "pytest_pycollect_makeitem",
        "pytest_generate_tests",
    ):
        assert hook not in body, f"tests/conftest.py grew a {hook}"

    assert collected_by_file() == committed_counts()

    thinned = dict(committed_counts())
    thinned["test_audio.py"] = 0
    red = run_perfile_gate(tmp_path, actual=thinned)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "test_audio.py: collected nothing" in red.stdout, red.stdout


# --------------------------------------------------------------------------
# The per-file gate, run against collections built for the occasion
# --------------------------------------------------------------------------


def test_the_perfile_gate_runs_after_the_suite_and_collects_independently() -> None:
    # It is a job step, not a collected test - that is the whole point, so a file
    # removed from collection cannot disable it. It collects the suite itself
    # rather than reading the JUnit report the other two gates read.
    assert index_of("uv run pytest") < index_of(PERFILE_GATE)
    assert "pytest --collect-only" in step(PERFILE_GATE)
    assert "JUNIT_REPORT" not in gate_script(PERFILE_GATE)

    # The plugin is written outside the checkout, under a mktemp directory, so
    # the suite's own walks over the tree never see it and it is not itself a
    # file that could be dropped.
    assert "mktemp" in step(PERFILE_GATE)
    assert "PYTHONPATH" in step(PERFILE_GATE)


def run_plugin(*extra_plugins: tuple[str, str]) -> dict:
    """Run the gate's own plugin against the real suite, with optional injections.

    Each extra plugin is ``(module_name, source)`` written beside the gate's
    plugin and loaded with ``-p``. Returns the collection payload the plugin
    writes - the same one the checker reads in CI.
    """
    with tempfile.TemporaryDirectory() as scratch_name:
        scratch = Path(scratch_name)
        (scratch / "_ci_collect.py").write_text(plugin_script(PERFILE_GATE))
        plugin_args = ["-p", "_ci_collect"]
        for module, body in extra_plugins:
            (scratch / f"{module}.py").write_text(body)
            plugin_args += ["-p", module]

        out = scratch / "collect.json"
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")
        }
        environment["CI_COLLECT_OUT"] = str(out)
        environment["PYTHONPATH"] = str(scratch)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                *plugin_args,
                "-p",
                "no:cacheprovider",
            ],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(out.read_text())


def test_the_collection_plugin_groups_counts_by_file() -> None:
    """The plugin from ci.yml, run against the real suite.

    It reports a count per file, taken from `session.items` - what survives every
    collection stage - and that grouping is exactly the committed map.
    """
    data = run_plugin()

    assert data["counts"] == committed_counts(), data["counts"]
    assert data["raw"] == data["items"] == expected_tests(), data


def test_the_perfile_count_is_taken_after_tree_walk_suppression() -> None:
    """The sixth attack, at the plugin: a `pytest_pycollect_makeitem` hook.

    It suppresses test functions during the collection tree-walk, before any
    per-item counter fires, so `raw` falls in lockstep with `items` - the check
    that beat attack 5 (`raw != items`) is blind to it. The per-file counts are
    not: they come from `session.items`, so `test_auth.py` falls to 5 there, and
    the checker reads that against the committed 41.
    """
    makeitem = textwrap.dedent(
        """
        _seen = {}

        def pytest_pycollect_makeitem(collector, name, obj):
            if name.startswith("test_") and "test_auth.py" in str(collector.nodeid):
                _seen[collector.nodeid] = _seen.get(collector.nodeid, 0) + 1
                if _seen[collector.nodeid] > 5:
                    return []
            return None
        """
    )
    data = run_plugin(("_ci_makeitem", makeitem))

    # raw and items agree - the deselection check would pass - but test_auth.py
    # has collapsed, and that is what the per-file map reads.
    assert data["raw"] == data["items"], data
    assert data["counts"]["test_auth.py"] == 5, data["counts"]["test_auth.py"]
    assert data["counts"]["test_auth.py"] != committed_counts()["test_auth.py"]


def test_the_plugin_sees_a_hook_thin_the_suite_after_collection() -> None:
    """Belt-and-braces: a `modifyitems` deselection still shows in raw != items."""
    thinner = textwrap.dedent(
        """
        def pytest_collection_modifyitems(config, items):
            keep = {}
            for item in items:
                keep.setdefault(item.nodeid.split("::", 1)[0], item)
            items[:] = list(keep.values())
        """
    )
    data = run_plugin(("_ci_thin", thinner))

    assert data["raw"] == expected_tests(), data
    assert data["items"] == len(committed_counts()), data
    assert data["raw"] > data["items"], data


def test_the_perfile_gate_passes_a_consistent_collection(tmp_path: Path) -> None:
    result = run_perfile_gate(tmp_path, actual=committed_counts())
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_perfile_gate_fails_when_a_test_file_is_added(tmp_path: Path) -> None:
    """A new file that collects tests but is not in the map."""
    actual = {**committed_counts(), "test_new.py": 4}
    result = run_perfile_gate(tmp_path, actual=actual)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_new.py: on disk, collected 4, not listed in TEST_COUNTS." in output, output
    assert "test_new.py 4" in output, output


def test_the_perfile_gate_fails_when_a_test_file_is_removed(tmp_path: Path) -> None:
    """A `git rm`: the file is gone from the collection, still in the map."""
    actual = {k: v for k, v in committed_counts().items() if k != "test_compose_worker.py"}
    result = run_perfile_gate(
        tmp_path,
        actual=actual,
        on_disk=sorted(actual),  # file not on disk either
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_compose_worker.py: collected nothing, TEST_COUNTS says 8" in output, output
    assert "(drop) test_compose_worker.py" in output, output


def test_the_perfile_gate_fails_when_a_test_file_is_renamed(tmp_path: Path) -> None:
    """The old name collects nothing; the new name is not in the map."""
    actual = {k: v for k, v in committed_counts().items() if k != "test_media_sweeper.py"}
    actual["test_renamed.py"] = 27
    result = run_perfile_gate(tmp_path, actual=actual)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_media_sweeper.py: collected nothing, TEST_COUNTS says 27" in output, output
    assert "test_renamed.py: on disk, collected 27, not listed in TEST_COUNTS." in output, output


def test_the_perfile_gate_fails_when_a_listed_file_collects_nothing(tmp_path: Path) -> None:
    """The `--ignore` shape, including `--ignore` of the guard file itself.

    The file is on disk and in the map, but pytest collected nothing from it. The
    count went to zero and that is what fires - there is no total to lower that
    would paper over one file's collapse.
    """
    actual = {k: v for k, v in committed_counts().items() if k != "test_ci_workflow.py"}
    result = run_perfile_gate(
        tmp_path,
        actual=actual,
        on_disk=sorted(committed_counts()),  # file still on disk
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_ci_workflow.py: collected nothing, TEST_COUNTS says" in output, output


def test_the_perfile_gate_fails_when_one_file_loses_a_subset(tmp_path: Path) -> None:
    """Attacks 6 and 7: a subset dropped from one file, the total lowered to match.

    A `pytest_pycollect_makeitem` or `pytest_generate_tests` hook shrinks
    `test_auth.py` from 41 to 5. Every other file is untouched, every file still
    contributes >=1, nothing is skipped, and the collection total is a
    self-consistent 1363. A single number - however measured, before or after any
    hook - agrees with itself at 1363. The per-file map does not: the auth line
    reads 5 against a committed 41, and no balancing act anywhere else restores it.
    """
    actual = {**committed_counts(), "test_auth.py": 5}
    result = run_perfile_gate(tmp_path, actual=actual)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_auth.py: collected 5, TEST_COUNTS says 41." in output, output
    assert "test_auth.py 5" in output, output


def test_the_perfile_gate_fails_when_a_hook_thins_the_suite(tmp_path: Path) -> None:
    """The modifyitems attack: every file thinned to one test, total lowered.

    Per-file, every file whose committed count is above one now reads wrong, so
    the gate is red on all of them - and the belt-and-braces deselection check
    fires as well because the plugin's raw outran the kept items.
    """
    thinned = dict.fromkeys(committed_counts(), 1)
    result = run_perfile_gate(tmp_path, actual=thinned, raw=expected_tests())
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    # Read the committed count rather than hard-coding it, so regenerating
    # TEST_COUNTS at integration does not break this assertion.
    permissions_count = committed_counts()["test_permissions.py"]
    assert f"test_permissions.py: collected 1, TEST_COUNTS says {permissions_count}." in output, (
        output
    )
    assert "were deselected after collection" in output, output


def test_the_perfile_gate_fails_when_a_file_count_moves_either_direction(tmp_path: Path) -> None:
    for name, delta in (("test_board.py", -5), ("test_board.py", +5)):
        actual = {**committed_counts(), name: committed_counts()[name] + delta}
        result = run_perfile_gate(tmp_path, actual=actual)
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        got = committed_counts()[name] + delta
        assert f"{name}: collected {got}, TEST_COUNTS says {committed_counts()[name]}." in output
        assert f"{name} {got}" in output, output


def test_the_perfile_gate_rejects_a_file_pinned_at_zero(tmp_path: Path) -> None:
    """The `--ignore`-the-guard escape, closed.

    A suppressed file collects nothing, and a committed 0 would match that
    nothing. So no file may be pinned at 0: to remove the guard file you must
    edit its line to 0, and that is refused - there is no green value for "this
    file runs no tests".
    """
    committed = {**committed_counts(), "test_ci_workflow.py": 0}
    actual = {k: v for k, v in committed.items() if k != "test_ci_workflow.py"}
    result = run_perfile_gate(
        tmp_path, actual=actual, committed=committed, on_disk=sorted(committed)
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_ci_workflow.py is pinned at 0 in TEST_COUNTS" in output, output


def test_the_perfile_gate_fails_a_map_that_sums_wrong(tmp_path: Path) -> None:
    """One source of truth: a map that does not sum to the collection is red.

    The collection is honest here - every file matches - but the committed map
    given to the gate carries a file the collection does not, so its sum overruns
    the total. This is the derived-total check the JUnit gate leans on.
    """
    inflated = {**committed_counts(), "test_ghost.py": 9}
    result = run_perfile_gate(
        tmp_path,
        actual=committed_counts(),
        committed=inflated,
        on_disk=sorted(committed_counts()),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "sums to" in output and "disagree on the total" in output, output


def test_the_perfile_gate_catches_the_balanced_move_the_junit_gates_cannot(tmp_path: Path) -> None:
    """Criterion 2 across the gates: tests moved between files, total unchanged.

    36 tests move from `test_auth.py` to `test_home.py`. The JUnit report is the
    right size and nothing is skipped, so the count gate and the skip gate pass.
    Only the per-file gate sees it - because it is the only guard that pins where
    the tests live, not merely how many there are.
    """
    balanced = junit_report(expected_tests())
    count_dir = tmp_path / "count"
    count_dir.mkdir()
    skip_dir = tmp_path / "skip"
    skip_dir.mkdir()

    assert run_gate(COUNT_GATE, count_dir, balanced).returncode == 0
    assert run_gate(SKIP_GATE, skip_dir, balanced).returncode == 0

    moved = {**committed_counts()}
    moved["test_auth.py"] -= 36
    moved["test_home.py"] += 36
    perfile = run_perfile_gate(tmp_path / "perfile", actual=moved)
    output = perfile.stdout + perfile.stderr
    assert perfile.returncode == 1, output
    assert "test_auth.py: collected 5, TEST_COUNTS says 41." in output, output
    assert "test_home.py: collected 37, TEST_COUNTS says 1." in output, output


def test_the_test_count_gate_fails_when_a_test_file_left_the_run(tmp_path: Path) -> None:
    """The exact shape of QA's attack: fewer tests, none of them skipped."""
    result = run_gate(COUNT_GATE, tmp_path, junit_report(expected_tests() - 19))

    assert result.returncode == 1, result
    output = result.stdout + result.stderr
    assert "::error::" in output, output
    assert str(expected_tests() - 19) in output and str(expected_tests()) in output, output


def test_the_test_count_gate_fails_a_run_that_grew_as_well(tmp_path: Path) -> None:
    """The half a floor cannot do, and the half that keeps the number honest.

    A run with more tests than expected is not a build to wave through: it is a
    branch that added tests without moving the line, and the next branch to
    remove tests would have hidden behind the slack.
    """
    result = run_gate(COUNT_GATE, tmp_path, junit_report(expected_tests() + 40))

    assert result.returncode == 1, result
    output = result.stdout + result.stderr
    assert "::error::" in output, output
    assert str(expected_tests() + 40) in output, output


def test_the_test_count_gate_passes_exactly_the_expected_run(tmp_path: Path) -> None:
    result = run_gate(COUNT_GATE, tmp_path, junit_report(expected_tests()))

    assert result.returncode == 0, result
    assert str(expected_tests()) in result.stdout, result.stdout


def test_the_test_count_gate_points_at_the_map_and_the_regenerate_command(tmp_path: Path) -> None:
    """The message has to be actionable by someone who has never read this file."""
    for ran in (expected_tests() - 1, expected_tests() + 40):
        result = run_gate(COUNT_GATE, tmp_path, junit_report(ran))
        output = result.stdout + result.stderr

        assert ".github/workflows/ci.yml" in output, output
        # It names the single source of truth and how to regenerate it, and says
        # the per-file gate will name which file moved.
        assert "TEST_COUNTS" in output, output
        assert "AGENTS.md" in output, output
        assert "same commit" in output, output

    # A run that lost tests says so before it says how to fix the number.
    lost = run_gate(COUNT_GATE, tmp_path, junit_report(expected_tests() - 1))
    assert "did not" in lost.stdout, lost.stdout


def test_the_junit_gate_derives_its_number_from_the_committed_map(tmp_path: Path) -> None:
    """No second committed total: the expected count is the sum of TEST_COUNTS."""
    total = sum(committed_counts().values())

    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    at_sum = run_gate(COUNT_GATE, ok_dir, junit_report(total))
    assert at_sum.returncode == 0, at_sum.stdout + at_sum.stderr

    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = run_gate(COUNT_GATE, off_dir, junit_report(total - 1))
    assert off.returncode == 1, off.stdout + off.stderr


def test_the_test_count_gate_fails_when_pytest_wrote_no_report(tmp_path: Path) -> None:
    result = run_gate(COUNT_GATE, tmp_path, None)

    assert result.returncode != 0, result
    assert "::error::" in result.stdout + result.stderr


def test_the_skip_gate_still_fails_a_skipped_test(tmp_path: Path) -> None:
    """#30's gate, run rather than read, so #67 and #72 cannot have replaced it."""
    report = junit_report(
        expected_tests(), skips=(("tests.test_audio::test_probe", "ffmpeg missing"),)
    )
    result = run_gate(SKIP_GATE, tmp_path, report)
    output = result.stdout + result.stderr

    assert result.returncode == 1, result
    assert "tests.test_audio::test_probe" in output, output
    assert "ffmpeg missing" in output, output


def test_the_skip_gate_passes_a_clean_report(tmp_path: Path) -> None:
    result = run_gate(SKIP_GATE, tmp_path, junit_report(expected_tests()))

    assert result.returncode == 0, result
    assert "No test skipped." in result.stdout, result.stdout


def test_the_two_gates_catch_different_things(tmp_path: Path) -> None:
    """Neither gate covers the other, which is why both are here."""
    lost = tmp_path / "lost"
    lost.mkdir()
    missing_tests = junit_report(expected_tests() - 19)

    # Tests gone from collection: nothing is skipped, so only the count sees it.
    assert run_gate(SKIP_GATE, lost, missing_tests).returncode == 0
    assert run_gate(COUNT_GATE, lost, missing_tests).returncode == 1

    hidden = tmp_path / "hidden"
    hidden.mkdir()
    skipped = junit_report(
        expected_tests(), skips=(("tests.test_audio::test_probe", "ffmpeg missing"),)
    )

    # The full suite ran and 1 test opted out: only the skip gate sees that.
    assert run_gate(COUNT_GATE, hidden, skipped).returncode == 0
    assert run_gate(SKIP_GATE, hidden, skipped).returncode == 1


# --------------------------------------------------------------------------
# The checks themselves
# --------------------------------------------------------------------------


def test_both_ruff_commands_run_as_separate_steps() -> None:
    text = commands()

    assert "run: uv run ruff check ." in text
    assert "run: uv run ruff format --check ." in text


def test_the_missing_migration_check_runs() -> None:
    assert "uv run manage.py makemigrations --check --dry-run" in commands()


def test_the_cheap_checks_run_first_and_nothing_swallows_a_failure() -> None:
    assert index_of("uv run ruff check .") < index_of("npm ci")
    assert index_of("uv run manage.py makemigrations --check --dry-run") < index_of("npm ci")
    assert index_of("npm ci") < index_of("uv run pytest")

    text = commands()
    # `continue-on-error` and a trailing `|| true` both turn a red step green.
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "if: always()" not in text


# --------------------------------------------------------------------------
# Documented, and no dependency added for any of it
# --------------------------------------------------------------------------


def test_agents_md_says_what_ci_runs_and_how_to_reproduce_it() -> None:
    agents = (BASE_DIR / "AGENTS.md").read_text()

    assert "\nCI\n" in agents, "AGENTS.md has no CI section"
    section = agents.split("\nCI\n", 1)[1]

    assert "skipped test" in section
    assert "uv run pytest -rs" in section
    assert "npm run build:css" in section
    # Both builds, in the description and in the command that reproduces a run:
    # a local run without `npm run build:js` reports two skips, and a skip is a
    # failure here.
    assert "npm run build:js" in section
    assert "collectstatic" in section
    assert ".github/workflows/ci.yml" in section

    reproduce = next(line for line in section.splitlines() if "uv run pytest -rs" in line)
    for command in ("npm ci", "npm run build:css", "npm run build:js"):
        assert command in reproduce, reproduce


def test_agents_md_documents_the_per_file_map_and_how_to_regenerate_it() -> None:
    agents = (BASE_DIR / "AGENTS.md").read_text()
    section = agents.split("\nCI\n", 1)[1]

    assert "TEST_COUNTS" in section
    assert "MINIMUM_TESTS" not in section
    assert "EXPECTED_TESTS" not in section
    assert "per-file" in section or "per file" in section
    # Named the same way in the docs as in the file someone will go and edit.
    assert ".github/workflows/ci.yml" in section
    # The two things a person merging a branch has to know: the map is pinned per
    # file, and moving it is their job in their own commit.
    assert "same commit" in section

    # And the one-command regenerator is present, so an integrator pastes a fresh
    # block rather than hand-counting.
    assert "collections" in section
    assert "pytest_collection_finish" in section
    assert "--collect-only" in section


def test_this_file_needs_no_yaml_parser_and_no_new_dependency() -> None:
    # Asked of the parsed module rather than of its text, because a test that
    # greps its own source for "import yaml" finds the string in its own
    # assertion and fails.
    tree = ast.parse(Path(__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    # django and pytest are already pinned in pyproject.toml; anything else
    # would be a package this issue said it did not need.
    already_pinned = {"django", "pytest"}

    assert imported <= set(sys.stdlib_module_names) | already_pinned, imported

    # And nothing was added to the project to make the workflow work either.
    pyproject = (BASE_DIR / "pyproject.toml").read_text()
    assert "yaml" not in pyproject
