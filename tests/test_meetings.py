"""Handing a meeting over, and watching it being processed.

Every test here maps to an acceptance criterion of issue #19. Four themes run
through the file.

The first is that a refusal is proved by attempting it. A member who may not
upload posts a real multipart form with a real CSRF token and is refused; the
test then asserts no row was written and nothing was left in the scratch
directory. A hidden button proves nothing.

The second is absence. Where the page must not offer something — the form
before the discussion, a poll on a record that has finished, a retry on a
failure whose recording has been deleted — the test asserts the control and its
markup are gone, not merely that some other text is present.

The third is that the file has to land where the worker can read it.
`SCRATCH_DIR` is a volume `web` and `worker` share, so every test that uploads
asserts the stored path is inside it, and asserts that no part of it came from
the name the uploader chose.

The fourth is the queue. The job is enqueued on commit, so a plain `django_db`
test would never run it; the tests that care use
`django_capture_on_commit_callbacks`, which is also what proves the enqueue was
deferred rather than done inline.
"""

import inspect
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.urls import reverse

from config.tasks import process_meeting_record
from cycles.models import FeedbackCycle
from meetings.forms import MeetingUploadForm
from meetings.models import MeetingRecord
from meetings.services import store_meeting_record, upload_is_open
from meetings.uploads import (
    ALLOWED_EXTENSIONS,
    AUDIO_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    TRANSCRIPT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    generated_upload_path,
    upload_root,
)
from projects.models import Membership, Project
from retro.models import STAGE_ORDER, Retrospective

User = get_user_model()

BASE_DIR = Path(settings.BASE_DIR)
PASSWORD = "keel-haul-mizzen-41"

MONDAY = date(2026, 7, 20)
OPENS_AT = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)

Stage = Retrospective.Stage
Kind = MeetingRecord.Kind
Status = MeetingRecord.Status

#: The stages before there is a meeting to hand over, and the stages from which
#: there is one. Derived from the order rather than typed out, so a stage added
#: later falls on one side or the other by itself.
DISCUSS_INDEX = STAGE_ORDER.index(Stage.DISCUSS)
BEFORE_DISCUSS = list(STAGE_ORDER[:DISCUSS_INDEX])
FROM_DISCUSS = list(STAGE_ORDER[DISCUSS_INDEX:])

MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 64


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def make_user(username: str, display_name: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, display_name=display_name)


def log_in(client: Client, user: User) -> None:
    client.login(username=user.username, password=PASSWORD)


def upload_url(retro: Retrospective) -> str:
    return reverse("meeting-upload", args=[retro.pk])


def status_url(record: MeetingRecord) -> str:
    return reverse("meeting-record-status", args=[record.pk])


def audio_file(name: str = "standup.mp3") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, MP3, content_type="audio/mpeg")


def stored_files() -> list[Path]:
    """Everything sitting in the upload area, however it got there."""
    root = upload_root()
    return sorted(path for path in root.glob("*")) if root.is_dir() else []


@pytest.fixture
def scratch(settings, tmp_path):
    """Point SCRATCH_DIR at a directory this test owns, as `web` and `worker` share one."""
    settings.SCRATCH_DIR = tmp_path
    return tmp_path


@pytest.fixture
def owner(db) -> User:
    return make_user("owner", "Olive Owner")


@pytest.fixture
def project(owner: User) -> Project:
    project = Project.objects.create(name="Platform", owner=owner)
    Membership.objects.create(project=project, user=owner, role=Membership.Role.FACILITATOR)
    return project


@pytest.fixture
def facilitator(project: Project) -> User:
    """This cycle's facilitator: the one person who may hand the meeting over."""
    user = make_user("facilitator", "Fay Facilitator")
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def member(project: Project) -> User:
    user = make_user("member", "Mel Member")
    Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
    return user


@pytest.fixture
def other_facilitator(project: Project) -> User:
    """A facilitator of the project who is not this cycle's facilitator."""
    user = make_user("other", "Otto Other")
    Membership.objects.create(project=project, user=user, role=Membership.Role.FACILITATOR)
    return user


@pytest.fixture
def outsider(db) -> User:
    return make_user("outsider", "Ora Outsider")


