
Django app for weekly Start/Stop/Continue cycles and the retrospectives that
follow.

Documents

- `_docs/process.md` - how work is organized

Commands

- `docker compose up -d db` - Postgres, needed before anything touches the
  database
- `uv sync` - install dependencies
- `uv run manage.py runserver` - dev server
- `uv run manage.py migrate` - apply migrations
- `uv run manage.py db_worker` - background worker, without Compose. Add
  `--batch` to drain the queue once and exit. Under Compose the worker runs
  with `--no-reload`, so editing a task means `docker compose restart worker`;
  `compose.yaml` says why
- `uv run manage.py sweep_media` - collect the recordings a killed worker left
  behind. It never touches media a live worker holds, and never judges a file a
  record still names by age. `--min-age 0` removes the only guard on an upload
  that is still being written, so leave it alone outside a test
- `uv run pytest` - the whole suite. The tests that read a built asset build it
  first, once per session, so a fresh checkout does not have to remember the npm
  commands below; a build that does not produce its file fails the run, naming
  the command to rerun
- `uv run pytest tests/test_home.py` - one test file
- `uv run ruff check . && uv run ruff format --check .` - lint and format check,
  run it before committing
- `npm install` - the build-time toolchain: the Tailwind CLI and Vite
- `npm run build:css` - compile `assets/css/app.css` into `static/css/app.css`
- `npm run watch:css` - the same build, rebuilding on every change, for
  development
- `npm run build:js` - bundle the React island: `assets/js/board.jsx` into
  `static/board/`, hashed, with a manifest beside it
- `npm run watch:js` - the same build, rebuilding on every change, for
  development

Rules

- Postgres is the only infrastructure.
- Configuration comes from the environment. A new setting means a new env var
  and a line in `.env.example`, never a hardcoded value or a checked-in secret.
- Tests live in `tests/`. `config/settings_test.py` supplies their environment,
  so production settings stay strict.
- Dependencies are pinned exactly in `pyproject.toml`. Do not add one without
  asking.
- Templates extend `templates/base_app.html`, which is `base.html` plus the
  account controls in the navigation bar.
- Styling: a template that renders a whole page extends `base_app.html`.
  Buttons, links, headings, panels and form fields use the named classes from
  `assets/css/app.css` - `.btn-primary`, `.btn-secondary`, `.link`,
  `.page-heading`, `.panel`, `.form-fields` - rather than a fresh class string.
  A form renders `{{ form.as_div }}` inside an element with `class="form-fields"`
  and needs nothing else to be styled, never `{{ form.as_p }}` and never a class
  attribute set in Python. A new colour is added to the `@theme` block, never
  written inline. `assets/css/app.css` is the one file to open to see what
  already exists; there is no second document to keep in sync.
- URLs in a template: the plain `{% url 'name' arg %}` tag, written straight
  into the attribute, and never the `{% url ... as var %}` form. `as var`
  swallows the `NoReverseMatch` a wrong or renamed route name raises and leaves
  the variable empty, so the page comes back 200 with `hx-get=""` and a button
  that silently does nothing - #62, found by breaking a route name and watching
  the suite stay green. The plain tag raises instead, which is the whole point.
  A partial is rendered on its own by the sweeps in `tests/template_render.py`
  as well as by its view, so a tag inside one still has a card or a cycle to
  reverse against: both scenes are built in that one module, and a new partial
  adds whatever it needs to them there. Never reach for `as var` to work round
  a context a test does not supply. A URL that cannot come from `{% url %}` -
  one a view computed - is passed in as context, and the template is then
  responsible for nothing. `tests/test_template_urls.py` enforces it by walking
  `templates/`: every `{% url %}` name must be a route that exists and take the
  arguments the tag passes, and no rendered page or fragment may emit an empty
  `href`, `action` or `hx-*` attribute.
- Tailwind is configured CSS-first in `assets/css/app.css`; there is no
  `tailwind.config.js`. htmx and Alpine are vendored in `static/vendor/` at
  pinned versions, never loaded from a CDN. Node is a build-time tool only, so
  the app runs from an image without it.
- The React island: one entry point, `assets/js/board.jsx`, one mount, the
  `#retro-board` element on the retrospective detail page, and no React
  anywhere else - every other screen is a Django template with HTMX. Its
  initial state crosses into it as `{{ ... |json_script }}` and carries the
  viewer's own data only; nothing a member may not see goes into the page.
  A template loads the bundle with `{% vite_bundle "assets/js/board.jsx" %}`,
  which reads the manifest and renders the hashed filename, and raises naming
  `npm run build:js` when the build is missing. npm dependencies follow the
  Python rule: pinned exactly, and ask first.
- Commit regularly.

Background tasks

Work that must not block a request goes on the queue. It is a Postgres table,
not a broker: `django.tasks` with the `django-tasks-db` ORM backend, drained by
`manage.py db_worker`. Tasks live in `config/tasks.py`.

