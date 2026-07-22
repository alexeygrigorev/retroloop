- Tasks are GitHub issues
- Commit regularly

Labels

- `mvp` - needed for the MVP defined in `_docs/outdated/plan.md`
- `post-mvp` - real work, deliberately not now
- Every issue carries exactly one of the two, new ones included

Background

- `_docs/decisions.md` - the calls already made, with reasons. Read it before
  grooming or implementing, and do not reopen a decision without changing it
  there first
- `_docs/outdated/` holds the plan, architecture, and original task list. They
  are reference, not the backlog - where they disagree with `decisions.md` or
  an issue, they lose

Roles

- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md


Orchestrator

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself.

The orchestrator owns three things the subagents cannot see: the
dependency order of the backlog, the worktrees, and the merge queue.


Working in parallel

Work runs in waves. A wave is a set of issues that can be built at the
same time without waiting on each other.

- Up to 5 agents run at once
- Every issue in a wave gets its own git worktree and its own branch
- Nothing is implemented in the main checkout. Main is for grooming,
  integration and the docs

An issue may enter a wave only when all of these hold:

- Every issue it depends on is closed and merged into main
- No other issue in the same wave adds migrations to the same Django app
- The orchestrator has read its Constraints section and knows which
  shared files it will touch

Everything else waits for the next wave. A wave is often smaller than 5
because the backlog runs out of independent work, not because the limit
was reached - that is normal, do not pad a wave to fill it.


Worktrees

One issue, one worktree, one branch:

    git worktree add ../wt/<issue> -b issue-<issue> main

Each worktree is a full checkout and needs its own setup before an agent
touches it:

- `uv sync` - the worktree has its own `.venv`
- `.env` copied from the main checkout, with `DATABASE_URL` pointed at a
  database of its own, `feedback_wt<issue>`
- `CREATE DATABASE feedback_wt<issue>` inside the Postgres container. If
  the name is already taken, pick a fresh one rather than dropping it

The database part is not optional. `.env` is git-ignored and
`config/settings_test.py` reads it, so each worktree gets both its own
development database and its own `test_*` database. Two worktrees
sharing one `DATABASE_URL` will drop each other's test database in the
middle of a run, and the failures look like impossible bugs in the code
rather than what they are.

There is a catch worth knowing about. A real environment variable beats
the `.env` loader, by design - that is what lets containers and CI ship
no `.env` at all. So if the terminal that launched the session exports
`DATABASE_URL`, as this one does, it silently shadows every worktree's
`.env` and puts all of them back on one database.

Two things guard against that:

- Commands are run with the database named explicitly:
  `DATABASE_URL=postgres://postgres:postgres@localhost:5432/feedback_wt<issue> uv run pytest`
- Each worktree's `.venv` carries an untracked `sitecustomize.py` that
  reads that worktree's `.env` and pins `DATABASE_URL` before Django
  starts. Python imports it automatically, so a forgotten prefix costs
  nothing. It stands down inside a container, where Compose sets
  `DATABASE_URL` on purpose and the checkout's `.env` names `localhost`,
  which no container can reach

The setup is not complete until `uv run python -c "import
config.settings_test as s; print(s.DATABASES['default']['NAME'])"`
prints the worktree's own database. Check it before an agent starts, not
after it reports a mysterious failure.

Postgres itself stays a single container. Databases inside it are cheap;
a second container is not.

A merged worktree is left where it is. Reuse it for the next issue that
lands in the same area, or leave it alone. A stale checkout and an idle
database cost nothing next to a stalled run - see below for why nobody
deletes them mid-session.


Destructive commands stall the run

The harness checks commands that destroy things and asks the person
running the session to approve them. That is the right behaviour, but it
means the work stops dead until someone is at the keyboard. A wave of
five agents can sit idle overnight on one `rm`.

So nothing in this process deletes. Not worktrees, not branches, not
databases, not temporary files.

- Restore a file you changed on purpose with `git checkout -- <path>` or
  `git restore <path>`, never by copying it aside and deleting the copy
- Write temporary files to the session scratchpad, which is outside the
  repository and needs no cleanup, never to `/tmp` and never next to the
  code
- Leave worktrees, branches and `feedback_wt<issue>` databases in place
  when an issue closes. They are a few megabytes and a row in
  `pg_database`
- Recreate a database with `CREATE DATABASE` on a fresh name rather than
  dropping and remaking the old one

If something genuinely has to be removed, that is the user's call. Say
what should go and why, and let them run it. Do not put a deletion in
front of an agent and hope it goes through.


Integration

Branches merge one at a time, never in parallel, in dependency order:

1. Rebase the branch on current main
2. Run the whole suite, the linter, and `makemigrations --check` again,
   in the worktree, after the rebase
3. Merge to main only if all three are clean
4. Close the issue
5. Rebase every still-open branch in the wave onto the new main

Step 5 is what keeps the wave honest. The second branch to merge is
being tested against code its author never saw, so it re-runs against
the merged result before it is trusted.

Conflicts concentrate in a few shared files - `config/settings.py`,
`config/urls.py`, `AGENTS.md`, `.env.example`, `templates/base.html`.
The orchestrator resolves them at integration. An engineer who finds a
conflict is looking at a stale branch and should rebase, not merge main
into their branch.

A rebase that breaks the branch goes back to that branch's engineer with
the failure, as a FAIL. The orchestrator does not fix it.


Lifecycle

1. Pick the next wave: open issues whose dependencies are all merged
2. PM grooms each ungroomed issue in the wave
3. Set up a worktree per issue, then launch one engineer per issue, in
   parallel
4. QA verifies each one in its own worktree, in parallel, as its
   engineer finishes - QA does not wait for the whole wave
5. On FAIL, back to step 3 for that issue alone, with the QA comment as
   input. The rest of the wave carries on
6. On PASS, integrate that branch through the merge queue and close the
   issue
7. Leave the worktree and its database in place
8. Repeat until the backlog is empty

Rules

- One issue per worktree, one engineer per issue
- Do not skip step 2, even when the task looks obvious
- The engineer does not close the issue, QA does not fix the code
- Do not commit until the tests pass
- An agent stays inside its own worktree. Reading main is fine, writing
  to it or to another worktree is not
- Only the orchestrator merges, closes issues, and deletes worktrees
