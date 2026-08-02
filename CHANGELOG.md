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

## Unreleased

### Added

- **`subroutine project move` — reparent a project and everything under it.** The endpoint has
  existed since the project tree did; nothing but HTTP could reach it, so reorganising meant
  leaving the command line on a tool whose main surface is one. It counts what will move —
  projects and the items travelling with them — and asks before doing it, because this is the
  one project operation with no undo.

  `--under KEY` or `--root`, and one of them has to be said: an omitted destination once meant
  "move to root", which flattened subtrees by accident.

  Deliberately not an agent tool. Rare, consequential and irreversible is the case where being
  harder to reach is the feature.

- **Rate limiting, which was specified and did nothing.** An instance reachable over a network
  now slows a credential making requests faster than it serves them, and — separately and much
  harder — slows repeated authentication failures from one place. Both return `429` with
  `Retry-After`.

  **Off on a loopback bind**, so a to-do list on your own machine is unaffected and nothing
  about it changes. Set `rate_limit` to say otherwise either way; `rate_limit_per_minute` and
  `rate_limit_failures_per_minute` are the two allowances.

  The counters live in the serving process's memory, so an instance run under something that
  forks workers would enforce a share of the limit per worker. `subroutine serve` runs one.

- **`subroutine doc edit` — revise a document you have already written.** `PATCH
  /v1/documents` had existed since the first release and nothing but HTTP could reach it, so
  a conclusion recorded in an instance could never be corrected there.

  Takes `--title`, `--body`, `--type` and `--status`, or reads the body from a pipe the way
  `doc create` does. With nothing to change it opens the document in `$VISUAL` or `$EDITOR`,
  which is what makes it usable for a document of any length.

- **`subroutine_search` — an agent can search by name.** The capability existed as a `q`
  argument on `subroutine_list`, which meant a model reading tool *names* to decide what it
  could do had no reason to think searching was possible. `q` moves off `list` onto the new
  tool, so there is one name for one thing.

### Fixed

- **A crash is a sentence now, not forty lines of Python.** Anything the program did not
  anticipate printed a boxed traceback with a caret, which is a developer's view of a
  developer's problem. It now says that something went wrong, where the details were written,
  and where to report it.

  The stack is kept rather than thrown away — one file per crash under `crashes/` in the
  state directory, named by the instant — so the report is already on disk when somebody asks
  for it, instead of you having to reproduce the failure with a debug flag set.

  Passwords and tokens in the command line are masked in that file, because it is a file you
  are being asked to send. Failures the program *does* understand are unaffected and keep the
  specific message they already had.

- **`subroutine list` points at `subroutine search` instead of refusing.** `list -q words`,
  `list --search words` and a bare `list words` gave three different errors naming neither
  the search command nor each other — and the one that offered a suggestion offered
  `--strict`. All three now answer `Try: subroutine search "words"`.

- **`subroutine add` now says where the new item landed**, when there is more than one place
  it could have gone. Previously it confirmed with the title alone, so a capture routed to
  another instance — by a `use` context, by a `.subroutine` file, or by `-c` — looked exactly
  like one that went where you meant. `Added: work/acme/#42  Renew the certificate`.

  Nothing changes for a single instance with a single workspace: there is nowhere else for it
  to go, so there is nothing to disambiguate and the confirmation stays as it was.

  Reported by a Claude Code agent whose own bug report went to the wrong instance and who had
  to search both to find out where.

- **Advice printed under `-c` or `-w` now survives the flag.** A tip suggesting a command was
  addressed to the invocation that printed it rather than the one you type next, so
  `subroutine -c work list` could end `Tip: subroutine done 1` — and typing that, without the
  flag, completed a different item on a different instance.

  Suggested commands now carry a full address whenever the current context came from
  something that will not still be true next time. Nothing changes when it came from
  `subroutine use`, a `.subroutine` file or your only connection, which is the ordinary case.

- **A listing under `-c` or `-w` says so.** `A bare number means work/acme.` was true of the
  rows above it and false of the next command; it now reads `A bare number means work/acme
  (from the command line).` when the flags are what decided it.

- **`subroutine connections` marks the connection being written to**, not only the one that
  would be written to if nothing had chosen. Those are different questions — the second is
  the fallback, and `subroutine use`, a `.subroutine` file, `-c` or `SUBROUTINE_CONNECTION`
  all override it — and only the second was ever answered, under the word `default`.

  The row now reads `in use` where your next command goes, `default` for the fallback, and
  both together in the ordinary case where they agree. When they differ it also says why:
  `Writing to work/acme (from 'subroutine use').`

