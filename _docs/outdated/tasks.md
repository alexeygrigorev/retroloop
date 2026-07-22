# Task Backlog — Weekly Team Feedback Tool

Companion to [plan.md](plan.md) (what to build) and [architecture.md](architecture.md)
(how it fits together).

Each task is sized for one session and written to be picked up by someone who has
not read the others. Where a task depends on earlier work, the description says
so explicitly.

## Verified stack versions (checked 2026-07-22)

| Component | Version | Note |
|---|---|---|
| Python | 3.14 | Django 6.0 requires ≥ 3.12 |
| Django | 6.0 | Ships `django.tasks`, template partials, built-in CSP |
| PostgreSQL | 18 | Supported to 2030 |
| psycopg | 3.3 | |
| gunicorn | 26.0 | |
| Background tasks | `django.tasks` + `django-tasks-db` 0.12 | Core ships only dummy/immediate backends; the ORM backend is a separate package |
| HTMX / Alpine.js | 2.0 / 3.15 | |
| React / Vite | 19.2 / 8.1 | Board island only |
| Tailwind | 4.3 | CSS-first config — no `tailwind.config.js` |
| ffmpeg | 8.1 | |
| pytest / pytest-django / ruff | 9.1 / 4.12 / 0.15 | |
| openai SDK | 2.46 | |
| Transcription model | `gpt-4o-transcribe-diarize` | 25 MB request cap; diarization gives speaker labels |
| Text model | `gpt-5.6-terra` | Structured outputs; `gpt-4o` is superseded |

Two of these changed the architecture, and [architecture.md](architecture.md) has
been updated to match: Django 6.0's built-in `django.tasks` replaces the
hand-rolled `SKIP LOCKED` worker, and `gpt-4o-transcribe-diarize` supplies
speaker labels that make action-item owner extraction substantially more
reliable.

---

## 1. Project skeleton with a passing test

Goal: A Django project that installs, runs, and has a green test suite.

Description: Create a Django 6.0 project on Python 3.14 with `pyproject.toml`,
pinned dependencies, and a `config/` settings module reading `DATABASE_URL`,
`SECRET_KEY`, `DEBUG`, and `ALLOWED_HOSTS` from the environment. Configure
pytest-django and ruff, and add one test asserting the homepage returns 200.
`pytest` must pass and `ruff check` must be clean before this task is done.

## 2. Docker Compose environment

Goal: `docker compose up` gives a working dev environment on a clean machine.

Description: Write a Dockerfile (Python 3.14 slim, ffmpeg 8.1 installed) and a
`compose.yaml` with three services: `db` (postgres:18, named volume), `web`
(gunicorn), and `worker` (placeholder command for now). Mount a shared `scratch`
volume into both `web` and `worker` — the media pipeline needs them to see the
same filesystem. Verify the test suite runs inside the container.

## 3. Base layout and frontend assets

Goal: A styled base template with HTMX and Alpine available on every page.

Description: Add Tailwind 4 (CSS-first configuration in a stylesheet, no
`tailwind.config.js`), HTMX 2, and Alpine 3 to a `base.html` layout with
navigation and a message/flash area. Set up the asset build so styles compile in
development and are collected for production. No application screens yet — a
placeholder page demonstrating a working HTMX swap is enough.

## 4. Authentication without email

Goal: Users can sign up, log in, and log out using a username and password.

Description: Wire `django.contrib.auth.urls` for login/logout and add a single
signup view with username, display name, and password. No email backend is
configured anywhere: no verification, no password reset, no allauth. Document in
the README that a forgotten password is reset by an admin via
`manage.py changepassword`.

## 5. Projects, membership, and join links

Goal: A user can create a project and invite others with a shareable link.

Description: Add `Project` (name, owner, `join_token`) and `Membership` (project,
user, role of MEMBER or FACILITATOR) models with project create/list/detail
views. Anyone logged in who opens `/join/<token>/` becomes a MEMBER. Add a
settings action that rotates `join_token`, invalidating every previously shared
link at once.

## 6. Permission predicates

Goal: One module holds every authorization rule in the app.

Description: Create `projects/permissions.py` with plain predicate functions
(`can_view_card`, `can_edit_card`, `can_advance_stage`, `can_see_vote_totals`,
`can_upload_recording`, …) that take a user plus a domain object. The rules are
stage-dependent, so most predicates read the retrospective's current stage.
Ship thorough unit tests — this module is the security boundary and everything
else calls into it. Assumes tasks 5 and 9 supply the models being guarded.

## 7. Feedback cycles

