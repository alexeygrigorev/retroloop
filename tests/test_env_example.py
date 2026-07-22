"""`.env.example` against what `config/settings.py` actually reads.

AGENTS.md says a new setting means a new env var *and* a line in
`.env.example`. #56 is what the second half being skipped looks like:
`SCRATCH_DIR` was read from the environment and set for both Compose services,
but never written down, so a `.env` copied from the example left the media
pipeline writing somewhere the person who copied it had not chosen.

These tests read the settings module itself instead of a list someone has to
remember to update, so the next setting configured from the environment fails
here until its line exists. `tests/test_auth.py` guards the other direction -
the exact set of names, which is decision 8's tripwire against a mail variable
appearing - and the two are complementary: that one says nothing may arrive
unnoticed, this one says nothing settings reads may be left out.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
EXAMPLE = BASE_DIR / ".env.example"

# The helpers in config/settings.py that take the name of an environment
# variable as their first argument, alongside the os.environ forms.
READERS = frozenset({"env_bool", "env_list", "getenv"})


def variables_settings_reads() -> set[str]:
    """Every environment variable named by a literal in `config/settings.py`.

    Parsed rather than imported: the module has already been imported by the
    time a test runs, and what is wanted is the names in the source, including
    any that a branch skipped.
    """
    tree = ast.parse((BASE_DIR / "config" / "settings.py").read_text())
    names: set[str] = set()

    for node in ast.walk(tree):
        # os.environ["NAME"]
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                names.add(node.slice.value)
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        attribute = isinstance(function, ast.Attribute)
        called = function.attr if attribute else getattr(function, "id", None)
        environ_get = called == "get" and attribute and _is_environ(function.value)
        if not environ_get and called not in READERS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)

    return names


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def example_names() -> set[str]:
    return {line.split("=", 1)[0].strip() for line in assignments()}


def assignments() -> list[str]:
    return [
        line.strip()
        for line in EXAMPLE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    ]


def test_the_parser_finds_the_variables_that_are_known_to_be_there() -> None:
    """The enumeration above is only worth anything if it really reads them."""
    assert variables_settings_reads() == {
        "DEBUG",
        "SECRET_KEY",
        "ALLOWED_HOSTS",
        "DATABASE_URL",
        "SCRATCH_DIR",
        "OPENAI_API_KEY",
    }


def test_every_variable_settings_reads_has_a_line_in_the_example() -> None:
    missing = sorted(variables_settings_reads() - example_names())

    assert missing == [], f".env.example does not mention {missing}"


def test_the_example_names_nothing_settings_does_not_read() -> None:
    """The other way round: a line for a setting that no longer exists."""
    stale = sorted(example_names() - variables_settings_reads())

    assert stale == [], f".env.example carries {stale}, which settings.py never reads"


def test_scratch_dir_carries_the_development_default() -> None:
    """`scratch/`, next to the checkout - the same place settings.py defaults to."""
    assert "SCRATCH_DIR=scratch" in assignments()
    assert "/scratch/" in (BASE_DIR / ".gitignore").read_text()


def test_the_scratch_dir_line_is_explained() -> None:
    """A value on its own does not say what the directory is for."""
    lines = EXAMPLE.read_text().splitlines()
    index = lines.index("SCRATCH_DIR=scratch")
    comment = []
    while index and lines[index - 1].startswith("#"):
        index -= 1
        comment.insert(0, lines[index])
    explanation = " ".join(comment)

    assert comment, "SCRATCH_DIR has no comment above it"
    assert "media pipeline" in explanation


def test_the_example_carries_no_credential() -> None:
    """Development defaults only. The one credential stays an empty placeholder."""
    assert "OPENAI_API_KEY=" in assignments()


@pytest.mark.parametrize("settings_module", ["config.settings", "config.settings_test"])
def test_a_copy_of_the_example_is_a_complete_configuration(settings_module: str) -> None:
    """Import the real settings with exactly what a copied `.env` would supply.

    Nothing is written into the checkout: the example's variables are passed as
    the environment directly, which is also what makes the result independent of
    whatever `.env` the developer running this has. A real environment variable
    beats the `.env` loader, so these values are the ones that land.
    """
    environment = {
        "PATH": os.environ["PATH"],
        "DJANGO_SETTINGS_MODULE": settings_module,
        **dict(line.split("=", 1) for line in assignments()),
    }
    script = (
        "import os, django;"
        "django.setup();"
        "from django.conf import settings as s;"
        "print(os.path.abspath(s.SCRATCH_DIR))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(BASE_DIR / "scratch")
