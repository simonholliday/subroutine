# Changelog

Notable changes, newest first. Dates are the date of the release.

**A release that changes the database schema says so at the top of its own section**, and CI
fails if it does not. `scripts/check_release_notes.py` compares the migration head against the
most recent tag rather than trusting anybody to remember, so the notice appears in the
unreleased section on the day the migration lands — written by whoever wrote the migration, not
by whoever cuts the release in a hurry three weeks later. `--emit` prints the wording.

The point of it is that you can *plan* a database upgrade instead of meeting one halfway
through installing something. See [docs/hosting.md](docs/hosting.md#upgrading) for what the
upgrade involves.

## Unreleased — 0.1.0

The first public release. Everything here is new, so this section says what exists rather than
what changed.

No migration notice: there is no previous release to upgrade from, and a fresh install builds
its schema outright with `subroutine init`.

### A personal to-do list

- `subroutine init`, `add`, `today`, `done`, `plan`, `defer`, `list`, `search`, `show`,
  `comment`, `project create`, `project list` and `doc create`. Three commands from install to
  a working list, and a fourth to tick something off.
- Quick capture: `subroutine add "Fix the deploy script by friday !4/2 ~2h #ops"` sets the
  deadline, both priority axes, the estimate and a tag from the line you already type.
- Items are addressed by a number allocated once and never reused, so it goes on meaning that
  item after you have finished a dozen others.
- `subroutine link 42 blocks 43` and `subroutine unlink`. `--ready` reads those links, so
  this is how that filter learns anything.
- `subroutine delete` and `subroutine restore`, with `list --trash` to see what is in there.
  Deleting is soft, so the wrong number costs nothing.
- `subroutine list --ready` shows only work that can actually be started — nothing unfinished
  blocks it and it is not deferred. It is the question a backlog cannot answer.
- `subroutine help` explains the concepts — dates, refs, the capture shorthand — where
  `subroutine --help` lists the commands.

### An HTTP API, and agents as first-class users

- The full REST API under `/v1`, with the same data model and the same permission checks the
  CLI uses. `GET /v1/docs/agent` is the guide an agent should read first.
- Scoped bearer tokens: an agent's credential can be narrower than the person who issued it,
  and may never be wider. Service accounts, per-workspace pins, and per-permission scopes.
- `subroutine mcp` serves the same instance over the Model Context Protocol, in seven tools.
- **A Claude Code plugin**, which wires those tools up and carries a skill describing the
  practice — including how to adopt Subroutine in a project that does not use it yet.
- Attribution on everything, a comment thread per item, and a history of every change.

### Running it

- `subroutine serve` listens on loopback and refuses a wider bind without TLS in front of it.
- SQLite by default with no configuration; PostgreSQL with the `postgres` extra.
- `subroutine upgrade` — reports both schema versions, takes a verified backup, migrates, then
  reads the result back. It does not install software, deliberately.
- Backups to a directory of your choosing, verified where they land, and a restore that makes
  you say whether it is a recovery or a clone.
- Separate instances on one machine with `--profile`, isolated across all three XDG roots.

### Known limits

Session handoffs, recorded decisions, verification evidence and claims are specified and not
built. `GET /v1/changes`, attachments, recurrence, calendar feeds and `db export --format json`
are parsed, honestly refused, and not implemented.
