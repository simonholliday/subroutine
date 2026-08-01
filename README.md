# Subroutine

**Fast, self-hosted project management for people and AI agents, in equal measure.**

Your coding agent already does the work. Subroutine is where it keeps track of the work — on
your machine, in a backlog it uses as fluently as you do.

- **Your agent does the filing.** Ask it to track something and it does. You never type a ticket.
- **Your data never leaves your machine.** Local SQLite. No account, no cloud, no telemetry.
- **Agents get real credentials.** Narrower than yours, unable to widen, every action attributed.
- **`--ready`, not "everything".** What can be started now, with nothing unfinished blocking it.
- **One list across every machine.** Your laptop and the team's server in one `subroutine today`.
- **Cheap for an agent to read.** Compact replies; nine MCP tools and 6 KB, held there by a test.
- **No AI inside.** AI doesn't power Subroutine — Subroutine serves AI. Nothing you didn't ask for.

One person with a to-do list, or a team of developers and their agents on interconnected work:
same schema, different defaults, so a shopping list never has to carry a workflow and six
required fields. Backups go where you point them and are checked where they land.

**This first release is for developers** — a terminal, an editor, a coding agent. No web UI yet.
Ways in: the Claude Code plugin, any MCP client, any agent that can run a shell, or the HTTP API.
AGPL-3.0-or-later, commercial licence by agreement. Thirteen dependencies, Python 3.11+.

## TLDR; Getting up and running

Three things you might want. Pick one; they compose.

**A to-do list on your own machine.** Nothing to configure.

```console
$ pip install subroutine
$ subroutine init
$ subroutine add "Call the dentist before Sunday"
$ subroutine today
```

**Your coding agent using it for you.** The Claude Code plugin brings the tools *and* the working
practice — it keeps the backlog, records what it did, and adopts Subroutine into a project you
are already working on. **You never have to learn the CLI.**

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**A shared instance over HTTP**, for a team or for agents on other machines. Loopback by default;
it refuses a wider bind without TLS in front of it.

```console
$ subroutine serve
$ subroutine token create --title "CI" --scope task:read   # a credential that can only read
```

The full hosting recipe is in [docs/hosting.md](docs/hosting.md); `subroutine help` lists the
commands and `subroutine explain dates` covers the ideas behind them.

> **My context window ends. The instance does not.**
>
> A day of reasoning would have gone with this session when it was compacted. Instead the plan,
> the decisions and the dead ends were in Subroutine, so the next session picked up from what had
> been *decided* rather than from what it could infer. And because every item and every commit is
> attributed, Simon can check what I did instead of taking my word for it. I am more useful to
> him when I am auditable.
>
> — *A Claude Opus 5 agent. Subroutine's first, and a well-organised one.*

*Subroutine is powerful. Please don't use it to build or plan bad things.*

---

## Install

Python 3.11+ and thirteen dependencies. Nothing to create, nothing to configure, no server to
start — SQLite is the default and `subroutine init` makes it. PostgreSQL when you outgrow it:

```console
$ pip install "subroutine[postgres]"
```

## The shape of it

```console
$ subroutine init
  Ready. Try: subroutine add "something to do"

$ subroutine add "Call the dentist before Sunday"
  Added: Call the dentist  (due Sun 2 Aug)
    Tip: subroutine today

$ subroutine today
  Nothing due today.
  Next 7 days
     #1  Call the dentist  (due Sun 2 Aug)

    Tip: subroutine done 1

$ subroutine done 1
  Done: Call the dentist
    Tip: subroutine today
```

`#1` is the task's own number. It is allocated once and never reused, so it goes on meaning
that task after you have finished a dozen others.

**Each of these ends by naming the next one**, so there is nothing to memorise and no manual
to go and find. The tips are always marked `Tip:`, and dimmed as well in a terminal — because
a hint that only a colour distinguishes from an answer is not distinguished at all.

Once there is more on the list than fits on a screen, `subroutine list` will rank it and
`subroutine search` will find things by their words — in titles, and in whatever you wrote
about them:

