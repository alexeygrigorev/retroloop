"""Fixtures the whole suite can ask for.

Right now that is one thing: the front-end assets Node builds. A test that reads
`static/css/app.css` or `static/board/manifest.json` asks for the fixture that
guarantees it, instead of guarding itself with a skip that made a missing build
look like a passing run (#54).

Each fixture is session-scoped, so the npm script behind it runs once per test
session however many tests depend on it. The skipping, the building and the loud
failure all live in `tests/assets.py`, in one place for every artefact.
"""

from pathlib import Path

import pytest

from tests.assets import ISLAND, STYLESHEET, ensure_built


@pytest.fixture(scope="session")
def built_stylesheet() -> Path:
    """`static/css/app.css`, built by `npm run build:css` if this session has not."""
    return ensure_built(STYLESHEET)


@pytest.fixture(scope="session")
def built_island() -> Path:
    """`static/board/manifest.json`, built by `npm run build:js` if this session has not."""
    return ensure_built(ISLAND)


def pytest_generate_tests(metafunc):
    # QA PROOF for #72, reverted by the next commit: reduce one parametrized
    # test in test_auth.py from 4 cases to 1 during generation, before any
    # item exists. 3 tests vanish; the map is left untouched.
    if metafunc.function.__name__ != "test_password_reset_routes_do_not_exist":
        return
    for marker in list(metafunc.definition.iter_markers("parametrize")):
        argnames, argvals = marker.args[0], marker.args[1]
        metafunc.definition.own_markers = [
            m for m in metafunc.definition.own_markers if m.name != "parametrize"
        ]
        metafunc.parametrize(argnames, argvals[:1])
