
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
  `--batch` to drain the queue once and exit
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file
- `uv run ruff check . && uv run ruff format --check .` - lint and format check,
  run it before committing
- `npm install` - the Tailwind CLI, the only thing in `package.json`
- `npm run build:css` - compile `assets/css/app.css` into `static/css/app.css`
- `npm run watch:css` - the same build, rebuilding on every change, for
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
- Tailwind is configured CSS-first in `assets/css/app.css`; there is no
  `tailwind.config.js`. htmx and Alpine are vendored in `static/vendor/` at
  pinned versions, never loaded from a CDN. Node is a build-time tool only, so
  the app runs from an image without it.
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

