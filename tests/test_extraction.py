"""Outcome extraction: the API seam, the resolution rules, and the drafts it writes.

Every test here maps to an acceptance criterion of issue #23, and the file keeps
the four themes #21 and #22 established, because extraction reuses that shape on
purpose.

The first is that no test makes a network call and no test needs a key, and
neither is arranged by skipping. `config/settings_test.py` points
``EXTRACTION_CLIENT`` at an inert stand-in for the whole suite, and the tests
that prove something about the real client hand
`ai.extraction.OpenAIExtractionClient` a fake SDK object and assert what was sent
to it: the model `gpt-5.6-terra`, the structured-output schema, and — the part
decisions 8, 9 and 10 turn on — a payload that carries the roster's display names
and never an email address, a username, a card, a card author or a `Card.pk`.

The second is that malformed model output cannot corrupt the retrospective. An
owner that is not a string, a due date that is not a date or falls before the
meeting, a decision with no text, an empty response — each is driven through and
the valid drafts are asserted to survive it while the bad item is dropped.

The third is the resolution this task owns: an owner name is matched against the
roster by fuzzy match, and an unmatched *or* ambiguous name leaves the owner
NULL; a due date is resolved against the meeting's date and never against the
moment the job runs.

The fourth is the wiring: the transcript store enqueues extraction on commit, and
the drafts land `EXTRACTED`/`DRAFT` — never confirmed — with the summary on the
retrospective and a supporting excerpt on every row.
"""

import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from ai import extraction
from ai.extraction import (
    MODEL,
    OWNER_MATCH_THRESHOLD,
    AgendaItem,
    ExtractionError,
    ExtractionInput,
    MissingAPIKeyError,
    OpenAIExtractionClient,
    classify,
    empty_result,
    extract_outcomes,
    parse_outcomes,
    resolve_due_date,
    resolve_owner,
)
from ai.fakes import EchoExtractionClient, NullExtractionClient
from cycles.models import FeedbackCycle
from meetings import pipeline
from meetings.extraction import RECOVERY, UNEXPECTED, extract_meeting_outcomes
from meetings.models import MeetingRecord, Transcript
from projects.models import Membership, Project
from retro.models import ActionItem, Cluster, Decision, Retrospective, Vote

User = get_user_model()

BASE_DIR = Path(django_settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Status = MeetingRecord.Status
Kind = MeetingRecord.Kind


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
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(content)]


class FakeChatCompletions:
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


def completion(*, summary="", decisions=None, action_items=None) -> FakeCompletion:
    """A structured response as the model would return it."""
    return FakeCompletion(
        json.dumps(
            {
                "summary": summary,
                "decisions": decisions or [],
                "action_items": action_items or [],
            }
        )
    )


class ScriptedExtractionClient:
    """An extraction client whose answer a test writes out.

    Records the `ExtractionInput` it was called with, so a test can drive the
    writer with exactly the output it wants to see survive and then assert what
    the writer sent.
    """

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[ExtractionInput] = []

    def extract(self, meeting: ExtractionInput) -> dict:
        self.calls.append(meeting)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class RefusingExtractionClient:
    """Fails the test if anything calls it."""

    def extract(self, meeting: ExtractionInput) -> dict:
        raise AssertionError("the extraction API was called, and it should not have been")


def _walk_values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_values(value)
    elif isinstance(obj, list | tuple):
        for item in obj:
            yield from _walk_values(item)
    else:
        yield obj


def _walk_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list | tuple):
        for item in obj:
            yield from _walk_keys(item)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


