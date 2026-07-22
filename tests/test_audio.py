"""Tests for the audio normalisation and chunking pipeline.

Fixtures are synthesised with ffmpeg when the suite runs — a tone, a tone with
silences in it, a continuous tone, a tone that steps quieter every few seconds,
a recording a hair over a chunk boundary, one whose average bitrate lies, a
video with sound and a video without — so no binary media is committed to the
repository.

Nothing in this file touches the database, and no test carries the
`django_db` marker: the pipeline is functions over paths, and if any of it
reached for a model these tests would fail rather than pass quietly.
"""

import json
import shutil
import subprocess
from itertools import pairwise

import pytest

from ai import audio
from ai.audio import (
    MAX_CHUNK_BYTES,
    MIN_CHUNK_SECONDS,
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


def mean_volume_of(path) -> float:
    """The mean level of a file in dBFS, as ffmpeg's volumedetect measures it.

    This is how a test can ask what a chunk *sounds* like rather than what it is
    called, which is the only way to tell a correctly ordered list of chunks
    from a reversed one.
    """
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=FFMPEG_FIXTURE_TIMEOUT,
    )
    for line in completed.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise AssertionError(f"volumedetect reported no mean_volume for {path}")


#: How far apart the steps of the `descending` fixture are, in dB. Two chunks
#: are only accepted as "in the order it was recorded" when the later one is
#: audibly quieter than the earlier one, not a rounding error quieter.
LOUDNESS_STEP_DB = 2.0

#: The `descending` fixture: twelve chunks is two past chunk-00009, and five
#: seconds is long enough for volumedetect to have something to measure.
DESCENDING_CHUNKS = 12
DESCENDING_SECONDS = 5

#: The `overrun` fixture runs a fifth of a second past two chunks of this long.
OVERRUN_LIMIT_SECONDS = 40


def assert_recorded_in_order(chunks) -> None:
    """Fail unless these chunks carry the descending signal in the order it was recorded.

    Deliberately says nothing about filenames: the assertion this replaces
    compared the returned names against those same names sorted, which is true
    however the list is arranged.
    """
    levels = [mean_volume_of(chunk) for chunk in chunks]
    steps_down = [later < earlier - LOUDNESS_STEP_DB / 2 for earlier, later in pairwise(levels)]
    assert all(steps_down), f"not monotonically quieter: {levels}"


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

    # Twelve five-second steps, each LOUDNESS_STEP_DB quieter than the last, so
    # a chunk can be placed in the recording by how loud it is. Twelve is chosen
    # to carry the returned list past chunk-00009 to chunk-00010, where
    # lexicographic and numeric order would part company if the names were ever
    # sorted as text. Every step stays above the silence floor, so silencedetect
    # finds nothing and the cuts land on the clock.
    descending = directory / "descending.wav"
    inputs: list[str] = []
    steps: list[str] = []
    for index in range(DESCENDING_CHUNKS):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DESCENDING_SECONDS}"]
        steps.append(f"[{index}:a]volume={-LOUDNESS_STEP_DB * index}dB[step{index}]")
    concat = "".join(f"[step{index}]" for index in range(DESCENDING_CHUNKS))
    run_ffmpeg(
        *inputs,
        "-filter_complex",
        f"{';'.join(steps)};{concat}concat=n={DESCENDING_CHUNKS}:v=0:a=1[out]",
        "-map",
        "[out]",
        str(descending),
    )

    # A hair over two whole chunks at OVERRUN_LIMIT_SECONDS: the shape that
    # produced the 0.2 s, 507 byte trailing chunk #57 was raised for.
    overrun = directory / "overrun.wav"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={2 * OVERRUN_LIMIT_SECONDS + 0.2}",
        str(overrun),
    )

    # A file whose average bitrate lies: two minutes of digital silence, which
    # Opus encodes to almost nothing, then twenty seconds of noise, which it
    # cannot. Planning from the average under-reads the end of this file badly,
    # which is the case a merged tail must not be trusted in.
    lying = directory / "lying.wav"
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono:d=120",
        "-f",
        "lavfi",
        "-i",
        "anoisesrc=r=44100:d=20.2:amplitude=0.8",
        "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map",
        "[out]",
        str(lying),
    )

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
        "descending": descending,
        "overrun": overrun,
        "lying": lying,
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
    # The signal, not the filenames: each five second step of this recording is
    # 2 dB quieter than the one before it, so the returned chunks are only in
    # playback order if they come back monotonically quieter.
    chunks = prepare_audio_chunks(
        media["descending"], work_root=tmp_path, max_chunk_seconds=DESCENDING_SECONDS
    )

    assert len(chunks) == DESCENDING_CHUNKS
    assert_recorded_in_order(chunks)
    assert sum(duration_of(chunk) for chunk in chunks) == pytest.approx(
        DESCENDING_CHUNKS * DESCENDING_SECONDS, abs=1.0
    )


def test_the_playback_order_check_fails_on_a_reversed_list(media, tmp_path):
    # The assertion this file used to make — names against sorted names — held
    # for any arrangement of the same chunks. This one has to reject the wrong
    # order, and the only way to know it does is to hand it one.
    chunks = prepare_audio_chunks(
        media["descending"], work_root=tmp_path, max_chunk_seconds=DESCENDING_SECONDS
    )

    with pytest.raises(AssertionError, match="not monotonically quieter"):
        assert_recorded_in_order(list(reversed(chunks)))


