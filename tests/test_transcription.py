"""Transcription: the API seam, the stitching, and the media that gets deleted.

Every test here maps to an acceptance criterion of issue #21. Four themes run
through the file.

The first is that no test makes a network call and no test needs a key, and
neither is arranged by skipping. `config/settings_test.py` points
``TRANSCRIPTION_CLIENT`` at a stand-in for the whole suite, and the tests that
need to prove something about the real client hand
`ai.transcription.OpenAITranscriptionClient` a fake SDK object and assert what
was sent to it: the model name, one request per chunk, and their order. A test
that skipped itself without a key would fail the build (AGENTS.md, "CI") and,
worse, would have hidden the two things most worth checking.

The second is the `finally`. `_docs/decisions.md` item 6 says the recording is
deleted whether transcription succeeded, failed or raised, so the deletion is
asserted on the success path, on a failure forced mid-transcription, on a
failure in the first chunk, on an error nobody predicted, and when the media was
already missing. Each of those asserts absence — the file is gone, the scratch
tree is empty, no transcript row exists — rather than the presence of a status.

The third is where the retry sits. It is inside the job, around the API call,
while the audio still exists; there is none after the `finally`, because by then
there is nothing to retry against. The tests measure the backoff by handing the
retry loop a `sleep` that records instead of sleeping.

The fourth is that a failure is worse than useless if it does not say what to do
about it. Every failure test asserts the message names the recovery, and the
missing-key test asserts it names `OPENAI_API_KEY` rather than repeating an
authentication error out of the SDK.
"""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from ai import transcription
from ai.fakes import EchoTranscriptionClient
from ai.transcription import (
    BACKOFF_SECONDS,
    CHUNK_MARKER,
    MAX_ATTEMPTS,
    MODEL,
    ChunkTranscript,
    MissingAPIKeyError,
    OpenAITranscriptionClient,
    PermanentTranscriptionError,
    Segment,
    TransientTranscriptionError,
    classify,
    stitch,
    transcribe_chunks,
)
from config.tasks import process_meeting_record
from cycles.models import FeedbackCycle
from meetings import pipeline
from meetings.models import MeetingRecord, Transcript
from projects.models import Membership, Project
from retro.models import Retrospective

User = get_user_model()

BASE_DIR = Path(django_settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Kind = MeetingRecord.Kind
Status = MeetingRecord.Status


# --------------------------------------------------------------------------
# Fakes: the API seam, and the clients a test writes the answers for
# --------------------------------------------------------------------------


class FakeSegment:
    """One segment as the diarized response carries it: a label and words."""

    def __init__(self, speaker: str, text: str) -> None:
        self.speaker = speaker
        self.text = text


class FakeResponse:
    def __init__(self, segments, duration: float = 0.0, text: str = "") -> None:
        self.segments = segments
        self.duration = duration
        self.text = text


class FakeTranscriptions:
    """Stands in for `openai.OpenAI().audio.transcriptions`, and remembers."""

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
        self.audio = type("Audio", (), {})()
        self.audio.transcriptions = FakeTranscriptions(responses)

    @property
    def calls(self) -> list[dict]:
        return self.audio.transcriptions.calls


class ScriptedClient:
    """A transcription client whose answers a test writes out in order.

    An answer that is an exception is raised. The last answer repeats, so
    "always rate limited" is one argument rather than three.
    """

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[Path] = []

    def transcribe(self, path: Path) -> ChunkTranscript:
        self.calls.append(Path(path))
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class RefusingClient:
    """A client that fails the test if anything calls it.

    What proves a transcript file skipped transcription is that nothing was
    sent, not that the text came out right.
    """

    def transcribe(self, path: Path) -> ChunkTranscript:
        raise AssertionError(f"the API was called for {path}, and it should not have been")


class StatusWatchingClient:
    """Reads the record's status out of the database mid-request.

    The only way to prove the page from #19 can see TRANSCRIBING: by the time
    the API is being called, the committed row already says so.
    """

    def __init__(self, record_id: int) -> None:
        self.record_id = record_id
        self.seen: list[str] = []

    def transcribe(self, path: Path) -> ChunkTranscript:
        self.seen.append(MeetingRecord.objects.get(pk=self.record_id).status)
        return chunk("A", "Status was read from the database while this ran.")


def chunk(*pairs: str, duration: float = 60.0, language: str = "en") -> ChunkTranscript:
    """A chunk transcript from alternating speaker/text arguments."""
    segments = tuple(
        Segment(speaker=pairs[index], text=pairs[index + 1]) for index in range(0, len(pairs), 2)
    )
    return ChunkTranscript(segments=segments, language=language, duration_seconds=duration)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def scratch(settings, tmp_path):
    """Point SCRATCH_DIR at a directory this test owns, as the containers share one."""
    settings.SCRATCH_DIR = tmp_path
    return tmp_path


@pytest.fixture
def facilitator(db) -> User:
    return User.objects.create_user(
        username="facilitator", password=PASSWORD, display_name="Fay Facilitator"
    )


@pytest.fixture
def retro(facilitator: User) -> Retrospective:
    project = Project.objects.create(name="Platform", owner=facilitator)
    Membership.objects.create(project=project, user=facilitator, role=Membership.Role.FACILITATOR)
    cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )
    return Retrospective.objects.create(cycle=cycle, stage=Retrospective.Stage.DISCUSS)


