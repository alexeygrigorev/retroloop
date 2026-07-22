"""Projects, membership, and join links.

Every test here maps to an acceptance criterion of issue #5. The recurring
themes are that the view, never the template, is what enforces a rule, and that
a project reveals nothing at all to someone who is not on it.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from projects.models import Membership, Project

User = get_user_model()

LOGIN_URL = "/accounts/login/"
SIGNUP_URL = "/accounts/signup/"
PROJECT_LIST_URL = "/projects/"
PROJECT_CREATE_URL = "/projects/new/"

PASSWORD = "keel-haul-mizzen-41"


def make_user(username: str = "alexey", display_name: str = "Alexey G") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def make_project(owner: User, name: str = "Platform") -> Project:
    """A project as the create view builds one: owner plus facilitator membership."""
    project = Project.objects.create(name=name, owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


def join_url(project: Project) -> str:
    return reverse("join-project", args=[project.join_token])


def rotate_url(project: Project) -> str:
    return reverse("project-rotate-link", args=[project.pk])


def log_in(client: Client, user: User) -> None:
    client.login(username=user.username, password=PASSWORD)


@pytest.fixture
def owner(db) -> User:
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    return make_project(owner)


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Mel Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_project_generates_its_own_unique_join_token(owner: User) -> None:
    first = make_project(owner, "Platform")
    second = make_project(owner, "Payments")

    assert isinstance(first.join_token, uuid.UUID)
    assert first.join_token != second.join_token
    assert Project._meta.get_field("join_token").unique
    assert Project._meta.get_field("join_token").db_index


@pytest.mark.django_db
def test_a_project_records_its_owner_and_creation_time(owner: User) -> None:
    project = make_project(owner)

    assert project.owner == owner
    assert project.created_at is not None


@pytest.mark.django_db
def test_two_teams_may_both_have_a_project_called_platform(owner: User, outsider: User) -> None:
    make_project(owner, "Platform")
    make_project(outsider, "Platform")

    assert Project.objects.filter(name="Platform").count() == 2


@pytest.mark.django_db
def test_membership_roles_are_member_and_facilitator() -> None:
    assert [choice[0] for choice in Membership.Role.choices] == ["MEMBER", "FACILITATOR"]


@pytest.mark.django_db
def test_a_second_membership_for_the_same_person_is_refused_by_the_database(
    project: Project, member: User
) -> None:
    """The view avoids the duplicate; the constraint is what makes it impossible."""
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(project=project, user=member)


@pytest.mark.django_db
def test_the_same_person_may_be_in_two_projects(owner: User, member: User) -> None:
    other = make_project(owner, "Payments")
    Membership.objects.create(project=other, user=member)

    assert Membership.objects.filter(user=member).count() == 2


# --------------------------------------------------------------------------
# Anonymous visitors are sent to login and come back
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_project_view_sends_an_anonymous_visitor_to_login_and_back(
    client: Client, project: Project
) -> None:
    for url in (
        PROJECT_LIST_URL,
        PROJECT_CREATE_URL,
        project.get_absolute_url(),
        join_url(project),
    ):
        response = client.get(url)

        assert response.status_code == 302
        assert response["Location"] == f"{LOGIN_URL}?next={url}"


@pytest.mark.django_db
def test_an_anonymous_post_to_rotate_goes_to_login_and_changes_nothing(
    client: Client, project: Project
) -> None:
    before = project.join_token

    response = client.post(rotate_url(project))

    assert response.status_code == 302
    assert response["Location"] == f"{LOGIN_URL}?next={rotate_url(project)}"
    project.refresh_from_db()
    assert project.join_token == before


@pytest.mark.django_db
def test_after_logging_in_the_visitor_lands_on_the_page_they_asked_for(
    client: Client, project: Project, member: User
) -> None:
    first = client.get(project.get_absolute_url())

    response = client.post(
        first["Location"], {"username": member.username, "password": PASSWORD}, follow=True
    )

    assert response.redirect_chain[-1] == (project.get_absolute_url(), 302)
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_create_form_asks_only_for_a_name(client: Client, owner: User) -> None:
    log_in(client, owner)

    response = client.get(PROJECT_CREATE_URL)

    assert response.status_code == 200
    assert list(response.context["form"].fields) == ["name"]


@pytest.mark.django_db
def test_creating_a_project_also_makes_the_creator_a_facilitator(
    client: Client, owner: User
) -> None:
    log_in(client, owner)

    response = client.post(PROJECT_CREATE_URL, {"name": "Platform"})

    project = Project.objects.get(name="Platform")
    assert response.status_code == 302
    assert response["Location"] == project.get_absolute_url()
    assert project.owner == owner
    membership = Membership.objects.get(project=project, user=owner)
    assert membership.role == Membership.Role.FACILITATOR


@pytest.mark.django_db
def test_no_project_ends_up_without_its_owner_as_a_member(
    client: Client, owner: User, monkeypatch
) -> None:
    """The membership is created in the same transaction as the project.

    If the second write fails, the first is rolled back rather than leaving a
    project nobody — not even its owner — is a member of.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("membership write failed")

    monkeypatch.setattr(Membership.objects, "create", explode)
    log_in(client, owner)

    with pytest.raises(RuntimeError):
        client.post(PROJECT_CREATE_URL, {"name": "Platform"})

    assert not Project.objects.filter(name="Platform").exists()