class Meeting:
    """A project, its members, a retrospective, and a meeting record ready to extract.

    The record is `EXTRACTING` with its transcript stored and its media already
    deleted, exactly the state #21's pipeline leaves it in when it hands over.
    """

    def __init__(
        self,
        *,
        member_names: list[str] | None = None,
        transcript: str = "Speaker 1: We agreed to ship on Friday.",
        stage: str = Retrospective.Stage.COMPLETE,
        status: str = Status.EXTRACTING,
        owner_name: str = "Olive Owner",
    ):
        suffix = uuid.uuid4().hex[:8]
        self.owner = make_user(f"owner-{suffix}", owner_name)
        self.project = Project.objects.create(name="Platform", owner=self.owner)
        Membership.objects.create(
            project=self.project, user=self.owner, role=Membership.Role.FACILITATOR
        )
        self.members = []
        for index, name in enumerate(member_names or []):
            member = make_user(f"m-{suffix}-{index}", name)
            Membership.objects.create(
                project=self.project, user=member, role=Membership.Role.MEMBER
            )
            self.members.append(member)
        self.cycle = FeedbackCycle.objects.create(
            project=self.project,
            week_start=MONDAY,
            opens_at=OPENS_AT,
            closes_at=CLOSES_AT,
            facilitator=self.owner,
            status=FeedbackCycle.Status.CLOSED,
        )
        self.retro = Retrospective.objects.create(cycle=self.cycle, stage=stage)
        self.record = MeetingRecord.objects.create(
            retrospective=self.retro,
            uploaded_by=self.owner,
            kind=Kind.PASTED_TEXT,
            temp_path=None,
            media_deleted_at=timezone.now(),
            status=status,
        )
        self.transcript = Transcript.objects.create(record=self.record, text=transcript)

    def cluster(self, name: str, position: int) -> Cluster:
        return Cluster.objects.create(retrospective=self.retro, name=name, position=position)

    def vote(self, cluster: Cluster, user: User, weight: int) -> Vote:
        return Vote.objects.create(
            retrospective=self.retro, cluster=cluster, user=user, weight=weight
        )

    def reload_record(self) -> MeetingRecord:
        self.record.refresh_from_db()
        return self.record

    def reload_retro(self) -> Retrospective:
        self.retro.refresh_from_db()
        return self.retro


@pytest.fixture
def meeting(db) -> Meeting:
    return Meeting(member_names=["Ada Kim", "Bruno Sato"])


# ==========================================================================
# The API seam: the model, the schema, and the payload that carries no pk
# ==========================================================================


def test_the_model_is_the_text_one() -> None:
    assert MODEL == "gpt-5.6-terra"


def test_no_other_model_is_named_anywhere_in_the_module() -> None:
    """No fallback model: only `gpt-5.6-terra` may appear in a string the request sends."""
    source = (BASE_DIR / "ai" / "extraction.py").read_text()
    literals = set(re.findall(r"\"([\w.-]*(?:gpt|whisper)[\w.-]*)\"", source))

    assert literals == {MODEL}


def test_the_client_sends_transcript_agenda_roster_and_meeting_date() -> None:
    meeting = ExtractionInput(
        transcript="Speaker 1: Ship it.",
        meeting_date=date(2026, 7, 20),
        agenda=(AgendaItem(id=7, name="Deploys", weight=5),),
        roster=("Ada Kim", "Bruno Sato"),
    )
    sdk = FakeSDK(completion())

    OpenAIExtractionClient(api_key="k", sdk=sdk).extract(meeting)

    assert sdk.calls[0]["model"] == MODEL
    payload = json.loads(sdk.calls[0]["messages"][-1]["content"])
    assert payload == {
        "meeting_date": "2026-07-20",
        "roster": ["Ada Kim", "Bruno Sato"],
        "agenda": [{"id": 7, "name": "Deploys", "weight": 5}],
        "transcript": "Speaker 1: Ship it.",
    }


