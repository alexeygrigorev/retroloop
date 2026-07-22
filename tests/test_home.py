from django.test import Client
from django.urls import reverse


def test_homepage_returns_200(client: Client) -> None:
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert b"Weekly Team Feedback" in response.content