@pytest.fixture
def cycle(project: Project, facilitator: User) -> FeedbackCycle:
    return FeedbackCycle.objects.create(
        project=project,
        week_start=MONDAY,
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )


@pytest.fixture
def retro(cycle: FeedbackCycle) -> Retrospective:
    """A retrospective in DISCUSS: the meeting has happened."""
    return Retrospective.objects.create(cycle=cycle, stage=Stage.DISCUSS)


@pytest.fixture
def as_facilitator(client: Client, facilitator: User) -> Client:
    log_in(client, facilitator)
    return client


def csrf_client(user: User) -> Client:
    """A client that checks CSRF, carrying a token it was really given.

    A refusal is only worth asserting if the request would otherwise have
    succeeded, so the tests that prove one send a valid token rather than
    relying on the middleware to turn them away first.
    """
    client = Client(enforce_csrf_checks=True)
    log_in(client, user)
    # Any application page renders `{% csrf_token %}` in the log-out form, so
    # this is where the cookie comes from.
    client.get(reverse("project-list"))
    return client


def csrf_token(client: Client) -> str:
    return client.cookies["csrftoken"].value


def at_stage(retro: Retrospective, stage: str) -> Retrospective:
    """Put a retrospective straight into `stage`, without the stage machine."""
    Retrospective.objects.filter(pk=retro.pk).update(stage=stage)
    retro.refresh_from_db()
    return retro


def make_record(retro: Retrospective, user: User, **kwargs) -> MeetingRecord:
    return MeetingRecord.objects.create(
        retrospective=retro,
        uploaded_by=user,
        kind=kwargs.pop("kind", Kind.AUDIO),
        temp_path=kwargs.pop("temp_path", "/scratch/uploads/deadbeef"),
        original_filename=kwargs.pop("original_filename", "standup.mp3"),
        size_bytes=kwargs.pop("size_bytes", 1024),
        **kwargs,
    )


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_record_carries_everything_the_pipeline_needs(
    retro: Retrospective, facilitator: User
) -> None:
    record = make_record(retro, facilitator)

    assert record.retrospective == retro
    assert record.uploaded_by == facilitator
    assert record.kind == Kind.AUDIO
    assert record.temp_path == "/scratch/uploads/deadbeef"
    assert record.original_filename == "standup.mp3"
    assert record.size_bytes == 1024
    # The state every record starts in, and the two counters the pipeline owns.
    assert record.status == Status.UPLOADED
    assert record.attempts == 0
    assert record.error_message == ""
    assert record.created_at is not None
    assert record.media_deleted_at is None


def test_the_four_kinds_and_the_five_statuses_are_the_ones_the_issue_names() -> None:
    assert [kind.value for kind in Kind] == ["AUDIO", "VIDEO", "TRANSCRIPT_FILE", "PASTED_TEXT"]
    assert [status.value for status in Status] == [
        "UPLOADED",
        "TRANSCRIBING",
        "EXTRACTING",
        "READY",
        "FAILED",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [Status.UPLOADED, Status.TRANSCRIBING, Status.EXTRACTING, Status.READY]
)
def test_a_second_record_is_refused_while_one_is_not_failed(
    retro: Retrospective, facilitator: User, status: str
) -> None:
    """The database holds the rule, so two requests cannot both win the check."""
    make_record(retro, facilitator, status=status)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_record(retro, facilitator)

    assert MeetingRecord.objects.filter(retrospective=retro).count() == 1


@pytest.mark.django_db
def test_uploading_again_is_allowed_once_the_last_attempt_failed(
    retro: Retrospective, facilitator: User
) -> None:
    """`_docs/decisions.md` item 6: re-uploading is the whole of the recovery."""
    failed = make_record(retro, facilitator, status=Status.FAILED, error_message="No audio track")

    fresh = make_record(retro, facilitator)

    assert MeetingRecord.objects.filter(retrospective=retro).count() == 2
    assert failed.status == Status.FAILED
    assert fresh.status == Status.UPLOADED


