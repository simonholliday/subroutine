# Subroutine

**Project management for people and agents, in equal measure.**

> ⚠️ **Early development.** The specification is settled; the code is being built.
> **The personal to-do list works, the HTTP API works, and an agent can reach it over
> MCP** — quick capture, an agenda, ranking, search, projects, tasks, documents and the links
> between them, comments, per-item histories, scoped tokens, backups, and a `serve` that
> refuses an unsafe bind. What is not built yet is most of what makes it interesting for an agent over *weeks*
> rather than minutes: session handoffs, recorded decisions, verification evidence and claims
> are specified and not written. The specification and the implementation plan are not
> published yet.

## In a hurry

Three things you might want. Pick one; they compose.

**A to-do list on your own machine.** Nothing to configure — `subroutine init` makes the
SQLite database and everything in it.

```console
$ pip install subroutine
$ subroutine init
$ subroutine add "Call the dentist before Sunday"
$ subroutine today
```

**Your coding agent using it for you.** Install the Claude Code plugin and it gets the tools
and the working practice together — it will keep the backlog, record what it did, and adopt
Subroutine into a project you are already working on. **You do not have to learn the CLI**:
ask Claude to add, rank, defer or close things and it will.

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**A shared instance over HTTP**, for a team or for agents on other machines. Loopback by
default; it refuses a wider bind without TLS in front of it.

```console
$ subroutine serve
$ subroutine token create --title "CI"      # a scoped credential for something else
```

The full recipe — systemd, PostgreSQL, TLS, backups — is in
[docs/hosting.md](docs/hosting.md).

New to it? `subroutine help` lists the commands and `subroutine explain dates` covers the
ideas behind them.

---

Every project management tool was built for humans, and has been bolting AI onto the side
ever since. Subroutine starts from the assumption that both kinds of user are here to
stay, and that neither should be a guest in the other's system. Same tasks, same data
model, same API — a person and an agent are just two principals with different strengths.

That means it has to work at both ends of the scale, and it does. It's the thing you use
to note that you need to call the dentist before Sunday. It's also the thing a company
uses to run a programme across six teams, with dependencies, custom workflows and a fleet
of agents working in parallel. Same model underneath, different defaults on top — so your
shopping list never has to look like a Jira ticket.

It also means either can work alone. Take away every agent and you still have a genuinely
good personal to-do list: three commands from install to a working list, and a fourth
to tick something off. Take away
every human and you have a substrate agents can plan, claim and verify work in. Neither is
a degraded mode of the other.

What that is *for* — and this part will keep changing as agents do — is continuity and
accountability. An agent leaves a handoff: what it did, what it verified, what it decided,
and what turned out to be a dead end, so the next session doesn't start cold or re-propose
something you already ruled out. A project can refuse to let a task be closed without
passing evidence attached to it. Every action is attributed, so you can see what happened
while you weren't watching — and an agent's token can be scoped narrower than your own,
because an agent you can't bound isn't one you can trust.

Of that, what works **now** is attribution, scoped tokens, documents linked to the tasks they
came from, a comment thread per item and a history of every change to it. The handoff, the
recorded decision and the evidence gate are written down in full and not yet built; the
warning at the top of this file is the honest boundary.

Free, open source, self-hosted. SQLite by default with no configuration, PostgreSQL when
you outgrow it. Your data stays yours.

---

## Install

```console
$ pip install subroutine
```

Python 3.11+, and that is the whole list. No database to create, no configuration file to
write, no server to start — SQLite is the default, and `subroutine init` makes it.
PostgreSQL when you outgrow it:

```console
$ pip install "subroutine[postgres]"
```

> **Not published yet.** The package lands on PyPI with the first public release; until then
> this is the command that will work, not one that does. The rest of this page is verified
> against a clean install from source.

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

**Every command ends by naming the next one**, so there is nothing to memorise and no manual
to go and find. The tips are dimmed in a terminal and marked `Tip:` everywhere else — because
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
    #7             Pay the gas bill
    work/acme/#12  Fix the deploy script
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

$ subroutine user add ana --role member
  ana is now member in acme

$ subroutine user list --workspace acme
  si   owner
  ana  member  Ana Ruiz
```

There is no password: Subroutine authenticates with tokens, so what Ana needs next is
`subroutine token create --service-account ana` — the flag issues for any account that already
exists, so it works for a person as well as for an agent. Roles belong to a workspace, so
`member` in one is not `member` in another, and the last account able to administer a workspace
cannot be removed from it.

**[docs/hosting.md](docs/hosting.md)** is the whole recipe: the service account, the systemd
unit, nginx and Caddy, when to move off SQLite, giving an agent a token narrower than your own,
backups on a separate volume, and what upgrading actually involves. Every command on that page
has been run, including the refusals.

If you modify Subroutine and serve it to other people, the AGPL entitles them to your changes —
which is what `source_url` in `GET /v1/meta` is for. Internal use and unmodified copies trigger
nothing.

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