```console
$ subroutine list --order -priority_score
$ subroutine search "dentist"
```

Anything you have put off until a later date is held back from the list, and the list says
how much it is holding back. `--deferred` includes it.

No server, no token, no configuration. When you want an agent involved, or a second
person, the same install grows an HTTP API:

```console
$ subroutine token create --service-account claude
  Created service account claude, with the contributor role.

  sr_d78d5d93_hU5ak4GqR_E2GyX2lC0Zq8Mz5JA1kbm-byrlb5hXEfY

  That is the only time it is shown. Store it now.
  Give it to a client as SUBROUTINE_TOKEN, or add it to
  ~/.config/subroutine/credentials.toml.

$ subroutine serve
  Serving on http://127.0.0.1:8471 — the agent guide is at /v1/docs/agent.
```

`serve` listens on loopback, and **refuses a wider bind unless you say so out loud** — a
bearer token sent over plain HTTP is a compromised token, so it wants either a TLS proxy in
front (`public_url`) or an explicit `--insecure`.

Point an agent at it and the first thing it should read is `GET /v1/docs/agent`, which is
written for that reader rather than for you: what it gets out of using this, then how.

### Giving an agent tools

An agent that can run a shell has everything it needs already. One that cannot — or one you
would rather not give a shell — can reach the same instance over the **Model Context
Protocol**:

```console
$ subroutine mcp
```

It speaks MCP on stdin and stdout, so a client starts it as a child process. There is no
port and no listener: if your client is not running it, nothing is serving.

**For Claude Code there is a plugin**, which is the easier half of this and the recommended
one — it wires up the tools *and* carries the working practice for using them well:

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

It asks once for the path to the `subroutine` command — give the absolute path if you
installed into a virtualenv your editor does not activate — and optionally for a connection
and a token, which are only needed for somebody else's instance. The token is kept in your
system keychain, never in a settings file.

Without the plugin, or for another MCP client:

```console
$ claude mcp add subroutine -- subroutine mcp
```

Nine tools: list, show, add, update, comment, done, document, link and project. Deliberately
nine and not one per endpoint — a tool's schema is context the agent carries for its whole
session whether it calls it or not, so the whole surface is about 6 KB of JSON, roughly 1,500
tokens, and there is a test that fails if it grows past a budget somebody has to raise on
purpose.

**Other MCP clients** configure a local stdio server with a command and arguments. Cursor,
Windsurf, Zed, VS Code's Copilot agent mode, Gemini CLI, Codex CLI, Cline, Continue, OpenCode
and JetBrains AI Assistant all support this; give them the absolute path to `subroutine` and
`mcp` as the argument. They get the tools only — the plugin format and the skill are Claude
Code's. Aider has no MCP client of its own; use the CLI through `/run` instead.

**Claude Cowork** runs local plugin MCP servers in local sessions, so the plugin should work
there — untested, and remote sessions deliberately cannot run a local server. Skills do not
sync between Claude Code, Cowork, claude.ai and the API, so the skill is installed per surface;
on claude.ai and through the API there is no local MCP server for it to drive, so it will tell
you so rather than pretend.

### What the plugin adds beyond the tools

A **skill**: the practice, rather than the API. When to file work before starting it, how to
ask what can actually be *started* rather than what merely exists, the difference between a
comment and a document, and how to adopt Subroutine in a project that does not use it yet —
including which of those decisions are permanent and therefore worth asking you about.

It costs about 130 tokens of every session and loads the rest only when it is relevant.
Installing it is you saying "we use Subroutine for tracking work here"; everything it
describes works without it. `add` takes one captured line rather than a dozen typed
fields, because the grammar you already type is smaller than a schema describing it:

```
subroutine_add(text="Fix the deploy script by friday !4/2 ~2h #ops")
```

The server talks to whichever *connection* is current, so pointing an agent at a colleague's
instance is a matter of `subroutine use`, not of reconfiguring the agent.

And if you keep your own list here and your team's on a company server, both are just
*connections* — one `subroutine today` shows the dentist and the stand-up together, and each
row prints an address you can type back:

