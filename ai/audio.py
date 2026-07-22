"""Turn an uploaded media file into audio the transcription API will accept.

One entry point, `prepare_audio_chunks`: a path in, a list of chunk paths out,
in playback order. A file that already fits comes back as a single-element
list, so callers never branch on "did it need chunking".

Nothing here reads or writes the database, imports a model, or knows what the
recording is of. ffmpeg and ffprobe are driven as binaries with an argument
list — no shell, so no filename a user chose is ever parsed as a command.
"""

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FFMPEG_BINARY = "ffmpeg"
FFPROBE_BINARY = "ffprobe"

# The transcription API rejects requests over 25 MB. Chunks stop short of that
# so the multipart envelope around them still fits.
MAX_CHUNK_BYTES = 24 * 1024 * 1024

# Cuts are planned from an average bitrate, and Ogg page headers are not in it.
# Plan against nine tenths of the ceiling and the estimate has room to be wrong.
CHUNK_SIZE_SAFETY_FRACTION = 0.9

# Speech at 16 kHz mono Opus, ~12 kbit/s: three hours is roughly 17 MB, so a
# normal meeting is one chunk.
TARGET_SAMPLE_RATE_HZ = 16000
TARGET_CHANNELS = 1
TARGET_BITRATE = "12k"
CHUNK_SUFFIX = ".opus"

# The longest a chunk may run even when it would still fit under the size cap.
MAX_CHUNK_SECONDS = 3 * 60 * 60

# The shortest a *final* chunk may be before it is merged into the one before
# it. A recording a hair over MAX_CHUNK_SECONDS otherwise ends in a tail of
# whatever is left — QA measured 0.2 s and 507 bytes for a six hour source — and
# every chunk is its own transcription request, so that tail is a round trip and
# a bill for a fifth of a second of audio. Thirty seconds is below anything
# worth a request of its own, and small enough that merging one back is free:
# at the ~1.57 kB/s this encoder produces it adds about 47 kB to a three hour
# chunk that sits around 17 MB, well under the 24 MB ceiling. The merge is
# refused outright when even that would not fit — see `_merge_short_tail`.
MIN_CHUNK_SECONDS = 30.0

# A cut is only looked for in the last part of the allowed window, so silence
# early in a chunk cannot produce a run of very short ones.
SILENCE_SEARCH_FRACTION = 0.5

# What counts as silence worth cutting on.
SILENCE_NOISE_FLOOR = "-30dB"
SILENCE_MIN_SECONDS = 0.5

# Every ffmpeg invocation is bounded, so a corrupt file cannot pin a worker.
FFMPEG_TIMEOUT_SECONDS = 30 * 60

# A chunk still over the ceiling is halved, but not forever.
MAX_RESPLIT_DEPTH = 8
MIN_SPLITTABLE_SECONDS = 1.0


class MediaProcessingError(Exception):
    """A media file could not be turned into transcribable audio."""


class MissingBinaryError(MediaProcessingError):
    """A binary the pipeline shells out to is not installed."""


class NoAudioTrackError(MediaProcessingError):
    """The file carries no audio to transcribe."""


class MediaTimeoutError(MediaProcessingError):
    """An ffmpeg or ffprobe invocation ran past its timeout."""


class MediaDecodeError(MediaProcessingError):
    """ffmpeg could not decode the file."""


