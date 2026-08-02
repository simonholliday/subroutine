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

## 0.1.2 — 2026-08-02

No migration notice: the schema head has not moved since 0.1.0. Nothing in the package itself
changed — this release is the plugin and the documentation around installing it.

### Fixed

- Installing the Claude Code plugin now gives you working tools without your having to supply
  a path. Your editor launches `subroutine` itself, so it has to be on your `PATH` — and
  `pip install` into a virtualenv satisfies "installed" while leaving every tool missing, with
  no error at the point of the mistake. The plugin route now tells you to install it as a
  tool, with `uv tool install subroutine` or `pipx install subroutine`, which is what puts the
  command where something other than your shell can find it.
- The plugin's skill sent that failure the wrong way. It could not tell "not installed" from
  "installed where the editor cannot see it" — both look the same from inside a session — so it
  told people who had already installed it to install it again. It now names `claude mcp list`,
  which is the one command that separates them.

`pip install subroutine` remains exactly right if you are only going to type commands
yourself.

## 0.1.1 — 2026-08-02

No migration notice: the schema head has not moved since 0.1.0.

### Fixed

- An agent working in a checkout marked for a different instance could not file anything.
  Committing a `.subroutine` file is how a team says which project a repository belongs to, so
  a colleague who cloned the repository and ran `subroutine init` had a marker naming a project
  that was not on their own instance — and `subroutine_add` refused every call rather than
  ignoring it, while the CLI beside it carried on. Reads were never affected. A renamed project
  is now also followed by its id over the Model Context Protocol, as it already was on the
  command line.

### Changed

- The version is derived from the git tag rather than written into `pyproject.toml`, so what
  the package reports and what was released cannot drift apart.

## 0.1.0 — 2026-08-01

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
- `subroutine start` and `subroutine stop`, so the list can say what you are in the middle of.
- `subroutine link 42 blocks 43` and `subroutine unlink`. `--ready` reads those links, so
  this is how that filter learns anything.
- `subroutine delete` and `subroutine restore`, with `list --trash` to see what is in there.
  Deleting is soft, so the wrong number costs nothing.
- `subroutine list --ready` shows only work that can actually be started — nothing unfinished
  blocks it and it is not deferred. It is the question a backlog cannot answer.
- `subroutine help` and `subroutine --help` do the same thing: they list the commands.
  `subroutine explain` covers the ideas behind them — dates, refs, the capture shorthand.

### An HTTP API, and agents as first-class users

- The HTTP API under `/v1`, with the same data model and the same permission checks the
  CLI uses. `GET /v1/docs/agent` is the guide an agent should read first.
- Scoped bearer tokens: an agent's credential can be narrower than the person who issued it,
  and may never be wider. Per-workspace pins and per-permission scopes.
- `subroutine token create --username ana` issues for a person, `--service-account claude` for
  a machine identity, creating it as it goes. Two flags because they are two decisions, and
  neither will issue a credential for an account that could not use it.
- `subroutine mcp` serves the same instance over the Model Context Protocol, in nine tools —
  including `link` and `project`, so an agent can say what blocks what and file its own work.
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

Session handoffs, verification evidence and claims are specified and not built — as are
attachments, calendar feeds, recurring tasks, a `GET /v1/changes` feed, and manual reordering.
There is no web UI.

Two of those are *refused out loud* rather than merely absent, which is worth telling apart:
the capture grammar recognises `every monday` well enough to leave it in your title and say it
did nothing with it, and a calendar credential presented to the API is turned down by name.
The rest are simply not there yet.
