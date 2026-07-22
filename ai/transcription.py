"""Turn prepared audio chunks into one transcript with speaker labels.

One entry point, :func:`transcribe_chunks`: chunk paths in playback order in, a
:class:`Transcription` out. Nothing here reads or writes the database, imports a
model, or knows which meeting it is transcribing — `meetings/pipeline.py` owns
all of that, and this module takes paths and returns data.

The API client is a seam, not an import
---------------------------------------

Everything that talks to OpenAI is behind :class:`TranscriptionClient`: one
method, ``transcribe(path) -> ChunkTranscript``. :class:`OpenAITranscriptionClient`
is the real one; ``settings.TRANSCRIPTION_CLIENT`` names the class the pipeline
builds, and the suite points it at `ai.fakes.EchoTranscriptionClient` so no test
needs a key, a network, or a skip when neither is there. The real client also
takes its SDK object as an argument, which is where a test asserts the model
name and the order of the requests without a key.

Retries live here, and that is deliberate
-----------------------------------------

Nothing on the queue is retried automatically (AGENTS.md, "Background tasks"):
the pipeline deletes the recording in a `finally` block, so a second run of the
job would have no file to run against. A transient failure is therefore retried
*here*, inside the job, while the audio is still on disk and a retry can still
work. The budget is small and fixed — see :data:`MAX_ATTEMPTS` — because the
worker is holding a job the whole time.

Stitching, and what it gets wrong
---------------------------------

See :func:`stitch`. Diarization numbers speakers per request, so the chunks
cannot simply be concatenated.
"""

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

#: The transcription model. `whisper-1` is the legacy snapshot and `gpt-4o` is
#: superseded; neither is a fallback, and there is no second model named here.
MODEL = "gpt-4o-transcribe-diarize"

#: The response shape that carries the per-speaker segments. Plain `json` would
#: return the words and lose who said them, which is the part #23 needs.
RESPONSE_FORMAT = "diarized_json"

#: Required by this model for anything longer than 30 seconds. It is the
#: server's own voice-activity segmentation *inside* one request, and has
#: nothing to do with the 25 MB chunking `ai/audio.py` does across requests.
CHUNKING_STRATEGY = "auto"

#: How long one chunk's request may take. A chunk is at most three hours of
#: 12 kbit/s speech, and the model is faster than real time by a wide margin.
REQUEST_TIMEOUT_SECONDS = 15 * 60

#: The retry budget, per chunk, spent inside the job. Three attempts covers the
#: failure this is for — a rate limit or a 5xx that clears in seconds — and
#: stops well short of a worker sitting on a dead API for minutes. There is no
#: second budget anywhere: the SDK's own retries are turned off in
#: :class:`OpenAITranscriptionClient` so this number means what it says.
MAX_ATTEMPTS = 3

#: What to wait before each retry. One entry per retry, so it is
#: ``MAX_ATTEMPTS - 1`` long and a mismatch is a programming error rather than
#: a silently different budget.
BACKOFF_SECONDS = (2.0, 8.0)

#: How a speaker is written into the stored text. The number is this
#: recording's, assigned by :func:`stitch`, not the one the API returned.
SPEAKER_LABEL = "Speaker {number}"

#: Marks where one request's transcript ends and the next begins, and warns a
#: reader — and the human editor of #46 — that the speaker numbers on either
#: side of it were matched up by a heuristic. Only ever written when there is
#: more than one chunk, which is the rare case.
CHUNK_MARKER = "[chunk {number} begins — speaker numbers realigned, see ai/transcription.py]"

MISSING_KEY_MESSAGE = (
    "OPENAI_API_KEY is not set in this environment, so the meeting was never sent "
    "for transcription. An administrator sets OPENAI_API_KEY and the file is "
    "uploaded again."
)

#: Status codes worth trying again: the rate limiter, and anything the far end
#: broke on. A 4xx that is not 408 or 429 is our request being wrong, and
#: sending it again would only be wrong again.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})


