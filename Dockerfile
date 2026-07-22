# syntax=docker/dockerfile:1

FROM python:3.14-slim

# Debian trixie only packages ffmpeg 7.1, so the 8.1 binaries come from the
# static build image. The media pipeline needs ffprobe as well as ffmpeg.
COPY --from=mwader/static-ffmpeg:8.1 /ffmpeg /ffprobe /usr/local/bin/
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /usr/local/bin/

# The virtualenv lives outside /app because Compose bind-mounts the working
# tree there for development, which would otherwise hide it.
# UV_FROZEN keeps `uv run` inside the container working from the committed lock
# file instead of re-resolving against PyPI on every invocation.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependencies resolve from the committed lock file, before the application
# code is copied, so editing a .py file does not reinstall packages.
COPY pyproject.toml uv.lock ./
RUN UV_COMPILE_BYTECODE=1 uv sync --frozen --no-install-project

COPY . .

EXPOSE 8000

# Production default. compose.yaml overrides this with runserver, which also
# serves static files.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