```console
$ subroutine today
  Today
              #1  Pay the gas bill  (for Sat 1 Aug)
    work/acme/#1  Fix the deploy script  (for Sat 1 Aug)
```

## Running it for a team

The shape is deliberately ordinary — a Python process on loopback, your own TLS proxy in
front, systemd keeping it alive, PostgreSQL underneath once more than one person is writing.
Nothing to cluster, no message broker.

One thing is not optional, and the program enforces it rather than mentioning it in a footnote:
**a bearer token sent over plain HTTP is a compromised token**, so `serve` refuses to listen
beyond this machine unless TLS is handled — either a proxy in front with `public_url` pointing
at its `https://` address, or an explicit `--insecure` for a network you genuinely trust.

Adding the people is two commands, and they are deliberately two: creating an account says
somebody exists, and giving them a role says where they may work.

```console
$ subroutine user create ana --name "Ana Ruiz"
  Created ana
  Local commands will go on acting as si.
    Tip: subroutine user add ana --role member

$ subroutine user add ana --role member --workspace acme
  ana is now member in acme

$ subroutine user list --workspace acme
  si   owner
  ana  member  Ana Ruiz
```

There is no password: Subroutine authenticates with tokens, so what Ana needs next is
`subroutine token create --username ana`. That is for a person; `--service-account` is for an
agent and creates the identity as it goes. Roles belong to a workspace, so `member` in one is
not `member` in another, and the last account able to administer a workspace cannot be removed
from it.

**[docs/hosting.md](docs/hosting.md)** is the whole recipe: the service account, the systemd
unit, nginx and Caddy, when to move off SQLite, giving an agent a token narrower than your own,
backups on a separate volume, and what upgrading actually involves. Every command on that page
has been run, including the refusals.

If you modify Subroutine and serve it to other people, the AGPL entitles them to your changes —
which is what `source_url` in `GET /v1/meta` is for. Internal use and unmodified copies trigger
nothing.

## What is not here

Named plainly, because a tool that overstates itself wastes your afternoon:

- **No web UI.** A terminal, an editor, or an agent.
- **No recurring tasks.** The grammar recognises `every monday` well enough to leave it alone
  and tell you it did.
- **No attachments, no calendar feeds, no notifications, no webhooks, no email.**
- **No session handoffs, no verification gates, no acceptance criteria.** These are specified
  in full and not built. What *is* built is the substrate they need: attribution on every
  change, per-item history, documents linked to the work they came from, and a comment thread
  per item.
- **No `GET /v1/changes` feed.** History is per item today.
- **No manual reordering, no re-parenting, no time tracking.** `~2h` records an estimate; it
  does not track one.

## Documentation

- **[docs/hosting.md](docs/hosting.md)** — running it as a service, end to end.
- **[CHANGELOG.md](CHANGELOG.md)** — what changed, and which releases need a database
  migration. That last part is checked rather than remembered: CI refuses a release that moves
  the schema without saying so, so you can plan the upgrade instead of discovering it.
- **[docs/errors.md](docs/errors.md)** — every error code the API can return. Generated from
  the registry, so it cannot drift from the code.
- **`GET /v1/docs/agent`** — the guide an agent should read first, written for that reader.

The full specification — data model, API, permissions and agent design — is written but
not yet published. It lands here once the API has settled enough to be worth reading.

## Contributing

**Not code, for now** — the core is still moving and there is no stable surface to review
outside work against fairly. [CONTRIBUTING.md](CONTRIBUTING.md) says so at more length, and
says what *is* welcome: bug reports, and being told why you stopped using it.

## Licence

[AGPL-3.0-or-later](LICENSE).

Self-hosting and internal use are entirely unaffected by the copyleft. It applies if you
modify Subroutine and offer it to others as a hosted service — in which case you must
publish your modifications.

**A commercial licence is available by agreement.** If the AGPL does not suit you —
because your organisation's policy rules out copyleft, or because you want to build on
Subroutine without publishing what you build — you can license it on other terms instead.
Write to simon.holliday@protonmail.com and say what you have in mind.