Goal: A facilitator can open and close a weekly feedback cycle for a project.

Description: Add `FeedbackCycle` (project, week_start, opens_at, closes_at,
facilitator, status of COLLECTING or CLOSED) with views to create a cycle and
close it. The facilitator is stored per cycle rather than derived from project
role, because the plan allows handing the role to another member for a given
week. Only one cycle per project may be COLLECTING at a time.

## 8. Feedback cards and submission form

Goal: Members can write, edit, and delete their own Start/Stop/Continue cards.

Description: Add a `Card` model (cycle, category, text, nullable author,
`is_anonymous`, position) and a three-column form where members add short cards
under Start, Stop, and Continue, each with an anonymous checkbox. Card
create/edit/delete happen over HTMX against the card list. Members see only
their own cards on this screen — enforced in the queryset, not the template.

## 9. Retrospective and stage machine

Goal: A retrospective moves forward through its stages under facilitator control.

Description: Add a `Retrospective` model (one-to-one with a cycle) with a
`stage` field over DRAFT → REVEAL → CLUSTER → VOTE → DISCUSS → COMPLETE and an
integer `version` counter. Implement a single `advance_stage()` service function
that is forward-only, facilitator-only, and runs each transition's side effects
in the same transaction as the stage write. Cover every legal and illegal
transition with tests.

## 10. Anonymity at reveal

Goal: Anonymous authorship is destroyed, not merely hidden, when cards are revealed.

Description: In the transition into REVEAL, null out `Card.author` for every card
with `is_anonymous=True`, and assign shuffled `position` values so reveal order
cannot be correlated with submission time. Add a `CycleParticipation` model
recording that a member submitted and how many cards, so the summary screen can
show participation without retaining the link. This is irreversible by design and
is the one decision that is painful to retrofit — write the tests carefully.

## 11. Board state endpoint

Goal: A single JSON endpoint returns everything the retro board needs to render.

Description: Build `GET /retros/<id>/state?v=<version>` returning clusters,
cards, stage, and the viewer's own votes, with a short-circuit response when the
client's `version` matches the stored one. Vote totals and other members' cards
are omitted from the payload entirely when the stage does not permit them — the
filtering happens server-side, never in the client. No UI in this task; tests
drive the endpoint directly.

## 12. Board mutation endpoints

Goal: Every board action has an endpoint that mutates state and bumps the version.

Description: Add POST endpoints for moving a card between clusters, creating,
renaming, merging and splitting clusters, and leaving cards ungrouped. Each runs
in a transaction that increments `Retrospective.version` and returns the fresh
full board state, so a client can apply the response directly. Moves are
rejected once the stage passes CLUSTER. Assumes task 11's serializer.

## 13. React island build pipeline

Goal: A React component mounts inside a Django template with a production build.

Description: Set up Vite 8 and React 19 building a single bundle that Django
serves through its static files, with a manifest-based template tag resolving
hashed filenames. Mount a trivial component into a Django-rendered page that
reads bootstrap JSON from the template and polls the state endpoint. This is the
only React in the app — every other screen stays server-rendered.

## 14. Cluster board UI

Goal: The team can drag cards into clusters and reorganize them together.

Description: Build the React board for the CLUSTER stage: cards in columns,
drag-and-drop between clusters, plus rename, merge, split, and ungroup actions
calling the mutation endpoints. Poll the state endpoint every 1.5 seconds and
replace local state when the version changes, with last-write-wins on conflicts.
Assumes tasks 12 and 13.

## 15. Voting

Goal: Each member spends three stackable votes without seeing anyone else's.

Description: Add a `Vote` model (retrospective, cluster, user, weight) and vote
cast/withdraw endpoints enforcing a three-vote budget per member per
retrospective, with multiple votes allowed on one cluster. Votes stay
reassignable while the stage is VOTE. Totals are absent from the state payload
until the stage advances, so no client change can expose them early.

## 16. Discussion mode

Goal: The facilitator works a ranked agenda and the team takes notes live.

Description: Render clusters ordered by total votes once the stage is DISCUSS,
with facilitator controls marking each as Discussed, Skipped, or Deferred. Add a
`Note` model so any member can record notes against the cluster under
discussion. Notes appear for everyone through the existing board polling.

## 17. Decisions and action items

Goal: Decisions and actions can be recorded, edited, and completed by hand.

Description: Add `Decision` (text, optional cluster, source, status) and
`ActionItem` (description, owner, optional due date, OPEN/DONE status, optional
cluster, source, review status) models with manual create and edit forms. Both
carry a `source` field distinguishing MANUAL from EXTRACTED entries, and a review
status so AI-generated rows can sit as drafts later. Action owners may flip their
own items to DONE.

