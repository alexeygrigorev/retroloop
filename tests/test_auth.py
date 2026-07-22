"""Signup, login, logout, and the deliberate absence of email.

Every test here maps to an acceptance criterion of issue #4. The recurring
assertion is negative: nothing in the flow may ask for, store, or send to an
email address.
"""

import builtins
import getpass
import io
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import NoReverseMatch, reverse

User = get_user_model()

SIGNUP_URL = "/accounts/signup/"
LOGIN_URL = "/accounts/login/"
LOGOUT_URL = "/accounts/logout/"
PASSWORD_CHANGE_URL = "/accounts/password_change/"
HOME_URL = "/"

# Long enough, not common, not numeric, not similar to any username used here.
PASSWORD = "keel-haul-mizzen-41"


def signup_payload(username="alexey", display_name="Alexey G", password=PASSWORD, **overrides):
    data = {
        "username": username,
        "display_name": display_name,
        "password1": password,
        "password2": password,
    }
    data.update(overrides)
    return data


def is_logged_in(client: Client) -> bool:
    return "_auth_user_id" in client.session


# --------------------------------------------------------------------------
# Signup
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_signup_page_shows_four_fields_and_no_email_field(client: Client) -> None:
    response = client.get(SIGNUP_URL)

    assert response.status_code == 200
    assert list(response.context["form"].fields) == [
        "username",
        "display_name",
        "password1",
        "password2",
    ]
    assert b"email" not in response.content.lower()


@pytest.mark.django_db
def test_signup_creates_the_user_logs_them_in_and_redirects_home(client: Client) -> None:
    response = client.post(SIGNUP_URL, signup_payload())

    assert response.status_code == 302
    assert response["Location"] == HOME_URL

    user = User.objects.get(username="alexey")
    assert user.display_name == "Alexey G"
    assert user.check_password(PASSWORD)
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
def test_signup_stores_no_email_address(client: Client) -> None:
    client.post(SIGNUP_URL, signup_payload())

    assert User.objects.get(username="alexey").email == ""


@pytest.mark.django_db
def test_signup_rejects_a_username_already_taken(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(SIGNUP_URL, signup_payload(username="alexey"))

    assert response.status_code == 200
    assert "username" in response.context["form"].errors
    assert b"A user with that username already exists." in response.content
    assert User.objects.filter(username="alexey").count() == 1
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_signup_rejects_a_username_differing_only_by_case(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(SIGNUP_URL, signup_payload(username="Alexey"))

    assert response.status_code == 200
    assert "username" in response.context["form"].errors
    assert b"A user with that username already exists." in response.content
    assert not User.objects.filter(username="Alexey").exists()
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_case_insensitive_uniqueness_is_enforced_by_the_database() -> None:
    """The form check is a nicety; the constraint is what makes it true."""
    from django.db import IntegrityError, transaction

    User.objects.create_user(username="alexey", password=PASSWORD)

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user(username="ALEXEY", password=PASSWORD)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("shortpw", "Ab3!x", "at least 8 characters"),
        ("commonpw", "password", "too common"),
        ("numericpw", "91740283561", "entirely numeric"),
        ("marianne", "marianne99", "too similar to the username"),
    ],
    ids=["too-short", "too-common", "all-numeric", "too-similar"],
)
def test_signup_rejects_a_password_the_validators_refuse(
    client: Client, username: str, password: str, message: str
) -> None:
    response = client.post(SIGNUP_URL, signup_payload(username=username, password=password))

    assert response.status_code == 200
    assert message.encode() in response.content
    assert not User.objects.filter(username=username).exists()
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_signup_rejects_mismatched_password_confirmation(client: Client) -> None:
    response = client.post(SIGNUP_URL, signup_payload(password2="a-completely-different-one-77"))

    assert response.status_code == 200
    assert "password2" in response.context["form"].errors
    assert not User.objects.exists()
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_signup_requires_a_display_name(client: Client) -> None:
    response = client.post(SIGNUP_URL, signup_payload(display_name=""))

    assert response.status_code == 200
    assert "display_name" in response.context["form"].errors
    assert not User.objects.exists()


@pytest.mark.django_db
def test_two_members_may_share_a_display_name(client: Client) -> None:
    client.post(SIGNUP_URL, signup_payload(username="alex.one", display_name="Alex"))

    other = Client()
    response = other.post(SIGNUP_URL, signup_payload(username="alex.two", display_name="Alex"))

    assert response.status_code == 302
    assert User.objects.filter(display_name="Alex").count() == 2


@pytest.mark.django_db
def test_signup_redirects_a_logged_in_visitor_home(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)
    client.login(username="alexey", password=PASSWORD)

    response = client.get(SIGNUP_URL)

    assert response.status_code == 302
    assert response["Location"] == HOME_URL


# --------------------------------------------------------------------------
# Login and logout
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_login_page_shows_username_and_password(client: Client) -> None:
    response = client.get(LOGIN_URL)

    assert response.status_code == 200
    assert list(response.context["form"].fields) == ["username", "password"]


