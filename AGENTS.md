# Weekly Team Feedback Tool

Django app for weekly Start/Stop/Continue cycles and the retrospectives that
follow. `_docs/plan.md` is the scope, `_docs/architecture.md` the design,
`_docs/tasks.md` the build order — read the relevant task before starting it.

## Commands

- `uv sync` - install dependencies
- `uv run manage.py runserver` - dev server
- `uv run manage.py migrate` - apply migrations
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file
- `uv run ruff check . && uv run ruff format --check .` - lint and format check,
  run it before committing

## Rules

- All authorization lives in `projects/permissions.py` as predicate functions
  taking a user and a domain object, never as inline `if request.user ==` checks
  in views
- If it reaches the browser it has leaked: filter what a viewer may not see in
  the queryset or serializer, never in the template or the client. Vote totals
  before DISCUSS and other members' cards before REVEAL are the two that matter.
- Anonymous authorship is destroyed at REVEAL, not hidden — `Card.author` is
  nulled and positions are shuffled, in the same transaction as the stage write.
  This is irreversible by design; do not add anything that reconstructs the link.
- Stage changes go through `advance_stage()` only. Forward-only,
  facilitator-only, each transition's side effects in the same transaction.
- Every board mutation bumps `Retrospective.version` inside its transaction and
  returns the full board state. Polling on that counter is the whole sync
  mechanism — no WebSockets, no diffs.
- Postgres is the only infrastructure. No Redis, no Celery, no object store, no
  mail backend. Background jobs are `django.tasks` with the `django-tasks-db`
  backend.
- There is no email anywhere: no verification, no password reset. A forgotten
  password is reset with `manage.py changepassword`.
- Recordings are transient. The scratch file is deleted in a `finally`, whether
  or not transcription succeeded, and `temp_path` is nulled.
- `ai/` holds no models and no views — functions that take domain objects and
  return plain dicts, so every OpenAI call is mockable. AI output always lands as
  DRAFT and is never authoritative until a facilitator confirms it.
- React is the retro board island only. Every other screen is Django templates
  with HTMX.
- Configuration comes from the environment. A new setting means a new env var
  plus an entry in `.env.example`, never a hardcoded value or a checked-in secret.
- Tests live in `tests/`, named after what they cover. `config/settings_test.py`
  supplies the test environment — production settings stay strict.
- Dependencies are pinned exactly in `pyproject.toml`; run `uv sync` so
  `uv.lock` moves with it. Do not add a dependency without asking.
- Do not edit `_docs/` as a side effect of building something. Changing the
  design is its own deliberate change.
- Commit regularly.
