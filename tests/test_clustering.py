"""Clustering: the API seam, the defensive parsing, and the board it writes.

Every test here maps to an acceptance criterion of issue #22. Four themes run
through the file, and they are the four #21 established for transcription,
because clustering reuses that shape on purpose.

The first is that no test makes a network call and no test needs a key, and
neither is arranged by skipping. `config/settings_test.py` points
``CLUSTERING_CLIENT`` at an inert stand-in for the whole suite, and the tests
that prove something about the real client hand
`ai.clustering.OpenAIClusteringClient` a fake SDK object and assert what was
sent to it: the model, the structured-output schema, and — the part decisions 9
and 10 turn on — a payload that carries each card's ``public_id`` and never its
pk, its author or its anonymity.

The second is that malformed model output cannot corrupt the board. A name that
is not a string, an id for a card that is not in the cycle, a card put in two
groups, an empty response, more clusters than cards — each is driven through and
the board is asserted to survive it, ending with the invariant that matters:
the card count is unchanged and every card is in one cluster or in none.

The third is that clustering never runs when it must not: no cards means no
call, a second run makes no second set of suggestions, and a failure leaves the
reveal standing and is recorded where a facilitator reads it rather than only in
the worker log.

The fourth is the wiring: the reveal enqueues the job on commit, and writing the
clusters bumps the board version so an open board picks them up.
"""

import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from ai import clustering
from ai.clustering import (
    MODEL,
    CardInput,
    ClusteringError,
    MissingAPIKeyError,
    OpenAIClusteringClient,
    classify,
    parse_suggestions,
    suggest_clusters,
)
from ai.fakes import EchoClusteringClient, NullClusteringClient
from cycles.models import Card, FeedbackCycle
from projects.models import Membership, Project
from retro.clustering import RECOVERY, UNEXPECTED, cluster_retrospective_cards
from retro.models import CLUSTER_NAME_MAX_LENGTH, Cluster, Retrospective
from retro.services import advance_stage

User = get_user_model()

BASE_DIR = Path(django_settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Category = Card.Category


# --------------------------------------------------------------------------
# Fakes: the API seam, and the clients a test writes the answers for
# --------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeCompletion:
    """One chat completion, carrying the structured JSON as its content."""

    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(content)]


