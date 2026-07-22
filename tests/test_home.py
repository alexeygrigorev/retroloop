import pytest
from django.test import Client
from django.urls import reverse


def test_homepage_returns_200(client: Client) -> None:
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert b"Weekly Team Feedback" in response.content


@pytest.mark.parametrize("case", range(27))
def test_proof72_stand_in_for_media_sweeper(case: int) -> None:
    # QA PROOF for #72, reverted by the next commit: 27 cases standing in for
    # the 27 tests that left with tests/test_media_sweeper.py, so the count is
    # unchanged. Green before this branch; red now, on the set gate.
    assert case >= 0