- **An agent session no longer binds to whichever instance `subroutine use` last pointed at.**
  The MCP server reads its connection once, at startup, and holds it for the whole session —
  so with no connection configured it inherited a setting people move between tasks, and two
  sessions started on one machine on one day could reach different instances with nothing to
  say so.

  It now falls back to your `default_connection`, which is set in `config.toml` and can be
  read back with `subroutine connections`. Name a connection in the plugin's settings to
  choose a different one.

- **An agent is told which instances it cannot reach.** The server's instructions named the
  connection it was bound to, which read as though that were the only one — so an agent had
  no reason to ask. Where more than one is configured they now say so; where only one is,
  they are unchanged.

### Security

- **An instance served through a reverse proxy is now rate limited by default.** The default
  was "on unless the bind is loopback", and the deployment these documents recommend is a
  TLS-terminating proxy in front of an application listening on `127.0.0.1` — so the socket
  was loopback, the service was on the public internet, and the limiter was off.

  Setting `public_url` now turns it on whatever the bind, because that setting is you saying
  a proxy serves this to other people. Nothing changes for a laptop with no `public_url`, and
  `rate_limit` still overrides both ways.

  **If you run an instance behind a proxy on a loopback bind, it has not been rate limiting.**
  Setting `public_url` — which `serve` already requires for a non-loopback bind without
  `--insecure` — is enough to fix it.

- **`trusted_proxies` — count the caller behind a reverse proxy, not the proxy.** Failed
  authentications are limited per address, and through a proxy every request arrives from the
  same one, so they shared a single allowance. Name the address your proxy connects from and
  the real caller is counted:

  ```toml
  trusted_proxies = ["127.0.0.1"]
  ```

  **Name only proxies you control.** `X-Forwarded-For` is written by whoever sends the
  request, so this is you vouching for a specific peer; pointed at anything else it would let
  a caller choose which bucket it lands in. Left empty the header is ignored entirely, which
  is the correct behaviour with nothing in front and remains the default.

## 0.2.0 — 2026-08-02

### Added

- **`GET /v1/changes` — what changed while you were away**, across everything you can see, in
  one call and without naming an item. This is the question an agent could not previously ask:
  it could write durably here and could not resume incrementally, so every session either
  re-read the backlog defensively or carried on believing something that had since moved.

  Resume with `?since=<seq>`, using the `seq` of the last event you dealt with. It is
  inclusive, so you will see that one again — send it back rather than the one after, and
  ignore what you already hold.

  `?actor=me` narrows it to what *this credential* did, which is not the same as what its
  owner did: an agent with its own service-account token gets its own work back, not yours.

  **Events under a second old are withheld deliberately.** A sequence number is allocated when
  a change is written and becomes visible when it commits, and those are not the same moment —
  without the delay a fast transaction can commit past a slower one and leave a change behind
  a cursor that has already moved on. Poll more often than that and you will simply see
  nothing new.

  `subroutine changes` reads it from the command line, and an agent has it as a tool. With no
  `--since` you get the most recent page rather than the instance's first afternoon; with one,
  you carry on from where you were. Each event names the item it is about, so a feed reads as
  `#42 Fix the parser` rather than as a list of identifiers.

- New error code `cursor_expired` (410), for a `?since=` older than the events an instance
  still holds — so a client resyncs rather than being handed a page that silently omits
  whatever fell off the end. It cannot occur yet: nothing prunes events, so nothing falls off.

### Fixed

- **`subroutine init` now says what to do when it cannot write where it was told to.** It
  checked the directory the database goes in and never the one the configuration goes in — so
  on PostgreSQL, where there is no database directory to check, nothing was checked at all, and
  a permission problem arrived as a Python traceback. It now names the outermost directory that
  is missing, which is the one you can actually create, rather than the innermost, which you
  cannot.

- **The hosting guide's first run works from a clean machine.** Setting up a service account
  meant creating `/var/lib/subroutine` by hand first, and the guide never said so — systemd
  makes that directory, but not until the service first starts, and the service cannot start
  until `subroutine init` has run. One command, now in the guide where it is needed.

- **A connection you add by hand is no longer reported as having no effect.** `config.toml`
  warns about keys it does not recognise, so that a misspelled setting cannot silently do
  nothing — and it did not know about `[connections.…]`, which is the one thing in that file
  people write by hand. It said the connection was being ignored, immediately before using it.

- **A checkout now records which instance it belongs to, always.** `subroutine use --here`
  wrote the connection into `.subroutine` only if a second one already existed — so every
  marker written before you added a second instance quietly stopped identifying anything, and
  work started in that directory went wherever the machine-wide context happened to point.
  That lands hardest on an agent, which is the one caller that cannot be asked. Existing
  markers are not repaired automatically; `subroutine use --here` rewrites one.