@pytest.mark.django_db
def test_a_record_for_another_retrospective_is_unaffected(
    retro: Retrospective, facilitator: User, project: Project
) -> None:
    """The rule is per retrospective; two weeks can be in flight at once."""
    other_cycle = FeedbackCycle.objects.create(
        project=project,
        week_start=date(2026, 7, 27),
        opens_at=OPENS_AT,
        closes_at=CLOSES_AT,
        facilitator=facilitator,
        status=FeedbackCycle.Status.CLOSED,
    )
    other = Retrospective.objects.create(cycle=other_cycle, stage=Stage.DISCUSS)

    make_record(retro, facilitator)
    make_record(other, facilitator)

    assert MeetingRecord.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_uploads_give_one_record_and_one_rejection(
    scratch, retro: Retrospective, facilitator: User
) -> None:
    """Rejected rather than racing: both threads pass the check, one loses to the index.

    The transactional test is the point — the usual wrapper would put both
    threads inside one transaction and the unique index would never be tested
    against a concurrent insert.
    """
    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, str] = {}

    def upload(name: str) -> None:
        try:
            user = User.objects.get(pk=facilitator.pk)
            mine = Retrospective.objects.get(pk=retro.pk)
            barrier.wait()
            store_meeting_record(retro=mine, user=user, kind=Kind.PASTED_TEXT, text=f"from {name}")
            results[name] = "stored"
        except IntegrityError:
            results[name] = "rejected"
        finally:
            connection.close()

    threads = [threading.Thread(target=upload, args=(name,)) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert sorted(results.values()) == ["rejected", "stored"]
    assert MeetingRecord.objects.filter(retrospective=retro).count() == 1


# --------------------------------------------------------------------------
# Uploading: what is accepted, and what it produces
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_audio_file_is_stored_and_recorded(
    scratch, as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    response = as_facilitator.post(upload_url(retro), {"media": audio_file()}, follow=True)

    record = MeetingRecord.objects.get()
    assert response.status_code == 200
    assert record.kind == Kind.AUDIO
    assert record.status == Status.UPLOADED
    assert record.uploaded_by == facilitator
    assert record.original_filename == "standup.mp3"
    assert record.size_bytes == len(MP3)
    assert Path(record.temp_path).read_bytes() == MP3


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("standup.mp3", Kind.AUDIO),
        ("standup.M4A", Kind.AUDIO),
        ("retro.mp4", Kind.VIDEO),
        ("retro.mkv", Kind.VIDEO),
        ("notes.txt", Kind.TRANSCRIPT_FILE),
        ("notes.vtt", Kind.TRANSCRIPT_FILE),
    ],
)
def test_the_extension_decides_which_kind_of_record_it_is(
    scratch, as_facilitator: Client, retro: Retrospective, filename: str, kind: str
) -> None:
    as_facilitator.post(
        upload_url(retro), {"media": SimpleUploadedFile(filename, MP3)}, follow=True
    )

    assert MeetingRecord.objects.get().kind == kind