def test_the_request_asks_for_the_structured_schema() -> None:
    sdk = FakeSDK(completion())

    OpenAIExtractionClient(api_key="k", sdk=sdk).extract(
        ExtractionInput(transcript="x", meeting_date=date(2026, 7, 20))
    )

    fmt = sdk.calls[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == extraction.RESPONSE_SCHEMA


def test_the_payload_carries_display_names_and_no_card_or_address() -> None:
    """Decisions 8, 9 and 10: the roster is names, and nothing about a card is sent."""
    meeting = ExtractionInput(
        transcript="Speaker 1: Ada will do it.",
        meeting_date=date(2026, 7, 20),
        agenda=(AgendaItem(id=7, name="Deploys", weight=5),),
        roster=("Ada Kim", "Bruno Sato"),
    )
    sdk = FakeSDK(completion())

    OpenAIExtractionClient(api_key="k", sdk=sdk).extract(meeting)

    payload = json.loads(sdk.calls[0]["messages"][-1]["content"])
    keys = set(_walk_keys(payload))
    # Nothing that could carry a card, an author, an anonymity flag or an address.
    assert not (keys & {"card", "cards", "card_id", "author", "is_anonymous", "email", "username"})
    values = [value for value in _walk_values(payload) if isinstance(value, str)]
    assert not any("@" in value for value in values)
    # The only names sent are the roster's display names.
    assert payload["roster"] == ["Ada Kim", "Bruno Sato"]


def test_the_client_reads_outcomes_out_of_the_response() -> None:
    sdk = FakeSDK(
        completion(
            summary="We shipped.",
            decisions=[{"text": "Ship on Friday.", "excerpt": "Speaker 1: ship Friday"}],
            action_items=[
                {
                    "description": "Cut the release.",
                    "owner": "Ada Kim",
                    "due_date": "2026-07-24",
                    "excerpt": "Speaker 1: Ada cuts it",
                }
            ],
        )
    )

    raw = OpenAIExtractionClient(api_key="k", sdk=sdk).extract(
        ExtractionInput(transcript="x", meeting_date=date(2026, 7, 20))
    )

    assert raw["summary"] == "We shipped."
    assert raw["decisions"][0]["text"] == "Ship on Friday."
    assert raw["action_items"][0]["owner"] == "Ada Kim"


@pytest.mark.parametrize("content", ["", "   ", "not json at all", "[1, 2, 3]", "42"])
def test_a_response_that_is_not_the_expected_object_is_no_outcomes(content: str) -> None:
    sdk = FakeSDK(FakeCompletion(content))

    raw = OpenAIExtractionClient(api_key="k", sdk=sdk).extract(
        ExtractionInput(transcript="x", meeting_date=date(2026, 7, 20))
    )

    assert raw == {}


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------


def test_a_missing_key_names_the_variable(settings) -> None:
    settings.OPENAI_API_KEY = ""

    with pytest.raises(MissingAPIKeyError) as raised:
        OpenAIExtractionClient()

    assert "OPENAI_API_KEY" in str(raised.value)


def test_the_key_is_read_from_the_environment_through_settings(settings) -> None:
    settings.OPENAI_API_KEY = "sk-from-the-environment"

    assert OpenAIExtractionClient().api_key == "sk-from-the-environment"


def test_the_suite_never_reaches_the_real_client() -> None:
    assert django_settings.EXTRACTION_CLIENT == "ai.fakes.NullExtractionClient"
    assert django_settings.OPENAI_API_KEY == ""


def test_a_rejected_key_says_which_variable_holds_it() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError("nope", response=response, body=None)

    translated = classify(error)

    assert isinstance(translated, ExtractionError)
    assert "OPENAI_API_KEY" in str(translated)


def test_a_status_error_becomes_an_extraction_error() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(500, request=request)
    error = openai.APIStatusError("boom", response=response, body=None)

    assert isinstance(classify(error), ExtractionError)


def test_something_that_is_not_an_sdk_error_is_left_alone() -> None:
    assert classify(ValueError("not from the SDK")) is None


# ==========================================================================
# Resolving owners: exact, near, ambiguous, and not-a-member (the AC's four)
# ==========================================================================


ROSTER = ("Ada Kim", "Bruno Sato", "Priya Patel")


def test_owner_exact_match_resolves() -> None:
    assert resolve_owner("Ada Kim", ROSTER) == "Ada Kim"


def test_owner_case_and_space_do_not_matter() -> None:
    assert resolve_owner("  ada kim ", ROSTER) == "Ada Kim"


def test_owner_first_name_resolves_to_the_one_member_who_carries_it() -> None:
    """ "Ada will do it" names the sole Ada on the roster."""
    assert resolve_owner("Ada", ROSTER) == "Ada Kim"


def test_owner_near_match_resolves() -> None:
    """A small typo the model made still finds the member."""
    assert resolve_owner("Priyah Patel", ROSTER) == "Priya Patel"


def test_owner_that_is_not_a_member_at_all_is_null() -> None:
    assert resolve_owner("Zed Zephyr", ROSTER) is None


def test_owner_that_is_ambiguous_is_null() -> None:
    """Two members called Alex: "Alex will do it" identifies neither, so it is NULL."""
    assert resolve_owner("Alex", ("Alex Kim", "Alex Ng", "Bruno Sato")) is None


def test_two_members_sharing_a_display_name_are_ambiguous() -> None:
    assert resolve_owner("Sam", ("Sam", "Sam")) is None


@pytest.mark.parametrize("value", [None, 123, ["Ada"], {"name": "Ada"}, "", "   "])
def test_owner_that_is_not_a_usable_string_is_null(value) -> None:
    assert resolve_owner(value, ROSTER) is None


def test_an_empty_roster_resolves_nobody() -> None:
    assert resolve_owner("Ada Kim", ()) is None


def test_the_threshold_is_high_enough_to_refuse_a_stranger() -> None:
    """A name below the threshold is nobody, not the least-bad guess."""
    assert OWNER_MATCH_THRESHOLD >= 0.8


# ==========================================================================
# Dates: stated, resolved against the meeting, or left NULL
# ==========================================================================


def test_a_stated_iso_date_is_stored() -> None:
    assert resolve_due_date("2026-07-24", date(2026, 7, 20)) == date(2026, 7, 24)


def test_a_date_is_read_against_the_meeting_not_against_now() -> None:
    """A due date years before today still stands, because it is after *its* meeting.

    The model resolves "next Friday" against the meeting date and returns the ISO
    date; this only parses it, and compares it to the meeting and never to a
    clock. A 2020 date, long past by any run in 2026, is kept because it is after
    a 2020 meeting — proof nothing here reads the current date.
    """
    long_ago_meeting = date(2020, 1, 6)
    assert resolve_due_date("2020-01-10", long_ago_meeting) == date(2020, 1, 10)


def test_a_due_date_before_the_meeting_is_left_null() -> None:
    """A relative date resolved the wrong way, or a hallucination, is not stored."""
    assert resolve_due_date("2026-07-10", date(2026, 7, 20)) is None


@pytest.mark.parametrize("value", [None, 20260724, "next friday", "2026-13-01", "", "   "])
def test_a_date_that_cannot_be_resolved_is_left_null(value) -> None:
    assert resolve_due_date(value, date(2026, 7, 20)) is None


# ==========================================================================
# Malformed model output: valid drafts land, bad items are dropped
# ==========================================================================


def test_an_empty_or_non_mapping_response_is_the_empty_result() -> None:
    for raw in ({}, None, [], "nonsense", 42):
        assert parse_outcomes(raw, roster=ROSTER, meeting_date=date(2026, 7, 20)) == empty_result()


def test_a_summary_that_is_not_a_string_becomes_empty() -> None:
    parsed = parse_outcomes(
        {"summary": ["not", "a", "string"], "decisions": [], "action_items": []},
        roster=ROSTER,
        meeting_date=date(2026, 7, 20),
    )
    assert parsed["summary"] == ""


def test_a_decision_with_no_usable_text_is_dropped() -> None:
    parsed = parse_outcomes(
        {
            "summary": "s",
            "decisions": [
                {"text": 123, "excerpt": "a"},
                {"text": "   ", "excerpt": "b"},
                {"text": "Kept.", "excerpt": 999},
                "not even a mapping",
            ],
            "action_items": [],
        },
        roster=ROSTER,
        meeting_date=date(2026, 7, 20),
    )
    # Only the one with real text survives, and its non-string excerpt is emptied.
    assert parsed["decisions"] == [{"text": "Kept.", "excerpt": ""}]


def test_an_action_item_with_no_usable_description_is_dropped() -> None:
    parsed = parse_outcomes(
        {
            "summary": "s",
            "decisions": [],
            "action_items": [
                {"description": None, "owner": "Ada Kim", "due_date": None, "excerpt": ""},
                {
                    "description": "Cut the release.",
                    "owner": 123,
                    "due_date": ["not", "a", "date"],
                    "excerpt": "Speaker 1: cut it",
                },
            ],
        },
        roster=ROSTER,
        meeting_date=date(2026, 7, 20),
    )
    # The bad owner and bad date on the surviving item both fall to None.
    assert parsed["action_items"] == [
        {
            "description": "Cut the release.",
            "owner": None,
            "due_date": None,
            "excerpt": "Speaker 1: cut it",
        }
    ]


def test_more_items_than_expected_are_all_read() -> None:
    """A model that returns many items does not overflow anything; each is read."""
    raw = {
        "summary": "s",
        "decisions": [{"text": f"Decision {i}.", "excerpt": ""} for i in range(50)],
        "action_items": [],
    }
    parsed = parse_outcomes(raw, roster=ROSTER, meeting_date=date(2026, 7, 20))
    assert len(parsed["decisions"]) == 50


def test_a_valid_owner_and_date_survive_parsing() -> None:
    parsed = parse_outcomes(
        {
            "summary": "s",
            "decisions": [],
            "action_items": [
                {
                    "description": "Cut it.",
                    "owner": "ada",
                    "due_date": "2026-07-24",
                    "excerpt": "e",
                }
            ],
        },
        roster=ROSTER,
        meeting_date=date(2026, 7, 20),
    )
    assert parsed["action_items"][0]["owner"] == "Ada Kim"
    assert parsed["action_items"][0]["due_date"] == date(2026, 7, 24)


# --------------------------------------------------------------------------
# An empty transcript never reaches the client
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transcript", ["", "   \n  "])
def test_an_empty_transcript_makes_no_call(transcript: str) -> None:
    meeting = ExtractionInput(transcript=transcript, meeting_date=date(2026, 7, 20))

    assert extract_outcomes(meeting, client=RefusingExtractionClient()) == empty_result()


# --------------------------------------------------------------------------
# The stand-in clients
# --------------------------------------------------------------------------


def test_the_null_client_extracts_nothing() -> None:
    meeting = ExtractionInput(transcript="Speaker 1: hi", meeting_date=date(2026, 7, 20))
    assert NullExtractionClient().extract(meeting) == {
        "summary": "",
        "decisions": [],
        "action_items": [],
    }


def test_the_echo_client_is_deterministic_and_names_the_first_member() -> None:
    meeting = ExtractionInput(
        transcript="Speaker 1: hello there",
        meeting_date=date(2026, 7, 20),
        roster=("Ada Kim", "Bruno Sato"),
    )
    result = EchoExtractionClient().extract(meeting)

    assert result["action_items"][0]["owner"] == "Ada Kim"
    assert result["decisions"][0]["excerpt"] == "Speaker 1: hello there"


def test_no_stand_in_client_imports_the_sdk() -> None:
    source = (BASE_DIR / "ai" / "fakes.py").read_text()

    assert "openai" not in source


# ==========================================================================
# Writing the drafts: EXTRACTED, DRAFT, with excerpts and a summary
# ==========================================================================


@pytest.mark.django_db
def test_the_drafts_are_written_extracted_and_draft_with_excerpts_and_a_summary(
    meeting: Meeting,
) -> None:
    client = ScriptedExtractionClient(
        {
            "summary": "The team met and agreed to ship.",
            "decisions": [
                {"text": "Ship on Friday.", "excerpt": "Speaker 1: ship Friday"},
            ],
            "action_items": [
                {
                    "description": "Cut the release.",
                    "owner": "Ada Kim",
                    "due_date": "2026-08-01",
                    "excerpt": "Speaker 1: Ada cuts it",
                }
            ],
        }
    )

    extract_meeting_outcomes(meeting.record.pk, client=client)

    decision = Decision.objects.get(retrospective=meeting.retro)
    assert decision.source == Decision.Source.EXTRACTED
    assert decision.status == Decision.Status.DRAFT
    assert decision.text == "Ship on Friday."
    assert decision.excerpt == "Speaker 1: ship Friday"
    assert decision.created_by_id is None
    assert decision.cluster_id is None

    action = ActionItem.objects.get(retrospective=meeting.retro)
    assert action.source == ActionItem.Source.EXTRACTED
    assert action.review_status == ActionItem.ReviewStatus.DRAFT
    assert action.status == ActionItem.Status.OPEN
    assert action.owner_id == meeting.members[0].pk
    assert action.due_date == date(2026, 8, 1)
    assert action.excerpt == "Speaker 1: Ada cuts it"
    assert action.cluster_id is None

    assert meeting.reload_retro().extraction_summary == "The team met and agreed to ship."
    assert meeting.reload_record().status == Status.READY


@pytest.mark.django_db
def test_nothing_is_ever_written_confirmed() -> None:
    """Every extracted row is a draft; not one lands confirmed."""
    m = Meeting(member_names=["Ada Kim"])
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [{"text": "A decision.", "excerpt": "e"}],
            "action_items": [
                {"description": "A task.", "owner": "Ada Kim", "due_date": None, "excerpt": "e"}
            ],
        }
    )

    extract_meeting_outcomes(m.record.pk, client=client)

    assert not Decision.objects.filter(status=Decision.Status.CONFIRMED).exists()
    assert not ActionItem.objects.filter(review_status=ActionItem.ReviewStatus.CONFIRMED).exists()