class TranscriptionError(Exception):
    """A chunk could not be transcribed. The message is shown to a facilitator."""


class TransientTranscriptionError(TranscriptionError):
    """A failure worth another attempt: a rate limit, a 5xx, a timeout."""


class PermanentTranscriptionError(TranscriptionError):
    """A failure that a second identical request would only repeat."""


class MissingAPIKeyError(PermanentTranscriptionError):
    """`OPENAI_API_KEY` is not configured.

    Raised before anything is sent, so a facilitator is told which variable is
    missing rather than being handed an authentication error out of the SDK.
    """


@dataclass(frozen=True)
class Segment:
    """One stretch of speech by one speaker, as the API labelled it."""

    speaker: str
    text: str


@dataclass(frozen=True)
class ChunkTranscript:
    """What one request came back with. Speaker labels are local to it."""

    segments: tuple[Segment, ...]
    language: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class Transcription:
    """Every chunk, stitched into the text that gets stored."""

    text: str
    language: str = ""
    duration_seconds: float | None = None
    chunk_count: int = 1
    speaker_count: int = 0
    attempts: int = 1


class TranscriptionClient(Protocol):
    """The seam. One method, one chunk, no knowledge of the meeting."""

    def transcribe(self, path: Path) -> ChunkTranscript: ...


def transcribe_chunks(
    paths: Iterable[str | Path],
    *,
    client: TranscriptionClient | None = None,
    max_attempts: int | None = None,
    backoff: Sequence[float] | None = None,
    sleep=time.sleep,
) -> Transcription:
    """Transcribe every chunk in order and stitch the results together.

    `client` defaults to whatever ``settings.TRANSCRIPTION_CLIENT`` names, which
    is how the suite gets a fake without a key. The retry parameters default to
    the module constants and are read at call time, so a test can shorten the
    backoff without shortening it in production.

    Raises :class:`TranscriptionError`. A chunk that cannot be transcribed after
    its retries takes the whole recording with it: a transcript missing its
    middle is worse than no transcript, and worse still is one that does not say
    so.
    """
    chunk_paths = [Path(path) for path in paths]
    if not chunk_paths:
        raise PermanentTranscriptionError("There was no audio to transcribe")

    attempts_allowed = MAX_ATTEMPTS if max_attempts is None else max_attempts
    waits = BACKOFF_SECONDS if backoff is None else backoff
    if client is None:
        client = build_client()

    results: list[ChunkTranscript] = []
    attempts_made = 0
    for index, path in enumerate(chunk_paths, start=1):
        chunk, attempts = _transcribe_one(
            client,
            path,
            index=index,
            of=len(chunk_paths),
            max_attempts=attempts_allowed,
            backoff=waits,
            sleep=sleep,
        )
        results.append(chunk)
        attempts_made += attempts

    stitched = stitch(results)
    return Transcription(
        text=stitched.text,
        language=stitched.language,
        duration_seconds=stitched.duration_seconds,
        chunk_count=len(results),
        speaker_count=stitched.speaker_count,
        attempts=attempts_made,
    )


def _transcribe_one(
    client: TranscriptionClient,
    path: Path,
    *,
    index: int,
    of: int,
    max_attempts: int,
    backoff: Sequence[float],
    sleep,
) -> tuple[ChunkTranscript, int]:
    """One chunk, retried with backoff while the failure looks transient.

    The audio still exists at every point in this loop — that is the whole
    reason the retry is here and not around the job.
    """
    last: TransientTranscriptionError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.transcribe(path), attempt
        except TransientTranscriptionError as exc:
            last = exc
            if attempt == max_attempts:
                break
            wait = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0.0
            logger.warning(
                "chunk %s of %s failed (%s); retrying in %.1fs (attempt %s of %s)",
                index,
                of,
                exc,
                wait,
                attempt + 1,
                max_attempts,
            )
            sleep(wait)
        except PermanentTranscriptionError as exc:
            raise PermanentTranscriptionError(
                f"{describe(index, of)} could not be transcribed: {exc}"
            ) from exc

    raise PermanentTranscriptionError(
        f"{describe(index, of)} could not be transcribed after {max_attempts} attempts: {last}"
    )


