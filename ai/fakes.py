"""Stand-ins for the OpenAI clients, for a machine with no key.

`config/settings_test.py` points ``TRANSCRIPTION_CLIENT`` at
:class:`EchoTranscriptionClient` and ``CLUSTERING_CLIENT`` at
:class:`NullClusteringClient`, so both pipelines run in the suite with no
`OPENAI_API_KEY`, no network, and — this is the point — no test that skips
itself when neither is there. CI fails on a skipped test (AGENTS.md, "CI"), so a
suite that quietly opted out would turn the build red rather than green.

They are kept in the application rather than in `tests/`, for the same reason
`config.tasks.always_fails` is: a Compose stack brought up without a key can
point at them too and watch the pipelines work end to end.

Nothing here is reachable in production unless a ``*_CLIENT`` setting is changed
to name it, which is a deliberate act and not a fallback.
"""

from collections import defaultdict
from pathlib import Path

from ai.clustering import CardInput
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


class NullClusteringClient:
    """Suggest no clusters at all, for any input.

    This is the suite's default (`config/settings_test.py`). It is deliberately
    inert: an auto-clusterer that grouped cards would rewrite the board under
    every fixture that reveals a cycle with cards in it, so the default the
    whole suite runs against produces nothing and the reveal-based fixtures stay
    exactly as their authors built them. The clustering behaviour itself is
    proven by tests that inject a client of their own — a scripted one, or the
    real client driven by a fake SDK.

    It also lets a keyless Compose stack advance a retrospective to REVEAL
    without a key and without a network: the job runs, finds no suggestions, and
    leaves the cards ungrouped, which is a valid board and not a failure.
    """

    def cluster(self, cards):
        return []


class EchoClusteringClient:
    """Group the cards by category, deterministically, needing no key.

    One cluster per category present, named after it, the ids in the order they
    arrived. Deterministic on purpose, so a test can assert the exact grouping,
    and category is a grouping a person recognises — which is what makes it
    useful for a keyless Compose stack that wants to see suggestions appear on
    the board end to end.

    It reads only the fields the real request carries — the id and the
    category — and never an author or a pk, because those never reach a
    clustering client in the first place (`_docs/decisions.md` items 9 and 10).
    """

    def cluster(self, cards):
        by_category: dict[str, list[str]] = defaultdict(list)
        for card in cards:
            card = card if isinstance(card, CardInput) else CardInput(**card)
            by_category[card.category].append(card.id)
        return [
            {"name": f"{category.title()} cards", "card_ids": ids}
            for category, ids in by_category.items()
        ]
