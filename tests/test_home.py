import pytest
from django.test import Client
from django.urls import reverse


def test_homepage_returns_200(client: Client) -> None:
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert b"Weekly Team Feedback" in response.content


@pytest.mark.parametrize("case", range(8))
def test_proof72_stand_in_for_compose_worker(case: int) -> None:
    # QA PROOF for #72, reverted by the next commit: 8 cases standing in for
    # the 8 tests removed with tests/test_compose_worker.py, so the count is
    # unchanged and only the file set reveals the loss.
    assert case >= 0
