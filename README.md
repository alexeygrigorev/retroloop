# Weekly Team Feedback Tool

A Django app for running weekly Start/Stop/Continue feedback cycles and the
retrospectives that follow them.

## Requirements

- Python 3.14
- PostgreSQL 18
- [uv](https://docs.astral.sh/uv/) for dependency management

## Getting started

```bash
uv sync
cp .env.example .env
uv run manage.py migrate
uv run manage.py runserver
```

Configuration comes entirely from the environment — `DATABASE_URL`,
`SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS`. In development those are read from
`.env`; in production they are set directly and no `.env` file is shipped.
`SECRET_KEY` has no fallback when `DEBUG` is off: the app refuses to start
rather than run on a default key.

Docker Compose replaces the manual Postgres setup in task 2.

## Tests and linting

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

The suite uses `config.settings_test`, which supplies fixed environment values
and then imports the production settings unchanged, so tests need no local
configuration.

## Accounts

There is no email backend anywhere in this project — no verification, no
password reset. A user who forgets their password is reset by an administrator:

```bash
uv run manage.py changepassword <username>
```