def prepare_audio_chunks(
    source: str | Path,
    *,
    work_root: str | Path | None = None,
    max_chunk_bytes: int = MAX_CHUNK_BYTES,
    max_chunk_seconds: float = MAX_CHUNK_SECONDS,
    min_chunk_seconds: float = MIN_CHUNK_SECONDS,
    timeout_seconds: float = FFMPEG_TIMEOUT_SECONDS,
) -> list[Path]:
    """Normalise `source` to 16 kHz mono Opus and split it into chunks.

    Returns the chunk paths in playback order — one element when the whole
    recording fits. The chunks live in a directory of their own under the
    scratch area and are the caller's to delete once it is done with them;
    every intermediate file is removed here, on success and on failure alike.

    A last chunk that would come out shorter than `min_chunk_seconds` is merged
    into the one before it rather than returned on its own, unless the merge
    would take that chunk over `max_chunk_bytes`.

    Raises `MediaProcessingError` (see its subclasses) with a message that says
    what went wrong. It never lets an ffmpeg traceback or a raw exit code out.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise MediaProcessingError(f"No such media file: {source_path}")

    ffmpeg = _resolve_binary(FFMPEG_BINARY)
    ffprobe = _resolve_binary(FFPROBE_BINARY)

    if not _has_audio_stream(ffprobe, source_path, timeout_seconds):
        raise NoAudioTrackError(f"{source_path.name} has no audio track to transcribe")

    root = Path(work_root) if work_root is not None else _default_work_root()
    root.mkdir(parents=True, exist_ok=True)
    # Generated names throughout: nothing downstream depends on what the
    # uploader called the file.
    work_dir = Path(tempfile.mkdtemp(prefix="audio-work-", dir=root))
    output_dir = Path(tempfile.mkdtemp(prefix="audio-chunks-", dir=root))

    try:
        normalized = _normalize(ffmpeg, source_path, work_dir, timeout_seconds)
        duration = _probe_duration(ffprobe, normalized, timeout_seconds)
        size = normalized.stat().st_size
        if duration <= 0 or size == 0:
            raise NoAudioTrackError(f"{source_path.name} decoded to an empty audio track")

        pieces = _split(
            ffmpeg,
            ffprobe,
            normalized,
            duration=duration,
            size=size,
            work_dir=work_dir,
            max_chunk_bytes=max_chunk_bytes,
            max_chunk_seconds=max_chunk_seconds,
            min_chunk_seconds=min_chunk_seconds,
            timeout_seconds=timeout_seconds,
        )
        return _collect(pieces, output_dir)
    except BaseException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def normalize_command(ffmpeg: str, source: Path, destination: Path) -> list[str]:
    """The argument list that strips video and downsamples to 16 kHz mono Opus."""
    return [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",  # drop any video track; the audio is all that gets transcribed
        "-map",
        "0:a:0",
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE_HZ),
        "-c:a",
        "libopus",
        "-b:a",
        TARGET_BITRATE,
        "-application",
        "voip",
        str(destination),
    ]


def _default_work_root() -> Path:
    """The scratch area `web` and `worker` share, read lazily.

    Imported here rather than at module scope so this module stays usable — and
    testable — without a configured Django.
    """
    from django.conf import settings

    return Path(settings.SCRATCH_DIR)


def _resolve_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MissingBinaryError(f"{name} is not installed or not on PATH; install {name}")
    return path


def _run(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run a command as an argument list, never through a shell, always bounded."""
    logger.debug("running %s", argv)
    try:
        # An argument list, so shell=False: no user-supplied name is ever parsed.
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaTimeoutError(
            f"{Path(argv[0]).name} timed out after {timeout_seconds:g} seconds"
        ) from exc
    except FileNotFoundError as exc:
        raise MissingBinaryError(f"{Path(argv[0]).name} is not installed or not on PATH") from exc


def _run_checked(argv: list[str], timeout_seconds: float, doing: str) -> str:
    completed = _run(argv, timeout_seconds)
    if completed.returncode != 0:
        raise MediaDecodeError(f"{doing} failed: {_reason(completed.stderr)}")
    return completed.stdout