## 18. Background task infrastructure

Goal: Work runs outside the request cycle on a Postgres-backed queue.

Description: Configure Django 6.0's built-in `django.tasks` framework with the
`django-tasks-db` ORM backend — core ships only dummy and immediate backends, so
the separate package is required for production. Point the Compose `worker`
service at its worker command and add a trivial task plus a test proving
enqueue-and-execute works. No Redis and no Celery.

## 19. Meeting upload

Goal: A facilitator can upload a recording or paste a transcript.

Description: Build the upload page accepting audio, video, a transcript file, or
pasted text, creating a `MeetingRecord` row (kind, temp_path, status, attempts,
error_message). Uploads stream to the shared scratch directory rather than
buffering in memory; cap them at 500 MB and match the limit in the proxy config.
The page polls `status` over HTMX and shows failures with their error message.

## 20. Audio normalization and chunking

Goal: Any uploaded media becomes transcription-ready audio under 25 MB per part.

Description: Write an ffmpeg wrapper that strips video to audio and downsamples
to 16 kHz mono Opus, then splits the result on silence boundaries into chunks
below the transcription API's 25 MB request cap. Return an ordered list of chunk
paths. Pure functions over files with fixture-based tests — no Django models
involved.

## 21. Transcription service

Goal: Prepared audio becomes a stored transcript with speaker labels.

Description: Add `ai/transcription.py` calling the OpenAI audio API with
`gpt-4o-transcribe-diarize`, transcribing chunks in order and concatenating them
into a `Transcript` row. Diarization matters here: speaker labels are what make
action-item owner extraction reliable in the next stage. The uploaded media file
is deleted in a `finally` block whether or not transcription succeeded.

## 22. AI clustering

Goal: Revealed cards arrive pre-grouped into editable suggested clusters.

Description: Add `ai/clustering.py` sending all cards as `{id, category, text}`
to `gpt-5.6-terra` with a structured-output schema and receiving
`{name, card_ids}` groups, written as `Cluster` rows flagged
`is_auto_generated=True`. Enqueue it as a background task on the REVEAL
transition. The flag drives display wording only and never restricts editing.

## 23. Outcome extraction

Goal: A transcript yields draft decisions, actions, owners, and a summary.

Description: Add `ai/extraction.py` sending the transcript, the ranked agenda,
and the project roster to `gpt-5.6-terra` with a structured-output schema,
producing decisions, action items with owner names and optional due dates, and a
short summary. Resolve owner names against the roster by fuzzy match, leaving
unmatched owners null rather than guessing. Everything is written with status
DRAFT.

## 24. Draft review and confirmation

Goal: Nothing generated by AI is published until the facilitator approves it.

Description: Build the facilitator screen listing extracted decisions and action
items as drafts, each with accept, edit, and reject controls, plus an owner
dropdown for unresolved assignments. Confirming promotes a row from DRAFT to
CONFIRMED; rejecting deletes it. Assumes task 23 produces the drafts.

## 25. Retrospective summary

Goal: A finished retrospective has one readable, shareable page.

Description: Build the summary view showing top discussion topics with vote
counts, key notes, confirmed decisions, confirmed action items, participation
figures from `CycleParticipation`, and the original feedback cards. It is
read-only once the stage is COMPLETE and is visible to every project member.

## 26. Project dashboard

Goal: The project page answers "what do I need to do this week?"

Description: Build the project detail page showing the current cycle with
submission status per member, the active or upcoming retrospective with a link
into the right stage, previous retrospectives, and open action items across all
cycles in the project. Open actions are a live query across retrospectives rather
than copied rows.

## 27. Security hardening

Goal: Production settings are locked down and verified.

Description: Configure Django 6.0's built-in Content Security Policy support via
`SECURE_CSP`, allowing a nonce for the React bundle, and turn on the standard
production settings (HSTS, secure cookies, `SECURE_SSL_REDIRECT`). Get
`manage.py check --deploy` to pass cleanly. Add a test asserting the CSP header
is present on a rendered page.

## 28. Demo data command

Goal: One command produces a realistic project to click through.

Description: Write `manage.py seed_demo` creating a project with several members,
a completed cycle with a mix of attributed and anonymous cards, clusters, votes,
and a finished retrospective with confirmed decisions and actions, plus a second
cycle in COLLECTING. Makes review and screenshots possible without running the
full flow by hand.