class FakeChatCompletions:
    """Stands in for `openai.OpenAI().chat.completions`, and remembers."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeSDK:
    """Shaped like `openai.OpenAI`, and nothing more of it is used."""

    def __init__(self, *responses) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeChatCompletions(responses)

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls


def completion(clusters) -> FakeCompletion:
    """A structured response naming these clusters, as the model would return it."""
    return FakeCompletion(json.dumps({"clusters": clusters}))


class ScriptedClusteringClient:
    """A clustering client whose answer a test writes out.

    Records that it was called, and returns the groups it was handed — so a test
    can drive the writer with exactly the output it wants to see survive.
    """

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[list[CardInput]] = []

    def cluster(self, cards):
        self.calls.append(list(cards))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class RefusingClusteringClient:
    """Fails the test if anything calls it.

    What proves a cycle skipped the API is that nothing was sent, not that the
    board came out empty.
    """

    def cluster(self, cards):
        raise AssertionError("the clustering API was called, and it should not have been")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


class Cycle:
    """A project, a facilitator, a closed cycle, its cards and its retrospective."""

    def __init__(self, *, cards: list[tuple[str, str]], stage: str = Retrospective.Stage.REVEAL):
        self.owner = make_user(f"owner-{uuid.uuid4().hex[:8]}", "Olive Owner")
        self.project = Project.objects.create(name="Platform", owner=self.owner)
        Membership.objects.create(
            project=self.project, user=self.owner, role=Membership.Role.FACILITATOR
        )
        self.cycle = FeedbackCycle.objects.create(
            project=self.project,
            week_start=MONDAY,
            opens_at=OPENS_AT,
            closes_at=CLOSES_AT,
            facilitator=self.owner,
            status=FeedbackCycle.Status.CLOSED,
        )
        self.cards = [
            Card.objects.create(cycle=self.cycle, author=self.owner, category=category, text=text)
            for category, text in cards
        ]
        self.retro = Retrospective.objects.create(cycle=self.cycle, stage=stage)

    def public_id(self, index: int) -> str:
        return str(self.cards[index].public_id)

    def refresh_cards(self) -> list[Card]:
        self.cards = [Card.objects.get(pk=card.pk) for card in self.cards]
        return self.cards


@pytest.fixture
def four_cards(db) -> Cycle:
    """Two START cards, one STOP, one CONTINUE — something for every branch."""
    return Cycle(
        cards=[
            (Category.START, "start pairing on deploys"),
            (Category.START, "start writing release notes"),
            (Category.STOP, "stop merging on red"),
            (Category.CONTINUE, "continue the demo on Fridays"),
        ]
    )


# --------------------------------------------------------------------------
# The API seam: the model, the schema, and the payload that carries no pk
# --------------------------------------------------------------------------


def test_the_model_is_the_text_one() -> None:
    assert MODEL == "gpt-5.6-terra"


def test_no_other_model_is_named_anywhere_in_the_module() -> None:
    """`gpt-4o` is superseded; it is not a fallback, so it is named in no literal.

    A fallback model that only fires when the real one is down is a grouping
    nobody knows is worse. The prose may say `gpt-4o` was superseded; no string
    the request could send may name it.
    """
    source = (BASE_DIR / "ai" / "clustering.py").read_text()
    literals = set(re.findall(r"\"([\w.-]*(?:gpt|whisper)[\w.-]*)\"", source))

    assert literals == {MODEL}


def test_the_client_sends_the_cards_as_id_category_text() -> None:
    cards = [
        CardInput(id="11111111-1111-4111-8111-111111111111", category="START", text="one"),
        CardInput(id="22222222-2222-4222-8222-222222222222", category="STOP", text="two"),
    ]
    sdk = FakeSDK(completion([{"name": "A group", "card_ids": [cards[0].id]}]))

    OpenAIClusteringClient(api_key="k", sdk=sdk).cluster(cards)

    assert sdk.calls[0]["model"] == MODEL
    payload = json.loads(sdk.calls[0]["messages"][-1]["content"])
    assert payload == {
        "cards": [
            {"id": cards[0].id, "category": "START", "text": "one"},
            {"id": cards[1].id, "category": "STOP", "text": "two"},
        ]
    }
    # The three keys and no fourth: nothing that could carry a person.
    assert all(set(card) == {"id", "category", "text"} for card in payload["cards"])


def test_the_request_asks_for_the_structured_schema() -> None:
    sdk = FakeSDK(completion([]))

    OpenAIClusteringClient(api_key="k", sdk=sdk).cluster(
        [CardInput(id="a", category="START", text="x")]
    )

    fmt = sdk.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == clustering.RESPONSE_SCHEMA


def test_the_client_reads_the_groups_out_of_the_response() -> None:
    sdk = FakeSDK(
        completion(
            [
                {"name": "Deploys", "card_ids": ["a", "b"]},
                {"name": "Reviews", "card_ids": ["c"]},
            ]
        )
    )

    groups = OpenAIClusteringClient(api_key="k", sdk=sdk).cluster(
        [CardInput(id="a", category="START", text="x")]
    )

    assert groups == [
        {"name": "Deploys", "card_ids": ["a", "b"]},
        {"name": "Reviews", "card_ids": ["c"]},
    ]


@pytest.mark.parametrize("content", ["", "   ", "not json at all", "[1, 2, 3]", '{"other": 1}'])
def test_a_response_that_is_not_the_expected_object_is_no_suggestions(content: str) -> None:
    """An empty body or one the model malformed leaves the board unclustered."""
    sdk = FakeSDK(FakeCompletion(content))

    groups = OpenAIClusteringClient(api_key="k", sdk=sdk).cluster(
        [CardInput(id="a", category="START", text="x")]
    )

    assert groups == []


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------


def test_a_missing_key_names_the_variable(settings) -> None:
    """Not an authentication error out of the SDK: the variable, by name."""
    settings.OPENAI_API_KEY = ""

    with pytest.raises(MissingAPIKeyError) as raised:
        OpenAIClusteringClient()

    assert "OPENAI_API_KEY" in str(raised.value)


def test_the_key_is_read_from_the_environment_through_settings(settings) -> None:
    settings.OPENAI_API_KEY = "sk-from-the-environment"

    assert OpenAIClusteringClient().api_key == "sk-from-the-environment"


def test_the_suite_never_reaches_the_real_client() -> None:
    """The fake is the default for the whole suite, not a per-test mock."""
    assert django_settings.CLUSTERING_CLIENT == "ai.fakes.NullClusteringClient"
    assert django_settings.OPENAI_API_KEY == ""


def test_a_rejected_key_says_which_variable_holds_it() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError("nope", response=response, body=None)

    translated = classify(error)

    assert isinstance(translated, ClusteringError)
    assert "OPENAI_API_KEY" in str(translated)


def test_a_status_error_becomes_a_clustering_error() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(500, request=request)
    error = openai.APIStatusError("boom", response=response, body=None)

    assert isinstance(classify(error), ClusteringError)


def test_something_that_is_not_an_sdk_error_is_left_alone() -> None:
    assert classify(ValueError("not from the SDK")) is None


# --------------------------------------------------------------------------
# Defensive parsing: malformed output becomes something storable or nothing
# --------------------------------------------------------------------------


def test_no_cards_makes_no_call_at_the_seam() -> None:
    """`suggest_clusters` short-circuits before any client, so an empty cycle
    cannot reach the API even by this lower door."""
    assert suggest_clusters([], client=RefusingClusteringClient()) == []


def test_an_empty_response_is_no_suggestions() -> None:
    assert parse_suggestions([]) == []
    assert parse_suggestions(None) == []


def test_a_name_that_is_not_a_string_is_dropped() -> None:
    parsed = parse_suggestions(
        [
            {"name": 123, "card_ids": ["a"]},
            {"name": None, "card_ids": ["b"]},
            {"name": ["a", "list"], "card_ids": ["c"]},
            {"name": "Kept", "card_ids": ["d"]},
        ]
    )

    assert parsed == [{"name": "Kept", "card_ids": ["d"]}]


def test_card_ids_that_is_not_a_list_becomes_empty_and_non_string_ids_are_dropped() -> None:
    parsed = parse_suggestions(
        [
            {"name": "A", "card_ids": "not a list"},
            {"name": "B", "card_ids": ["ok", 7, None, "fine"]},
            {"name": "C"},
        ]
    )

    assert parsed == [
        {"name": "A", "card_ids": []},
        {"name": "B", "card_ids": ["ok", "fine"]},
        {"name": "C", "card_ids": []},
    ]


# --------------------------------------------------------------------------
# The stand-in clients
# --------------------------------------------------------------------------


def test_the_null_client_suggests_nothing() -> None:
    assert NullClusteringClient().cluster([CardInput(id="a", category="START", text="x")]) == []


def test_the_echo_client_groups_by_category_deterministically() -> None:
    groups = EchoClusteringClient().cluster(
        [
            CardInput(id="a", category="START", text="one"),
            CardInput(id="b", category="STOP", text="two"),
            CardInput(id="c", category="START", text="three"),
        ]
    )

    assert groups == [
        {"name": "Start cards", "card_ids": ["a", "c"]},
        {"name": "Stop cards", "card_ids": ["b"]},
    ]


def test_no_stand_in_client_imports_the_sdk() -> None:
    """They stand in for the SDK, so reaching for it would be a contradiction."""
    source = (BASE_DIR / "ai" / "fakes.py").read_text()

    assert "openai" not in source


# --------------------------------------------------------------------------
# Writing the board: suggestions become clusters, and no card is lost
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_suggestions_become_auto_generated_clusters_with_the_cards_assigned(
    four_cards: Cycle,
) -> None:
    client = ScriptedClusteringClient(
        [
            {"name": "Deploys", "card_ids": [four_cards.public_id(0), four_cards.public_id(1)]},
            {"name": "Process", "card_ids": [four_cards.public_id(2)]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    clusters = list(Cluster.objects.filter(retrospective=four_cards.retro).order_by("position"))
    assert [c.name for c in clusters] == ["Deploys", "Process"]
    assert all(c.is_auto_generated for c in clusters)
    assert [c.position for c in clusters] == [1, 2]

    cards = four_cards.refresh_cards()
    assert cards[0].cluster_id == clusters[0].pk
    assert cards[1].cluster_id == clusters[0].pk
    assert cards[2].cluster_id == clusters[1].pk
    assert cards[3].cluster_id is None  # the model left it out; ungrouped is normal


@pytest.mark.django_db
def test_a_card_id_that_is_not_in_the_cycle_is_ignored(four_cards: Cycle) -> None:
    stranger = str(uuid.uuid4())
    client = ScriptedClusteringClient(
        [{"name": "Mixed", "card_ids": [four_cards.public_id(0), stranger]}]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    cluster = Cluster.objects.get(retrospective=four_cards.retro)
    assert cluster.cards.count() == 1
    assert four_cards.refresh_cards()[0].cluster_id == cluster.pk


@pytest.mark.django_db
def test_a_card_in_two_groups_joins_the_first_and_is_not_duplicated(four_cards: Cycle) -> None:
    shared = four_cards.public_id(0)
    client = ScriptedClusteringClient(
        [
            {"name": "First", "card_ids": [shared, four_cards.public_id(1)]},
            {"name": "Second", "card_ids": [shared, four_cards.public_id(2)]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    first, second = Cluster.objects.filter(retrospective=four_cards.retro).order_by("position")
    assert four_cards.refresh_cards()[0].cluster_id == first.pk
    assert set(first.cards.values_list("pk", flat=True)) == {
        four_cards.cards[0].pk,
        four_cards.cards[1].pk,
    }
    assert set(second.cards.values_list("pk", flat=True)) == {four_cards.cards[2].pk}


@pytest.mark.django_db
def test_the_card_count_is_unchanged_and_every_card_is_in_one_cluster_or_none(
    four_cards: Cycle,
) -> None:
    """The test that matters. Whatever the model said, the cycle stays coherent."""
    before = Card.objects.filter(cycle=four_cards.cycle).count()
    client = ScriptedClusteringClient(
        [
            {"name": "One", "card_ids": [four_cards.public_id(0), four_cards.public_id(0)]},
            {"name": "Two", "card_ids": [four_cards.public_id(0), four_cards.public_id(1)]},
            {"name": "Ghost", "card_ids": [str(uuid.uuid4())]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    cards = Card.objects.filter(cycle=four_cards.cycle)
    assert cards.count() == before
    cluster_ids = set(
        Cluster.objects.filter(retrospective=four_cards.retro).values_list("pk", flat=True)
    )
    for card in cards:
        assert card.cluster_id is None or card.cluster_id in cluster_ids
    # No card is counted twice: the cards a cluster holds, summed over clusters,
    # equals the number of cards that are grouped at all — no overlap possible.
    grouped_cards = cards.exclude(cluster__isnull=True).count()
    summed_over_clusters = sum(
        cluster.cards.count() for cluster in Cluster.objects.filter(retrospective=four_cards.retro)
    )
    assert grouped_cards == summed_over_clusters


@pytest.mark.django_db
def test_cluster_names_are_trimmed_and_capped(four_cards: Cycle) -> None:
    long_name = "x" * (CLUSTER_NAME_MAX_LENGTH + 50)
    client = ScriptedClusteringClient(
        [
            {"name": "   Padded   ", "card_ids": [four_cards.public_id(0)]},
            {"name": long_name, "card_ids": [four_cards.public_id(1)]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    names = list(
        Cluster.objects.filter(retrospective=four_cards.retro)
        .order_by("position")
        .values_list("name", flat=True)
    )
    assert names[0] == "Padded"
    assert names[1] == "x" * CLUSTER_NAME_MAX_LENGTH


@pytest.mark.django_db
def test_a_group_with_a_blank_name_is_not_written(four_cards: Cycle) -> None:
    client = ScriptedClusteringClient(
        [
            {"name": "   ", "card_ids": [four_cards.public_id(0)]},
            {"name": "Real", "card_ids": [four_cards.public_id(1)]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    names = list(
        Cluster.objects.filter(retrospective=four_cards.retro).values_list("name", flat=True)
    )
    assert names == ["Real"]
    assert four_cards.refresh_cards()[0].cluster_id is None  # its card stays ungrouped


@pytest.mark.django_db
def test_more_clusters_than_cards_writes_no_empty_clusters(four_cards: Cycle) -> None:
    """Empty suggested groups — the surplus when there are more than cards — are noise."""
    client = ScriptedClusteringClient(
        [
            {"name": "Has a card", "card_ids": [four_cards.public_id(0)]},
            {"name": "Empty one", "card_ids": []},
            {"name": "All strangers", "card_ids": [str(uuid.uuid4())]},
        ]
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    assert list(
        Cluster.objects.filter(retrospective=four_cards.retro).values_list("name", flat=True)
    ) == ["Has a card"]


@pytest.mark.django_db
def test_a_non_string_name_group_leaves_its_card_ungrouped(four_cards: Cycle) -> None:
    """The parse drops it, so the writer never sees it and the card stays ungrouped."""
    client = ScriptedClusteringClient([{"name": 42, "card_ids": [four_cards.public_id(0)]}])

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    assert Cluster.objects.filter(retrospective=four_cards.retro).count() == 0
    assert four_cards.refresh_cards()[0].cluster_id is None


# --------------------------------------------------------------------------
# No pk, no author, leaks through the request or the cluster
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_request_carries_public_ids_and_never_a_pk_or_an_author(four_cards: Cycle) -> None:
    """The whole-pipeline proof of decisions 9 and 10, at the point cards leave.

    The job builds the request from real rows, so this is where a pk or an
    author would leak if one were going to. The request is inspected: every id
    is a card's `public_id`, no card's `pk` appears, and no author or anonymity
    field is anywhere in the body.
    """
    public_ids = [four_cards.public_id(index) for index in range(4)]
    sdk = FakeSDK(completion([{"name": "Everything", "card_ids": public_ids}]))
    client = OpenAIClusteringClient(api_key="k", sdk=sdk)

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    body = sdk.calls[0]["messages"][-1]["content"]
    payload = json.loads(body)
    sent_ids = [card["id"] for card in payload["cards"]]
    assert sorted(sent_ids) == sorted(public_ids)
    for card in four_cards.cards:
        assert str(card.pk) not in body
    assert "author" not in body
    assert "is_anonymous" not in body
    # And the cluster the pipeline wrote names no person: it carries only the
    # board fields, and the cards it grouped kept their own authors untouched.
    cluster = Cluster.objects.get(retrospective=four_cards.retro)
    assert {f.name for f in cluster._meta.fields} == {
        "id",
        "retrospective",
        "name",
        "position",
        "is_auto_generated",
        "status",
    }
    assert all(card.author_id == four_cards.owner.pk for card in four_cards.refresh_cards())


# --------------------------------------------------------------------------
# When it does not run
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_cycle_with_no_cards_makes_no_call_and_creates_no_clusters(db) -> None:
    empty = Cycle(cards=[])

    cluster_retrospective_cards(empty.retro.pk, client=RefusingClusteringClient())

    assert Cluster.objects.filter(retrospective=empty.retro).count() == 0


@pytest.mark.django_db
def test_running_twice_produces_one_set_of_suggestions(four_cards: Cycle) -> None:
    first = ScriptedClusteringClient([{"name": "Once", "card_ids": [four_cards.public_id(0)]}])
    cluster_retrospective_cards(four_cards.retro.pk, client=first)
    assert Cluster.objects.filter(retrospective=four_cards.retro).count() == 1

    # The second run detects the auto-generated cluster and never reaches the API.
    cluster_retrospective_cards(four_cards.retro.pk, client=RefusingClusteringClient())

    assert Cluster.objects.filter(retrospective=four_cards.retro).count() == 1
    assert len(first.calls) == 1


@pytest.mark.django_db
def test_it_does_not_overwrite_a_cluster_the_team_made_by_hand(four_cards: Cycle) -> None:
    """A hand-made cluster is not auto-generated, so its presence does not stop a
    first run — but a suggested cluster does. Here the team has already made one
    auto-run's worth, and the job stands down rather than adding a second set."""
    Cluster.objects.create(
        retrospective=four_cards.retro, name="Team made", position=1, is_auto_generated=True
    )

    cluster_retrospective_cards(four_cards.retro.pk, client=RefusingClusteringClient())

    assert list(
        Cluster.objects.filter(retrospective=four_cards.retro).values_list("name", flat=True)
    ) == ["Team made"]


