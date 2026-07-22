"""Resolve the React island's bundle through the Vite manifest.

The bundle's filename carries a content hash, so it changes on every build and
no template may name it. A template names the entry point it wants — the source
path Vite was given — and this tag looks up what the build called it.

Two behaviours are deliberate:

- the manifest is read once and kept when `DEBUG` is off, and re-read on every
  render when it is on, so a rebuild during development is picked up without a
  restart while production does no file I/O per request;
- a missing or unreadable manifest raises, naming the command that fixes it. It
  never renders a `<script>` with an empty or guessed `src`: a page that quietly
  loads nothing is a bug that reaches production, and a page that refuses to
  render is a bug that reaches the person who forgot to run the build.
"""

import json
from pathlib import Path

from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()

#: Named in every failure message, because the fix is always this.
BUILD_COMMAND = "npm run build:js"


class ManifestError(RuntimeError):
    """The Vite manifest is missing, unreadable, or does not know the entry."""


#: Parsed manifests by path, populated only when DEBUG is off. Keyed by path so
#: a test that points the setting somewhere else never reads this one's answer.
_manifests: dict[Path, dict] = {}


def load_manifest() -> dict:
    """The parsed manifest, cached when `DEBUG` is off."""
    path = Path(settings.VITE_MANIFEST)
    if not settings.DEBUG and path in _manifests:
        return _manifests[path]

    try:
        manifest = json.loads(path.read_text())
    except OSError as missing:
        raise ManifestError(
            f"The Vite manifest {path} is missing or unreadable ({missing}). "
            f"Run `{BUILD_COMMAND}` to build the React bundle."
        ) from missing
    except json.JSONDecodeError as unparseable:
        raise ManifestError(
            f"The Vite manifest {path} is not valid JSON ({unparseable}). "
            f"Run `{BUILD_COMMAND}` to rebuild it."
        ) from unparseable

    if not settings.DEBUG:
        _manifests[path] = manifest
    return manifest


def bundle_url(entry: str) -> str:
    """The static URL of the built file for `entry`, hash and all."""
    manifest = load_manifest()
    chunk = manifest.get(entry) if isinstance(manifest, dict) else None
    file = chunk.get("file") if isinstance(chunk, dict) else None
    if not file:
        raise ManifestError(
            f"The Vite manifest {settings.VITE_MANIFEST} has no built file for {entry!r}. "
            f"Run `{BUILD_COMMAND}` after checking the entry points in vite.config.js."
        )
    return static(f"{settings.VITE_BUILD_SUBDIR}/{file}")


@register.simple_tag
def vite_bundle(entry: str) -> str:
    """Render the `<script>` tag that loads `entry`'s built bundle.

    The whole tag, not just the URL: a template that wrote the `src` itself
    would be one edit away from naming a hashed file directly.
    """
    return format_html('<script type="module" src="{}"></script>', bundle_url(entry))