def test_the_playback_order_check_reads_past_the_ninth_chunk(media, tmp_path):
    # Where lexicographic and numeric order diverge for anything unpadded, and
    # the boundary QA's own run crossed. Both sides of it are in the list under
    # test, so an ordering that only holds below ten cannot pass.
    chunks = prepare_audio_chunks(
        media["descending"], work_root=tmp_path, max_chunk_seconds=DESCENDING_SECONDS
    )

    names = [chunk.name for chunk in chunks]
    assert "chunk-00009.opus" in names
    assert "chunk-00010.opus" in names
    assert mean_volume_of(chunks[10]) < mean_volume_of(chunks[9])


def test_cuts_land_on_silence_rather_than_the_hard_limit(media, tmp_path):
    # Silence runs 4s-6s and 10s-12s, so the cuts wanted are those midpoints —
    # 5s and 11s — not the 9s hard limit. Tail merging is off: this test is
    # about where a cut lands, and the 5 s left after the last one is under the
    # real minimum, which has a test of its own.
    chunks = prepare_audio_chunks(
        media["gaps"], work_root=tmp_path, max_chunk_seconds=9, min_chunk_seconds=0
    )

    assert len(chunks) == 3
    assert duration_of(chunks[0]) == pytest.approx(5.0, abs=0.5)
    assert duration_of(chunks[1]) == pytest.approx(6.0, abs=0.5)


def test_plan_cuts_prefers_the_latest_silence_in_the_window():
    cuts = audio._plan_cuts(duration=16.0, silences=[(4.0, 6.0), (10.0, 12.0)], limit=9.0)

    assert cuts == [5.0, 11.0]


# --- the short trailing chunk ----------------------------------------------


def test_a_recording_just_over_the_boundary_has_no_stub_chunk(media, tmp_path):
    # Two whole chunks and a fifth of a second, the shape that produced a
    # 0.2 s, 507 byte chunk of its own — and, since every chunk is its own
    # transcription request, an API round trip for a fifth of a second.
    chunks = prepare_audio_chunks(
        media["overrun"], work_root=tmp_path, max_chunk_seconds=OVERRUN_LIMIT_SECONDS
    )

    durations = [duration_of(chunk) for chunk in chunks]
    assert len(chunks) == 2
    assert min(durations) >= MIN_CHUNK_SECONDS
    # The tail was merged, not dropped: all of the audio is still there.
    assert sum(durations) == pytest.approx(2 * OVERRUN_LIMIT_SECONDS + 0.2, abs=0.5)


def test_the_boundary_recording_would_otherwise_end_in_a_stub_chunk(media, tmp_path):
    # The same input with the minimum turned off, which is what this module did
    # before #57. Without it the test above would pass whatever the code did.
    chunks = prepare_audio_chunks(
        media["overrun"],
        work_root=tmp_path,
        max_chunk_seconds=OVERRUN_LIMIT_SECONDS,
        min_chunk_seconds=0,
    )

    assert len(chunks) == 3
    assert duration_of(chunks[-1]) < 1.0


def test_a_short_tail_is_merged_into_the_chunk_before_it():
    # 0.2 s left after the cut at 80 s, and room for it: 40.2 s at 1 kB/s is
    # 40 200 bytes against a 90 000 byte budget.
    cuts = audio._merge_short_tail(
        [40.0, 80.0],
        duration=80.2,
        bytes_per_second=1000,
        min_chunk_seconds=30,
        max_chunk_bytes=100_000,
    )

    assert cuts == [40.0]


def test_a_tail_is_left_alone_when_merging_it_would_break_the_size_cap():
    # The same tail against a ceiling the merged chunk would not fit under:
    # 40.2 s at 1 kB/s against a 36 000 byte budget. The cut stays, and the
    # short chunk with it — a chunk no request can carry is the worse outcome.
    cuts = audio._merge_short_tail(
        [40.0, 80.0],
        duration=80.2,
        bytes_per_second=1000,
        min_chunk_seconds=30,
        max_chunk_bytes=40_000,
    )

    assert cuts == [40.0, 80.0]


def test_a_tail_long_enough_to_stand_alone_is_kept():
    cuts = audio._merge_short_tail(
        [40.0, 80.0],
        duration=115.0,
        bytes_per_second=1000,
        min_chunk_seconds=30,
        max_chunk_bytes=100_000,
    )

    assert cuts == [40.0, 80.0]


def test_merging_never_returns_a_chunk_over_the_size_ceiling(media, tmp_path):
    # Two minutes of silence then twenty seconds of noise: the average bitrate
    # the cuts are planned from is nothing like the bitrate at the end of this
    # file, so a tail merged on that average is a chunk the API could refuse.
    ceiling = 25_000

    chunks = prepare_audio_chunks(
        media["lying"], work_root=tmp_path, max_chunk_seconds=70, max_chunk_bytes=ceiling
    )

    assert len(chunks) > 1
    for chunk in chunks:
        assert 0 < chunk.stat().st_size <= ceiling
    assert sum(duration_of(chunk) for chunk in chunks) == pytest.approx(140.2, abs=1.0)
    # And the trade-off, stated rather than assumed: the bitrate says the merge
    # would not fit, so a chunk under the minimum comes back instead. A short
    # request costs a round trip; an oversized one is refused outright.
    assert min(duration_of(chunk) for chunk in chunks) < MIN_CHUNK_SECONDS


# --- the awkward inputs ----------------------------------------------------


def test_a_recording_with_no_silence_still_gets_split(media, tmp_path):
    # Tail merging off, as above: every chunk here is meant to be the hard
    # limit long, including the last one.
    chunks = prepare_audio_chunks(
        media["continuous"], work_root=tmp_path, max_chunk_seconds=5, min_chunk_seconds=0
    )

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
