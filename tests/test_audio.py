"""Tests for the audio normalisation and chunking pipeline.

Fixtures are synthesised with ffmpeg when the suite runs — a tone, a tone with
silences in it, a continuous tone, a video with sound and a video without —
so no binary media is committed to the repository.

Nothing in this file touches the database, and no test carries the
`django_db` marker: the pipeline is functions over paths, and if any of it
reached for a model these tests would fail rather than pass quietly.
"""

import json
import shutil
import subprocess

import pytest

from ai import audio
from ai.audio import (
    MAX_CHUNK_BYTES,
    MediaDecodeError,
    MediaProcessingError,
    MediaTimeoutError,
    MissingBinaryError,
    NoAudioTrackError,
    prepare_audio_chunks,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required to generate fixtures",
)

FFMPEG_FIXTURE_TIMEOUT = 120


def run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=FFMPEG_FIXTURE_TIMEOUT,
    )


def probe(path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=codec_type,codec_name,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=FFMPEG_FIXTURE_TIMEOUT,
    )
    return json.loads(completed.stdout)


def duration_of(path) -> float:
    return float(probe(path)["format"]["duration"])


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Every fixture file the suite needs, generated once per module."""
    directory = tmp_path_factory.mktemp("media")

    # Speech-shaped enough for silencedetect: tone, silence, tone, silence, tone.
    gaps = directory / "gaps.wav"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:duration=4",
        "-filter_complex",
        "[0:a][1:a][2:a][1:a][0:a]concat=n=5:v=0:a=1[out]",
        "-map",
        "[out]",
        str(gaps),
    )

    # No gap anywhere: the fallback hard cut is the only way to split this.
    continuous = directory / "continuous.wav"
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=16", str(continuous))

    # Shorter than a second.
    tiny = directory / "tiny.wav"
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", str(tiny))

    # A video with an audio track, and one without.
    video = directory / "video.mp4"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=6",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=6",
        "-map",
        "1:v",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(video),
    )
    mute_video = directory / "mute.mp4"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(mute_video),
    )

    # Not media at all, whatever the extension claims.
    junk = directory / "broken.mp4"
    junk.write_bytes(b"not a media file" * 256)

    return {
        "gaps": gaps,
        "continuous": continuous,
        "tiny": tiny,
        "video": video,
        "mute_video": mute_video,
        "junk": junk,
    }


# --- the transformation ----------------------------------------------------


def test_video_is_stripped_to_its_audio_track(media, tmp_path):
    chunks = prepare_audio_chunks(media["video"], work_root=tmp_path)

    assert len(chunks) == 1
    streams = probe(chunks[0])["streams"]
    assert [stream["codec_type"] for stream in streams] == ["audio"]
    assert streams[0]["codec_name"] == "opus"


def test_audio_is_downsampled_to_16khz_mono_opus(media, tmp_path):
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path)

    stream = probe(chunks[0])["streams"][0]
    assert stream["codec_name"] == "opus"
    assert stream["channels"] == 1

    # Opus always decodes at 48 kHz, so the container cannot show the rate the
    # input was resampled to. Assert on the conversion itself instead.
    command = audio.normalize_command("ffmpeg", media["gaps"], tmp_path / "out.opus")
    assert command[0] == "ffmpeg"
    assert "-vn" in command
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-c:a") + 1] == "libopus"


def test_a_file_that_fits_returns_a_single_element_list(media, tmp_path):
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path)

    assert len(chunks) == 1
    assert chunks[0].is_file()
    assert chunks[0].stat().st_size > 0


def test_every_returned_chunk_is_under_the_size_ceiling(media, tmp_path):
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path)

    assert MAX_CHUNK_BYTES == 24 * 1024 * 1024
    for chunk in chunks:
        assert 0 < chunk.stat().st_size < MAX_CHUNK_BYTES


def test_an_oversized_recording_is_split_under_the_ceiling(media, tmp_path):
    # A real 24 MB ceiling needs hours of audio, so the ceiling is lowered
    # instead. The rule under test is the same one.
    ceiling = 8 * 1024

    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path, max_chunk_bytes=ceiling)

    assert len(chunks) > 1
    for chunk in chunks:
        assert 0 < chunk.stat().st_size <= ceiling
    assert sum(duration_of(chunk) for chunk in chunks) == pytest.approx(16.0, abs=1.0)


def test_chunks_come_back_in_playback_order(media, tmp_path):
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path, max_chunk_seconds=7)

    assert len(chunks) > 1
    assert [chunk.name for chunk in chunks] == sorted(chunk.name for chunk in chunks)
    assert sum(duration_of(chunk) for chunk in chunks) == pytest.approx(16.0, abs=1.0)


def test_cuts_land_on_silence_rather_than_the_hard_limit(media, tmp_path):
    # Silence runs 4s-6s and 10s-12s, so the cuts wanted are those midpoints —
    # 5s and 11s — not the 9s hard limit.
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path, max_chunk_seconds=9)

    assert len(chunks) == 3
    assert duration_of(chunks[0]) == pytest.approx(5.0, abs=0.5)
    assert duration_of(chunks[1]) == pytest.approx(6.0, abs=0.5)


def test_plan_cuts_prefers_the_latest_silence_in_the_window():
    cuts = audio._plan_cuts(duration=16.0, silences=[(4.0, 6.0), (10.0, 12.0)], limit=9.0)

    assert cuts == [5.0, 11.0]


# --- the awkward inputs ----------------------------------------------------


def test_a_recording_with_no_silence_still_gets_split(media, tmp_path):
    chunks = prepare_audio_chunks(media["continuous"], work_root=tmp_path, max_chunk_seconds=5)

    assert len(chunks) > 1
    for chunk in chunks:
        assert duration_of(chunk) <= 5.5


def test_a_file_shorter_than_a_second_returns_one_chunk(media, tmp_path):
    chunks = prepare_audio_chunks(media["tiny"], work_root=tmp_path)

    assert len(chunks) == 1
    assert chunks[0].stat().st_size > 0
    assert duration_of(chunks[0]) == pytest.approx(0.4, abs=0.2)


def test_a_file_with_no_audio_track_fails_saying_so(media, tmp_path):
    with pytest.raises(NoAudioTrackError) as excinfo:
        prepare_audio_chunks(media["mute_video"], work_root=tmp_path)

    assert "no audio track" in str(excinfo.value)
    assert not list(tmp_path.iterdir())


def test_a_file_ffmpeg_cannot_decode_fails_with_the_reason(media, tmp_path):
    with pytest.raises(MediaDecodeError) as excinfo:
        prepare_audio_chunks(media["junk"], work_root=tmp_path)

    message = str(excinfo.value)
    assert "broken.mp4" in message
    assert "Invalid data" in message


def test_a_missing_file_is_reported_not_traced(tmp_path):
    with pytest.raises(MediaProcessingError) as excinfo:
        prepare_audio_chunks(tmp_path / "absent.mp4", work_root=tmp_path)

    assert "No such media file" in str(excinfo.value)


def test_exceeding_the_timeout_is_a_failure_with_a_message(media, tmp_path):
    with pytest.raises(MediaTimeoutError) as excinfo:
        prepare_audio_chunks(media["video"], work_root=tmp_path, timeout_seconds=0.001)

    assert "timed out" in str(excinfo.value)


@pytest.mark.parametrize("constant", ["FFMPEG_BINARY", "FFPROBE_BINARY"])
def test_a_missing_binary_is_named(media, tmp_path, monkeypatch, constant):
    monkeypatch.setattr(audio, constant, f"{constant.lower()}-not-installed")

    with pytest.raises(MissingBinaryError) as excinfo:
        prepare_audio_chunks(media["video"], work_root=tmp_path)

    assert f"{constant.lower()}-not-installed" in str(excinfo.value)


# --- hygiene ---------------------------------------------------------------


def test_only_the_returned_chunks_survive_a_successful_run(media, tmp_path):
    chunks = prepare_audio_chunks(media["gaps"], work_root=tmp_path, max_chunk_bytes=8 * 1024)

    survivors = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    assert survivors == sorted(chunks)


def test_nothing_survives_a_failed_run(media, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise MediaDecodeError("Scanning for silence failed: contrived")

    monkeypatch.setattr(audio, "_detect_silences", explode)

    with pytest.raises(MediaDecodeError):
        prepare_audio_chunks(media["gaps"], work_root=tmp_path, max_chunk_bytes=8 * 1024)

    assert not list(tmp_path.iterdir())


def test_filenames_are_generated_not_taken_from_the_source(media, tmp_path):
    awkward = tmp_path / "sub"
    awkward.mkdir()
    # A name a shell would mangle. Nothing is ever passed through a shell, and
    # the name never reaches the output either.
    hostile = awkward / "; rm -rf $HOME #.wav"
    shutil.copy(media["gaps"], hostile)

    chunks = prepare_audio_chunks(hostile, work_root=tmp_path)

    assert hostile.is_file()
    assert len(chunks) == 1
    assert chunks[0].name == "chunk-00000.opus"


def test_the_scratch_root_defaults_to_the_shared_scratch_dir(media, tmp_path, settings):
    settings.SCRATCH_DIR = tmp_path / "scratch"

    chunks = prepare_audio_chunks(media["tiny"])

    assert chunks[0].is_relative_to(tmp_path / "scratch")