@pytest.mark.django_db
def test_the_job_tolerates_the_retrospective_having_gone(db) -> None:
    cluster_retrospective_cards(987654321, client=RefusingClusteringClient())


# --------------------------------------------------------------------------
# Failure: the reveal stands, and a facilitator is told
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_clustering_error_is_recorded_where_a_facilitator_reads_it(four_cards: Cycle) -> None:
    client = ScriptedClusteringClient(ClusteringError("the grouping API refused the request (500)"))

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    four_cards.retro.refresh_from_db()
    assert "refused the request (500)" in four_cards.retro.clustering_error
    assert RECOVERY in four_cards.retro.clustering_error
    # The board is untouched: no clusters, every card ungrouped.
    assert Cluster.objects.filter(retrospective=four_cards.retro).count() == 0
    assert all(card.cluster_id is None for card in four_cards.refresh_cards())


@pytest.mark.django_db
def test_an_unexpected_failure_is_recorded_without_a_traceback(four_cards: Cycle) -> None:
    client = ScriptedClusteringClient(ZeroDivisionError("nobody wrote a message for this"))

    cluster_retrospective_cards(four_cards.retro.pk, client=client)

    four_cards.retro.refresh_from_db()
    assert four_cards.retro.clustering_error.startswith(UNEXPECTED)
    assert "ZeroDivisionError" not in four_cards.retro.clustering_error


