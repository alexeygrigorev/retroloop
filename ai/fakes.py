"""Stand-ins for the transcription API, for a machine with no key.

`config/settings_test.py` points ``TRANSCRIPTION_CLIENT`` at
:class:`EchoTranscriptionClient`, so the whole pipeline runs in the suite with
no `OPENAI_API_KEY`, no network, and — this is the point — no test that skips
itself when neither is there. CI fails on a skipped test (AGENTS.md, "CI"), so a
suite that quietly opted out would turn the build red rather than green.

It is kept in the application rather than in `tests/`, for the same reason
`config.tasks.always_fails` is: a Compose stack brought up without a key can
point at it too and watch the pipeline work end to end.

Nothing here is reachable in production unless ``TRANSCRIPTION_CLIENT`` is
changed to name it, which is a deliberate act and not a fallback.
"""

from pathlib import Path

from ai.transcription import ChunkTranscript, Segment

#: What the fake says, per chunk. Two speakers, so the stitching and the
#: speaker labels have something real to work on.
STAND_IN_NOTICE = "This transcript was produced by a stand-in client, not by the transcription API."

#: Roughly a minute of speech per chunk, so `duration_seconds` is not zero and
#: the sum across chunks is visibly a sum.
FAKE_SECONDS_PER_CHUNK = 61.0


class EchoTranscriptionClient:
    """Return a deterministic transcript naming the chunk it was given.

    Deterministic on purpose: a test can assert the exact text, and two runs
    over the same chunks produce the same transcript.
    """

    def transcribe(self, path: Path) -> ChunkTranscript:
        chunk = Path(path)
        size = chunk.stat().st_size if chunk.is_file() else 0
        return ChunkTranscript(
            segments=(
                Segment(speaker="A", text=f"{STAND_IN_NOTICE} The chunk was {chunk.name}."),
                Segment(speaker="B", text=f"It was {size} bytes long."),
            ),
            language="en",
            duration_seconds=FAKE_SECONDS_PER_CHUNK,
        )
