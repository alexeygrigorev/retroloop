"""What may be uploaded, how big it may be, and where the bytes land.

Three things live here, away from the model and the form, because the reverse
proxy, the form and the tests all have to agree on them:

* :data:`MAX_UPLOAD_BYTES` — the 500 MB cap. `deploy/nginx.conf` names this
  constant in a comment and sets `client_max_body_size` to the same number, so
  the proxy refuses what the application would refuse anyway rather than
  cutting an upload off with a page nobody wrote.
* the extension allowlist, which is also what decides whether a file is audio,
  video or a transcript. There is one list, the page prints it, and the form
  refuses anything else before a byte is written.
* :func:`stream_to_scratch`, which writes the upload into ``SCRATCH_DIR`` in
  chunks. ``SCRATCH_DIR`` is a volume `web` and `worker` both mount, so the
  path this returns means the same thing in both containers. Anywhere else —
  the system temp directory, a directory next to the code — exists only in the
  container that wrote it, and the worker finds nothing there.

Nothing here touches the database, so the rules can be tested without one.
"""

from pathlib import Path
from uuid import uuid4

from django.conf import settings

#: Uploads are capped at 500 MB, server side. `deploy/nginx.conf` sets
#: `client_max_body_size` to the same value and says so in a comment: the two
#: are one limit expressed twice, and moving one means moving the other.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

#: How the cap reads on the page and in an error message.
MAX_UPLOAD_LABEL = "500 MB"

#: The allowlist, by the kind of record each extension produces. Everything is
#: lower case and includes the dot, which is what an extension is compared
#: against after `Path(name).suffix.lower()`.
AUDIO_EXTENSIONS = (".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav")
VIDEO_EXTENSIONS = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm")
TRANSCRIPT_EXTENSIONS = (".md", ".srt", ".txt", ".vtt")

#: Every extension the form accepts, in the order the page lists them.
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS + TRANSCRIPT_EXTENSIONS

#: How much of the file is held in memory at a time while it is written out.
CHUNK_BYTES = 256 * 1024


def upload_root() -> Path:
    """The directory uploads land in, inside the shared scratch volume.

    Read at call time rather than at import, so a test that repoints
    ``SCRATCH_DIR`` at a directory of its own is obeyed.
    """
    return Path(settings.SCRATCH_DIR) / "uploads"


def generated_upload_path() -> Path:
    """A path nobody supplied: a fresh UUID under :func:`upload_root`.

    No part of it comes from the uploaded filename — not the stem and not the
    extension. A name a person chose is data, and data that reaches a path is
    how a traversal, an overwrite or a shell-quoting bug gets in. What the file
    is called is kept on the record for display; what kind of file it is is
    kept in `MeetingRecord.kind`; neither is needed to read the bytes back, and
    ffprobe identifies a media file by its content anyway.
    """
    return upload_root() / uuid4().hex


def extension_of(filename: str) -> str:
    """The lower-cased extension of `filename`, including the dot."""
    return Path(filename).suffix.lower()


def is_allowed(filename: str) -> bool:
    return extension_of(filename) in ALLOWED_EXTENSIONS


def stream_to_scratch(upload, destination: Path) -> int:
    """Write `upload` to `destination` a chunk at a time, and return its size.

    `upload.chunks()` is Django's stream over the uploaded file, so the whole
    recording is never held in memory — which is what
    ``FILE_UPLOAD_MAX_MEMORY_SIZE`` being low buys, and what pulling the file
    in one go would throw away again.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("wb") as out:
        for chunk in upload.chunks(CHUNK_BYTES):
            out.write(chunk)
            written += len(chunk)
    return written


def write_text_to_scratch(text: str, destination: Path) -> int:
    """Write pasted text into the scratch volume, and return its size in bytes.

    Pasted text takes the same route as a file for one reason: the worker then
    has one thing to open, `temp_path`, whatever the facilitator handed over.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    destination.write_bytes(payload)
    return len(payload)
