
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
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file
- `uv run ruff check . && uv run ruff format --check .` - lint and format check,
  run it before committing

Rules

- Postgres is the only infrastructure.
- Configuration comes from the environment. A new setting means a new env var
  and a line in `.env.example`, never a hardcoded value or a checked-in secret.
- Tests live in `tests/`. `config/settings_test.py` supplies their environment,
  so production settings stay strict.
- Dependencies are pinned exactly in `pyproject.toml`. Do not add one without
  asking.
- Commit regularly.