@pytest.mark.django_db
def test_pasted_text_is_stored_as_a_record_of_its_own(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    as_facilitator.post(upload_url(retro), {"pasted_text": "Alex: we ship on Friday."}, follow=True)

    record = MeetingRecord.objects.get()
    assert record.kind == Kind.PASTED_TEXT
    # Nothing was uploaded, so there is no filename to keep.
    assert record.original_filename == ""
    assert Path(record.temp_path).read_text() == "Alex: we ship on Friday."
    assert record.size_bytes == len("Alex: we ship on Friday.")


@pytest.mark.parametrize(
    ("kind", "skips"),
    [
        (Kind.TRANSCRIPT_FILE, True),
        (Kind.PASTED_TEXT, True),
        (Kind.AUDIO, False),
        (Kind.VIDEO, False),
    ],
)
def test_text_skips_transcription_and_a_recording_does_not(kind: str, skips: bool) -> None:
    """A transcript is already the thing transcription would produce."""
    assert MeetingRecord(kind=kind).skips_transcription is skips


# --------------------------------------------------------------------------
# Where the bytes land
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_upload_lands_in_the_scratch_directory_both_containers_share(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    """`web` writes it and `worker` reads it, so it has to be on the shared volume."""
    as_facilitator.post(upload_url(retro), {"media": audio_file()}, follow=True)

    stored = Path(MeetingRecord.objects.get().temp_path)
    assert stored.is_file()
    assert stored.is_relative_to(Path(settings.SCRATCH_DIR))
    assert stored.parent == upload_root()


def test_compose_gives_web_and_worker_the_same_scratch_volume() -> None:
    """The path only means the same thing in both containers because of this.

    A directory that exists only in `web` — the system temp directory, a folder
    beside the code — passes every test on a laptop and leaves the worker with
    a path to nothing.
    """
    compose = (BASE_DIR / "compose.yaml").read_text()

    assert compose.count("SCRATCH_DIR: /scratch") == 2
    assert compose.count("- scratch:/scratch") == 2
    assert re.search(r"^volumes:\n(.*\n)*?  scratch:", compose, re.MULTILINE)


@pytest.mark.django_db
def test_no_part_of_the_stored_path_comes_from_the_uploaded_filename(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    """Not the stem, not the extension: the name is data, and data never becomes a path."""
    hostile = "../../etc/pass<script>word.mp3"

    as_facilitator.post(upload_url(retro), {"media": SimpleUploadedFile(hostile, MP3)}, follow=True)

    stored = Path(MeetingRecord.objects.get().temp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", stored.name)
    for fragment in ("..", "etc", "pass", "script", "word", ".mp3"):
        assert fragment not in stored.name
    assert stored.parent == upload_root()


def test_two_generated_paths_are_never_the_same(scratch) -> None:
    assert generated_upload_path() != generated_upload_path()


@pytest.mark.django_db
def test_the_filename_is_kept_for_display_and_escaped_when_shown(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    """A file called `<img onerror=...>` reads as its own name and runs nothing."""
    name = "<img src=x onerror=alert(1)>.mp3"

    as_facilitator.post(upload_url(retro), {"media": SimpleUploadedFile(name, MP3)}, follow=True)
    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert MeetingRecord.objects.get().original_filename == name
    assert "&lt;img src=x onerror=alert(1)&gt;.mp3" in body
    assert "<img src=x onerror=alert(1)>" not in body


# --------------------------------------------------------------------------
# Who may upload
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_this_cycles_facilitator_is_offered_the_upload(
    as_facilitator: Client, retro: Retrospective
) -> None:
    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert 'enctype="multipart/form-data"' in body


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["member", "other_facilitator"])
def test_a_member_who_is_not_the_facilitator_is_refused(
    scratch, request, retro: Retrospective, who: str
) -> None:
    """A real POST with a real token, so the refusal is the rule and not the middleware."""
    user = request.getfixturevalue(who)
    client = csrf_client(user)

    response = client.post(
        upload_url(retro),
        {"media": audio_file(), "csrfmiddlewaretoken": csrf_token(client)},
    )

    assert response.status_code == 403
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
def test_a_non_member_is_not_told_the_retrospective_exists(
    scratch, retro: Retrospective, outsider: User
) -> None:
    client = csrf_client(outsider)

    response = client.post(
        upload_url(retro),
        {"media": audio_file(), "csrfmiddlewaretoken": csrf_token(client)},
    )

    assert response.status_code == 404
    assert client.get(upload_url(retro)).status_code == 404
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
def test_an_anonymous_visitor_is_sent_to_log_in(
    scratch, client: Client, retro: Retrospective
) -> None:
    response = client.post(upload_url(retro), {"media": audio_file()})

    assert response.status_code == 302
    assert response.url.startswith(settings.LOGIN_URL)
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
@pytest.mark.parametrize(("who", "refusal"), [("member", 403), ("outsider", 404)])
def test_the_progress_fragment_is_the_facilitators_too(
    retro: Retrospective, facilitator: User, request, who: str, refusal: int
) -> None:
    """Watching is part of uploading, so it is refused the same way and as firmly."""
    record = make_record(retro, facilitator)
    client = Client()
    log_in(client, request.getfixturevalue(who))

    assert client.get(status_url(record)).status_code == refusal


# --------------------------------------------------------------------------
# Not before the discussion
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("stage", BEFORE_DISCUSS)
def test_the_upload_is_not_offered_before_the_discussion(
    scratch, as_facilitator: Client, retro: Retrospective, stage: str
) -> None:
    at_stage(retro, stage)

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert upload_is_open(retro) is False
    assert 'enctype="multipart/form-data"' not in body
    assert 'data-upload-closed="true"' in body


@pytest.mark.django_db
@pytest.mark.parametrize("stage", BEFORE_DISCUSS)
def test_posting_before_the_discussion_is_refused(
    scratch, retro: Retrospective, facilitator: User, stage: str
) -> None:
    at_stage(retro, stage)
    client = csrf_client(facilitator)

    response = client.post(
        upload_url(retro),
        {"media": audio_file(), "csrfmiddlewaretoken": csrf_token(client)},
    )

    assert response.status_code == 403
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
@pytest.mark.parametrize("stage", FROM_DISCUSS)
def test_the_upload_is_offered_from_the_discussion_on(
    scratch, as_facilitator: Client, retro: Retrospective, stage: str
) -> None:
    at_stage(retro, stage)

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert upload_is_open(retro) is True
    assert 'enctype="multipart/form-data"' in body


@pytest.mark.django_db
def test_the_retrospective_links_to_the_upload_only_when_it_is_open(
    as_facilitator: Client, retro: Retrospective
) -> None:
    """The link on the retrospective page follows the same two rules."""
    at_stage(retro, Stage.VOTE)
    before = as_facilitator.get(reverse("retro-detail", args=[retro.pk])).content.decode()

    at_stage(retro, Stage.DISCUSS)
    after = as_facilitator.get(reverse("retro-detail", args=[retro.pk])).content.decode()

    assert upload_url(retro) not in before
    assert upload_url(retro) in after


@pytest.mark.django_db
def test_a_member_is_not_shown_the_link_on_the_retrospective(
    client: Client, retro: Retrospective, member: User
) -> None:
    log_in(client, member)

    body = client.get(reverse("retro-detail", args=[retro.pk])).content.decode()

    assert upload_url(retro) not in body


# --------------------------------------------------------------------------
# The size cap
# --------------------------------------------------------------------------


def test_the_cap_is_500_mb() -> None:
    assert MAX_UPLOAD_BYTES == 500 * 1024 * 1024


@pytest.mark.django_db
def test_a_request_declaring_more_than_the_cap_is_refused_before_it_is_read(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    """A 501 MB upload gets an error a person can act on, and no truncated file."""
    response = as_facilitator.post(
        upload_url(retro),
        {"media": audio_file()},
        CONTENT_LENGTH=str(MAX_UPLOAD_BYTES + 1024 * 1024),
    )
    body = response.content.decode()

    assert response.status_code == 413
    assert "larger than the 500 MB limit" in body
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


def test_a_file_over_the_cap_is_refused_by_the_form() -> None:
    """The second half of the cap: what the request declared is not what it carried."""
    oversized = SimpleUploadedFile("long.mp4", MP3, content_type="video/mp4")
    oversized.size = MAX_UPLOAD_BYTES + 1

    form = MeetingUploadForm({}, {"media": oversized})

    assert form.is_valid() is False
    assert "larger than the 500 MB limit" in str(form.errors)


def test_a_file_exactly_at_the_cap_is_accepted() -> None:
    """The limit is a limit, not an off-by-one."""
    at_the_line = SimpleUploadedFile("long.mp4", MP3, content_type="video/mp4")
    at_the_line.size = MAX_UPLOAD_BYTES

    assert MeetingUploadForm({}, {"media": at_the_line}).is_valid() is True


def test_the_proxy_and_the_application_state_one_limit_between_them() -> None:
    """The comment tying the two together is the acceptance criterion, so it is asserted."""
    proxy = (BASE_DIR / "deploy" / "nginx.conf").read_text()

    assert "client_max_body_size 500m;" in proxy
    assert "MAX_UPLOAD_BYTES" in proxy
    assert "meetings/uploads.py" in proxy


def test_uploads_stream_to_disk_rather_than_buffering_in_memory() -> None:
    """`FILE_UPLOAD_MAX_MEMORY_SIZE` stays low, and the rest of the request matches it."""
    import config.settings as production_settings

    assert production_settings.FILE_UPLOAD_MAX_MEMORY_SIZE == 1024 * 1024
    assert (
        production_settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        == production_settings.FILE_UPLOAD_MAX_MEMORY_SIZE
    )
    assert production_settings.DATA_UPLOAD_MAX_MEMORY_SIZE < MAX_UPLOAD_BYTES


def test_the_upload_is_written_a_chunk_at_a_time() -> None:
    """`.chunks()` is what keeps a 500 MB file out of the process's memory."""
    source = (BASE_DIR / "meetings" / "uploads.py").read_text()

    assert ".chunks(" in source
    assert ".read()" not in source


# --------------------------------------------------------------------------
# The extension allowlist
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("filename", ["notes.exe", "notes.pdf", "notes.mp3.exe", "notes"])
def test_anything_outside_the_allowlist_is_rejected_before_it_is_written(
    scratch, as_facilitator: Client, retro: Retrospective, filename: str
) -> None:
    response = as_facilitator.post(upload_url(retro), {"media": SimpleUploadedFile(filename, MP3)})

    assert response.status_code == 200
    assert "not accepted" in response.content.decode()
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


def test_the_allowlist_covers_audio_video_and_transcripts() -> None:
    assert set(ALLOWED_EXTENSIONS) == set(
        AUDIO_EXTENSIONS + VIDEO_EXTENSIONS + TRANSCRIPT_EXTENSIONS
    )
    assert all(
        extension.startswith(".") and extension.islower() for extension in ALLOWED_EXTENSIONS
    )


@pytest.mark.django_db
def test_the_page_documents_what_it_accepts(as_facilitator: Client, retro: Retrospective) -> None:
    body = as_facilitator.get(upload_url(retro)).content.decode()

    for extension in ALLOWED_EXTENSIONS:
        assert extension in body
    assert "500 MB" in body


# --------------------------------------------------------------------------
# Nothing to process
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_zero_byte_file_is_rejected(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    response = as_facilitator.post(
        upload_url(retro), {"media": SimpleUploadedFile("silence.mp3", b"")}
    )

    assert "empty" in response.content.decode()
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_pasted_text_is_rejected(
    scratch, as_facilitator: Client, retro: Retrospective, text: str
) -> None:
    response = as_facilitator.post(upload_url(retro), {"pasted_text": text})

    assert "nothing to process" in response.content.decode()
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
def test_a_file_and_pasted_text_together_are_rejected(
    scratch, as_facilitator: Client, retro: Retrospective
) -> None:
    """Two meetings' worth of input, and no way to say which one is the meeting."""
    response = as_facilitator.post(
        upload_url(retro), {"media": audio_file(), "pasted_text": "Alex: we ship on Friday."}
    )

    assert "one thing at a time" in response.content.decode()
    assert MeetingRecord.objects.count() == 0
    assert stored_files() == []


@pytest.mark.django_db
def test_a_second_upload_while_one_is_running_is_refused_readably(
    scratch, as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    make_record(retro, facilitator, status=Status.TRANSCRIBING)

    response = as_facilitator.post(upload_url(retro), {"media": audio_file()}, follow=True)
    body = response.content.decode()

    assert "already has a meeting being processed" in body
    assert MeetingRecord.objects.count() == 1


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_job_is_enqueued_only_once_the_row_is_committed(
    scratch, as_facilitator: Client, retro: Retrospective, django_capture_on_commit_callbacks
) -> None:
    """`transaction.on_commit`, per #18: a worker cannot claim a job for a row it cannot see.

    Capturing the callbacks is also the proof: an enqueue done inline would
    leave this list empty.
    """
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        as_facilitator.post(upload_url(retro), {"media": audio_file()}, follow=True)

    assert len(callbacks) == 1
    record = MeetingRecord.objects.get()
    assert record.status == Status.UPLOADED


def test_the_job_takes_an_id_rather_than_a_model_instance() -> None:
    """The queue row stores its arguments as JSON, so only an id survives."""
    signature = inspect.signature(process_meeting_record.func)

    assert list(signature.parameters) == ["record_id"]
    assert signature.parameters["record_id"].annotation is int


@pytest.mark.django_db
def test_the_job_tolerates_the_row_having_gone(scratch) -> None:
    """Time passes between the enqueue and the run; the row may not be there."""
    process_meeting_record.func(123456789)


@pytest.mark.django_db
def test_the_job_leaves_a_record_another_worker_has_taken_alone(
    retro: Retrospective, facilitator: User
) -> None:
    record = make_record(retro, facilitator, status=Status.TRANSCRIBING)

    process_meeting_record.func(record.pk)

    record.refresh_from_db()
    assert record.status == Status.TRANSCRIBING


def test_the_enqueue_goes_through_the_shared_helper() -> None:
    """`enqueue_on_commit`, never `.enqueue()` from inside the transaction."""
    source = (BASE_DIR / "meetings" / "services.py").read_text()

    assert "enqueue_on_commit(process_meeting_record, record.pk)" in source
    assert ".enqueue(" not in source


# --------------------------------------------------------------------------
# Watching it work
# --------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Status.UPLOADED, Status.TRANSCRIBING, Status.EXTRACTING])
def test_the_page_polls_while_there_is_something_to_wait_for(
    as_facilitator: Client, retro: Retrospective, facilitator: User, status: str
) -> None:
    record = make_record(retro, facilitator, status=status)

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert status_url(record) in body
    assert 'hx-trigger="every 3s"' in body
    assert 'data-polling="true"' in body


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Status.READY, Status.FAILED])
def test_polling_stops_once_the_record_is_finished(
    as_facilitator: Client, retro: Retrospective, facilitator: User, status: str
) -> None:
    """The last fragment to arrive is the one that does not ask again."""
    record = make_record(retro, facilitator, status=status, error_message="No audio track")

    page = as_facilitator.get(upload_url(retro)).content.decode()
    fragment = as_facilitator.get(status_url(record)).content.decode()

    for body in (page, fragment):
        assert 'data-polling="false"' in body
        assert "hx-get" not in body
        assert "hx-trigger" not in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [Status.UPLOADED, Status.TRANSCRIBING, Status.EXTRACTING, Status.READY]
)
def test_the_stage_is_shown_in_words_and_never_as_the_enum(
    as_facilitator: Client, retro: Retrospective, facilitator: User, status: str
) -> None:
    record = make_record(retro, facilitator, status=status)

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert record.progress in body
    # The stored constant never reaches the page — not in the text, not in a
    # data attribute, not in a class name.
    assert status not in body


@pytest.mark.django_db
def test_the_fragment_is_the_same_markup_the_page_carries(
    as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    """One piece of markup, so a swap cannot disagree with the page it replaces."""
    record = make_record(retro, facilitator, status=Status.TRANSCRIBING)

    fragment = as_facilitator.get(status_url(record)).content.decode()

    assert fragment.strip().startswith("<div")
    assert 'id="meeting-status"' in fragment
    assert "<html" not in fragment
    assert record.progress in fragment


@pytest.mark.django_db
def test_a_failure_says_the_file_has_to_be_uploaded_again_and_offers_no_retry(
    as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    """`_docs/decisions.md` item 6: the recording is gone, so a retry button would lie."""
    make_record(
        retro,
        facilitator,
        status=Status.FAILED,
        error_message="The transcription service refused the file.",
    )

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert "The transcription service refused the file." in body
    assert "deleted" in body
    assert "Upload the file once more to start over." in body
    assert "Retry" not in body
    assert "Try again" not in body


@pytest.mark.django_db
def test_a_failure_leaves_the_form_offered_again(
    as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    make_record(retro, facilitator, status=Status.FAILED, error_message="No audio track")

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert 'enctype="multipart/form-data"' in body


@pytest.mark.django_db
def test_a_running_record_takes_the_form_off_the_page(
    as_facilitator: Client, retro: Retrospective, facilitator: User
) -> None:
    make_record(retro, facilitator, status=Status.TRANSCRIBING)

    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert 'enctype="multipart/form-data"' not in body
    assert 'data-upload-busy="true"' in body


@pytest.mark.django_db
def test_the_page_steers_towards_audio_or_a_pasted_transcript(
    as_facilitator: Client, retro: Retrospective
) -> None:
    """And says why: a long video is chunked, and every chunk boundary is a seam."""
    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert "90-minute video" in body
    assert "split into chunks" in body
    assert "picture is thrown" in body
    assert "Paste it here" in body


@pytest.mark.django_db
def test_nothing_uploaded_yet_says_so_and_does_not_poll(
    as_facilitator: Client, retro: Retrospective
) -> None:
    body = as_facilitator.get(upload_url(retro)).content.decode()

    assert "Nothing has been handed over" in body
    assert 'data-polling="false"' in body
