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
import re
import sys
from pathlib import Path

from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
WORKFLOW_DIR = BASE_DIR / ".github" / "workflows"
WORKFLOW = WORKFLOW_DIR / "ci.yml"

#: The three actions issue #30 allows, and nothing else from the marketplace.
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-node", "astral-sh/setup-uv"}

USES = re.compile(r"uses:\s*(\S+)")


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
    """Why building it matters: two tests read the real manifest, and skip without it.

    tests/test_island.py guards them on the file `npm run build:js` writes, and
    a skipped test fails this job. So the build step is not decoration - remove
    it and the skip gate turns the run red.
    """
    island = (BASE_DIR / "tests" / "test_island.py").read_text()

    # The two guards live in #54's file, not this one. What this test owns is
    # the other half of the bargain: whatever they guard on, this job builds.
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