def _reason(stderr: str) -> str:
    """The last thing ffmpeg said, which is the part that names the problem."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "ffmpeg gave no reason"


def _has_audio_stream(ffprobe: str, source: Path, timeout_seconds: float) -> bool:
    argv = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        str(source),
    ]
    output = _run_checked(argv, timeout_seconds, f"Reading {source.name}")
    try:
        streams = json.loads(output or "{}").get("streams", [])
    except json.JSONDecodeError as exc:
        raise MediaDecodeError(f"Reading {source.name} failed: unreadable stream listing") from exc
    return bool(streams)


def _probe_duration(ffprobe: str, path: Path, timeout_seconds: float) -> float:
    argv = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    output = _run_checked(argv, timeout_seconds, f"Measuring {path.name}")
    try:
        value = json.loads(output or "{}").get("format", {}).get("duration")
        return float(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MediaDecodeError(f"Measuring {path.name} failed: no duration reported") from exc


def _normalize(ffmpeg: str, source: Path, work_dir: Path, timeout_seconds: float) -> Path:
    destination = work_dir / f"normalized{CHUNK_SUFFIX}"
    _run_checked(
        normalize_command(ffmpeg, source, destination),
        timeout_seconds,
        f"Converting {source.name} to audio",
    )
    if not destination.is_file():
        raise MediaDecodeError(f"Converting {source.name} to audio produced nothing")
    return destination


def _detect_silences(ffmpeg: str, path: Path, timeout_seconds: float) -> list[tuple[float, float]]:
    """Silent stretches as (start, end) seconds, from ffmpeg's silencedetect."""
    argv = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={SILENCE_NOISE_FLOOR}:d={SILENCE_MIN_SECONDS}",
        "-f",
        "null",
        "-",
    ]
    completed = _run(argv, timeout_seconds)
    if completed.returncode != 0:
        raise MediaDecodeError(
            f"Scanning {path.name} for silence failed: {_reason(completed.stderr)}"
        )

    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in completed.stderr.splitlines():
        if "silence_start:" in line:
            start = _float_after(line, "silence_start:")
        elif "silence_end:" in line:
            end = _float_after(line, "silence_end:")
            if start is not None and end is not None:
                silences.append((start, end))
            start = None
    return silences


def _float_after(line: str, marker: str) -> float | None:
    tail = line.split(marker, 1)[1].strip().split("|", 1)[0].strip().split()
    if not tail:
        return None
    try:
        return float(tail[0])
    except ValueError:
        return None


def _plan_cuts(duration: float, silences: list[tuple[float, float]], limit: float) -> list[float]:
    """Where to cut: the latest silence inside each window, else the window's end.

    Cutting mid-silence rather than at a fixed offset keeps sentences whole. A
    recording with no detectable silence — continuous speech, constant noise —
    falls through to the hard cut and still gets split.
    """
    midpoints = sorted((start + end) / 2 for start, end in silences)
    cuts: list[float] = []
    position = 0.0
    while duration - position > limit:
        window_end = position + limit
        window_start = position + limit * SILENCE_SEARCH_FRACTION
        candidates = [m for m in midpoints if window_start < m <= window_end]
        cuts.append(max(candidates) if candidates else window_end)
        position = cuts[-1]
    return cuts


def _merge_short_tail(
    cuts: list[float],
    *,
    duration: float,
    bytes_per_second: float,
    min_chunk_seconds: float,
    max_chunk_bytes: int,
) -> list[float]:
    """Drop the last cut when the tail after it is too short to stand alone.

    Not cutting there is the merge: the leftover audio stays inside the chunk
    before it instead of being returned as a chunk — and a request — of its own.

    The size cap outranks this. Chunking exists because the transcription API
    refuses a request over `max_chunk_bytes`, so a merge that the bitrate says
    would take the combined chunk past the same budget the cuts were planned
    against is refused, and the short tail is returned rather than risk a chunk
    no request can carry. That budget is `CHUNK_SIZE_SAFETY_FRACTION` of the
    ceiling, exactly as in `_duration_limit`, which means the merge only ever
    happens when the clock cap decided the chunk length and there are megabytes
    of headroom underneath it. `_enforce_size` measures the files afterwards
    either way, so an estimate that turns out wrong costs a re-split, never an
    oversized chunk.
    """
    if not cuts:
        return cuts
    if duration - cuts[-1] >= min_chunk_seconds:
        return cuts
    merged_seconds = duration - (cuts[-2] if len(cuts) > 1 else 0.0)
    if merged_seconds * bytes_per_second > max_chunk_bytes * CHUNK_SIZE_SAFETY_FRACTION:
        return cuts
    return cuts[:-1]


