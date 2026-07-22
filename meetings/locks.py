"""Who owns a record's media right now, answered by Postgres rather than a clock.

The sweeper in `meetings/sweeper.py` deletes recordings of private meetings.
Deleting one out from under a worker that is still transcribing it is worse than
the leak the sweeper exists to fix, so it has to be able to ask a question the
database can answer truthfully: *is a live process working on this record?*

A status column cannot answer it. `TRANSCRIBING` is what a worker that was
`SIGKILL`ed leaves behind and what a worker that is halfway through a chunk
leaves behind; the row looks the same either way. Neither can a timestamp: a
lease that expires is a guess about how long ffmpeg and an API call may take,
and the guess is wrong in one of two directions — too short and the sweeper
races a live transcription, too long and a killed worker's recording sits on
disk until the lease runs out.

A Postgres *session* advisory lock answers it exactly, because the answer is
tied to the process rather than to a value someone wrote:

* :func:`media_lock` takes the lock for one record and never waits — the caller
  is told whether it got it and decides what to do, so nothing here can block a
  worker or a sweep behind anything else;
* the pipeline holds it for the whole of `meetings.pipeline.run`, across the
  transactions it opens and closes, which is why it cannot be a row lock: the
  status page has to see the committed `TRANSCRIBING` row while the run is still
  going, so the run cannot sit inside one long transaction;
* when the process dies — `SIGKILL`, a pulled plug, `kill -9` on the container —
  its backend goes with it and Postgres releases the lock itself. There is
  nothing to time out and nothing to clean up. That is the whole reason this is
  an advisory lock and not a column.

No new infrastructure: this is the Postgres the project already runs, and the
lock costs one row in `pg_locks`.

Lock order
----------

The project's global order is retrospective < cycle < card (#10, found by QA as
what keeps the reveal deadlock-free). This adds one rule ahead of all of them:
**the media advisory lock is taken before any row lock, and a holder of it takes
no other lock than the `MeetingRecord` row it names.** The pipeline and the
sweeper both obey it, so the two cannot form a cycle. It is also `try`-only —
neither ever waits for it — so even a mistake here costs a skipped file rather
than a stuck worker.

One caveat, worth knowing rather than working around: advisory locks belong to a
*session*, and a session is re-entrant. Two calls on one connection both succeed.
Workers are separate processes on separate connections, so this never happens in
production — but it does mean a test cannot prove the exclusion from inside one
process. `tests/test_media_sweeper.py` spawns a real second process for that.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from django.db import connection

logger = logging.getLogger(__name__)

#: The high half of every media lock key: "ME", for meetings. Advisory locks
#: share one key space across the whole database, so the id of a record is
#: namespaced rather than used raw — otherwise any other advisory lock this
#: project ever takes on the number 7 would be the same lock as record 7's.
MEDIA_LOCK_NAMESPACE: Final[int] = 0x4D45

#: The low half is the record id, wrapped into 32 bits so a key always fits the
#: signed 64-bit argument. Two records four billion apart would share a key; the
#: only consequence is that one of them is skipped by a sweep that could have
#: collected it, which the next sweep puts right.
_ID_SPACE: Final[int] = 1 << 32


def media_lock_key(record_id: int) -> int:
    """The advisory lock key that stands for one record's media."""
    return (MEDIA_LOCK_NAMESPACE << 32) | (record_id % _ID_SPACE)


@contextmanager
def media_lock(record_id: int) -> Iterator[bool]:
    """Hold this record's media lock for the block, if it is free.

    Yields whether the lock was taken. It never waits: `False` means another
    process is working on that record's media right now, and the caller's job is
    to leave it alone rather than to queue up behind it.

    Released on the way out however the block ends, including on a
    `BaseException` — and released by Postgres itself if the process does not
    get that far.
    """
    key = media_lock_key(record_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        held = bool(cursor.fetchone()[0])

    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
