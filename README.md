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
npm install
npm run build:css
uv run manage.py migrate
uv run manage.py runserver
```

Configuration comes entirely from the environment — `DATABASE_URL`,
`SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS`. In development those are read from
`.env`; in production they are set directly and no `.env` file is shipped.
`SECRET_KEY` has no fallback when `DEBUG` is off: the app refuses to start
rather than run on a default key.

## Frontend assets

Assets are built on the host, not in a container. Node is a build-time tool
only: no image in this project ships a Node runtime, and the app serves nothing
but the files the build leaves behind.

```bash
npm install          # Tailwind CLI, the only thing in package.json
npm run build:css    # assets/css/app.css -> static/css/app.css
npm run watch:css    # the same, rebuilding as you edit templates
```

`static/css/app.css` is generated and git-ignored, so build it once after
cloning and after pulling template changes. Tailwind 4 is configured CSS-first
inside `assets/css/app.css` — there is no `tailwind.config.js`.

htmx and Alpine are committed under `static/vendor/` at pinned versions and
served from this project's own domain. Nothing on a page reaches a CDN.

Compose bind-mounts the working tree into the container, so a stylesheet built
on the host is picked up there without a rebuild.

## Docker Compose

Compose replaces the manual Postgres setup described in "Getting started" — on
a machine with only Docker installed, it brings up the database, the app, and
the background worker:

```bash
docker compose up
docker compose run --rm web uv run manage.py migrate
docker compose run --rm web uv run pytest
```

The app is served at `http://localhost:8000/`. Migrations never run
automatically on container start, so `migrate` is an explicit command. The
`db` service also publishes port 5432, so `uv run pytest` on the host works
against the same database.

Database rows live in a named volume: `docker compose down` keeps them and
`docker compose down -v` discards them. The `worker` service is a placeholder
that idles until the task backend arrives.

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
