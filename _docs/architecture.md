# Architecture — Weekly Team Feedback Tool

Companion to [plan.md](plan.md). Describes *how* the MVP is built. No code yet.

## Stack

| Concern | Choice |
|---|---|
| Language / framework | Python 3.12, Django 5.x |
| Database | PostgreSQL 16 |
| Templates / interactivity | Django templates + HTMX + Alpine.js |
| Retro board | React island (Vite build), mounted in one Django template |
| Styling | Tailwind |
| Auth | Django's built-in `contrib.auth` — username + password, no email |
| Invites | Shareable project join link (rotatable token) |
| Background jobs | One `manage.py` worker polling Postgres (`SKIP LOCKED`) |
| File storage | None — recordings live in a temp dir and are deleted after transcription |
| Media processing | ffmpeg (audio extraction) |
| Transcription | OpenAI Whisper (`whisper-1`, audio transcriptions API) |
| LLM (clustering + extraction) | OpenAI `gpt-4o` with structured outputs |
| Sessions / cache | Database-backed sessions, local-memory cache |
| Tests / lint | pytest-django, ruff |
| Deploy | Docker Compose — `web`, `worker`, `db` |

**Postgres is the only infrastructure dependency.** No Redis, no object store, no
mail server, no third-party auth. One `openai` SDK and one `OPENAI_API_KEY` cover
both transcription and extraction.