- A task is a module-level function decorated with `@task`, taking only
  arguments that survive JSON - an id, a path, a flag. Never a model instance.
  The body re-fetches what it needs by id and tolerates the row having changed,
  or gone, since the enqueue.
- Enqueue with `enqueue_on_commit(some_task, id)` from `config.tasks`, not with
  `some_task.enqueue(id)`, whenever the call could be inside an `atomic` block.
  The worker is another process on another connection: a job queued by the same
  transaction as the rows it reads can be claimed before that transaction
  commits, or survive a rollback that threw the work away. Outside a
  transaction the helper enqueues immediately, so it is never the wrong call.
- Nothing is retried automatically and no backoff is configured. A task that
  raises is marked FAILED with its traceback, the worker logs it and takes the
  next job, and that is where it stops. Re-running one is a deliberate act.
  This is deliberate: the media pipeline deletes its source recording in a
  `finally` block (`_docs/decisions.md`, item 6), so a retry would run against a
  file that is gone. A task that wants a second attempt arranges it in its own
  body, where it can say what it is safe to retry against.
- The suite runs on the immediate backend, so task bodies execute inline and no
  worker has to be running for a test. A plain `django_db` test never commits,
  so work enqueued on commit does not run in one: wrap it in the
  `django_capture_on_commit_callbacks(execute=True)` fixture, or ask for
  `django_db(transaction=True)` when the test needs the real queue table.


CI

`.github/workflows/ci.yml` runs on every push to any branch, on pull requests
targeting `main`, and on demand. One job: `uv sync --locked`, both ruff checks,
`makemigrations --check --dry-run`, `npm ci`, `npm run build:css` and
`npm run build:js`, `collectstatic`, then the whole suite against a
`postgres:18` service container. It builds both assets and installs ffmpeg
itself, so nothing the suite needs is assumed to be there.

- Both builds are checked, not trusted: a step after each one asserts the output
  exists and fails naming the command that should have written it. The bundle's
  path comes from `config/settings.py`, so it is the file the template tag
  reads, and `collectstatic` then proves Django's finders see both.

- A skipped test fails the build. The suite writes a JUnit report and a step
  after it fails the job when any test skipped, printing the node id and the
  reason. A skip is how a silent opt-out - all of `tests/test_audio.py` without
  ffmpeg - would otherwise leave a broken run reading green. The tests that read
  a built asset used to opt out the same way; they now build it and fail naming
  the command instead, and skip only where no npm exists to build it with (#54).
  A test that genuinely has to skip changes that gate in the same commit.
- A test removed from collection fails the build too. A file that is never
  collected writes no result at all, so the skip gate above cannot see it - an
  `--ignore=` in `addopts` once produced a fully green run with the whole media
  pipeline gone. So the number of tests that ran is checked against
  `EXPECTED_TESTS` in `.github/workflows/ci.yml`, and it is an exact count, not
  a floor: a run with more tests than that is red as well as a run with fewer.
  **Every branch that adds or removes a test changes that one line, in the same
  commit.** The failure prints the line to paste, with the number already in it.
  A floor was tried first (#67) and went stale twice in a day, because the run
  that asks for it to be raised is green and the run that tempts you to lower it
  is red.
- The number cannot be set to whatever suits a build.
  `tests/test_ci_workflow.py` collects the suite with pytest and asserts
  `EXPECTED_TESTS` equals what collection produces, so a wrong number fails the
  suite one step before the gate.
- The *set* of test files is pinned too, in `TEST_FILES` in
  `.github/workflows/ci.yml` - the sorted list of `tests/test_*.py` basenames.
  **Every branch that adds, removes or renames a test file changes that one
  line, in the same commit.** A count alone cannot see a file swapped for
  parametrized cases elsewhere, or renamed off the `test_*.py` convention, or
  `git rm`'d: the total stays put. So a final job step re-collects the suite
  itself and fails if the files on disk are not exactly `TEST_FILES`, if any
  listed file collected nothing (an `--ignore` or a `collect_ignore`), if a
  `pytest_collection_modifyitems` hook deselected anything (it counts items
  before and after the hook), or if the count is not `EXPECTED_TESTS`. It is a
  job step and not a test on purpose: the suite's own guards live in a collected
  file, and this one still fires when that file is the one dropped.
- A newer push to the same branch cancels the run it supersedes, so the run
  worth reading is always the one for the tip commit.
- Reproduce a CI run locally with one command:
  `uv sync --locked && npm ci && npm run build:css && npm run build:js && uv run ruff check . && uv run ruff format --check . && uv run manage.py makemigrations --check --dry-run && uv run pytest -rs`
  The `-rs` is the point: it lists every skip, which CI turns into a failure.
  `npm run build:css` and `npm run build:js` are part of it because CI builds
  both assets as steps of its own and checks what they wrote, before the suite
  runs at all - it does not lean on the suite to build them.