@pytest.mark.django_db
def test_an_unmatched_owner_name_leaves_the_owner_null() -> None:
    m = Meeting(member_names=["Ada Kim", "Bruno Sato"])
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [],
            "action_items": [
                {"description": "Do it.", "owner": "Zed Zephyr", "due_date": None, "excerpt": ""}
            ],
        }
    )

    extract_meeting_outcomes(m.record.pk, client=client)

    assert ActionItem.objects.get(retrospective=m.retro).owner_id is None


@pytest.mark.django_db
def test_an_ambiguous_owner_name_leaves_the_owner_null() -> None:
    """Two members called Alex: the draft is written, but unassigned."""
    m = Meeting(member_names=["Alex Kim", "Alex Ng"])
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [],
            "action_items": [
                {"description": "Do it.", "owner": "Alex", "due_date": None, "excerpt": ""}
            ],
        }
    )

    extract_meeting_outcomes(m.record.pk, client=client)

    action = ActionItem.objects.get(retrospective=m.retro)
    assert action.owner_id is None
    assert action.description == "Do it."


@pytest.mark.django_db
def test_a_near_match_owner_resolves_to_the_member() -> None:
    m = Meeting(member_names=["Priya Patel", "Bruno Sato"])
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [],
            "action_items": [
                {"description": "Do it.", "owner": "Priyah", "due_date": None, "excerpt": ""}
            ],
        }
    )

    extract_meeting_outcomes(m.record.pk, client=client)

    assert ActionItem.objects.get(retrospective=m.retro).owner_id == m.members[0].pk


