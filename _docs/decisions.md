# Decisions

Calls made while grooming the backlog. The architecture doc left six questions
open and grooming turned up two more. They are settled here so issues stop
re-litigating them.

Where this file and `_docs/outdated/` disagree, this file wins.

## 1. Cards are locked at reveal

Members edit and delete their own cards only while the cycle is `COLLECTING`.
Once the retrospective advances to `REVEAL` the cards are frozen for everyone.

Why: the plan says contributors edit *before* the retrospective, and after
reveal anonymous cards have no author, so there is no one to authorize an edit
against. A card the team has already clustered and voted on should not change
underneath them.

## 2. Votes are reassignable until voting closes

While the stage is `VOTE` a member may withdraw and re-place their three votes
freely. When the stage advances the allocation is final.

Why: totals are hidden during voting, so changing a vote leaks nothing and
tells no one anything. Locking the first click would make people vote timidly.

## 3. Destroying anonymous authorship is irreversible, and that is the point

At `REVEAL`, `Card.author` is set to NULL for anonymous cards. There is no
archive, no audit table, no admin override. No later feature may reintroduce
one.

Why: "anonymous" has to mean the same thing to the person typing as it does to
the database. A recoverable link is not anonymity, it is a delay.

Cost accepted: participation is countable but never attributable, and abuse of
anonymity cannot be traced. For an internal team tool that is the right trade.

## 3a. Participation is reported as a yes or no, not as a count per person

`CycleParticipation.card_count` is stored, because it is useful for aggregates,
but no screen shows one member's count next to their name. The summary shows
who submitted and who did not, plus team-wide totals.

Why: counts leak by arithmetic. In a team of six where one person submitted a
single card and exactly one anonymous card exists, the count identifies the
author as surely as a name would. Decision 3 is only worth anything if the
numbers around it cannot undo it.

## 4. A cycle closes on the facilitator's schedule, not on full attendance

The facilitator can close a cycle and start the retrospective whenever they
choose. Non-submitters do not block it and are visible as "did not submit" on
the participation view.

Why: waiting for the last person means the retro is hostage to whoever is on
holiday. Visibility is enough pressure.

## 5. Open action items are a live query, never copied rows

The project dashboard's "open action items" is a query across every
retrospective in the project for `status=OPEN`. Actions are not duplicated,
rolled over, or re-created in a new cycle.

Why: a copy is a second source of truth that drifts. An action belongs to the
retrospective where it was agreed, and is shown wherever it is useful.

## 6. A failed transcription means re-uploading the file

The recording is deleted in a `finally` block whether transcription succeeded
or not. There is no retry button, because there is nothing left to retry
against. The failure message says so in words.

Why: keeping media "just for 24 hours in case" reintroduces the retention
policy, the storage config, and the backup question that discarding it removed.
Re-uploading is a rare, cheap inconvenience.

## 7. The project owns its user model

`accounts.User` subclasses `AbstractUser` and adds `display_name`.
`AUTH_USER_MODEL` points at it from the first migration.

Why: the architecture doc says `django.contrib.auth.models.User`, but that
model has no display name and every screen shows one. Swapping the user model
after other tables carry FKs to it is a data migration on live data; doing it
in task #4 costs one line. The inherited `email` field stays unused and is
never rendered — see #8 in this file.

Supersedes: `_docs/outdated/architecture.md`, "Identity and membership".

## 8. No email, permanently

No mail backend, no `EMAIL_*` settings, no verification, no self-serve password
reset. An admin resets a password with `manage.py changepassword`.

Why: it removes a whole class of infrastructure and failure modes from an
internal tool. This is a product decision, not deferred work — there is no
follow-up issue for it, and there should not be one.

## 9. A card's public identifier is not its primary key

`Card.pk` does not leave the server. It appears in no response body, in no JSON
embedded in a page, and in no request the server accepts. A card is addressed
publicly by `Card.public_id`: a random UUID4, written when the card is created,
unique and never reused. The board payload's `cards[].id` is that value, and
#12's mutation endpoints take it and refuse a bare integer. #73 adds the column
and lands before #12.

Why: `Card.pk` comes from a table-wide sequence, so sorting one cycle's ids
recovers submission order — the exact ordering #10's shuffle exists to destroy,
with a `SystemRandom` chosen so that no seed anywhere in the process can
reproduce it. Serializing the sequence hands that ordering back through
devtools, on a page every member of the project is entitled to open. A defence
that costs a database connection to break is a trade; one that costs a keystroke
is not a defence.

Random, and assigned at creation: a counter allocated in submission order is the
same leak wearing a different type, which also rules out a time-ordered UUID (v1,
v6, v7). Assigning at reveal instead would leave a card with no handle during the
week it is being written and edited, and would change a card's identity
underneath the board at the reveal.

Cost accepted: one UUID column, one unique index, and one migration on a table
that will hold thousands of rows, not millions. Paid now because #12 mutates by
this handle and #14 keys React components by it; once those exist, the same
change costs the migration plus a request-shape change in two more places.

Deliberate exception, decided rather than deferred: the pre-reveal own-card URLs
from #8 — `card-show`, `card-edit`, `card-delete` — keep the integer pk. Every
card they address is one the viewer wrote, on a screen that shows nobody else's,
and item 1 freezes those cards at reveal. The only ordering they expose is the
viewer's own submission order, which they already know. There is no follow-up
issue to convert them and there should not be one. New surfaces get no such
exception: anything that renders, returns or accepts a card from here on uses
`public_id`.

Scope: this is about `Card`. `Project`, `FeedbackCycle`, `Retrospective` and the
`Cluster` #12 adds keep their integer pks in URLs and payloads — their creation
order is not a fact about a person, and clustering happens in front of the whole
team as it is done.

This does not touch item 3a. The other re-identification route on #69 —
`CycleParticipation.card_count`, plus day-truncated `submitted_at`, plus the
`Card.created_at` that survives the reveal — is untouched and still needs the
owner's call. This decision is worth making either way that one goes: if that
route is accepted as a stated limit, this one is still the cheaper attack,
because it needs no database access; if it is closed, an id sequence in a payload
would become the shortest way back to the same ordering.