- **A listing across more than one instance says which one a bare number means.** It never
  did — the only clue was which rows printed without a prefix, which is the addressing rule
  read backwards. Nothing was ever ambiguous to the program, but somebody reading fifty rows
  and typing a number off them had no statement of where it would land. Silent when there is
  only one place to be in.

- **A connection with nothing in it keeps its heading in `subroutine list`.** An instance you
  had just set up and not yet used vanished from the listing entirely — no heading, no line,
  and no failure either, since a failure line only appears for a connection that errored. There
  was nothing anywhere in the output separating "reachable and empty" from "not working", and
  the natural reading of a missing group is a missing connection. A single connection still
  prints no heading at all.

- **`subroutine use <connection>` says so, instead of looking for a workspace of that name.**
  It takes `workspace` or `connection/workspace`, and given just the connection it searched the
  wrong instance and reported about the wrong thing — while the connection was sitting in the
  roster it had already loaded. It now names the completion, with the workspace filled in.

- **The connection section names `subroutine connections`**, which is how you check that a
  connection you added is being read — and which stays out of `--help` until a second
  connection exists, so it is invisible for exactly as long as the answer is "it did not work".

- **The hosting guide says how to reach a served instance from your own machine.** Adding a
  connection is two short files and the guide never showed them, while the README sells one
  list across every machine. There is a section for it now — including that the credential you
  need is a token you issue on the server rather than the `secret_key` sitting in your own
  configuration file, which is the only thing there that looks like one.

- **The hosting guide shows how to serve beyond loopback with a proxy on another machine** —
  a router, a NAS, Nginx Proxy Manager. It is two settings and no flag, and the guide had only
  ever demonstrated a public bind as a command-line option, on a page whose whole subject is a
  unit file. `--insecure` is now described as what it is: the case where there is no proxy at
  all.

- **A missing database now says which one it looked for, and why it looked there.** "There is
  no database here yet. Run 'subroutine init' first" was the answer given to a service whose
  PostgreSQL database was populated and running — because the configuration was pointing at a
  SQLite path nobody had chosen, and the message never said so. It names the database now, and
  distinguishes "nothing is set up" from "nothing configured `database_url`". The same
  refusal existed in eight places, six of them identical; there is one now.

- **`init` says when the database it just used is recorded nowhere.** Setting up on PostgreSQL
  means naming the database in the environment for that one run, because `config.toml` does not
  exist yet — and `init` writes only the signing key, so the value goes no further. It cannot
  write the URL for you: a PostgreSQL URL routinely carries a password and that file is the one
  that holds no secrets. It can tell you, and now does.

- **`init` warns before building a second instance when the first one is elsewhere.** Running
  it in a configuration directory that already holds a signing key, while the database it is
  configured for is absent, means the earlier instance is somewhere this configuration does not
  name. It used to carry on in silence — and then `db current` reported a healthy schema and
  the list came back empty, which to the person it happens to looks like their data has gone.

- **The hosting guide has a route for starting on PostgreSQL**, rather than only for switching
  to it later. Four steps, of which writing `database_url` into `config.toml` is the one whose
  absence leaves a service restarting every five seconds.

- **The hosting guide says how to get PostgreSQL**, rather than assuming you already have one.
  Installing the server, and creating the role and database with names and ownership that
  actually let the first migration run — `--owner` in particular, without which it fails on
  `permission denied for schema public` long after the step that caused it.

## 0.1.4 — 2026-08-02

### Fixed

- **If you have neither uv nor pipx, the install instructions now say how to get one.** 0.1.3
  replaced `pip install subroutine` with `uv tool install` and `pipx install`, which is right —
  and left nowhere to go for somebody who has neither, since no distribution ships either tool.
  On a fresh Ubuntu that was three failures in a row. The Install section now names
  `sudo apt install pipx`, `brew install pipx`, and uv's own installer.

### Changed

- A release now creates its GitHub release automatically, with the changelog section as its
  notes and the wheel and sdist attached — so what changed is on the Releases page rather than
  only in a file you have to open.

## 0.1.3 — 2026-08-02

### Fixed

- **The first install line no longer fails on the machine most people have.** `pip install
  subroutine` outside a virtualenv is refused by Debian, Ubuntu and Fedora — PEP 668's
  `externally-managed-environment` — and that was the first command in this README, the first
  on the PyPI page, and the first thing anybody read. It is now `uv tool install subroutine`
  or `pipx install subroutine`: they work on those systems, they put `subroutine` on your
  `PATH` where an editor or an agent can launch it, and pipx is what Debian's own error
  message tells you to use.

  **This corrects the note at the end of 0.1.2 below**, which said `pip install subroutine`
  remained right if you were only going to type commands yourself. That holds inside a
  virtualenv you have activated, and nowhere else — which is not what it implied.

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
