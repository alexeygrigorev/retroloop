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
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
WORKFLOW_DIR = BASE_DIR / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "ci.yml"

#: The three actions issue #30 allows, and nothing else from the marketplace.
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-node", "astral-sh/setup-uv"}

USES = re.compile(r"uses:\s*(\S+)")

#: The two gates that read the JUnit report, by step name.
COUNT_GATE = "Fail if fewer tests ran than the floor"
SKIP_GATE = "Fail if any test was skipped"


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
    if "MINIMUM_TESTS" in step(name):
        environment["MINIMUM_TESTS"] = str(floor())

    return subprocess.run(
        [sys.executable, "-c", gate_script(name)],
        capture_output=True,
        text=True,
        env=environment,
    )


def floor() -> int:
    """The committed floor on the number of tests, read out of the workflow."""
    declared = re.findall(r'MINIMUM_TESTS: "(\d+)"', source())
    assert len(declared) == 1, f"the floor is declared {len(declared)} times, not once"
    return int(declared[0])


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
    assert "timeout-minutes: 15" in text
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
# A test removed from collection is red too — issue #67
#
# The skip gate reads `<skipped>` elements. A test that is never collected
# writes no `<testcase>` at all, so it is invisible to that gate: QA put
# `--ignore=tests/test_audio.py` in addopts and got a green run with a fifth of
# the suite gone. The count of tests that ran is checked against a committed
# floor as well, and the two gates are independent.
# --------------------------------------------------------------------------


def test_the_test_count_gate_runs_after_the_suite_and_reads_the_same_report() -> None:
    assert index_of("uv run pytest") < index_of(COUNT_GATE)
    assert "JUNIT_REPORT" in step(COUNT_GATE)
    # The count comes off the report the suite wrote, not out of the pytest log,
    # and it is the root element's own total rather than a tally of elements the
    # gate happened to find.
    assert 'get("tests"' in gate_script(COUNT_GATE)


def test_the_floor_is_one_line_next_to_the_gate_that_reads_it() -> None:
    block = step(COUNT_GATE)

    # Declared once, in the step that uses it, on a line of its own.
    assert re.search(r'\n +MINIMUM_TESTS: "\d+"\n', block), block
    assert len(re.findall(r'MINIMUM_TESTS: "\d+"', source())) == 1

    # And the comment beside it - not the failure message, the comment someone
    # editing the number reads first - says which way the number may move.
    comments = "\n".join(line for line in block.splitlines() if line.lstrip().startswith("#"))
    assert "never lowered to make a build pass" in comments, comments
    assert "deliberately" in comments, comments


def test_the_floor_is_not_a_number_the_suite_would_clear_with_tests_missing() -> None:
    """A floor of 1 would pass every mutilated run there is.

    Every test function in `tests/` contributes at least one collected test, so
    the number of them is a lower bound on an honest floor - and a floor set
    below it would be one that a suite with whole files missing still clears.
    """
    functions = sum(
        len(re.findall(r"^def test_", path.read_text(), re.M))
        for path in sorted((BASE_DIR / "tests").glob("test_*.py"))
    )

    assert functions > 0
    assert floor() >= functions, f"floor {floor()} is below the {functions} tests defined in tests/"


def test_the_test_count_gate_fails_when_a_test_file_left_the_run(tmp_path: Path) -> None:
    """The exact shape of QA's attack: fewer tests, none of them skipped."""
    result = run_gate(COUNT_GATE, tmp_path, junit_report(floor() - 19))

    assert result.returncode == 1, result
    output = result.stdout + result.stderr
    assert "::error::" in output, output
    assert str(floor() - 19) in output and str(floor()) in output, output


def test_the_test_count_gate_passes_a_full_run_and_a_grown_one(tmp_path: Path) -> None:
    at_the_floor = run_gate(COUNT_GATE, tmp_path, junit_report(floor()))
    assert at_the_floor.returncode == 0, at_the_floor

    grown = run_gate(COUNT_GATE, tmp_path, junit_report(floor() + 40))
    assert grown.returncode == 0, grown


def test_the_test_count_gate_says_raising_the_floor_is_one_line(tmp_path: Path) -> None:
    """The message has to be actionable by someone who has never read this file."""
    result = run_gate(COUNT_GATE, tmp_path, junit_report(floor() - 1))
    output = result.stdout + result.stderr

    assert "MINIMUM_TESTS" in output, output
    assert ".github/workflows/ci.yml" in output, output
    assert "one line" in output, output
    assert "never lowered" in output, output

    # And a run that has outgrown the floor says what the new number would be.
    grown = run_gate(COUNT_GATE, tmp_path, junit_report(floor() + 40))
    assert f'MINIMUM_TESTS: "{floor() + 40}"' in grown.stdout, grown.stdout


def test_the_test_count_gate_fails_when_pytest_wrote_no_report(tmp_path: Path) -> None:
    result = run_gate(COUNT_GATE, tmp_path, None)

    assert result.returncode != 0, result
    assert "::error::" in result.stdout + result.stderr


def test_the_skip_gate_still_fails_a_skipped_test(tmp_path: Path) -> None:
    """#30's gate, run rather than read, so #67 cannot quietly have replaced it."""
    report = junit_report(floor(), skips=(("tests.test_audio::test_probe", "ffmpeg missing"),))
    result = run_gate(SKIP_GATE, tmp_path, report)
    output = result.stdout + result.stderr

    assert result.returncode == 1, result
    assert "tests.test_audio::test_probe" in output, output
    assert "ffmpeg missing" in output, output


def test_the_skip_gate_passes_a_clean_report(tmp_path: Path) -> None:
    result = run_gate(SKIP_GATE, tmp_path, junit_report(floor()))

    assert result.returncode == 0, result
    assert "No test skipped." in result.stdout, result.stdout


def test_the_two_gates_catch_different_things(tmp_path: Path) -> None:
    """Neither gate covers the other, which is why both are here."""
    lost = tmp_path / "lost"
    lost.mkdir()
    missing_tests = junit_report(floor() - 19)

    # Tests gone from collection: nothing is skipped, so only the count sees it.
    assert run_gate(SKIP_GATE, lost, missing_tests).returncode == 0
    assert run_gate(COUNT_GATE, lost, missing_tests).returncode == 1

    hidden = tmp_path / "hidden"
    hidden.mkdir()
    skipped = junit_report(floor(), skips=(("tests.test_audio::test_probe", "ffmpeg missing"),))

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


def test_agents_md_says_the_number_of_tests_has_a_floor_and_how_to_raise_it() -> None:
    agents = (BASE_DIR / "AGENTS.md").read_text()
    section = agents.split("\nCI\n", 1)[1]

    assert "MINIMUM_TESTS" in section
    assert "collection" in section
    # Named the same way in the docs as in the file someone will go and edit.
    assert ".github/workflows/ci.yml" in section


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
