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

The rendering used to be done with an empty context. It is now done with the
two scenes in `tests/template_render.py`, and the reason is issue #62: an empty
context is why the templates reversed their URLs with
`{% url 'card-show' card.pk as show_url %}`, which swallows a NoReverseMatch and
renders an empty attribute. The plain tag needs a card to reverse against, so
one is supplied — in that module, once, for this sweep and the URL sweep both.

The sweep did not shrink in the trade. It still walks `templates/` and still
renders every template and every `{% partialdef %}`; each is now rendered twice,
once in a scene where every control is permitted and once in a scene where none
is, so the `{% else %}` half of the tree is rendered too, which the empty
context never reached.
"""

import re

import pytest

from tests.template_render import SCENES, TEMPLATES_DIR, render, template_names

#: A `{# ... #}` whose opening and closing braces are on different lines.
MULTILINE_COMMENT = re.compile(r"{#(?:[^#]|#(?!}))*?\n(?:[^#]|#(?!}))*?#}")

TEMPLATE_NAMES = template_names()


def test_the_template_tree_is_actually_discovered() -> None:
    """Guards the parametrization: an empty list would make every case vacuous."""
    assert TEMPLATES_DIR.is_dir()
    assert len(TEMPLATE_NAMES) >= 7
    assert "base.html" in TEMPLATE_NAMES
    assert "home.html" in TEMPLATE_NAMES
    assert "home.html#frontend_check" in TEMPLATE_NAMES
    assert "cycles/card_list.html#card_edit_form" in TEMPLATE_NAMES


@pytest.mark.django_db
@pytest.mark.parametrize("scene_name", SCENES)
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_renders_a_comment_as_text(name: str, scene_name: str) -> None:
    rendered = render(name, scene_name)

    assert rendered.strip(), f"{name} rendered nothing, so the check below proves nothing"
    leaked = [line for line in rendered.splitlines() if "{#" in line or "#}" in line]
    assert leaked == [], (
        f"{name} leaked comment source into its output in the {scene_name} scene. "
        f"A `{{# #}}` comment spanning more than one line is not a comment — use "
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