@pytest.mark.django_db
def test_an_empty_transcript_writes_no_drafts_and_finishes_ready() -> None:
    """A meeting where nothing was decided is a real outcome, not a failure."""
    m = Meeting(transcript="   ")

    extract_meeting_outcomes(m.record.pk, client=RefusingExtractionClient())

    assert not Decision.objects.filter(retrospective=m.retro).exists()
    assert not ActionItem.objects.filter(retrospective=m.retro).exists()
    assert m.reload_record().status == Status.READY


@pytest.mark.django_db
def test_a_failure_marks_failed_keeps_the_transcript_and_says_it_is_retryable(
    meeting: Meeting,
) -> None:
    client = ScriptedExtractionClient(ExtractionError("the extraction API refused the request"))

    extract_meeting_outcomes(meeting.record.pk, client=client)

    record = meeting.reload_record()
    assert record.status == Status.FAILED
    # Retryable, not re-uploadable: the message never tells them to upload again.
    assert RECOVERY in record.error_message
    assert "run again" in record.error_message
    assert "upload the file" not in record.error_message
    # The transcript, extraction's durable input, is kept for that retry.
    assert Transcript.objects.filter(record=record).exists()


@pytest.mark.django_db
def test_an_unexpected_failure_is_a_generic_message(meeting: Meeting) -> None:
    client = ScriptedExtractionClient(RuntimeError("something the SDK never named"))

    extract_meeting_outcomes(meeting.record.pk, client=client)

    record = meeting.reload_record()
    assert record.status == Status.FAILED
    assert UNEXPECTED.rstrip(".") in record.error_message
    assert "something the SDK never named" not in record.error_message