def make_record(
    retro: Retrospective,
    user: User,
    scratch: Path,
    *,
    kind: str = Kind.AUDIO,
    contents: bytes = b"pretend this is a recording",
    filename: str = "standup.mp3",
) -> MeetingRecord:
    """A record whose `temp_path` really is a file on the shared volume."""
    uploads = scratch / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    media = uploads / "0123456789abcdef"
    media.write_bytes(contents)
    return MeetingRecord.objects.create(
        retrospective=retro,
        uploaded_by=user,
        kind=kind,
        temp_path=str(media),
        original_filename=filename,
        size_bytes=len(contents),
        status=Status.UPLOADED,
    )


@pytest.fixture
def record(retro: Retrospective, facilitator: User, scratch: Path) -> MeetingRecord:
    return make_record(retro, facilitator, scratch)


@pytest.fixture
def chunked(monkeypatch):
    """Stand in for #20's `prepare_audio_chunks`, without needing ffmpeg.

    It writes real files into the work directory the pipeline made, so what the
    `finally` has to clean up is real too. How many chunks it produces is set
    per test.

    The pipeline's own call into #20 is asserted separately, in
    `test_the_chunks_come_from_the_prepared_audio`.
    """

    def use(count: int = 1) -> list[Path]:
        produced: list[Path] = []

        def fake_prepare(source, *, work_root=None, **kwargs):
            root = Path(work_root)
            root.mkdir(parents=True, exist_ok=True)
            for index in range(count):
                part = root / f"chunk-{index:05d}.opus"
                part.write_bytes(Path(source).read_bytes())
                produced.append(part)
            return list(produced)

        monkeypatch.setattr(pipeline, "prepare_audio_chunks", fake_prepare)
        return produced

    return use


@pytest.fixture
def no_waiting(monkeypatch):
    """Keep the real retry budget, spend none of the real seconds on it."""
    monkeypatch.setattr(transcription, "BACKOFF_SECONDS", (0.0,) * (MAX_ATTEMPTS - 1))


