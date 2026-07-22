"""A real worker process, transcribing a real record, that a test can `SIGKILL`.

`tests/test_media_sweeper.py` needs two things no in-process test can give it,
and both need a second operating-system process:

* **a real `SIGKILL`.** The defect in #71 is that a killed worker runs no
  `finally`. A mocked kill runs Python, and Python is exactly what a `SIGKILL`
  does not run. So the thing being killed here is a genuine process, killed with
  a genuine signal 9, with no handler and no chance to tidy up;
* **a second database session.** The sweeper decides whether a worker is alive
  by trying for that record's Postgres advisory lock, and advisory locks are
  re-entrant within one session: a test that took the lock on its own connection
  would find its own sweep able to take it too, and would prove nothing. Two
  processes are two sessions, which is what production has.

What it does, in the parent's own scratch directory and against the parent's own
test database:

1. runs the real `meetings.pipeline.run` on the record it is given, so the media
   lock, the `TRANSCRIBING` row and the work directory of chunks are all made by
   the code under test rather than by a fixture pretending to be it;
2. blocks for ever inside the transcription client, once the chunks are on disk,
   having written the file the parent waits on. The parent kills it there —
   between chunks, holding everything.

Chunking is stood in for the same way `tests/test_transcription.py` does it, so
the harness needs no ffmpeg. Run as::

    python -m tests.killable_worker <record-id> <ready-file>

with `SWEEPER_TEST_DATABASE` and `SCRATCH_DIR` set. It never returns.
"""

import os
import sys
import time
from pathlib import Path

#: How many chunk files the stand-in cuts. More than one, so a kill inside the
#: first transcription is genuinely a kill *between* chunks with work left.
CHUNKS = 3


def main(argv: list[str]) -> int:
    record_id = int(argv[0])
    ready = Path(argv[1])

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_test")
    import django

    django.setup()

    from django.conf import settings
    from django.db import connection

    # The parent is a `transaction=True` test, so its rows are committed in the
    # test database Django made for it. This process reaches that database and
    # no other: `.venv`'s sitecustomize pins DATABASE_URL to the worktree's own
    # *development* database, which is the right thing for every other command
    # and the wrong one here, so the name is replaced before anything connects.
    expected = os.environ["SWEEPER_TEST_DATABASE"]
    settings.DATABASES["default"]["NAME"] = expected
    if connection.settings_dict["NAME"] != expected:
        sys.exit(f"connected to {connection.settings_dict['NAME']!r}, wanted {expected!r}")

    from meetings import pipeline
    from meetings.models import MeetingRecord

    def prepare_chunks(source, *, work_root=None, **kwargs) -> list[Path]:
        """Stand in for #20's ffmpeg work: real files, in the real work directory."""
        root = Path(work_root)
        root.mkdir(parents=True, exist_ok=True)
        parts = []
        for index in range(CHUNKS):
            part = root / f"chunk-{index:05d}.opus"
            part.write_bytes(Path(source).read_bytes())
            parts.append(part)
        return parts

    pipeline.prepare_audio_chunks = prepare_chunks

    class BlockingClient:
        """Says it has started, then never finishes. The kill lands here."""

        def transcribe(self, path: Path):
            ready.write_text(f"{os.getpid()} {path}\n")
            while True:
                time.sleep(0.05)

    pipeline.run(MeetingRecord.objects.get(pk=record_id), client=BlockingClient())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