@pytest.mark.django_db
def test_login_with_wrong_credentials_creates_no_session(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(LOGIN_URL, {"username": "alexey", "password": "not-it-at-all"})

    assert response.status_code == 200
    assert response.context["form"].errors
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_login_lands_on_home(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(LOGIN_URL, {"username": "alexey", "password": PASSWORD})

    assert response.status_code == 302
    assert response["Location"] == HOME_URL
    assert is_logged_in(client)


@pytest.mark.django_db
def test_login_honours_a_next_path_on_this_site(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(
        f"{LOGIN_URL}?next=/accounts/password_change/",
        {"username": "alexey", "password": PASSWORD},
    )

    assert response.status_code == 302
    assert response["Location"] == PASSWORD_CHANGE_URL


@pytest.mark.django_db
def test_login_ignores_a_next_pointing_off_site(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)

    response = client.post(
        f"{LOGIN_URL}?next=https://example.com/phish",
        {"username": "alexey", "password": PASSWORD},
    )

    assert response.status_code == 302
    assert response["Location"] == HOME_URL


@pytest.mark.django_db
def test_logout_needs_a_post_with_a_csrf_token() -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)
    client = Client(enforce_csrf_checks=True)
    client.login(username="alexey", password=PASSWORD)

    home = client.get(HOME_URL)
    assert b'name="csrfmiddlewaretoken"' in home.content

    without_token = client.post(LOGOUT_URL)
    assert without_token.status_code == 403
    assert is_logged_in(client)

    response = client.post(LOGOUT_URL, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})

    assert response.status_code == 302
    assert response["Location"] == HOME_URL
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_get_on_the_logout_url_does_not_end_the_session(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)
    client.login(username="alexey", password=PASSWORD)

    response = client.get(LOGOUT_URL)

    assert response.status_code == 405
    assert is_logged_in(client)


@pytest.mark.django_db
def test_after_logout_the_visitor_is_anonymous_on_home(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD)
    client.login(username="alexey", password=PASSWORD)

    response = client.post(LOGOUT_URL, follow=True)

    assert response.redirect_chain[-1] == (HOME_URL, 302)
    assert response.status_code == 200
    assert not response.context["user"].is_authenticated


# --------------------------------------------------------------------------
# No email anywhere
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url",
    [
        "/accounts/password_reset/",
        "/accounts/password_reset/done/",
        "/accounts/reset/MQ/set-password/",
        "/accounts/reset/done/",
    ],
)
def test_password_reset_routes_do_not_exist(client: Client, url: str) -> None:
    assert client.get(url).status_code == 404


def test_password_reset_view_names_are_not_registered() -> None:
    for name in ("password_reset", "password_reset_done", "password_reset_confirm"):
        with pytest.raises(NoReverseMatch):
            reverse(name)


def test_no_email_setting_is_defined_in_config() -> None:
    config_dir = Path(settings.BASE_DIR) / "config"
    offenders = [
        path.name
        for path in sorted(config_dir.rglob("*.py"))
        if re.search(r"^\s*EMAIL_[A-Z0-9_]*\s*=", path.read_text(), flags=re.MULTILINE)
    ]

    assert offenders == []


def test_env_example_gains_no_new_variable() -> None:
    """No mail variable, and every other one is here on purpose.

    Decision 8 is what this guards: `EMAIL_HOST`, `EMAIL_BACKEND` and the rest
    of that family must never appear, because there is no mail backend and
    there is not going to be one.

    The set is exact rather than a search for `EMAIL_`, so a variable added for
    any other reason still has to be written down here in the same commit.
    `OPENAI_API_KEY` is one that has been: #21 sends the meeting audio for
    transcription and AGENTS.md requires the credential to come from the
    environment with a line in this file. `SCRATCH_DIR` is the other: #56 wrote
    down the setting the media pipeline had been reading without an example
    line. Both were added by extending this set, never by relaxing it into a
    subset or superset check - the point of the test is that a new name cannot
    arrive unnoticed, and a `names <= expected` or `expected <= names` test
    would let exactly that happen.
    """
    text = (Path(settings.BASE_DIR) / ".env.example").read_text()
    names = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }

    assert names == {
        "DEBUG",
        "SECRET_KEY",
        "ALLOWED_HOSTS",
        "DATABASE_URL",
        "SCRATCH_DIR",
        "OPENAI_API_KEY",
    }
    assert not any(name.startswith("EMAIL") for name in names)


@pytest.mark.django_db
def test_createsuperuser_never_asks_for_an_email_address(monkeypatch) -> None:
    """Drive the real interactive command and record every prompt it prints."""
    prompts: list[str] = []

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "cli-admin"

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return PASSWORD

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(getpass, "getpass", fake_getpass)

    call_command("createsuperuser", stdin=Tty(), stdout=io.StringIO(), stderr=io.StringIO())

    assert not any("email" in prompt.lower() for prompt in prompts)
    admin = User.objects.get(username="cli-admin")
    assert admin.is_superuser
    assert admin.email == ""
    assert admin.display_name == ""