def files_left(root: Path) -> list[Path]:
    """Everything still on the shared volume, however it got there."""
    return sorted(path for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_transcript_belongs_to_one_record_and_carries_the_meeting(
    record: MeetingRecord,
) -> None:
    stored = Transcript.objects.create(
        record=record, text="Speaker 1: we ship on Friday.", language="en", duration_seconds=91.5
    )

    assert record.transcript == stored
    assert stored.text == "Speaker 1: we ship on Friday."
    assert stored.language == "en"
    assert stored.duration_seconds == 91.5
    assert stored.created_at is not None


@pytest.mark.django_db
def test_a_record_cannot_have_two_transcripts(record: MeetingRecord) -> None:
    """One to one, held by the database: the transcript *is* the record's text."""
    Transcript.objects.create(record=record, text="The first one.")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Transcript.objects.create(record=record, text="A second one.")

    assert Transcript.objects.count() == 1


@pytest.mark.django_db
def test_a_transcript_that_arrived_as_text_has_no_duration(record: MeetingRecord) -> None:
    """Null, not zero: nobody measured it, and zero would say somebody had."""
    stored = Transcript.objects.create(record=record, text="Pasted.", duration_seconds=None)

    assert stored.duration_seconds is None


# --------------------------------------------------------------------------
# The API seam: the model name, the requests, and their order
# --------------------------------------------------------------------------


def test_the_model_is_the_diarizing_one() -> None:
    assert MODEL == "gpt-4o-transcribe-diarize"


def test_no_other_model_is_named_anywhere_in_the_module() -> None:
    """`whisper-1` is the legacy snapshot and `gpt-4o` is superseded.

    Neither is a fallback, so neither may appear — a fallback that only fires
    when the real model is down is a transcript nobody knows is worse.
    """
    source = (BASE_DIR / "ai" / "transcription.py").read_text()
    # Every string literal in the module that looks like a model name. The
    # prose may discuss the other two; nothing may send them.
    literals = set(re.findall(r"\"([\w.-]*(?:gpt|whisper)[\w.-]*)\"", source))

    assert literals == {MODEL}


def test_the_client_sends_one_request_per_chunk_in_order(tmp_path) -> None:
    paths = [tmp_path / f"chunk-{index}.opus" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_bytes(b"audio" * (index + 1))
    sdk = FakeSDK(
        FakeResponse([FakeSegment("A", "one")], duration=10.0),
        FakeResponse([FakeSegment("A", "two")], duration=20.0),
        FakeResponse([FakeSegment("A", "three")], duration=30.0),
    )

    result = transcribe_chunks(paths, client=OpenAITranscriptionClient(api_key="k", sdk=sdk))

    assert [call["model"] for call in sdk.calls] == [MODEL] * 3
    assert [Path(call["file"].name) for call in sdk.calls] == paths
    assert result.chunk_count == 3
    assert result.duration_seconds == 60.0


def test_the_request_asks_for_the_diarized_response(tmp_path) -> None:
    """Plain `json` returns the words and loses who said them."""
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    sdk = FakeSDK(FakeResponse([FakeSegment("A", "hello")]))

    transcribe_chunks([path], client=OpenAITranscriptionClient(api_key="k", sdk=sdk))

    assert sdk.calls[0]["response_format"] == "diarized_json"
    # Required by this model for anything over 30 seconds, and every chunk can be.
    assert sdk.calls[0]["chunking_strategy"] == "auto"


def test_speaker_labels_survive_into_the_stored_text(tmp_path) -> None:
    """They are what makes owner extraction in #23 work at all."""
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    sdk = FakeSDK(
        FakeResponse(
            [
                FakeSegment("A", "We ship on Friday."),
                FakeSegment("B", "I will write the release note."),
                FakeSegment("A", "Thanks."),
            ]
        )
    )

    result = transcribe_chunks([path], client=OpenAITranscriptionClient(api_key="k", sdk=sdk))

    assert result.text == (
        "Speaker 1: We ship on Friday.\n"
        "Speaker 2: I will write the release note.\n"
        "Speaker 1: Thanks."
    )
    assert result.speaker_count == 2


def test_a_response_with_no_segments_keeps_the_words(tmp_path) -> None:
    """Losing who said it is bad. Losing what was said is worse."""
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    sdk = FakeSDK(FakeResponse([], text="Undiarized but real."))

    result = transcribe_chunks([path], client=OpenAITranscriptionClient(api_key="k", sdk=sdk))

    assert result.text == "Undiarized but real."
    assert result.speaker_count == 0


def test_an_empty_response_is_a_failure_rather_than_an_empty_transcript(tmp_path) -> None:
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    sdk = FakeSDK(FakeResponse([], text="   "))

    with pytest.raises(PermanentTranscriptionError, match="empty transcript"):
        transcribe_chunks([path], client=OpenAITranscriptionClient(api_key="k", sdk=sdk))


def test_nothing_to_transcribe_is_a_failure(tmp_path) -> None:
    with pytest.raises(PermanentTranscriptionError, match="no audio"):
        transcribe_chunks([], client=ScriptedClient())


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------


def test_a_missing_key_names_the_variable(settings) -> None:
    """Not an authentication error out of the SDK: the variable, by name."""
    settings.OPENAI_API_KEY = ""

    with pytest.raises(MissingAPIKeyError) as raised:
        OpenAITranscriptionClient()

    assert "OPENAI_API_KEY" in str(raised.value)


def test_the_key_is_read_from_the_environment_through_settings(settings) -> None:
    settings.OPENAI_API_KEY = "sk-from-the-environment"

    assert OpenAITranscriptionClient().api_key == "sk-from-the-environment"


def test_the_example_environment_file_names_the_key_and_carries_no_value() -> None:
    """A checked-in secret is the one thing this file must never become."""
    lines = (BASE_DIR / ".env.example").read_text().splitlines()
    keys = [line for line in lines if line.startswith("OPENAI_API_KEY")]

    assert keys == ["OPENAI_API_KEY="]


def test_the_suite_never_reaches_the_real_client() -> None:
    """The fake is the default for the whole suite, not a per-test mock."""
    assert django_settings.TRANSCRIPTION_CLIENT == "ai.fakes.EchoTranscriptionClient"
    assert django_settings.OPENAI_API_KEY == ""


# --------------------------------------------------------------------------
# Retrying, inside the job, while the audio still exists
# --------------------------------------------------------------------------


def test_the_budget_and_the_backoff_agree() -> None:
    """One wait per retry. A mismatch would be a budget nobody chose."""
    assert len(BACKOFF_SECONDS) == MAX_ATTEMPTS - 1


def test_a_transient_failure_is_retried_with_backoff(tmp_path) -> None:
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    client = ScriptedClient(
        TransientTranscriptionError("the transcription API returned 429"),
        TransientTranscriptionError("the transcription API returned 503"),
        chunk("A", "It worked on the third go."),
    )
    waits: list[float] = []

    result = transcribe_chunks([path], client=client, sleep=waits.append)

    assert len(client.calls) == 3
    assert waits == list(BACKOFF_SECONDS)
    assert result.text == "Speaker 1: It worked on the third go."
    assert result.attempts == 3


def test_the_retry_budget_is_bounded(tmp_path) -> None:
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    client = ScriptedClient(TransientTranscriptionError("the transcription API returned 429"))
    waits: list[float] = []

    with pytest.raises(PermanentTranscriptionError) as raised:
        transcribe_chunks([path], client=client, sleep=waits.append)

    assert len(client.calls) == MAX_ATTEMPTS
    assert len(waits) == MAX_ATTEMPTS - 1
    assert f"after {MAX_ATTEMPTS} attempts" in str(raised.value)


def test_a_permanent_failure_is_not_retried(tmp_path) -> None:
    """Sending the same rejected request again could only be rejected again."""
    path = tmp_path / "chunk.opus"
    path.write_bytes(b"audio")
    client = ScriptedClient(PermanentTranscriptionError("the transcription API refused it (400)"))

    with pytest.raises(PermanentTranscriptionError):
        transcribe_chunks([path], client=client, sleep=lambda seconds: None)

    assert len(client.calls) == 1


def test_one_chunk_failing_fails_the_whole_recording(tmp_path) -> None:
    """A transcript missing its middle is worse than no transcript."""
    paths = [tmp_path / "a.opus", tmp_path / "b.opus", tmp_path / "c.opus"]
    for path in paths:
        path.write_bytes(b"audio")
    client = ScriptedClient(
        chunk("A", "The first chunk."),
        PermanentTranscriptionError("the transcription API refused the request (400)"),
        chunk("A", "The third chunk, which nobody will ever see."),
    )

    with pytest.raises(PermanentTranscriptionError, match="Chunk 2 of 3"):
        transcribe_chunks(paths, client=client, sleep=lambda seconds: None)

    # It stopped there rather than carrying on and stitching a hole.
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("status", "transient"),
    [(429, True), (500, True), (503, True), (408, True), (400, False), (404, False)],
)
def test_status_codes_are_sorted_into_worth_retrying_and_not(status: int, transient: bool) -> None:
    # httpx is the SDK's own transport and comes with it; building a real
    # `openai` error is the only way to prove the real classification.
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    response = httpx.Response(status, request=request)
    error = openai.APIStatusError("boom", response=response, body=None)

    assert isinstance(classify(error), TransientTranscriptionError) is transient


def test_a_timeout_is_worth_retrying() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")

    assert isinstance(classify(openai.APITimeoutError(request)), TransientTranscriptionError)


def test_a_rejected_key_says_which_variable_holds_it() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError("nope", response=response, body=None)

    translated = classify(error)

    assert isinstance(translated, PermanentTranscriptionError)
    assert "OPENAI_API_KEY" in str(translated)


def test_something_that_is_not_an_sdk_error_is_left_alone() -> None:
    """Only what the SDK raises is translated; the rest keeps its own type."""
    assert classify(ValueError("not from the SDK")) is None


# --------------------------------------------------------------------------
# Stitching, and what it admits it gets wrong
# --------------------------------------------------------------------------


def test_one_chunk_is_labelled_by_the_api_and_says_nothing_about_chunks() -> None:
    """The ordinary meeting: one request, the API's own numbering, no marker."""
    stitched = stitch([chunk("A", "Morning.", "B", "Morning.")])

    assert stitched.text == "Speaker 1: Morning.\nSpeaker 2: Morning."
    assert CHUNK_MARKER.format(number=2) not in stitched.text


def test_chunks_are_stitched_and_the_seam_is_written_into_the_text() -> None:
    """Not concatenated: chunk two's `A` is not necessarily chunk one's `A`.

    The heuristic renumbers each chunk by order of first appearance, and says
    where it did so, because getting it silently wrong is the one option the
    issue rules out.
    """
    stitched = stitch(
        [
            chunk("A", "I will take the deploy.", "B", "And I will review it."),
            chunk("C", "Back from the kitchen.", "A", "Welcome back."),
        ]
    )

    assert stitched.text == (
        "Speaker 1: I will take the deploy.\n"
        "Speaker 2: And I will review it.\n"
        "\n"
        f"{CHUNK_MARKER.format(number=2)}\n"
        "\n"
        "Speaker 1: Back from the kitchen.\n"
        "Speaker 2: Welcome back."
    )


def test_a_speakers_consecutive_turns_are_one_line() -> None:
    stitched = stitch([chunk("A", "First thought.", "A", "Second thought.", "B", "Noted.")])

    assert stitched.text == "Speaker 1: First thought. Second thought.\nSpeaker 2: Noted."


def test_the_stitching_limits_are_written_down_where_the_code_is() -> None:
    """A wrong-but-explained heuristic is acceptable; an unexplained one is not."""
    documentation = transcription.stitch.__doc__

    assert "heuristic" in documentation.lower()
    assert "shifted" in documentation


def test_the_durations_add_up_and_the_language_carries() -> None:
    stitched = stitch(
        [chunk("A", "one", duration=90.0), chunk("A", "two", duration=30.0, language="")]
    )

    assert stitched.duration_seconds == 120.0
    assert stitched.language == "en"


# --------------------------------------------------------------------------
# The pipeline: the happy path
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_recording_becomes_a_transcript_and_the_record_moves_on(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    chunked(1)
    client = ScriptedClient(chunk("A", "We ship on Friday.", "B", "I will write the note."))

    pipeline.process_meeting(record.pk, client=client)

    record.refresh_from_db()
    assert record.status == Status.EXTRACTING
    assert record.error_message == ""
    assert record.attempts == 1
    assert record.transcript.text == (
        "Speaker 1: We ship on Friday.\nSpeaker 2: I will write the note."
    )
    assert record.transcript.language == "en"
    assert record.transcript.duration_seconds == 60.0


@pytest.mark.django_db
def test_the_recording_is_gone_on_the_success_path(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """The `finally` runs when everything worked, too."""
    media = Path(record.temp_path)
    chunked(2)

    pipeline.process_meeting(record.pk, client=ScriptedClient(chunk("A", "Said something.")))

    record.refresh_from_db()
    assert not media.exists()
    assert record.temp_path is None
    assert record.media_deleted_at is not None
    # The chunks cut from it are gone as well; nothing is left on the volume.
    assert files_left(scratch) == []


@pytest.mark.django_db
def test_the_transcript_in_postgres_is_the_only_thing_left(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """`_docs/decisions.md` item 6, as an assertion about the disk."""
    chunked(1)

    pipeline.process_meeting(record.pk, client=ScriptedClient(chunk("A", "The whole meeting.")))

    assert files_left(scratch) == []
    assert Transcript.objects.get().text == "Speaker 1: The whole meeting."


@pytest.mark.django_db
def test_every_chunk_is_sent_in_playback_order(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    chunks = chunked(3)
    client = ScriptedClient(chunk("A", "one"), chunk("A", "two"), chunk("A", "three"))

    pipeline.process_meeting(record.pk, client=client)

    assert client.calls == chunks
    assert record.transcript.text.count(CHUNK_MARKER.format(number=2)) == 1


@pytest.mark.django_db
def test_the_chunks_come_from_the_prepared_audio(
    record: MeetingRecord, scratch: Path, monkeypatch
) -> None:
    """#20 does the preparing, and it is handed the file the upload wrote.

    The 25 MB cap is its problem: nothing in the pipeline re-chunks or
    re-measures what comes back.
    """
    seen: dict = {}

    def fake_prepare(source, *, work_root=None, **kwargs):
        seen["source"] = Path(source)
        seen["work_root"] = Path(work_root)
        part = Path(work_root) / "chunk-00000.opus"
        part.write_bytes(b"prepared")
        return [part]

    monkeypatch.setattr(pipeline, "prepare_audio_chunks", fake_prepare)
    media = Path(record.temp_path)

    pipeline.process_meeting(record.pk, client=ScriptedClient(chunk("A", "Prepared.")))

    assert seen["source"] == media
    assert seen["work_root"].is_relative_to(scratch)


@pytest.mark.django_db
def test_the_record_says_transcribing_while_the_api_is_being_called(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """Which is what the page from #19 polls for and shows in words."""
    chunked(1)
    client = StatusWatchingClient(record.pk)

    pipeline.process_meeting(record.pk, client=client)

    assert client.seen == [Status.TRANSCRIBING]


@pytest.mark.django_db
def test_the_upload_page_shows_the_transcribing_status(
    record: MeetingRecord, facilitator: User, retro: Retrospective, client: Client
) -> None:
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)
    client.login(username=facilitator.username, password=PASSWORD)

    body = client.get(reverse("meeting-upload", args=[retro.pk])).content.decode()

    assert "Listening to the meeting and writing down what was said." in body
    assert 'data-polling="true"' in body


# --------------------------------------------------------------------------
# The pipeline: text that arrived as text
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("kind", [Kind.PASTED_TEXT, Kind.TRANSCRIPT_FILE])
def test_text_becomes_a_transcript_without_any_api_call(
    retro: Retrospective, facilitator: User, scratch: Path, kind: str
) -> None:
    record = make_record(
        retro,
        facilitator,
        scratch,
        kind=kind,
        contents=b"Speaker 1: it is already written down.\n",
        filename="notes.txt",
    )
    media = Path(record.temp_path)

    pipeline.process_meeting(record.pk, client=RefusingClient())

    record.refresh_from_db()
    assert record.status == Status.EXTRACTING
    assert record.transcript.text == "Speaker 1: it is already written down."
    # Nobody measured a duration, so none is claimed.
    assert record.transcript.duration_seconds is None
    # The scratch copy of the text goes the same way the audio does.
    assert not media.exists()
    assert record.temp_path is None
    assert record.media_deleted_at is not None


@pytest.mark.django_db
def test_text_never_passes_through_transcribing(
    retro: Retrospective, facilitator: User, scratch: Path
) -> None:
    """There is nothing to transcribe, so the page never claims there is.

    Claiming the record is where a run would say so, and for text it does not:
    the attempt is counted and the status is left where it was until the
    transcript is stored and the record goes straight to EXTRACTING.
    """
    record = make_record(retro, facilitator, scratch, kind=Kind.PASTED_TEXT, contents=b"Notes.")

    pipeline._claim(record)

    record.refresh_from_db()
    assert record.status == Status.UPLOADED
    assert record.attempts == 1


@pytest.mark.django_db
def test_an_empty_pasted_transcript_fails_rather_than_storing_nothing(
    retro: Retrospective, facilitator: User, scratch: Path
) -> None:
    record = make_record(retro, facilitator, scratch, kind=Kind.PASTED_TEXT, contents=b"   \n")

    pipeline.process_meeting(record.pk, client=RefusingClient())

    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert Transcript.objects.count() == 0
    assert files_left(scratch) == []


# --------------------------------------------------------------------------
# The pipeline: failure, and the `finally` that runs anyway
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_failure_midway_through_still_deletes_the_recording(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """Forced on the second of three chunks: the file must not survive it.

    A failed transcription leaving a recording of a team's meeting on disk is
    the failure mode this whole design exists to rule out.
    """
    media = Path(record.temp_path)
    chunked(3)
    client = ScriptedClient(
        chunk("A", "The first chunk arrived."),
        PermanentTranscriptionError("the transcription API refused the request (400)"),
    )

    pipeline.process_meeting(record.pk, client=client)

    record.refresh_from_db()
    assert not media.exists()
    assert files_left(scratch) == []
    assert record.temp_path is None
    assert record.media_deleted_at is not None
    assert record.status == Status.FAILED
    # Nothing partial was kept: half a meeting is not a transcript.
    assert Transcript.objects.count() == 0


@pytest.mark.django_db
def test_the_failure_message_says_the_file_has_to_be_uploaded_again(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """`_docs/decisions.md` item 6: there is no retry, so the words are the recovery."""
    chunked(1)
    client = ScriptedClient(PermanentTranscriptionError("the transcription API refused it (400)"))

    pipeline.process_meeting(record.pk, client=client)

    record.refresh_from_db()
    assert "upload the file once more" in record.error_message
    assert "deleted" in record.error_message
    assert "refused it (400)" in record.error_message


@pytest.mark.django_db
def test_a_record_fails_once_the_retries_are_exhausted(
    record: MeetingRecord, scratch: Path, chunked, no_waiting
) -> None:
    """The retries happen inside the job; the failure is what comes out of it."""
    chunked(1)
    client = ScriptedClient(TransientTranscriptionError("the transcription API returned 429"))
    media = Path(record.temp_path)

    pipeline.process_meeting(record.pk, client=client)

    record.refresh_from_db()
    assert len(client.calls) == MAX_ATTEMPTS
    assert record.status == Status.FAILED
    assert f"after {MAX_ATTEMPTS} attempts" in record.error_message
    assert not media.exists()


@pytest.mark.django_db
def test_a_failure_nobody_predicted_still_deletes_the_recording(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """`finally`, not `except`: the clause that runs for the errors we did not name."""
    media = Path(record.temp_path)
    chunked(1)

    class Exploding:
        def transcribe(self, path):
            raise ZeroDivisionError("something nobody wrote a message for")

    pipeline.process_meeting(record.pk, client=Exploding())

    record.refresh_from_db()
    assert not media.exists()
    assert record.status == Status.FAILED
    # The traceback went to the log, not onto the facilitator's page.
    assert "ZeroDivisionError" not in record.error_message
    assert "Something went wrong" in record.error_message
    assert "upload the file once more" in record.error_message


@pytest.mark.django_db
def test_a_failure_preparing_the_audio_still_deletes_the_recording(
    record: MeetingRecord, scratch: Path, monkeypatch
) -> None:
    """The recording can be undecodable, and it still does not stay on disk."""
    from ai.audio import NoAudioTrackError

    def refuse(source, *, work_root=None, **kwargs):
        Path(work_root).mkdir(parents=True, exist_ok=True)
        (Path(work_root) / "half-written.opus").write_bytes(b"partial")
        raise NoAudioTrackError("standup.mp3 has no audio track to transcribe")

    monkeypatch.setattr(pipeline, "prepare_audio_chunks", refuse)
    media = Path(record.temp_path)

    pipeline.process_meeting(record.pk, client=ScriptedClient())

    record.refresh_from_db()
    assert not media.exists()
    assert files_left(scratch) == []
    assert record.status == Status.FAILED
    assert "no audio track" in record.error_message


@pytest.mark.django_db
def test_a_missing_key_fails_the_record_by_name(
    record: MeetingRecord, scratch: Path, chunked, settings
) -> None:
    """Named, not an SDK authentication error, and no audio is prepared first."""
    settings.TRANSCRIPTION_CLIENT = "ai.transcription.OpenAITranscriptionClient"
    settings.OPENAI_API_KEY = ""
    chunked(1)
    media = Path(record.temp_path)

    pipeline.process_meeting(record.pk)

    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert "OPENAI_API_KEY" in record.error_message
    assert "upload the file once more" in record.error_message
    assert not media.exists()


@pytest.mark.django_db
def test_a_recording_that_is_no_longer_there_fails_without_crashing(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """The worker can arrive after something else cleaned the volume."""
    chunked(1)
    Path(record.temp_path).unlink()

    pipeline.process_meeting(record.pk, client=ScriptedClient())

    record.refresh_from_db()
    assert record.status == Status.FAILED
    assert "no longer on the shared volume" in record.error_message
    # There was nothing to delete, and the row says so rather than pretending.
    assert record.temp_path is None
    assert record.media_deleted_at is not None


# --------------------------------------------------------------------------
# The job the worker runs
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_job_runs_the_whole_pipeline_with_the_configured_client(
    record: MeetingRecord, scratch: Path, chunked
) -> None:
    """No client is injected here: the suite's default is `ai.fakes`, with no key."""
    chunked(1)
    media = Path(record.temp_path)

    process_meeting_record.func(record.pk)

    record.refresh_from_db()
    assert record.status == Status.EXTRACTING
    assert "stand-in client" in record.transcript.text
    assert not media.exists()


@pytest.mark.django_db
def test_the_job_leaves_a_record_another_worker_has_claimed_alone(
    record: MeetingRecord, scratch: Path
) -> None:
    """Re-running by hand is a deliberate act, and it is not a second attempt."""
    MeetingRecord.objects.filter(pk=record.pk).update(status=Status.TRANSCRIBING)
    media = Path(record.temp_path)

    process_meeting_record.func(record.pk)

    record.refresh_from_db()
    assert record.status == Status.TRANSCRIBING
    assert record.attempts == 0
    # It did not touch the file either, which is what makes this safe to call.
    assert media.is_file()


@pytest.mark.django_db
def test_the_job_tolerates_the_record_having_gone(scratch: Path) -> None:
    process_meeting_record.func(987654321)


def test_nothing_retries_the_job_itself() -> None:
    """The retry is inside the body, around the API call, never around the job.

    After the `finally` the recording is gone, so a queue-level retry could only
    fail differently. AGENTS.md says so, `config/tasks.py` says so, and this
    asserts the pipeline is not quietly arranging one of its own.

    Chaining to the *next* stage is a different thing and is allowed: the store
    enqueues #23's extraction on commit, exactly as #22's reveal enqueues
    clustering. What the pipeline never does is re-enqueue its own job — that is
    the self-retry this test forbids.
    """
    source = (BASE_DIR / "meetings" / "pipeline.py").read_text()

    assert ".enqueue(" not in source
    assert "enqueue_on_commit(process_meeting_record" not in source
    # The delete is the last thing the run does, and the retry is upstream of it.
    assert source.index("transcribe_chunks(chunks") < source.index("def _discard_media")


# --------------------------------------------------------------------------
# The stand-in client
# --------------------------------------------------------------------------


def test_the_stand_in_client_needs_no_key_and_no_network(tmp_path) -> None:
    path = tmp_path / "chunk-00000.opus"
    path.write_bytes(b"audio bytes")

    result = EchoTranscriptionClient().transcribe(path)

    assert result.segments[0].speaker == "A"
    assert "chunk-00000.opus" in result.segments[0].text
    assert result.duration_seconds > 0


def test_the_stand_in_client_does_not_import_the_sdk() -> None:
    """It is a stand-in for the SDK, so reaching for it would be a contradiction."""
    source = (BASE_DIR / "ai" / "fakes.py").read_text()

    assert "openai" not in source
