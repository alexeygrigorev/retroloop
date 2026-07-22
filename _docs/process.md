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
- `createdb feedback_wt<issue>` inside the Postgres container

The database part is not optional. `.env` is git-ignored and
`config/settings_test.py` reads it, so each worktree gets both its own
development database and its own `test_*` database. Two worktrees
sharing one `DATABASE_URL` will drop each other's test database in the
middle of a run, and the failures look like impossible bugs in the code
rather than what they are.

Postgres itself stays a single container. Databases inside it are cheap;
a second container is not.

When an issue is merged and closed, remove its worktree and drop its
database:

    git worktree remove ../wt/<issue>
    git branch -d issue-<issue>


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
7. Tear down the worktree and its database
8. Repeat until the backlog is empty

Rules

- One issue per worktree, one engineer per issue
- Do not skip step 2, even when the task looks obvious
- The engineer does not close the issue, QA does not fix the code
- Do not commit until the tests pass
- An agent stays inside its own worktree. Reading main is fine, writing
  to it or to another worktree is not
- Only the orchestrator merges, closes issues, and deletes worktrees