@pytest.mark.django_db
def test_password_change_works_with_the_current_password(client: Client) -> None:
    user = User.objects.create_user(username="alexey", password=PASSWORD)
    client.login(username="alexey", password=PASSWORD)
    new_password = "trawler-lantern-63"

    assert client.get(PASSWORD_CHANGE_URL).status_code == 200

    response = client.post(
        PASSWORD_CHANGE_URL,
        {
            "old_password": PASSWORD,
            "new_password1": new_password,
            "new_password2": new_password,
        },
    )

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password(new_password)


@pytest.mark.django_db
def test_password_change_rejects_a_wrong_current_password(client: Client) -> None:
    user = User.objects.create_user(username="alexey", password=PASSWORD)
    client.login(username="alexey", password=PASSWORD)

    response = client.post(
        PASSWORD_CHANGE_URL,
        {
            "old_password": "never-was-the-password",
            "new_password1": "trawler-lantern-63",
            "new_password2": "trawler-lantern-63",
        },
    )

    assert response.status_code == 200
    assert "old_password" in response.context["form"].errors
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


@pytest.mark.django_db
def test_password_change_redirects_an_anonymous_visitor_to_login(client: Client) -> None:
    response = client.get(PASSWORD_CHANGE_URL)

    assert response.status_code == 302
    assert response["Location"].startswith(LOGIN_URL)


def test_readme_documents_the_admin_password_reset() -> None:
    readme = (Path(settings.BASE_DIR) / "README.md").read_text()

    assert "manage.py changepassword <username>" in readme


# --------------------------------------------------------------------------
# User model
# --------------------------------------------------------------------------


def test_auth_user_model_is_the_projects_own() -> None:
    assert settings.AUTH_USER_MODEL == "accounts.User"
    assert User._meta.label == "accounts.User"


@pytest.mark.django_db
def test_no_migrations_are_missing() -> None:
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db
def test_users_are_listed_and_editable_in_the_admin(client: Client) -> None:
    admin = User.objects.create_superuser(username="root", password=PASSWORD)
    admin.display_name = "Root"
    admin.save()
    member = User.objects.create_user(username="member", password=PASSWORD, display_name="Old Name")
    client.login(username="root", password=PASSWORD)

    changelist = client.get("/admin/accounts/user/")
    assert changelist.status_code == 200
    assert b"member" in changelist.content

    change_url = f"/admin/accounts/user/{member.pk}/change/"
    assert client.get(change_url).status_code == 200

    response = client.post(
        change_url,
        {
            "username": "member",
            "display_name": "New Name",
            "first_name": "",
            "last_name": "",
            "is_active": "on",
            "date_joined_0": member.date_joined.strftime("%Y-%m-%d"),
            "date_joined_1": member.date_joined.strftime("%H:%M:%S"),
            "last_login_0": "",
            "last_login_1": "",
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    member.refresh_from_db()
    assert member.display_name == "New Name"


@pytest.mark.django_db
def test_users_can_be_added_from_the_admin(client: Client) -> None:
    """The add form is customised to carry `display_name`, so exercise it."""
    User.objects.create_superuser(username="root", password=PASSWORD)
    client.login(username="root", password=PASSWORD)

    assert client.get("/admin/accounts/user/add/").status_code == 200

    response = client.post(
        "/admin/accounts/user/add/",
        {
            "username": "recruit",
            "display_name": "Recruit",
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    recruit = User.objects.get(username="recruit")
    assert recruit.display_name == "Recruit"
    assert recruit.check_password(PASSWORD)


@pytest.mark.django_db
def test_admin_never_offers_an_email_field() -> None:
    from django.contrib import admin as django_admin

    user_admin = django_admin.site._registry[User]
    fields = {
        field
        for fieldset in (*user_admin.fieldsets, *user_admin.add_fieldsets)
        for field in fieldset[1]["fields"]
    }

    assert "email" not in fields


@pytest.mark.django_db
def test_a_user_is_displayed_by_display_name_falling_back_to_username() -> None:
    named = User.objects.create_user(username="alexey", password=PASSWORD, display_name="Alexey G")
    unnamed = User.objects.create_user(username="root", password=PASSWORD)

    assert str(named) == "Alexey G"
    assert str(unnamed) == "root"


@pytest.mark.django_db
def test_home_shows_the_display_name_and_falls_back_to_the_username(client: Client) -> None:
    User.objects.create_user(username="alexey", password=PASSWORD, display_name="Alexey G")
    client.login(username="alexey", password=PASSWORD)

    assert b"Alexey G" in client.get(HOME_URL).content

    client.logout()
    User.objects.create_user(username="rootuser", password=PASSWORD)
    client.login(username="rootuser", password=PASSWORD)

    assert b"rootuser" in client.get(HOME_URL).content