@pytest.mark.django_db
def test_creating_a_project_requires_a_name(client: Client, owner: User) -> None:
    log_in(client, owner)

    response = client.post(PROJECT_CREATE_URL, {"name": ""})

    assert response.status_code == 200
    assert "name" in response.context["form"].errors
    assert not Project.objects.exists()


@pytest.mark.django_db
def test_the_creator_cannot_hand_the_project_to_someone_else(
    client: Client, owner: User, outsider: User
) -> None:
    """Owner and token come from the server, never from the posted form."""
    log_in(client, owner)

    client.post(
        PROJECT_CREATE_URL,
        {"name": "Platform", "owner": outsider.pk, "join_token": uuid.uuid4()},
    )

    project = Project.objects.get(name="Platform")
    assert project.owner == owner


# --------------------------------------------------------------------------
# Seeing
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_list_shows_only_projects_the_viewer_is_a_member_of(
    client: Client, owner: User, member: User, project: Project
) -> None:
    make_project(owner, "Payments")
    log_in(client, member)

    response = client.get(PROJECT_LIST_URL)
    body = response.content.decode()

    assert response.status_code == 200
    assert list(response.context["projects"]) == [project]
    assert "Platform" in body
    assert "Payments" not in body


@pytest.mark.django_db
def test_the_list_is_empty_for_someone_in_no_project(client: Client, outsider: User) -> None:
    log_in(client, outsider)

    response = client.get(PROJECT_LIST_URL)

    assert response.status_code == 200
    assert list(response.context["projects"]) == []


@pytest.mark.django_db
def test_the_detail_page_shows_the_name_the_members_and_the_join_link(
    client: Client, project: Project, owner: User, member: User
) -> None:
    log_in(client, member)

    response = client.get(project.get_absolute_url())
    body = response.content.decode()

    assert response.status_code == 200
    assert "Platform" in body
    assert "Olive Owner" in body
    assert "Facilitator" in body
    assert "Mel Member" in body
    assert "Member" in body
    assert join_url(project) in body


@pytest.mark.django_db
def test_a_logged_in_non_member_gets_404_and_no_hint_that_the_project_exists(
    client: Client, project: Project, outsider: User
) -> None:
    log_in(client, outsider)

    response = client.get(project.get_absolute_url())

    assert response.status_code == 404
    assert b"Platform" not in response.content


@pytest.mark.django_db
def test_a_project_id_that_never_existed_is_the_same_404(client: Client, outsider: User) -> None:
    log_in(client, outsider)

    assert client.get("/projects/99999/").status_code == 404


# --------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_opening_a_join_link_makes_the_visitor_a_member(
    client: Client, project: Project, outsider: User
) -> None:
    log_in(client, outsider)

    response = client.get(join_url(project), follow=True)

    assert response.redirect_chain == [(project.get_absolute_url(), 302)]
    assert response.status_code == 200
    membership = Membership.objects.get(project=project, user=outsider)
    assert membership.role == Membership.Role.MEMBER
    assert "joined" in response.content.decode().lower()