@pytest.mark.django_db
def test_malformed_output_still_lands_the_valid_drafts(meeting: Meeting) -> None:
    """One bad item does not fail the batch — the good decision still lands."""
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [
                {"text": 123, "excerpt": "dropped"},
                {"text": "Kept decision.", "excerpt": "e"},
            ],
            "action_items": [
                {"description": "   ", "owner": "Ada Kim", "due_date": None, "excerpt": ""},
            ],
        }
    )

    extract_meeting_outcomes(meeting.record.pk, client=client)

    assert [d.text for d in Decision.objects.filter(retrospective=meeting.retro)] == [
        "Kept decision."
    ]
    assert not ActionItem.objects.filter(retrospective=meeting.retro).exists()
    assert meeting.reload_record().status == Status.READY


@pytest.mark.django_db
def test_a_record_that_is_not_extracting_is_left_alone() -> None:
    m = Meeting(status=Status.READY)

    extract_meeting_outcomes(m.record.pk, client=RefusingExtractionClient())

    assert not Decision.objects.filter(retrospective=m.retro).exists()


@pytest.mark.django_db
def test_a_gone_record_is_a_return_not_an_error() -> None:
    extract_meeting_outcomes(123456789, client=RefusingExtractionClient())


@pytest.mark.django_db
def test_re_running_replaces_the_extracted_drafts_and_leaves_manual_ones_alone(
    meeting: Meeting,
) -> None:
    # A hand-written decision the facilitator typed, and a previously confirmed
    # extracted one, both of which a re-run must not touch.
    manual = Decision.objects.create(
        retrospective=meeting.retro,
        text="Typed by hand.",
        source=Decision.Source.MANUAL,
        status=Decision.Status.CONFIRMED,
    )
    confirmed = Decision.objects.create(
        retrospective=meeting.retro,
        text="Extracted, then confirmed in #24.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.CONFIRMED,
    )
    stale_draft = Decision.objects.create(
        retrospective=meeting.retro,
        text="A stale draft from the last run.",
        source=Decision.Source.EXTRACTED,
        status=Decision.Status.DRAFT,
    )

    client = ScriptedExtractionClient(
        {
            "summary": "fresh",
            "decisions": [{"text": "A fresh draft.", "excerpt": "e"}],
            "action_items": [],
        }
    )
    extract_meeting_outcomes(meeting.record.pk, client=client)

    texts = set(Decision.objects.filter(retrospective=meeting.retro).values_list("text", flat=True))
    assert manual.text in texts
    assert confirmed.text in texts
    assert stale_draft.text not in texts
    assert "A fresh draft." in texts


