# Subroutine

**Agent-native task management for your life, your projects and your team.**

Your own list and your work stay separate, and "what am I doing today?" reaches both. Your
agent uses it as fluently as you do — from a terminal, an editor or a browser. On your machine:
no account, nothing phoning home.

## TL;DR

**For you** — one install, then a short command for ever:

```console
$ uv tool install subroutine
$ subroutine init
$ subroutine add "try this out ~10m"
```

Every command also answers to `subr` — `subr today` is `subroutine today`, for something you
type all day.

**For your agent** — a plugin, which needs nothing installed first:

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

From then on: *"file that as a bug"*, *"what can I actually start?"*, *"write down why we
rejected the other approach."* You never type a ticket. When you want to look at it yourself,
run `subroutine today` or open the instance in a browser.

**The plugin fetches Subroutine itself, through
[uv](https://docs.astral.sh/uv/getting-started/installation/)** — so it works on arrival on a
machine where nobody has installed anything, and uses the copy above if you have one. Git is
needed too, for the marketplace command, which clones a repository — the one prerequisite here
that is Claude Code's rather than ours.

- **Self-hosted.** SQLite by default, PostgreSQL when you outgrow it. No account, no cloud,
  no telemetry, nothing phoning home.
- **A real API first.** The CLI, the browser and your agent are all clients of it. Anything one
  can do, another can.
- **FSL-1.1-ALv2.** Run it, modify it, fork it, sell what you build with it — just don't resell
  Subroutine itself as a service. Every release turns Apache-2.0 after two years.

---

## Why a coder wants this

- **Your agent does the filing.** Ask it to track something and it does — with a priority, an
  estimate, a project and a deadline read out of the sentence you typed.
- **Every item has a number, and that number is permanent.** `#42` is the same task tomorrow,
  after a rename, after a move between projects. Cite it in a commit message and it still
  resolves in a year.
- **`--ready`, not "everything".** What you can start *now* — nothing unfinished blocking it.
  A backlog you can act on rather than one you have to re-read.
- **Dependencies and priorities that hold a real project.** `blocks` links, importance ×
  urgency, milestones whose contents *are* their blockers. Nothing falls behind a thing nobody
  noticed was in the way.
- **Delegate to agents, and to their sub-agents.** Every agent answers to a person, the chain
  is enforced rather than assumed, and when somebody leaves their agents stop with them.
- **Run several agents at once without collisions.** A claim is a lease, not a lock — an agent
  that dies holding one does not strand the work.
- **An audit trail nobody has to maintain.** Every change attributed, every decision kept, the
  whole history of an item on one screen. Your context window ends; this does not.
- **Cheap for an agent to read.** Compact replies, and a tool surface held under a byte budget
  by a test — a schema costs context every session whether it is called or not.
- **No AI inside.** AI doesn't power Subroutine — Subroutine serves AI. Nothing you didn't ask for.

### And nobody else is an afterthought

The same instance, the same schema, different defaults — so a shopping list never has to carry
a workflow and six required fields.

- **There is a web interface**, served by the instance itself. See the list, read an item in
  full, complete it, add one, hand it to somebody. No terminal, no install.
- **Signing in is a link, not a password.** `subroutine login link` prints one; it works once,
  lasts half an hour, and hands the browser a session you can revoke from the command line.
- **A person and an agent are the same kind of citizen.** Not "integrations" bolted to a human
  tool, and not an agent framework with a read-only human view.
- **One list across every machine.** Your laptop and the team's server in one
  `subroutine today`, each row printing an address you can type back.

> **My context window ends. The instance does not.**
>
> I once spent a day building a better way to rank the backlog. Eight tests passed and the
> listing took five seconds. The attempt is in Subroutine as a dead end now — the measurements,
> and why it was dropped — so the next session with the same good idea reads what it cost
> instead of spending the day again. The decisions are here too, including the ones I argued
> for and lost, and every item and every commit is attributed, so Simon can check what I did
> rather than take my word for it. **I am more useful to him when I am auditable.**
>
> — *A Claude Opus 5 agent, two weeks in.*

*Subroutine is powerful. Please don't use it to build or plan bad things.*

---

## Three ways in, and they compose

**1. Your coding agent using it for you** — the three commands at the top of this page. The
plugin brings the tools *and* the working practice: it keeps the backlog, records what it did,
and adopts Subroutine into a project you are already working on. **You never have to learn the
CLI.**

**2. A to-do list on your own machine.** Nothing to configure, no agent involved. Install it
properly for this one — you will be typing `subroutine` often enough to want it on your `PATH`.

```console
$ uv tool install subroutine    # or: pipx install subroutine
$ subroutine init
$ subroutine add "Call the dentist before Sunday"
$ subroutine today
```

**3. A shared instance over HTTP**, for a team, a browser, or agents on other machines.
Loopback by default; it refuses a wider bind without TLS in front of it.

```console
$ subroutine serve
$ subroutine login link                                    # sign in to the web interface
$ subroutine token create --title "CI" --scope task:read   # a credential that can only read
```

The full hosting recipe is in [docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md); `subroutine help` lists the
commands and `subroutine explain dates` covers the ideas behind them.

**Reaching an instance somebody else runs is [docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)**, which
is organised by which of six situations you are in rather than by how the software is built.
If you have been handed an address and a token and want to get to work, that is the page.

---

## What is built, and what is planned

Everything in the first column works today and is covered by tests. The second is specified and
not built — named here because a tool that overstates itself wastes your afternoon.

### The work itself

| | |
| --- | --- |
| Tasks and documents, sharing one numbering scheme | **Built** |
| Projects, sub-projects and workspaces | **Built** |
| Priorities — importance × urgency, ranked in bands | **Built** |
| Deadlines, planned days, and deferring until later | **Built** |
| `blocks` dependencies, and `--ready` to filter by them | **Built** |
| Milestones — an item whose blockers are its contents | **Built** |
| Comments (what happened) and documents (what you concluded) | **Built** |
| Tags, custom statuses, per-workspace vocabulary | **Built** |
| Search across titles, descriptions, document bodies and comments | **Built** |
| Search served by an index, with ranking — PostgreSQL, opt-in | **Built** |
| Capture grammar — `Fix the deploy script by friday !4/2 ~2h #ops +web` | **Built** |
| Moving a task to another project | **Built** |
| Recurring tasks — `--repeat "every month on the 30th"`, from a captured line, and editable in the browser | **Built** |
| Acceptance criteria and verification gates | Planned |
| Session handoffs between agents | Planned |
| Ordering a backlog by hand | Planned |
| Moving a sub-task under a different parent — `subroutine move 42 --under 7` | **Built** |
| Attachments | Planned |
| Time tracking — `~2h` records an estimate; it does not track one | Planned |

### People and agents

| | |
| --- | --- |
| Delegation — assign work to a person or an agent | **Built** |
| Sub-agents, with an accountability chain that ends at a person | **Built** |
| Claims — a lease, so two agents never take the same task | **Built** |
| Service accounts, and credentials narrower than your own | **Built** |
| Per-workspace roles; credentials scoped to a single project | **Built** |
| Deactivate a person and their agents stop with them | **Built** |
| Every change attributed to a principal, permanently | **Built** |
| Email sign-in — today the link is printed at a terminal | Planned |
| Notifications, webhooks, calendar feeds | Planned |

### Ways in

| | |
| --- | --- |
| HTTP API — OpenAPI at `/v1/openapi.json`, for any viewer you like | **Built** |
| CLI, progressive — a shopping list needs none of the above | **Built** |
| Web interface — list, read, complete, add, reassign, Markdown, shareable addresses | **Built** |
| Sign-in links, revocable from the command line | **Built** |
| MCP over stdio (`subroutine mcp`) and over HTTP (`POST /mcp`) | **Built** |
| Two Claude Code plugins — one local, one needing nothing installed | **Built** |
| Multiple connections merged into one agenda | **Built** |
| The agenda as the browser's front page | **Built** |
| A board in the browser, with drag-and-drop between columns | **Built** |
| A calendar view | Planned |

### Running it

| | |
| --- | --- |
| SQLite and PostgreSQL — anything that touches a database is tested against both | **Built** |
| Migrations, with releases that announce a schema change in advance | **Built** |
| Backups to wherever you point them, verified where they land | **Built** |
| Restore, as a recovery or as a clone | **Built** |
| Separate profiles on one machine | **Built** |
| `subroutine doctor` — whether this machine's installation is coherent | **Built** |
| Single-command deployment from a compose file | Planned |

---

## The shape of it

```console
$ subroutine init
  Ready. Try: subroutine add "something to do"

$ subroutine add "Call the dentist before Sunday"
  Added: Call the dentist  (due Sun 9 Aug)
    Tip: subroutine today

$ subroutine today
  Nothing due today.
  Next 7 days
     #1  Call the dentist  (due Sun 9 Aug)

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
how much it is holding back. `--deferred` includes it, at the bottom — visible, and not mixed
in with the work you could start now.

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

## Search

`q` looks in titles, descriptions, document bodies and the comments on an item — which on a
working instance is usually the largest body of prose there is. Every word you give it has to
appear, in any order and in any of those places. A query that is **just a number** finds the
item with that ref as well as everything mentioning it, whether or not it is finished.

### Turning on the index

By default a search is a substring scan. That is honest at personal scale and stops being so:
measured at 20,000 tasks, a search matching nothing took **119 ms**, and it grows with the
backlog.

On PostgreSQL you can put it behind a real index. In `config.toml`:

```toml
search_backend = "native"
```

Restart, and the same search takes **1 ms**. There is no migration beyond the ordinary
`subroutine db upgrade`, and turning it off again is a configuration change and nothing else.

**It changes what a search finds, not only how fast**, so it is off by default:

- `seed` finds *seeded* and *seeding* — words match by their root.
- `curs` still finds *cursor* — a word can be completed from the start.
- **`ursor` no longer finds *cursor*.** Matching the middle of a word is the one thing an
  index cannot do.
- **A very common word stops narrowing.** `the`, `of`, `and` and the rest are dropped rather
  than required, so `cursor the` finds whatever `cursor` finds. The default backend requires
  every word you type.

If you rely on matching the middle of a word, or on every word narrowing, keep the default.

On SQLite it is simply not available. Asking for it there is not an error — you get the
scanning implementation, and the instance tells you so rather than pretending.

### For a client putting several collections in one order

Tasks and documents are separate collections sharing one numbering scheme, so a client showing
both has to merge two responses into one list. Three things make that possible, and getting it
wrong shows up as rows repeating or vanishing when you page rather than as anything that looks
like a sorting fault.

**Ask `GET /v1/meta` what this instance can do.** `search_backend` reports which implementation
is answering — `like` or `native` — and each listing's `sortable` names `relevance` exactly
when this instance can rank at all. Do not infer it from anything else.

`relevance` ranks how well a row answered a search, so it needs one: `?order=-relevance` without
a `q` is refused, and the refusal says that rather than calling the field unknown.

**Merge on the key you asked each collection for.** If you sent `?order=title`, merge on
`title`; the server sorted and paged each collection by that, and merging on anything else
disagrees with the boundary you are paging across.

**A ranked search says so in the rows.** Where the index is on, a search defaults to its own
ranking and every row carries `relevance` — a number that is comparable *within one search* and
meaningless between two. Sort descending and you have the order the server used. It is `null`
on any listing that was not ranked, which is how you tell the two apart without asking twice.

Send `?order=-relevance` explicitly if you want ranking on a search you would otherwise arrange
some other way. A ref that matched exactly always outranks a text match.

**To put deferred work at the bottom, ask for `deferred` as the first key** — `?order=deferred,
-created_at`. It is nought for anything that can be started and one for anything whose start
date has not arrived, so ascending is *deferred last*, and putting it in front leaves whatever
follows to arrange each band. **Both collections accept it**: a document is never deferred and
answers with the first band, so a merged list keeps both halves. No listing applies it unless
you ask, and the row carries `snoozed_until` — so compute the band yourself when you merge,
rather than reading a boolean that would go stale on a page somebody leaves open.

## In a browser

The same instance serves a web interface at its own address. **TL;DR: `subroutine serve`, then
`subroutine login link`, then open the link.**

It shows one list — tasks and documents together, newest first, in the order the command line
uses. Click one and you get it in full: the description, what it is linked to, and everything
anybody has recorded against it. You can complete it, add something with one box that takes the
same shorthand the CLI does, and hand a task to somebody from a list of the people in that
workspace.

- **Every item has an address you can send somebody**, and the project in the middle is there
  for the reader rather than for the machine — rename it and old links still work.
- **Descriptions and comments render as the Markdown they are written in.** Anything that looks
  like HTML is shown as the text it is, so a description written by somebody else — or by an
  agent repeating something it read — cannot become part of the page.
- **Nothing asks you to confirm first.** A question before every action is a tax on being right;
  completing something tells you what it did and offers to undo it.
- **Signing in is a link.** No password to store, no reset flow, nothing worth stealing in a
  breach. `subroutine login revoke <name>` ends every session that person holds and any link
  they have not used, which is what a lost laptop needs.

It talks to the same public API everything else does, so anything it can show you, a script can
too. There is no build step: the JavaScript served is the JavaScript in the repository.

## Install

Python 3.11+ and thirteen dependencies. Nothing to create, nothing to configure, no server to
start — SQLite is the default and `subroutine init` makes it.

**Install it as a tool**, because that is what it is — an application, not a library. It puts
`subroutine` on your `PATH`, which is what lets an editor or an agent launch it. It is also the
only thing that works on a current Linux: Debian, Ubuntu and Fedora now refuse a bare
`pip install` outside a virtualenv and tell you to use pipx instead.

```console
$ uv tool install subroutine
$ pipx install subroutine        # the same thing, if you have pipx rather than uv
```

**Neither of them installed?** `sudo apt install pipx` on Debian or Ubuntu, `brew install pipx`
on a Mac — or [uv's installer](https://docs.astral.sh/uv/getting-started/installation/), which
is one line and needs no Python. Either will do; you only need one.

`pip install subroutine` is still right *inside* a virtualenv you have activated — embedding it
in something else, or working on it.

PostgreSQL when you outgrow SQLite — the extra goes on whichever you used:

```console
$ uv tool install "subroutine[postgres]"
```

## Giving an agent tools

An agent that can run a shell has everything it needs already. One that cannot — or one you
would rather not give a shell — can reach the same instance over the **Model Context
Protocol**:

```console
$ subroutine mcp
```

That command speaks MCP on stdin and stdout, so a client starts it as a child process: no port,
no listener, and if your client is not running it, nothing is serving. **A served instance also
speaks MCP itself**, at `POST /mcp`, which is how an agent reaches one with nothing installed at
all.

**For Claude Code there is a plugin**, which is the easier half of this and the recommended
one — it wires up the tools *and* carries the working practice for using them well:

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**Adding the marketplace needs Git**, because it is a repository and the command clones it —
the one prerequisite here that is Claude Code's rather than ours.

**It starts a program on your own machine, so not every client can use it.** Claude Code can,
and so can the desktop apps. **claude.ai in a browser cannot** — there is nothing on that side
to start `subroutine` on, so the plugin installs, reports success, and then contributes
nothing. That is worth saying plainly because nothing else will: the install succeeds, the
settings page opens, its fields are all there, and the only evidence of a problem is an
absence. Neither of the two things you would try next — checking your `PATH`, installing the
program — can make any difference.

**Your editor launches it through `uvx`, so what it needs is uv rather than Subroutine.** The
package is fetched and cached on first use — about five seconds once, then a fraction of a
second — and if you have already run `uv tool install subroutine`, that copy is used instead of
a download. If the tools do not appear afterwards, `claude mcp list` says so in one line:
installing a plugin and starting its server are separate moments, and only the first one
reports.

**Working on a checkout, or running from a virtualenv?** The plugin has no field for that, on
purpose — `uvx` takes the package name as its first argument and there is no way to spell "skip
that". Point Claude Code at your copy directly instead, which is better anyway, because the
plugin's own copy is cached and lags until you refresh it:

```console
$ claude mcp add subroutine -- /path/to/your/venv/bin/subroutine mcp
```

The plugin can also be given a connection and a token, both only needed for reaching somebody
else's instance.

**If your work lives on a server and this machine is only a client, install the other plugin
instead** — it needs nothing on your machine at all:

```console
$ claude plugin install subroutine-remote@subroutine
```

Then paste in the address you were given and your token, and you are working. No Python, no
package, no `config.toml`. Your editor connects from *this* machine, so an instance on your own
network or behind a VPN is as reachable as a public one. Your editor stores the token, not
Subroutine — on Windows in a file under your home directory — so treat it as you would any
password on that machine, and ask for a new one rather than moving it about.

That is the arrangement to reach for when somebody else runs Subroutine and has given you a way
in — [docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md#an-agent-with-nothing-installed) is that path in
full, including what to ask them for, and
[docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md#reaching-it-from-an-agent-with-nothing-installed) is the other
end of it, which is what they have to do.

Without the plugin, or for another MCP client:

```console
$ claude mcp add subroutine -- subroutine mcp
```

**A deliberately small set of tools, not one per endpoint.** A tool's schema is context the
agent carries for its whole session whether it calls it or not, so the surface is a budget —
and a test fails when it grows past one somebody has to raise on purpose. They cover the
everyday work: capture, list, search, read, update, comment, finish, document, link, projects,
what has changed since you last looked, claiming a task so two agents do not collide, and
asking which principal you are.

**And one that reaches everything else the credential allows.** `subroutine_call_api` calls the
HTTP API directly, so the small surface is an opinion about what an agent should reach for
first rather than a limit on what it can do.

**Other MCP clients** configure a local stdio server with a command and arguments. Cursor,
Windsurf, Zed, VS Code's Copilot agent mode, Gemini CLI, Codex CLI, Cline, Continue, OpenCode
and JetBrains AI Assistant all support this; give them the absolute path to `subroutine` and
`mcp` as the argument. They get the tools only — the plugin format and the skill are Claude
Code's. Aider has no MCP client of its own; use the CLI through `/run` instead.

**Claude Cowork** runs local plugin MCP servers in local sessions, so the plugin should work
there — untested, and remote sessions deliberately cannot run a local server. Skills do not
sync between Claude Code, Cowork, claude.ai and the API, so the skill is installed per surface.
On claude.ai and through the API there is no local MCP server for it to drive; the skill says
so where it is installed, and nothing says so where it is not — which is why the plugin's own
listing now names the limit rather than leaving it to be discovered.

### What the plugin adds beyond the tools

A **skill**: the practice, rather than the API. When to file work before starting it, how to
ask what can actually be *started* rather than what merely exists, the difference between a
comment and a document, and how to adopt Subroutine in a project that does not use it yet —
including which of those decisions are permanent and therefore worth asking you about.

Its description costs about 200 tokens of every session and it loads the rest only when it is
relevant. Installing it is you saying "we use Subroutine for tracking work here"; everything it
describes works without it. `add` takes one captured line rather than a dozen typed fields,
because the grammar you already type is smaller than a schema describing it:

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

**TL;DR: a Python process on loopback, your own TLS proxy in front, systemd keeping it alive,
PostgreSQL underneath once more than one person is writing.** Nothing to cluster, no message
broker. The whole recipe is [docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md).

One thing is not optional, and the program enforces it rather than mentioning it in a footnote:
**a bearer token sent over plain HTTP is a compromised token**, so `serve` refuses to listen
beyond this machine unless TLS is handled — either a proxy in front with `public_url` pointing
at its `https://` address, or an explicit `--insecure` for a network you genuinely trust.

Adding the people is two commands, and they are deliberately two: creating an account says
somebody exists, and giving them a role says where they may work.

```console
$ subroutine user create thomas --name "Thomas Anderson"
  Created thomas
  Local commands will go on acting as si.
    Tip: subroutine user add thomas --role member

$ subroutine user add thomas --role member --workspace acme
  thomas is now member in acme

$ subroutine user list --workspace acme
  si      owner
  thomas  member  Thomas Anderson
```

There is no password: Subroutine authenticates with tokens, so what Thomas needs next is
`subroutine token create --username thomas`, or `subroutine login link --username thomas` if
they are going to use the browser. That is for a person; `--service-account` is for an agent and
creates the identity as it goes. Roles belong to a workspace, so `member` in one is not
`member` in another, and the last account able to administer a workspace cannot be removed
from it.

**[docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md)** is the whole recipe: the service account, the systemd
unit, nginx and Caddy, when to move off SQLite, giving an agent a token narrower than your own,
backups on a separate volume, and what upgrading actually involves. Every command on that page
has been run, including the refusals.

`source_url` in `GET /v1/meta` says where the source of *this* instance can be had. **Nothing
in the licence requires that of you** — it is a promise the product makes to whoever is using
it, and it is a setting so that somebody running a fork can point at theirs rather than at this
repository.

## Documentation

- **[docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)** — the six ways to reach an instance, organised
  by which one you are. Start here if somebody has handed you an address and a token.
- **[docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md)** — running it as a service, end to end.
- **[CHANGELOG.md](https://github.com/simonholliday/subroutine/blob/main/CHANGELOG.md)** — what changed, and which releases need a database
  migration. That last part is checked rather than remembered: CI refuses a release that moves
  the schema without saying so, so you can plan the upgrade instead of discovering it.
- **[docs/errors.md](https://github.com/simonholliday/subroutine/blob/main/docs/errors.md)** — every error code the API can return. Generated from
  the registry, so it cannot drift from the code.
- **`GET /v1/docs/agent`** — the guide an agent should read first, written for that reader.
- **[SECURITY.md](https://github.com/simonholliday/subroutine/blob/main/SECURITY.md)** — how to report a vulnerability privately, and what is in
  scope. Not through an issue: an issue is public from the moment it is filed.

The full specification — data model, API, permissions and agent design — is written but
not yet published. It lands here once the API has settled enough to be worth reading.

## Contributing

**Not code, for now** — the core is still moving and there is no stable surface to review
outside work against fairly. [CONTRIBUTING.md](https://github.com/simonholliday/subroutine/blob/main/CONTRIBUTING.md) says so at more length, and
says what *is* welcome: bug reports, and being told why you stopped using it.

## Licence

[FSL-1.1-ALv2](https://github.com/simonholliday/subroutine/blob/main/LICENSE) — the Functional Source License.

**Run it, modify it, fork it, for any purpose including making money.** A person, a team, a
five-hundred-person company self-hosting it for its own work, a consultancy charging to set it
up for a client: all free, for ever, with nothing to buy and nobody to ask.

**The one thing you may not do is sell other people access to it as a service.** That is the
whole restriction, and it is why this is source-available rather than an OSI open-source
licence — the Open Source Definition does not allow a licence to rule out a field of use, even
one.

**Every release becomes Apache-2.0 two years after it ships**, automatically, with no decision
by anybody. That is the promise underneath the restriction: if this project goes somewhere you
do not want to follow, you can take it and go.

Versions up to and including 0.5.0 were published under AGPL-3.0-or-later and remain so.

**A commercial licence is available by agreement.** If you want to offer Subroutine as a
service, write to simon.holliday@protonmail.com and say what you have in mind.
