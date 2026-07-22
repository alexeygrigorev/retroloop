from django.test import Client
from django.urls import reverse


def test_homepage_returns_200(client: Client) -> None:
    response = client.get(reverse("home"))

    # QA probe for issue #30: the suite must stop the job before the skip gate.
    assert response.status_code == 418
    assert b"Weekly Team Feedback" in response.content