Whisper caps uploads at 25 MB, which is the real constraint on the media
pipeline — see [Media pipeline](#media-pipeline). ffmpeg downsampling to 16 kHz
mono Opus keeps roughly 3 hours of speech under that ceiling; longer recordings
are chunked.

## Django apps

```
config/          settings, urls
accounts/        signup / login views (thin wrapper over contrib.auth)
projects/        Project, Membership, join links, permission predicates
cycles/          FeedbackCycle, Card, CycleParticipation
retro/           Retrospective, Cluster, Vote, Note, Decision, ActionItem
meetings/        MeetingRecord, Transcript, the worker command
ai/              transcription + clustering + extraction services (no models)
board/           the React island: view, state endpoint, mutation endpoints
```

`ai/` holds no models and no views — it exposes three functions that take domain
objects and return plain dicts. That keeps every OpenAI call mockable in tests
and swappable by provider.

## Data model

### Identity and membership

```
User            django.contrib.auth.models.User — username, display name, password
Project         name, owner -> User, join_token (uuid), created_at
Membership      project, user, role {MEMBER, FACILITATOR}, joined_at
                unique(project, user)
```

**Auth is deliberately minimal: username and password, no email anywhere.**
`django.contrib.auth.urls` gives login and logout; signup is one form view. No
allauth, no password reset flow, no verification, no mail backend configured.

**Invites are a link, not a message.** Each project carries a `join_token`; the
facilitator shares `/join/<token>/`, and any logged-in user who opens it becomes
a `MEMBER`. The token is rotatable from the project settings page, which
revokes every old link at once. No `Invitation` table, no pending-invite state,
no email delivery to debug.

The trade-offs this accepts, both fine for an internal team tool and both
awkward later:

- **A forgotten password needs an admin.** With no mail backend there is no
  self-serve reset — a superuser resets it via Django admin or
  `manage.py changepassword`.
- **A leaked join link is an open door** until someone rotates the token. Since
  the link grants access to honest feedback about a team, treat rotation as a
  real operation, not a buried setting.

The facilitator role is **per cycle**, not only per project — the plan allows
assigning it to another team member. So `FeedbackCycle.facilitator -> User`
carries the authority for that week's retro; `Membership.role` is the default
used when creating a cycle.

### Feedback collection

```
FeedbackCycle   project, week_start, opens_at, closes_at, facilitator -> User
                status {COLLECTING, CLOSED}
Card            cycle, category {START, STOP, CONTINUE}, text
                author -> User (NULLABLE), is_anonymous, created_at, position
CycleParticipation  cycle, user, card_count, submitted_at
                    unique(cycle, user)
```

### Retrospective

```
Retrospective   cycle (1:1), stage, started_at, completed_at, version (int)
                votes_per_member (default 3)
Cluster         retrospective, name, position, is_auto_generated
                status {PENDING, DISCUSSED, SKIPPED, DEFERRED}
Card.cluster    FK -> Cluster, nullable   # ungrouped cards allowed
Vote            retrospective, cluster, user, weight (1..3)
                unique(retrospective, cluster, user)
Note            retrospective, cluster (nullable), author, text, created_at
Decision        retrospective, cluster (nullable), text
                source {MANUAL, EXTRACTED}, status {DRAFT, CONFIRMED}
ActionItem      retrospective, cluster (nullable), description
                owner -> User (nullable), due_date (nullable)
                status {OPEN, DONE}, source, review_status {DRAFT, CONFIRMED}
```

`Retrospective.version` is a monotonic counter bumped inside every mutating
transaction. It is the entire board-sync mechanism (see below).

### Meeting record

```
MeetingRecord   retrospective, uploaded_by, kind {AUDIO, VIDEO, TRANSCRIPT_FILE, PASTED_TEXT}
                temp_path (nullable), original_filename, size_bytes
                status {UPLOADED, TRANSCRIBING, EXTRACTING, READY, FAILED}
                attempts, error_message, created_at, media_deleted_at
Transcript      meeting_record (1:1), text, language, duration_seconds
```

`temp_path` points at a scratch file on disk and is nulled the moment
transcription succeeds. The `Transcript.text` in Postgres is the only durable
record of the meeting — see below.

## The anonymity design — read this before the first migration

The plan promises anonymous authors are **never** revealed, to anyone. But
contributors must be able to edit their own cards before the retro, which
requires knowing who wrote them. These two requirements conflict in time, not in
principle — so resolve them in time.

**`Card.author` is nullable, and at reveal it is destroyed for anonymous cards:**

```
UPDATE cycles_card SET author_id = NULL
WHERE cycle_id = %s AND is_anonymous = true;
```

This runs inside the same transaction that advances the retrospective to
`REVEAL`. Before that moment, `author` exists so the owner can edit, and the
query layer only ever returns a member their own cards. After that moment the
link does not exist anywhere — not for the facilitator, not for a DB admin, not
in a backup taken tomorrow.

Two consequences to handle deliberately:

- **Participation metrics survive via `CycleParticipation`.** It records *that*
  a member submitted and *how many* cards, never which ones. The summary screen's
  "attendance and participation" section reads from here.
- **Ordering leaks identity.** Cards revealed in `created_at` order let an
  observer correlate an anonymous card with someone who was typing at that time.
  On reveal, assign `Card.position` in shuffled order and sort by it everywhere
  afterwards.

This is the one decision in the whole system that is expensive to retrofit — it
would mean a data migration on the most sensitive table, on data already
collected under a broken promise.

## Permissions

All authorization lives in `projects/permissions.py` as plain predicate
functions (`can_reveal(user, retro)`, `can_edit_card(user, card)`, …), called
from views. Not scattered `if request.user ==` checks, not DRF permission
classes — this app's rules are stage-dependent, and a predicate that takes the
stage into account is the only readable form.

The rules that matter:

| Action | Rule |
|---|---|
| See another member's card | Never before `REVEAL`; everyone after |
| Edit / delete own card | Only while stage is `COLLECTING` |
| See vote totals | Only when stage is past `VOTE`, or `votes_revealed` |
| Advance stage, reveal, close voting | Cycle facilitator only |
| Upload meeting record, confirm extractions | Cycle facilitator only |
| Update an action item | Its owner, or the facilitator |

Vote totals are **omitted from the API payload** during the vote stage — not
hidden in the client. Same for other members' cards before reveal. If it reaches
the browser, it has leaked.

## Stage machine

```
DRAFT -> REVEAL -> CLUSTER -> VOTE -> DISCUSS -> COMPLETE
```

Forward-only, facilitator-driven, guarded server-side in a single
`advance_stage()` service function. Each transition has side effects that must
be transactional with the stage write:

- `-> REVEAL`: null out anonymous authorship, shuffle positions, enqueue the
  auto-clustering job.
- `-> VOTE`: freeze cluster membership (moves rejected afterwards).
- `-> DISCUSS`: compute the ranked agenda, unhide vote totals.
- `-> COMPLETE`: lock the board; the summary becomes the read surface.

## Board sync — polling on a version counter

The board is the only screen with concurrent editors. It works like this:

1. Django renders `board.html` with the initial state serialized into the page.
2. A React bundle (Vite, ~one component tree) mounts and takes over.
3. Every 1.5s it GETs `/retros/<id>/state?v=<known_version>`. If
   `Retrospective.version` is unchanged the response is `304`-ish and tiny; if it
   changed, the full board state comes back and replaces client state.
4. Every mutation (`move card`, `merge clusters`, `rename`, `cast vote`,
   `mark discussed`) POSTs to its own endpoint, which mutates and bumps
   `version` in one transaction, and returns the new full state.

Full-state replacement rather than diffs, last-write-wins on card moves. For a
board of 5–8 people and a few dozen cards, the payload is a handful of KB and
the semantics are trivially correct. No WebSockets, no Redis pub/sub, no CRDT,
no reconciliation logic.

Upgrade path if it ever feels laggy: replace the poll with SSE from an ASGI
worker. Nothing else changes — same endpoints, same state shape.

Every other screen (project page, feedback form, upload status, summary) is
plain Django templates with HTMX for partial updates. No React outside the board.

## Media pipeline

**The recording is transient.** It is never stored — it lands in a scratch
directory, gets transcribed, and is deleted. Only `Transcript.text` survives, in
Postgres. There is no bucket, no `MEDIA_ROOT` to back up, and no retention
policy to write, because after a few minutes there is nothing left to retain.

```
browser --multipart POST--> Django (streams to /scratch/<uuid>)
                              |
                              +--> MeetingRecord(UPLOADED, temp_path=...)
                                        |
                              worker picks it up (SKIP LOCKED)
                                        |
   +------------------------------------+
   |  1. video? -> ffmpeg: strip to audio
   |  2. ffmpeg: downsample to 16 kHz mono Opus
   |  3. >25 MB? -> split into chunks on silence boundaries
   |  4. Whisper per chunk -> concatenate -> Transcript   [TRANSCRIBING]
   |     (pasted text / transcript file skips 1-4)
   |  5. DELETE the scratch file, null temp_path          <-- always, even on failure
   |  6. gpt-4o extraction over transcript                [EXTRACTING]
   |  7. write Decision/ActionItem rows as DRAFT          [READY]
   +------------------------------------+
```

Step 5 is in a `finally`. A failed transcription must not leave a meeting
recording sitting on disk.

Two consequences of dropping object storage, both worth accepting knowingly:

- **Django receives the bytes.** Set `FILE_UPLOAD_MAX_MEMORY_SIZE` low so uploads
  stream to disk rather than buffering in RAM, cap uploads at 500 MB, and raise
  the reverse-proxy body limit to match. A long upload occupies a web worker for
  its duration, so run a handful of workers.
- **`web` and `worker` must share a filesystem.** They mount the same scratch
  volume in Compose. This is the constraint that pins us to a single host — fine
  for the MVP, and the thing to revisit before scaling out.

Because Whisper caps at 25 MB per request, the UI should steer people toward
audio or a pasted transcript over a 90-minute video upload. Chunking works, but
each split is a place a sentence can break across a boundary.

## The worker

No Celery, no Redis, no broker. One management command, `manage.py
process_recordings`, running as its own Compose service:

```
loop forever:
    claim one row inside a transaction:
        SELECT ... FROM meetings_meetingrecord
        WHERE status = 'UPLOADED' AND attempts < 3
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED LIMIT 1
    process it; on exception increment attempts, record error_message,
    and set FAILED once attempts hits 3
    sleep 5s when the queue is empty
```

`FOR UPDATE SKIP LOCKED` is what makes this safe — two workers never claim the
same row, so scaling is `docker compose up --scale worker=3`. The whole thing is
about 40 lines and has no dependencies beyond Django.

This is the right size for the job because there is exactly **one** task type
with a low arrival rate (a few per team per week). If a second and third job type
appear — scheduled cycle reminders, digest generation — swap in `django-q2`,
which keeps the Postgres-only property while giving you scheduling and a UI.

Failures set `FAILED` with a readable `error_message`; the facilitator sees it on
the upload page (which polls `status` over HTMX) and can retry, which resets
`attempts`. Retrying is only possible if the media still exists — and it doesn't.
**So a failed transcription means re-uploading the file**, which is the direct
cost of not keeping recordings. Say so in the error message rather than offering
a retry button that cannot work.

## The two OpenAI calls

Both live in `ai/`, both use structured outputs (JSON schema), both produce
**suggestions that are never authoritative**.

**Clustering** (on reveal): all cards with `{id, category, text}` in, a list of
`{name, card_ids}` out. Written as `Cluster` rows with `is_auto_generated=True`.
The team edits freely from there — the flag is only for display ("suggested"),
never for permissions.

**Extraction** (after transcription): transcript + the ranked agenda + the
project roster in; decisions, action items with owner *names*, due dates, and a
summary out. Owner names are resolved to `User` rows by fuzzy match against the
roster, and an unmatched owner stays `null` rather than guessing — the
facilitator picks from a dropdown.

Everything lands as `DRAFT`. The confirm step is a single facilitator screen with
per-item accept/edit/reject. Nothing is published until they act.

Note: clustering sends card text — including anonymous cards — to OpenAI, and the
transcript goes there too. That is fine, but it belongs in the privacy copy, not
as a surprise.

## Deployment

`docker compose up`, three services:

| Service | Command | Notes |
|---|---|---|
| `db` | postgres:16 | named volume for data |
| `web` | gunicorn | mounts `scratch` volume; ffmpeg in the image |
| `worker` | `manage.py process_recordings` | same image, same `scratch` mount |

The scratch volume is shared between `web` and `worker` and holds nothing of
value — it can be wiped between deploys. `db` holds everything that matters, so
it is the only thing to back up.

Config is environment variables: `DATABASE_URL`, `OPENAI_API_KEY`,
`SECRET_KEY`, `ALLOWED_HOSTS`, `DEBUG`. No mail settings, no storage
credentials.

## Open questions for you

1. **Can members edit cards after reveal?** The plan implies no ("*before* the
   retrospective, contributors can edit"). The model above locks them at
   `COLLECTING`. Confirm.
2. **Are votes changeable during the vote stage?** Assumed yes — freely
   reassignable until voting closes, since totals are hidden anyway.
3. **Destroying anonymous authorship is irreversible.** It's the right call for
   the product promise, but it means no future feature can ever recover it.
   Confirm you want it.
4. **What closes a cycle when someone doesn't submit?** Assumed the facilitator
   can close and reveal regardless, with non-submitters visible as such.
5. **Does an action item carry across cycles?** The project page shows "open
   action items" — assumed project-scoped query over all retros, not a copy.
6. **Discarding the recording means a failed transcription is unrecoverable** —
   the facilitator has to upload the file again. Confirm that's the trade you
   want, versus keeping the media for 24 hours to make retries possible.