# --------------------------------------------------------------------------
# Privacy and ranking of what the writer sends the model
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_writer_sends_display_names_and_never_an_address_or_username() -> None:
    ada = make_user("ada_login", "Ada Kim")
    # A member with no display name falls back to the username, never the email.
    plain = User.objects.create_user(username="plainuser", password=PASSWORD, display_name="")
    plain.email = "plain@example.com"
    plain.save(update_fields=["email"])

    m = Meeting(member_names=[])
    Membership.objects.create(project=m.project, user=ada, role=Membership.Role.MEMBER)
    Membership.objects.create(project=m.project, user=plain, role=Membership.Role.MEMBER)

    client = ScriptedExtractionClient(empty_result())
    extract_meeting_outcomes(m.record.pk, client=client)

    roster = client.calls[0].roster
    assert "Ada Kim" in roster
    assert "plainuser" in roster  # display name empty, so the username stands in
    assert not any("@" in name for name in roster)
    assert "plain@example.com" not in roster


@pytest.mark.django_db
def test_the_agenda_is_ranked_by_vote_weight() -> None:
    """The DISCUSS ordering #16 defines: highest weight first, then position, then id."""
    m = Meeting(member_names=["Ada Kim", "Bruno Sato"], stage=Retrospective.Stage.COMPLETE)
    low = m.cluster("Low topic", position=1)
    high = m.cluster("High topic", position=2)
    none = m.cluster("Unvoted topic", position=3)
    m.vote(low, m.members[0], 1)
    m.vote(high, m.members[0], 3)
    m.vote(high, m.members[1], 2)

    client = ScriptedExtractionClient(empty_result())
    extract_meeting_outcomes(m.record.pk, client=client)

    agenda = client.calls[0].agenda
    assert [item.id for item in agenda] == [high.pk, low.pk, none.pk]
    assert [item.weight for item in agenda] == [5, 1, 0]
    # A cluster's integer id is a public handle; no card is reachable from here.
    assert all(isinstance(item.id, int) for item in agenda)