def describe(index: int, of: int) -> str:
    """How a chunk is named in a message a facilitator reads.

    A recording that fitted in one request is "the recording": telling someone
    their meeting failed at "chunk 1 of 1" explains an implementation detail
    they never asked about.
    """
    return "The recording" if of == 1 else f"Chunk {index} of {of}"


@dataclass(frozen=True)
class _Stitched:
    text: str
    language: str
    duration_seconds: float
    speaker_count: int


def stitch(chunks: Sequence[ChunkTranscript]) -> _Stitched:
    """Join the chunks, renumbering speakers so the numbers mean one thing.

    The problem: diarization numbers speakers per request. The API labels them
    `A`, `B`, `C` in the order it first hears them, and it starts again from `A`
    on the next request. Concatenating the chunks would silently claim that the
    first person to speak after a cut is the same person who opened the meeting.

    The heuristic: within each chunk, speakers are numbered by the order in
    which they first speak, and chunk two's first speaker becomes `Speaker 1`
    exactly as chunk one's did. So the numbering is stable in shape, and the
    text says where the chunk boundaries are.

    What it gets wrong, plainly:

    * If the person who opens chunk two is not the person who opened chunk one —
      the usual case, since chunks are cut at silences and a silence is often a
      handover — every label after that boundary is shifted.
    * A speaker who is silent for a whole chunk vanishes from the numbering
      there, and the speakers after them shift up by one.
    * Two chunks with different numbers of speakers cannot both be right.

    Why it is acceptable anyway: chunking only happens above ~3 hours of speech
    or 24 MB of Opus, so almost every real meeting is a single request where the
    mapping is the API's own and exact. When it does happen, #23 resolves owners
    against the project roster from the words rather than from the numbers, and
    #46 lets a human fix the labels. What is not acceptable is doing this
    silently, so the boundary is written into the text as
    :data:`CHUNK_MARKER`.
    """
    blocks: list[str] = []
    duration = 0.0
    language = ""
    speaker_count = 0

    for number, chunk in enumerate(chunks, start=1):
        duration += chunk.duration_seconds or 0.0
        language = language or chunk.language
        lines = _lines_for(chunk)
        speaker_count = max(speaker_count, _distinct_speakers(chunk))
        if number > 1 and len(chunks) > 1:
            blocks.append(CHUNK_MARKER.format(number=number))
        if lines:
            blocks.append("\n".join(lines))

    return _Stitched(
        text="\n\n".join(blocks).strip(),
        language=language,
        duration_seconds=duration,
        speaker_count=speaker_count,
    )


def _distinct_speakers(chunk: ChunkTranscript) -> int:
    return len({segment.speaker for segment in chunk.segments if segment.speaker})


def _lines_for(chunk: ChunkTranscript) -> list[str]:
    """One line per turn, ``Speaker n: ...``, consecutive turns merged.

    A segment the API gave no speaker for — a response that carried text but no
    diarization — is written as its own line without a label, rather than being
    attributed to whoever spoke last.
    """
    numbers: dict[str, int] = {}
    lines: list[str] = []
    current: str | None = None

    for segment in chunk.segments:
        text = segment.text.strip()
        if not text:
            continue
        if not segment.speaker:
            lines.append(text)
            current = None
            continue
        if segment.speaker not in numbers:
            numbers[segment.speaker] = len(numbers) + 1
        label = SPEAKER_LABEL.format(number=numbers[segment.speaker])
        if label == current:
            lines[-1] = f"{lines[-1]} {text}"
        else:
            lines.append(f"{label}: {text}")
            current = label
    return lines


