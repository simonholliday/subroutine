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
write, no server to start — SQLite is the default and it is made on first use. PostgreSQL
when you outgrow it:

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
  subroutine today

$ subroutine today
  Nothing due today.
  Next 7 days
     #1  Call the dentist  (due Sun 2 Aug)

  subroutine done 1

$ subroutine done 1
  Done: Call the dentist
  subroutine today
```

`#1` is the task's own number. It is allocated once and never reused, so it goes on meaning
that task after you have finished a dozen others — and every command tells you the next one
to try.

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

For Claude Code, from inside the project:

```console
$ claude mcp add subroutine -- subroutine mcp
```

`subroutine` has to be on the `PATH` the client will use. If you installed into a
virtualenv that your editor does not activate, give the absolute path instead — for example
`~/.venvs/subroutine/bin/subroutine`.

Six tools: list, show, add, update, comment, done. Deliberately six and not one per
endpoint — a tool's schema is context the agent carries for its whole session whether it
calls it or not, so the whole surface is 3,206 bytes of JSON — about 800 tokens — and there
is a test that fails if it grows. `add` takes one captured line rather than a dozen typed
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

**[docs/hosting.md](docs/hosting.md)** is the whole recipe: the service account, the systemd
unit, nginx and Caddy, when to move off SQLite, giving an agent a token narrower than your own,
backups on a separate volume, and what upgrading actually involves. Every command on that page
has been run, including the refusals.

If you modify Subroutine and serve it to other people, the AGPL entitles them to your changes —
which is what `source_url` in `GET /v1/meta` is for. Internal use and unmodified copies trigger
nothing.

## Documentation

- **[docs/hosting.md](docs/hosting.md)** — running it as a service, end to end.
- **[docs/errors.md](docs/errors.md)** — every error code the API can return. Generated from
  the registry, so it cannot drift from the code.
- **`GET /v1/docs/agent`** — the guide an agent should read first, written for that reader.

The full specification — data model, API, permissions and agent design — is written but
not yet published. It lands here once the API has settled enough to be worth reading.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first — it
covers the house style and the one piece of paperwork, a [contributor licence
agreement](CLA.md) that has to be agreed before a pull request can be merged.

## Licence

[AGPL-3.0-or-later](LICENSE).

Self-hosting and internal use are entirely unaffected by the copyleft. It applies if you
modify Subroutine and offer it to others as a hosted service — in which case you must
publish your modifications.

**A commercial licence is available by agreement.** If the AGPL does not suit you —
because your organisation's policy rules out copyleft, or because you want to build on
Subroutine without publishing what you build — you can license it on other terms instead.
Write to simon.holliday@protonmail.com and say what you have in mind.