@pytest.mark.django_db
def test_extraction_does_not_bump_the_board_version(meeting: Meeting) -> None:
    """Drafts are reviewed in #24, not shown on the board, so no poll is woken."""
    before = meeting.reload_retro().version
    client = ScriptedExtractionClient(
        {
            "summary": "s",
            "decisions": [{"text": "A decision.", "excerpt": "e"}],
            "action_items": [],
        }
    )

    extract_meeting_outcomes(meeting.record.pk, client=client)

    assert meeting.reload_retro().version == before


# ==========================================================================
# Wiring: the pipeline enqueues extraction on commit, and it chains to READY
# ==========================================================================


def test_the_pipeline_enqueues_extraction_on_commit() -> None:
    source = (BASE_DIR / "meetings" / "pipeline.py").read_text()

    assert "enqueue_on_commit(extract_meeting_outcomes, record.pk)" in source
    assert ".enqueue(" not in source


def test_the_job_takes_an_id_rather_than_a_model_instance() -> None:
    import inspect

    from config.tasks import extract_meeting_outcomes as job

    signature = inspect.signature(job.func)
    assert list(signature.parameters) == ["record_id"]
    assert signature.parameters["record_id"].annotation is int


@pytest.mark.django_db
def test_transcription_chains_into_extraction_and_finishes_ready(
    settings, tmp_path, django_capture_on_commit_callbacks
) -> None:
    """End to end with the suite's inert clients: a pasted transcript reaches READY.

    The pipeline stores the transcript, moves the record to EXTRACTING and
    enqueues extraction on commit; executing the commit callbacks runs it with the
    default `NullExtractionClient`, which finds nothing and finishes the record.
    """
    settings.SCRATCH_DIR = tmp_path
    m = Meeting(status=Status.UPLOADED)
    # Give the record a real pasted-text file to read, the way an upload would.
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    media = uploads / "pasted.txt"
    media.write_bytes(b"Speaker 1: Nothing much was decided today.")
    m.transcript.delete()
    m.record.temp_path = str(media)
    m.record.media_deleted_at = None
    m.record.save(update_fields=["temp_path", "media_deleted_at"])

    with django_capture_on_commit_callbacks(execute=True):
        pipeline.process_meeting(m.record.pk)

    record = m.reload_record()
    assert record.status == Status.READY
    assert Transcript.objects.filter(record=record).exists()