def build_client() -> TranscriptionClient:
    """Build the client ``settings.TRANSCRIPTION_CLIENT`` names.

    Imported lazily, like everything Django in this package, so the module stays
    importable — and testable — without a configured settings module.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    return import_string(settings.TRANSCRIPTION_CLIENT)()


def configured_api_key() -> str:
    """`OPENAI_API_KEY`, as the environment supplied it, or the empty string."""
    from django.conf import settings

    return getattr(settings, "OPENAI_API_KEY", "") or ""


@dataclass
class OpenAITranscriptionClient:
    """The real client: one request per chunk, and nothing else.

    `sdk` is the injection point. Left unset it is built on first use from
    `OPENAI_API_KEY`; a test passes an object shaped like `openai.OpenAI` and
    asserts what was sent to it — the model, the file, the order — without a key
    and without a network.

    The SDK's own retries are switched off. The retry budget belongs to
    :func:`transcribe_chunks`, where it is bounded and visible; two nested
    budgets would multiply into a wait nobody chose.
    """

    api_key: str | None = None
    sdk: object | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    model: str = MODEL

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = configured_api_key()
        # Checked here rather than at the first request, so a missing key is
        # reported as a missing key before any audio is opened.
        if not self.api_key and self.sdk is None:
            raise MissingAPIKeyError(MISSING_KEY_MESSAGE)

    def _client(self):
        if self.sdk is None:
            import openai

            self.sdk = openai.OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            )
        return self.sdk

    def transcribe(self, path: Path) -> ChunkTranscript:
        """Send one chunk, and read the segments back out of the response."""
        sdk = self._client()
        try:
            with Path(path).open("rb") as handle:
                response = sdk.audio.transcriptions.create(
                    model=self.model,
                    file=handle,
                    response_format=RESPONSE_FORMAT,
                    chunking_strategy=CHUNKING_STRATEGY,
                )
        except OSError as exc:
            raise PermanentTranscriptionError(
                f"The prepared audio could not be read: {exc}"
            ) from exc
        except Exception as exc:
            translated = classify(exc)
            if translated is None:
                raise
            raise translated from exc
        return parse_response(response)


def classify(exc: BaseException) -> TranscriptionError | None:
    """Turn an SDK exception into ours, or return None if it is not one.

    Transient means "the same request might work in a moment": the rate limiter,
    a 5xx, a timeout, a connection that dropped. Everything else — a bad
    request, a rejected key — is permanent, because sending it again unchanged
    could only fail again.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK is a pinned dependency
        return None

    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return TransientTranscriptionError(f"the transcription API was unreachable ({exc})")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in RETRYABLE_STATUS_CODES:
            return TransientTranscriptionError(f"the transcription API returned {status}")
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return PermanentTranscriptionError(
                "the transcription API rejected OPENAI_API_KEY; the key is missing a "
                "permission or is no longer valid"
            )
        return PermanentTranscriptionError(f"the transcription API refused the request ({status})")
    if isinstance(exc, openai.OpenAIError):
        return PermanentTranscriptionError(f"the transcription API could not be called ({exc})")
    return None


def parse_response(response) -> ChunkTranscript:
    """Read the diarized response into segments, defensively.

    Read with `getattr` rather than by type: the client is a seam, and a fake
    SDK in a test hands back an object of its own shape. A response that carried
    text but no segments is kept as one unlabelled segment rather than dropped —
    losing the words would be worse than losing who said them.
    """
    segments: list[Segment] = []
    for raw in getattr(response, "segments", None) or []:
        text = str(getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        segments.append(Segment(speaker=str(getattr(raw, "speaker", "") or ""), text=text))

    if not segments:
        whole = str(getattr(response, "text", "") or "").strip()
        if not whole:
            raise PermanentTranscriptionError("the transcription API returned an empty transcript")
        segments = [Segment(speaker="", text=whole)]

    return ChunkTranscript(
        segments=tuple(segments),
        # This model does not report a detected language; the field is read
        # anyway so a response format that does will fill it in.
        language=str(getattr(response, "language", "") or ""),
        duration_seconds=float(getattr(response, "duration", 0.0) or 0.0),
    )