@pytest.mark.django_db
def test_the_failure_message_shows_on_the_retrospective_page(four_cards: Cycle) -> None:
    cluster_retrospective_cards(
        four_cards.retro.pk,
        client=ScriptedClusteringClient(ClusteringError("the grouping API was unreachable")),
    )
    http = Client()
    http.login(username=four_cards.owner.username, password=PASSWORD)

    body = http.get(reverse("retro-detail", args=[four_cards.retro.pk])).content.decode()

    assert "was unreachable" in body
    assert RECOVERY in body


@pytest.mark.django_db
def test_a_later_successful_run_clears_an_earlier_error(four_cards: Cycle) -> None:
    """A re-run is a deliberate act; when it works, the stale message goes."""
    Retrospective.objects.filter(pk=four_cards.retro.pk).update(clustering_error="old failure")

    cluster_retrospective_cards(
        four_cards.retro.pk,
        client=ScriptedClusteringClient(
            [{"name": "Now it worked", "card_ids": [four_cards.public_id(0)]}]
        ),
    )

    four_cards.retro.refresh_from_db()
    assert four_cards.retro.clustering_error == ""


# --------------------------------------------------------------------------
# Wiring: the reveal enqueues it, and the write bumps the version
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_writing_the_clusters_bumps_the_board_version(four_cards: Cycle) -> None:
    before = Retrospective.objects.get(pk=four_cards.retro.pk).version

    cluster_retrospective_cards(
        four_cards.retro.pk,
        client=ScriptedClusteringClient(
            [{"name": "Grouped", "card_ids": [four_cards.public_id(0)]}]
        ),
    )

    assert Retrospective.objects.get(pk=four_cards.retro.pk).version == before + 1


