"""No template may leak the source of a `{# #}` comment into the page.

Django's lexer matches `{#.*?#}` without `re.DOTALL`, so a comment that spans
more than one line is never tokenized as a comment: its source is emitted as
literal text, on every page that inherits the template. A comment needing more
than one line has to use `{% comment %}`/`{% endcomment %}`, which does span
lines.

The check is done by rendering, not by reading the source, because rendering is
what the defect was invisible to. Every template under `templates/` is rendered
on its own — not only the pages a view happens to serve — so a leak inside a
block that some child overrides is still caught, and so is a leak in a template
partial.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.template.loader import get_template
from django.test import RequestFactory

BASE_DIR = Path(settings.BASE_DIR)
TEMPLATES_DIR = BASE_DIR / "templates"

#: A `{# ... #}` whose opening and closing braces are on different lines.
MULTILINE_COMMENT = re.compile(r"{#(?:[^#]|#(?!}))*?\n(?:[^#]|#(?!}))*?#}")

PARTIALDEF = re.compile(r"{%\s*partialdef\s+([\w-]+)")


def _template_names() -> list[str]:
    """Every template in `templates/`, plus every partial defined inside one."""
    names = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        name = path.relative_to(TEMPLATES_DIR).as_posix()
        names.append(name)
        names += [f"{name}#{partial}" for partial in PARTIALDEF.findall(path.read_text())]
    return names


TEMPLATE_NAMES = _template_names()


def _render(name: str) -> str:
    """Render `name` with enough of a request that context processors work.

    Missing context variables are not an error in Django's engine, so a page
    template renders without its view: the parts that matter here — the literal
    text between the tags — are the parts that do not depend on context.
    """
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    return get_template(name).render({}, request)


def test_the_template_tree_is_actually_discovered() -> None:
    """Guards the parametrization: an empty list would make every case vacuous."""
    assert TEMPLATES_DIR.is_dir()
    assert len(TEMPLATE_NAMES) >= 7
    assert "base.html" in TEMPLATE_NAMES
    assert "home.html" in TEMPLATE_NAMES
    assert "home.html#frontend_check" in TEMPLATE_NAMES


@pytest.mark.django_db
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_renders_a_comment_as_text(name: str) -> None:
    rendered = _render(name)

    assert rendered.strip(), f"{name} rendered nothing, so the check below proves nothing"
    leaked = [line for line in rendered.splitlines() if "{#" in line or "#}" in line]
    assert leaked == [], (
        f"{name} leaked comment source into its output. A `{{# #}}` comment "
        f"spanning more than one line is not a comment — use "
        f"`{{% comment %}}`/`{{% endcomment %}}`.\n" + "\n".join(leaked)
    )


@pytest.mark.parametrize("name", [n for n in TEMPLATE_NAMES if "#" not in n])
def test_no_template_source_holds_a_multi_line_hash_comment(name: str) -> None:
    """The same rule stated against the source, so it also covers unrendered branches."""
    source = (TEMPLATES_DIR / name).read_text()

    assert MULTILINE_COMMENT.search(source) is None, (
        f"{name} has a `{{# #}}` comment spanning more than one line; "
        f"use `{{% comment %}}`/`{{% endcomment %}}`."
    )
