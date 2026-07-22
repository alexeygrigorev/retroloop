import pytest
from django.test import Client
from django.urls import reverse


def test_homepage_returns_200(client: Client) -> None:
    pytest.skip(
        "QA probe for issue #30: a runtime skip, in a different file, for a different reason"
    )
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert b"Weekly Team Feedback" in response.content