@pytest.mark.django_db
def test_a_run_that_suggests_nothing_does_not_bump_the_version(four_cards: Cycle) -> None:
    before = Retrospective.objects.get(pk=four_cards.retro.pk).version

    cluster_retrospective_cards(four_cards.retro.pk, client=ScriptedClusteringClient([]))

    assert Retrospective.objects.get(pk=four_cards.retro.pk).version == before


@pytest.mark.django_db
def test_the_reveal_enqueues_clustering_and_it_groups_the_cards_end_to_end(
    settings, django_capture_on_commit_callbacks
) -> None:
    """The whole wiring, with the real transition and the queue running inline.

    The reveal is entered through the stage machine; the job is enqueued on
    commit and runs against the committed, revealed cards. Pointed at the
    category-grouping stand-in — no key, no network — it produces the
    suggestions a keyless Compose stack would show.
    """
    settings.CLUSTERING_CLIENT = "ai.fakes.EchoClusteringClient"
    setup = Cycle(
        cards=[(Category.START, "a"), (Category.START, "b"), (Category.STOP, "c")],
        stage=Retrospective.Stage.DRAFT,
    )
    # The cycle is CLOSED in the fixture; put it back so the reveal's own close
    # has something to do and the transition is the real one.
    FeedbackCycle.objects.filter(pk=setup.cycle.pk).update(status=FeedbackCycle.Status.COLLECTING)

    with django_capture_on_commit_callbacks(execute=True):
        advance_stage(setup.owner, setup.retro)

    setup.retro.refresh_from_db()
    assert setup.retro.stage == Retrospective.Stage.REVEAL
    clusters = Cluster.objects.filter(retrospective=setup.retro).order_by("position")
    assert [c.name for c in clusters] == ["Start cards", "Stop cards"]
    assert all(c.is_auto_generated for c in clusters)
    assert setup.retro.clustering_error == ""


@pytest.mark.django_db
def test_the_reveal_transition_succeeds_when_clustering_raises(
    monkeypatch, settings, django_capture_on_commit_callbacks
) -> None:
    """The stage has already advanced; a job that raises leaves it there.

    Clustering runs after the reveal has committed, so its failure cannot roll
    the reveal back. The transition returns, the cards are ungrouped, and the
    failure is on the retrospective for a facilitator to read.
    """
    setup = Cycle(cards=[(Category.START, "a")], stage=Retrospective.Stage.DRAFT)
    FeedbackCycle.objects.filter(pk=setup.cycle.pk).update(status=FeedbackCycle.Status.COLLECTING)

    def boom(cards, *, client=None):
        raise ClusteringError("the grouping API fell over")

    monkeypatch.setattr("retro.clustering.suggest_clusters", boom)

    with django_capture_on_commit_callbacks(execute=True):
        advance_stage(setup.owner, setup.retro)

    setup.retro.refresh_from_db()
    assert setup.retro.stage == Retrospective.Stage.REVEAL
    assert "fell over" in setup.retro.clustering_error
    assert Cluster.objects.filter(retrospective=setup.retro).count() == 0