@pytest.mark.django_db
def test_joining_never_grants_facilitator(client: Client, project: Project, outsider: User) -> None:
    log_in(client, outsider)

    client.get(join_url(project))

    assert Membership.objects.get(project=project, user=outsider).role == "MEMBER"
    assert Membership.objects.filter(role=Membership.Role.FACILITATOR).count() == 1


@pytest.mark.django_db
def test_an_anonymous_visitor_is_returned_to_the_join_url_after_logging_in(
    client: Client, project: Project, outsider: User
) -> None:
    """The round trip in full: the link, the login page, then the project."""
    target = join_url(project)

    first = client.get(target)
    assert first["Location"] == f"{LOGIN_URL}?next={target}"

    login_page = client.get(first["Location"])
    assert login_page.context["next"] == target

    response = client.post(
        first["Location"], {"username": outsider.username, "password": PASSWORD}, follow=True
    )

    assert response.redirect_chain == [(target, 302), (project.get_absolute_url(), 302)]
    assert response.status_code == 200
    assert Membership.objects.filter(project=project, user=outsider).exists()
    assert "joined" in response.content.decode().lower()


@pytest.mark.django_db
def test_an_anonymous_visitor_is_returned_to_the_join_url_after_signing_up(
    client: Client, project: Project
) -> None:
    target = join_url(project)

    login_page = client.get(f"{LOGIN_URL}?next={target}")
    assert f"{SIGNUP_URL}?next=" in login_page.content.decode()

    signup_page = client.get(f"{SIGNUP_URL}?next={target}")
    assert signup_page.context["next"] == target
    assert f'name="next" value="{target}"' in signup_page.content.decode()

    response = client.post(
        f"{SIGNUP_URL}?next={target}",
        {
            "username": "newcomer",
            "display_name": "Newcomer",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
        follow=True,
    )

    assert response.redirect_chain == [(target, 302), (project.get_absolute_url(), 302)]
    assert response.status_code == 200
    newcomer = User.objects.get(username="newcomer")
    assert Membership.objects.get(project=project, user=newcomer).role == Membership.Role.MEMBER


@pytest.mark.django_db
def test_signup_still_ignores_a_next_pointing_off_site(client: Client) -> None:
    response = client.post(
        f"{SIGNUP_URL}?next=https://example.com/phish",
        {
            "username": "newcomer",
            "display_name": "Newcomer",
            "password1": PASSWORD,
            "password2": PASSWORD,
        },
    )

    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.django_db
def test_opening_a_join_link_twice_does_not_create_a_second_membership(
    client: Client, project: Project, outsider: User
) -> None:
    log_in(client, outsider)
    client.get(join_url(project))

    response = client.get(join_url(project), follow=True)

    assert response.status_code == 200
    assert Membership.objects.filter(project=project, user=outsider).count() == 1
    assert "already a member" in response.content.decode().lower()


@pytest.mark.django_db
def test_a_facilitator_opening_the_link_is_not_demoted(
    client: Client, project: Project, owner: User
) -> None:
    log_in(client, owner)

    response = client.get(join_url(project), follow=True)

    assert response.status_code == 200
    assert Membership.objects.get(project=project, user=owner).role == Membership.Role.FACILITATOR
    assert Membership.objects.filter(project=project, user=owner).count() == 1
    assert "already a member" in response.content.decode().lower()


@pytest.mark.django_db
def test_an_unknown_token_is_a_404_with_no_hint(client: Client, outsider: User) -> None:
    log_in(client, outsider)

    response = client.get(reverse("join-project", args=[uuid.uuid4()]))

    assert response.status_code == 404
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_a_token_that_is_not_a_uuid_is_a_404(client: Client, outsider: User) -> None:
    log_in(client, outsider)

    assert client.get("/join/not-a-token/").status_code == 404


# --------------------------------------------------------------------------
# Rotating
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_owner_is_offered_the_rotate_action(
    client: Client, project: Project, owner: User
) -> None:
    log_in(client, owner)

    response = client.get(project.get_absolute_url())
    body = response.content.decode()

    assert response.context["can_rotate"] is True
    assert rotate_url(project) in body
    assert "csrfmiddlewaretoken" in body


@pytest.mark.django_db
def test_a_facilitator_who_is_not_the_owner_is_offered_the_rotate_action(
    client: Client, project: Project
) -> None:
    facilitator = make_user("facil", "Fay Facilitator")
    Membership.objects.create(project=project, user=facilitator, role=Membership.Role.FACILITATOR)
    log_in(client, facilitator)

    response = client.get(project.get_absolute_url())

    assert response.context["can_rotate"] is True
    assert rotate_url(project) in response.content.decode()


@pytest.mark.django_db
def test_a_plain_member_is_not_offered_the_rotate_action(
    client: Client, project: Project, member: User
) -> None:
    log_in(client, member)

    response = client.get(project.get_absolute_url())

    assert response.context["can_rotate"] is False
    assert rotate_url(project) not in response.content.decode()


@pytest.mark.django_db
def test_a_direct_post_from_a_plain_member_is_rejected(
    client: Client, project: Project, member: User
) -> None:
    """Hiding the button is a courtesy. This is the enforcement."""
    before = project.join_token
    log_in(client, member)

    response = client.post(rotate_url(project))

    assert response.status_code == 403
    project.refresh_from_db()
    assert project.join_token == before


@pytest.mark.django_db
def test_a_direct_post_from_a_non_member_is_a_404(
    client: Client, project: Project, outsider: User
) -> None:
    before = project.join_token
    log_in(client, outsider)

    response = client.post(rotate_url(project))

    assert response.status_code == 404
    project.refresh_from_db()
    assert project.join_token == before


@pytest.mark.django_db
def test_rotation_replaces_the_token_so_the_old_link_stops_working(
    client: Client, project: Project, owner: User, outsider: User
) -> None:
    old_url = join_url(project)
    log_in(client, owner)

    response = client.post(rotate_url(project))

    assert response.status_code == 302
    assert response["Location"] == project.get_absolute_url()
    project.refresh_from_db()
    new_url = join_url(project)
    assert new_url != old_url

    stranger = Client()
    log_in(stranger, outsider)
    assert stranger.get(old_url).status_code == 404
    assert not Membership.objects.filter(user=outsider).exists()

    assert stranger.get(new_url, follow=True).status_code == 200
    assert Membership.objects.get(project=project, user=outsider).role == Membership.Role.MEMBER


@pytest.mark.django_db
def test_rotation_keeps_the_people_already_in_the_project(
    client: Client, project: Project, owner: User, member: User
) -> None:
    log_in(client, owner)

    client.post(rotate_url(project))

    assert Membership.objects.filter(project=project).count() == 2


@pytest.mark.django_db
def test_rotation_is_never_a_get(client: Client, project: Project, owner: User) -> None:
    before = project.join_token
    log_in(client, owner)

    response = client.get(rotate_url(project))

    assert response.status_code == 405
    project.refresh_from_db()
    assert project.join_token == before

    detail = client.get(project.get_absolute_url()).content.decode()
    assert f'href="{rotate_url(project)}"' not in detail


@pytest.mark.django_db
def test_rotation_needs_a_csrf_token(project: Project, owner: User) -> None:
    client = Client(enforce_csrf_checks=True)
    log_in(client, owner)
    before = project.join_token

    without_token = client.post(rotate_url(project))
    assert without_token.status_code == 403
    project.refresh_from_db()
    assert project.join_token == before

    client.get(project.get_absolute_url())
    response = client.post(
        rotate_url(project),
        {"csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )

    assert response.status_code == 302
    project.refresh_from_db()
    assert project.join_token != before


@pytest.mark.django_db
def test_the_page_says_what_the_link_is_and_what_rotating_it_does(
    client: Client, project: Project, owner: User
) -> None:
    log_in(client, owner)

    body = client.get(project.get_absolute_url()).content.decode().lower()

    assert "only thing anyone needs in order to join" in body
    assert "breaks every copy already shared" in body


@pytest.mark.django_db
def test_no_migrations_are_missing_for_projects() -> None:
    from django.core.management import call_command

    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