def _segment(
    ffmpeg: str, path: Path, cuts: list[float], out_dir: Path, timeout_seconds: float
) -> list[Path]:
    """Cut `path` at `cuts` without re-encoding, into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_times",
        ",".join(f"{cut:.3f}" for cut in cuts),
        "-reset_timestamps",
        "1",
        str(out_dir / f"part%05d{CHUNK_SUFFIX}"),
    ]
    _run_checked(argv, timeout_seconds, f"Splitting {path.name}")
    parts = sorted(out_dir.glob(f"part*{CHUNK_SUFFIX}"))
    if not parts:
        raise MediaDecodeError(f"Splitting {path.name} produced no chunks")
    return parts


def _split(
    ffmpeg: str,
    ffprobe: str,
    normalized: Path,
    *,
    duration: float,
    size: int,
    work_dir: Path,
    max_chunk_bytes: int,
    max_chunk_seconds: float,
    min_chunk_seconds: float,
    timeout_seconds: float,
) -> list[Path]:
    limit = _duration_limit(duration, size, max_chunk_bytes, max_chunk_seconds)
    if duration <= limit and size <= max_chunk_bytes:
        return [normalized]

    silences = _detect_silences(ffmpeg, normalized, timeout_seconds)
    cuts = _merge_short_tail(
        _plan_cuts(duration, silences, limit),
        duration=duration,
        bytes_per_second=size / duration,
        min_chunk_seconds=min_chunk_seconds,
        max_chunk_bytes=max_chunk_bytes,
    )
    if not cuts:
        parts = [normalized]
    else:
        parts = _segment(ffmpeg, normalized, cuts, work_dir / "parts", timeout_seconds)
    return _enforce_size(
        ffmpeg, ffprobe, parts, work_dir, max_chunk_bytes, timeout_seconds, depth=0
    )


def _duration_limit(
    duration: float, size: int, max_chunk_bytes: int, max_chunk_seconds: float
) -> float:
    """How long a chunk may run: the size cap in seconds, capped by the clock."""
    bytes_per_second = size / duration
    seconds_by_size = (max_chunk_bytes * CHUNK_SIZE_SAFETY_FRACTION) / bytes_per_second
    return max(min(max_chunk_seconds, seconds_by_size), MIN_SPLITTABLE_SECONDS)


def _enforce_size(
    ffmpeg: str,
    ffprobe: str,
    parts: list[Path],
    work_dir: Path,
    max_chunk_bytes: int,
    timeout_seconds: float,
    depth: int,
) -> list[Path]:
    """Halve any chunk the bitrate estimate still left over the ceiling."""
    checked: list[Path] = []
    for index, part in enumerate(parts):
        if part.stat().st_size <= max_chunk_bytes:
            checked.append(part)
            continue
        duration = _probe_duration(ffprobe, part, timeout_seconds)
        if depth >= MAX_RESPLIT_DEPTH or duration <= MIN_SPLITTABLE_SECONDS:
            raise MediaProcessingError(
                f"A {duration:.1f}s chunk stays above {max_chunk_bytes} bytes; "
                "the recording cannot be split small enough"
            )
        halves = _segment(
            ffmpeg,
            part,
            [duration / 2],
            work_dir / f"resplit-{depth}-{index}",
            timeout_seconds,
        )
        checked.extend(
            _enforce_size(
                ffmpeg, ffprobe, halves, work_dir, max_chunk_bytes, timeout_seconds, depth + 1
            )
        )
    return checked


def _collect(parts: list[Path], output_dir: Path) -> list[Path]:
    """Move the surviving chunks out of the work area under generated names."""
    chunks = []
    for index, part in enumerate(parts):
        destination = output_dir / f"chunk-{index:05d}{CHUNK_SUFFIX}"
        shutil.move(str(part), destination)
        chunks.append(destination)
    return chunks
