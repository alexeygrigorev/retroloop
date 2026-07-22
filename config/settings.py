"""Django settings, configured entirely from the environment.

Every value that differs between a laptop and production comes from an
environment variable: DATABASE_URL, SECRET_KEY, DEBUG, ALLOWED_HOSTS. See
.env.example for the development defaults.
"""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Seed os.environ from a KEY=value file, for development convenience.

    Real environment variables always win, so containers and CI are unaffected —
    they set variables directly and ship no .env file.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DEBUG", default=False)

# Only development may fall back to a throwaway key; production must supply one.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("SECRET_KEY must be set when DEBUG is off")
    SECRET_KEY = "django-insecure-development-only-key"

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Stores the background task queue in Postgres. Core Django ships only the
    # dummy and immediate backends for django.tasks, so the ORM backend that
    # gives us a real queue comes from django-tasks-db.
    "django_tasks_db",
    "accounts",
    "projects",
    "cycles",
    "retro",
    "meetings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get(
            "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/feedback"
        ),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Sessions live in Postgres, so there is no second piece of infrastructure to run.
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# The background queue lives in Postgres too, for the same reason: no Redis, no
# broker, nothing to run beside the database. `manage.py db_worker` consumes it;
# see the "Background tasks" section of AGENTS.md for the local command, the
# enqueue-after-commit convention, and why nothing is retried automatically.
TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.DatabaseBackend",
        "QUEUES": ["default"],
    }
}

# The project owns its user model, so later tables can carry a foreign key to a
# user that has a display name. There is no mail backend and no EMAIL_* setting:
# a forgotten password is reset by an admin with `manage.py changepassword`.
AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# The React island. `npm run build:js` writes a hashed bundle and the manifest
# naming it into static/board/, which is a static file directory like any other,
# so collectstatic collects them and the running application needs no Node. A
# path, not a URL and not an env var: it is where the build puts its output, the
# same kind of structural fact as STATIC_ROOT above. The template tag in
# retro/templatetags/vite.py is the only reader.
VITE_BUILD_SUBDIR = "board"
VITE_MANIFEST = BASE_DIR / "static" / VITE_BUILD_SUBDIR / "manifest.json"

# Uploads stream to disk instead of buffering in memory; the media pipeline
# hands these paths to the worker over a shared volume.
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
# The same intent for the rest of the request. A meeting upload is capped at
# 500 MB (`meetings.uploads.MAX_UPLOAD_BYTES`, and `client_max_body_size` in
# `deploy/nginx.conf`), and none of it is held in memory: the file streams to
# disk past FILE_UPLOAD_MAX_MEMORY_SIZE, and this says the non-file part of a
# request may not buffer more than the same megabyte. A pasted transcript is
# tens of kilobytes, so the cap it puts on pasted text is generous; what it
# rules out is a request that is large without being a file.
DATA_UPLOAD_MAX_MEMORY_SIZE = FILE_UPLOAD_MAX_MEMORY_SIZE
SCRATCH_DIR = Path(os.environ.get("SCRATCH_DIR", BASE_DIR / "scratch"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
