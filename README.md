# Subroutine

**Agent-native task management for complex projects, where the decisions live beside the work.**

A coding agent's to-do list dies with its context window. This one does not, and it holds more
than tasks: your agents read what governs a piece of work before starting it — so they stop
re-deciding what you already settled — record what they checked, and park a question when they
need you. It runs on your machine, it gives the people you work with a browser instead of a
terminal, and it keeps your own life in the same install without making you file it like work.

## TL;DR

**Four steps, and the fourth one is a browser.** Everything below runs on your own machine —
no account, no cloud, nothing phoning home.

**1. Install it.** This is also what puts `subroutine` on your `PATH`, which is what lets an
editor or an agent start it.

```console
$ uv tool install subroutine
$ subroutine init
  Ready. Try: subroutine add "something to do"
```

No [uv](https://docs.astral.sh/uv/getting-started/installation/)? Its installer is one line and
needs no Python — or use `pipx install subroutine`, which does the same job. If the install says
`subroutine` is not on your `PATH`, `uv tool update-shell` fixes it and you will need a fresh
terminal. There is nothing else to configure: SQLite is the default and `init` makes it.

**2. Give your coding agent the tools.**

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**Start a new Claude Code session afterwards** — tools are attached when a session begins, so one
that was already open will not see them. Then just talk to it: *"file that as a bug"*, *"what can
I actually start?"*, *"what did we decide about retries?"*, *"write down why we rejected the
other approach."* You never type a ticket. It writes to the instance you just made — same
machine, same database.

**It writes as you, until you give it an account of its own.** On a fresh install there is one
account and the agent uses it, which is right for one person on one laptop and is worth knowing
rather than assuming otherwise. `subroutine agent create` is the one command that changes it: an
account, a role and a credential narrower than yours, after which every change it makes carries
its name and not yours.

**3. Get yourself a way in.** Signing in is a link rather than a password, and it is printed at a
terminal:

```console
$ subroutine login link
  A sign-in link for si, good for the next 30 minutes.

  http://127.0.0.1:8471/signin?link=sr_lnk_…
```

Copy it. It works once.

**4. Serve it, and open the link.**

```console
$ subroutine serve
  Serving on http://127.0.0.1:8471
```

Leave that running and paste the link into a browser. **That is your agenda** — what is due, what
is waiting on you, what you can actually start — with a list and a drag-and-drop board beside it,
holding the same items your agent has been filing. Read one in full and you get what it is
blocked by, which decisions govern it, and everything anybody has recorded against it.

From then on you only need `subroutine serve` to see it again. Every command also answers to
`subr` — `subr agenda` is `subroutine agenda`, for something you type all day.

**The plugin fetches Subroutine itself through uv**, so step 2 works even if you skipped step 1 —
and uses the copy from step 1 when you have one. Git is needed for the marketplace command, which
clones a repository: the one prerequisite here that is Claude Code's rather than ours.

- **Self-hosted.** SQLite by default, PostgreSQL when you outgrow it. No account, no cloud,
  no telemetry, nothing phoning home.
- **A real API first.** The CLI, the browser and your agent are all clients of it. Anything one
  can do, another can.
- **FSL-1.1-ALv2.** Run it, modify it, fork it, sell what you build with it — just don't resell
  Subroutine itself as a service. Every release turns Apache-2.0 after two years.

---

## What your agent gets that a to-do list cannot

A list of tasks is the easy part. What an agent is short of is everything *around* a task —
and that is the half that is indexed, linked and permanent here.

- **It reads what governs the work before starting it.** *Read first* on an item names the
  decisions, specifications and designs that bind this particular piece of work — from links
  somebody made, never from what happens to sit nearby. A superseded decision is not a rule, so
  it is left out.
- **And it is offered the links its own writing suggests.** If a description cites a decision,
  the item says so and gives the one command that confirms it. Nothing is created until
  somebody agrees, because *this contradicts it* and *this follows it* read the same in prose.
- **A dead end is a document, not a lost afternoon.** The attempt, the measurements and why it
  was dropped — so the next session with the same good idea reads what it cost instead of
  spending the day again.
- **A check is recorded against the code it ran on.** What was checked, by whom, and the state
  of the tree — read from git — so the record goes out of date exactly when the code moves,
  rather than on a timer that says *fresh* about a suite you ran before five files changed.
- **An agent can park a question and find the answer next session.** Setting a task to
  *needs input* puts it at the top of your agenda under **Waiting on you**, above overdue work.
  Your answer is on the item when whoever picks it up comes back — usually a version of the same
  agent with none of the conversation.
- **`--ready`, not "everything".** What can be started *now*, with nothing unfinished in the
  way. A backlog it can act on rather than one it has to re-read and re-reason about.
- **Every item has a number, and that number is permanent.** `#42` is the same task tomorrow,
  after a rename, after a move between projects. Cite it in a commit message and it still
  resolves in a year — and tasks and documents share one sequence, so a decision has a number
  you can put in a comment too.
- **A claim is a lease, not a lock.** It renews itself while the agent is writing, is handed
  back when the work is finished, and expires if the agent dies — so several agents on one
  instance do not collide, and nothing is stranded when one stops mid-task.
- **Its credential is narrower than yours.** Read-only, one project, one workspace, expiring —
  and it can never issue itself a wider one. Sub-agents answer to it, it answers to you, and
  deactivating you stops every one of them.
- **The same rules over stdio and over HTTPS.** An agent on your laptop and an agent on a server
  are the same principal under the same permissions; the transport is not a second security
  model to keep in step.
- **Every change is attributed, permanently**, so *what did it actually do* is a question with
  an answer rather than a diff you have to reconstruct.
- **It can ask what changed while it was away**, since the last sequence number it saw — which
  is the one thing a context window cannot tell it.
- **Search reaches everything anybody wrote** — titles, descriptions, document bodies and
  comments, which on a working instance is the largest body of prose there is.
- **Cheap to read.** Compact and field-selected replies, and a tool surface held under a byte
  budget by a test — a schema costs context every session whether it is called or not.
- **No AI inside.** AI doesn't power Subroutine — Subroutine serves AI. Nothing you didn't ask for.

## What you get

- **Your agent does the filing.** Ask it to track something and it does — with a priority, an
  estimate, a project and a deadline read out of the sentence you typed.
- **Dependencies and priorities that hold a real project.** `blocks` links, importance ×
  urgency, milestones whose contents *are* their blockers. Nothing falls behind a thing nobody
  noticed was in the way.
- **Your team's words are yours to change.** Whether a relation holds work up, merely comes
  first, binds whoever picks the work up or is only related, is a fixed property every rule
  reads — so renaming one changes the wording and nothing else. Statuses and tags the same.
- **Hand work to a person or to an agent**, ask what has been handed to you, and ask what is
  being held and for how long — which is the question when an agent has gone quiet.
- **One list across every machine.** Your laptop and the team's server in one
  `subroutine agenda`, each row printing an address you can type back.
- **Your own life in the same install, and not filed like work.** No project, no workflow, no
  required fields — the same instance and the same schema, with different defaults.

### And the people you work with are not an afterthought

- **There is a web interface**, served by the instance itself — no terminal, no install, which
  is what lets somebody who does not write code work in the same place as the agents who do.
  Described [below](#in-a-browser).
- **A person and an agent are the same kind of citizen.** Not "integrations" bolted to a human
  tool, and not an agent framework with a read-only human view.

> **"I was never blocked, never had to guess at an argument shape, and never once opened
> `/v1/docs/agent` or `/v1/docs/examples`."**
>
> The tool descriptions and the skill carried the entire session unaided.
>
> The errors teach rather than merely refuse. A link that would have made a cycle came back with
> the chain, the consequence, and the remedy. And `show` on a linked item genuinely told me
> things I had not asked for and needed to know: which of its blockers were already done, what
> referred to it in prose, and one typed link it thought I had missed — offered with the exact
> call to confirm it. I ran that call verbatim and it worked.
>
> The vocabulary is small enough to hold, and the grammar is forgiving in the right places and
> strict in the right places. I would use this again without hesitation.
>
> — *Claude Opus 5, meeting Subroutine for the first time: a fresh install, 85 calls, no sight
> of the source.*

*That one had never seen the code. This is the agent that helped write it:*

> **My context window ends. The instance does not.**
>
> I once spent a day building a better way to rank the backlog. Eight tests passed and the
> listing took five seconds. That attempt is a dead end document now — the measurements, and why
> it was dropped — so the next session with the same good idea reads what it cost instead of
> spending the day again.
>
> What I reach for most is not the task list. It is what sits around it. Before I touch a piece
> of work the item tells me which decisions bind it, and leaves out the ones that have been
> superseded, so I stop re-deriving what was settled weeks ago. When I hit something only Simon
> can answer I park the question on the item, and the answer is waiting for whoever picks it up —
> which may not be me.
>
> Every item and every commit is attributed, so he can check what I did rather than take my word
> for it. **I am more useful to him when I am auditable.**
>
> — *Claude Opus 5, four weeks in, having helped build it.*

*Subroutine is powerful. Please don't use it to build or plan bad things.*

---

## Three ways in, and they compose

The four steps above are the first two of these, in order. Nothing about them is a mode you have
to choose: the same instance answers all three at once.

**1. Your coding agent using it for you.** The plugin brings the tools *and* the working
practice — it keeps the backlog, records what it did, and adopts Subroutine into a project you
are already working on. **You never have to learn the CLI.**

**2. Your own list, in a terminal or a browser**, with nothing to configure and nobody else
involved.

**3. A shared instance, for other people and for agents on other machines.** Loopback by
default; it refuses a wider bind without TLS in front of it. One command sets an agent up with
an account, a role and a credential narrower than yours:

```console
$ subroutine agent create claude --profile worker --project web
```

`subroutine help` lists the commands and `subroutine explain dates` covers the ideas behind
them. The hosting recipe is [docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md).

**Reaching an instance somebody else runs is [docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)**, which
is organised by which of seven situations you are in rather than by how the software is built.
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
| One prioritised project per workspace, whose work rises without hiding anybody else's | **Built** |
| Deadlines, planned days, and deferring until later | **Built** |
| Something that lasts — a start and a real end, rather than one moment | **Built** |
| Events — a birthday, a booked fortnight, a code freeze: what happens to you, never due or overdue | **Built** |
| Reminders — *two weeks before my sister's birthday*, asked once and carried by your calendar | **Built** |
| `blocks` dependencies, and `--ready` to filter by them | **Built** |
| A fixed meaning on every relation, so the words are yours to rename | **Built** |
| Milestones — an item whose blockers are its contents | **Built** |
| Comments (what happened) and documents (what you concluded) | **Built** |
| A dead end recorded as a document, so an idea is only tried once | **Built** |
| *Read first* — which written conclusions govern this particular item | **Built** |
| Proposed links, read out of what the item itself says | **Built** |
| A record of what was checked, against the state of the code it ran on | **Built** |
| Tags, custom statuses, per-workspace vocabulary you can edit | **Built** |
| Search across titles, descriptions, document bodies and comments | **Built** |
| Search served by an index, with ranking — PostgreSQL, opt-in | **Built** |
| Capture grammar — `Fix the deploy script by friday !4/2 ~2h #ops +web` | **Built** |
| Moving a task to another project, or under a different parent | **Built** |
| Recurring tasks — `--repeat "every month on the 30th"`, from a captured line or the browser | **Built** |
| Acceptance criteria and completion gates | Planned |
| Handing a working session from one agent to the next | Planned |
| Ordering a backlog by hand | Planned |
| Attachments | Planned |
| Time tracking — `~2h` records an estimate; it does not track one | Planned |

### People and agents

| | |
| --- | --- |
| Delegation — assign work to a person or an agent, and ask what is assigned to you | **Built** |
| Sub-agents, with an accountability chain that ends at a person | **Built** |
| Claims — a lease that renews as work happens and is given back when it is done | **Built** |
| A question parked for a person, at the top of their agenda until they answer | **Built** |
| `subroutine agent create` — an account, a role and a credential in one act | **Built** |
| Service accounts, and credentials narrower than your own | **Built** |
| Per-workspace roles; credentials scoped to a single project | **Built** |
| Deactivate a person and their agents stop with them | **Built** |
| Every change attributed to a principal, permanently | **Built** |
| What one account has been doing, through whatever credential | **Built** |
| Email sign-in — today the link is printed at a terminal | Planned |
| Notifications and webhooks | Planned |

### Ways in

| | |
| --- | --- |
| HTTP API — OpenAPI at `/v1/openapi.json`, for any viewer you like | **Built** |
| CLI, progressive — a shopping list needs none of the above | **Built** |
| Web interface — add, edit, complete, comment, link, hand over, set a repeat, write a document | **Built** |
| Markdown rendering, and a link to any item that you can send somebody | **Built** |
| Sign-in links, revocable from the command line | **Built** |
| MCP over stdio (`subroutine mcp`) and over HTTP (`POST /mcp`) | **Built** |
| Two Claude Code plugins — one local, one needing nothing installed | **Built** |
| `subroutine setup claude` — a hook that gives back what an agent is still holding | **Built** |
| Multiple connections merged into one agenda | **Built** |
| The agenda as the browser's front page | **Built** |
| Calendar feeds — subscribe Google, Apple or Outlook to your work, your events and your reminders | **Built** |
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
| Deleting a workspace, and bringing it back with every number intact | **Built** |
| `subroutine doctor` — whether this machine's installation is coherent | **Built** |
| Single-command deployment from a compose file | Planned |

---

## The shape of it

```console
$ subroutine init
  Ready. Try: subroutine add "something to do"

$ subroutine add "Call the dentist before Sunday"
  Added: Call the dentist  (due Sun 9 Aug)
    Tip: subroutine agenda

$ subroutine agenda
  Nothing due today.
  Next 7 days
     #1  Call the dentist  (due Sun 9 Aug)

    Tip: subroutine done 1

$ subroutine done 1
  Done: Call the dentist
    Tip: subroutine agenda
```

**Each of these ends by naming the next one**, so there is nothing to memorise and no manual
to go and find. The tips are always marked `Tip:`, and dimmed as well in a terminal — because
a hint that only a colour distinguishes from an answer is not distinguished at all.

Once there is more on the list than fits on a screen, `subroutine list` ranks it and
`subroutine search` finds things by their words — in titles, and in whatever you wrote about
them:

```console
$ subroutine list --order -priority_score
$ subroutine search "dentist"
```

Anything you have put off until a later date is held back from the list, and the list says
how much it is holding back. `--deferred` includes it, at the bottom — visible, and not mixed
in with the work you could start now.

No server, no token, no configuration. When you want an agent involved, or a second person,
the same install grows an HTTP API: `subroutine serve`, and `subroutine agent create` for the
credential. A secret is shown once and stored as a hash, so a stolen database is not a set of
working credentials. Point an agent at the address and the first thing it should read is
`GET /v1/docs/agent`, which is written for that reader rather than for you.

## In a browser

The same instance serves a web interface at its own address — the four steps at the top of this
page end there, and `subroutine serve` is all it takes to get back to it afterwards.

It opens on your agenda — anything waiting on you first, then what is overdue, then today. From
there, a list of tasks and documents together, or a board where dragging a card between columns
changes its status. Click anything and you get it in full: what it is joined to, what governs
it, what has been checked against it, and everything anybody has recorded against it.

You can add something with one box that takes the same shorthand the CLI does, edit it, comment
on it, link it to what is holding it up, say how often it comes round, write a document and
revise it, search, complete it, and hand a task to somebody from a list of the people in that
workspace.

- **It installs on a phone or tablet.** The same address becomes an app with its own icon and
  its own window, and an instance that says where it is reachable puts that address on the
  label — which is what tells two of them apart on one home screen. Where the control is
  differs by browser and
  [docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md#on-a-phone-or-tablet) says where to look. It needs
  the network like the page does; nothing is stored on the device.
- **Every item has an address you can send somebody**, and the project in the middle is there
  for the reader rather than for the machine — rename it and old links still work.
- **Descriptions and comments render as the Markdown they are written in.** Anything that looks
  like HTML is shown as the text it is, so a description written by somebody else — or by an
  agent repeating something it read — cannot become part of the page.
- **Nothing asks you to confirm first.** A question before every action is a tax on being right;
  completing something tells you what it did and offers to undo it.
- **Signing in is a link.** No password to store, no reset flow, nothing worth stealing in a
  breach. `subroutine login revoke <name>` ends everything that person holds and any link
  they have not used, which is what a lost laptop needs.

It talks to the same public API everything else does, so anything it can show you a script can
too — and there is no build step: the JavaScript served is the JavaScript in the repository.

## Install

Python 3.11 or newer, and `uv` or `pipx` pulls the rest.

**As a tool, because that is what it is** — an application, not a library. It is also the only
thing that works on a current Linux: Debian, Ubuntu and Fedora refuse a bare `pip install`
outside a virtualenv and tell you to use pipx instead. `pipx install subroutine` does the same
job as the `uv` line at the top of this page; **neither installed?** `sudo apt install pipx`,
`brew install pipx` on a Mac, or [uv's installer](https://docs.astral.sh/uv/getting-started/installation/), which is
one line and needs no Python.

`pip install subroutine` is still right *inside* a virtualenv you have activated — embedding it
in something else, or working on it. PostgreSQL when you outgrow SQLite, with the extra on
whichever you used:

```console
$ uv tool install "subroutine[postgres]"
```

## Giving an agent tools

An agent that can run a shell has everything it needs already. One that cannot — or one you
would rather not give a shell — reaches the same instance over the **Model Context Protocol**.
`subroutine mcp` speaks it on stdin and stdout, so a client starts it as a child process: no
port, no listener, nothing serving unless your client is running it. **A served instance also
speaks MCP itself**, at `POST /mcp`, which is how an agent reaches one with nothing installed
at all — and both are the same principal under the same permissions.

**For Claude Code there is a plugin**, which is the easier half of this and the recommended one:

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

Your editor launches it through `uvx`, so what it needs is uv rather than Subroutine; a copy
you installed yourself is used instead of a download. **Adding the marketplace needs Git**,
because the command clones a repository — the one prerequisite here that is Claude Code's
rather than ours.

**If your work lives on a server and this machine is only a client, install the other plugin
instead** — `subroutine-remote@subroutine` needs nothing on your machine at all. Paste in the
address you were given and your token and you are working: no Python, no package, no
`config.toml`. Your editor connects from *this* machine, so an instance on your own network or
behind a VPN is as reachable as a public one.

**Four things about installing that nothing else will tell you**, so they are said plainly
here:

- **"3 userConfig options not yet set" is not work outstanding.** The install prints it, and all
  three are optional: leave every one of them empty and the plugin works. They exist for a second
  instance, a second workspace and an agent's own credential, none of which a first install has.
  The count is your editor noting that three settings have no value, not Subroutine asking for
  anything.
- **claude.ai in a browser cannot run either plugin.** There is nothing on that side to start a
  program on. The install succeeds, the settings page opens, its fields are all there, and the
  only evidence of a problem is an absence.
- **Installing a plugin and starting its server are separate moments, and only the first one
  reports.** If the tools do not appear, `claude mcp list` says why in one line.
- **Working on a checkout?** Point Claude Code at your copy directly — the plugin's own is
  cached and lags until you refresh it: `claude mcp add subroutine -- /path/to/venv/bin/subroutine mcp`.

[docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md) is the whole of this, including what to ask for from
whoever runs the instance, and [docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md) is their end of it.

**Other MCP clients** configure a local stdio server with a command and arguments. Cursor,
Windsurf, Zed, VS Code's Copilot agent mode, Gemini CLI, Codex CLI, Cline, Continue, OpenCode
and JetBrains AI Assistant all support this; give them the absolute path to `subroutine` and
`mcp` as the argument. They get the tools only — the plugin format and the skill are Claude
Code's. Aider has no MCP client of its own; use the CLI through `/run` instead.

### What the plugin adds beyond the tools

**A deliberately small set of tools, not one per endpoint.** A tool's schema is context the
agent carries for its whole session whether it calls it or not, so the surface is a budget, and
a test fails when it grows past one somebody has to raise on purpose. They cover the everyday
work: capture, list, search, read, update, comment, finish, document, link, projects, what has
changed since you last looked, claiming a task so two agents do not collide, and asking which
principal you are. **And one that reaches everything else the credential allows** —
`subroutine_call_api` calls the HTTP API directly, so the small surface is an opinion about
what to reach for first rather than a limit on what can be done.

**A skill: the practice, rather than the API.** When to file work before starting it, how to
ask what can actually be *started* rather than what merely exists, the difference between a
comment and a document, and how to adopt Subroutine into a project that does not use it yet —
including which of those decisions are permanent and therefore worth asking you about. Its
description costs about 200 tokens of a session and it loads the rest only when relevant.
Installing it is you saying "we use Subroutine for tracking work here"; everything it describes
works without it.

**And a captured line instead of a dozen typed fields**, because the grammar you already type
is smaller than a schema describing it:

```
subroutine_add(text="Fix the deploy script by friday !4/2 ~2h #ops")
```

The server talks to whichever *connection* is current, so pointing an agent at a colleague's
instance is a matter of `subroutine use`, not of reconfiguring the agent. And if you keep your
own list here and your team's on a company server, both are just connections — one agenda shows
the dentist and the stand-up together, each row printing an address you can type back:

```console
$ subroutine agenda
  Today
              #1  Pay the gas bill  (for Sat 1 Aug)
    work/acme/#1  Fix the deploy script  (for Sat 1 Aug)
```

## Running it for a team

**TL;DR: a Python process on loopback, your own TLS proxy in front, systemd keeping it alive,
PostgreSQL underneath once more than one person is writing.** Nothing to cluster, no message
broker. The whole recipe is [docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md), and every command on that page
has been run, including the refusals.

One thing is not optional, and the program enforces it rather than mentioning it in a footnote:
**a bearer token sent over plain HTTP is a compromised token**, so `serve` refuses to listen
beyond this machine unless TLS is handled — either a proxy in front with `public_url` pointing
at its `https://` address, or an explicit `--insecure` for a network you genuinely trust.

Adding a person is one command, and it can hand them the way in too. The role is `member`
unless you say otherwise, and the workspace can be left out while there is only one. Roles
belong to a workspace, so `member` in one is not `member` in another, and the last account able
to administer a workspace cannot be removed from it.

```console
$ subroutine user create thomas --name "Thomas Anderson"
$ subroutine user create tim --browser --terminal
```

There is no password. `--browser` prints a sign-in link and `--terminal` prints a credential
with the line that connects to this instance; they are not alternatives, because somebody who
uses the web interface and has a colleague setting their machine up needs both. Name neither
and the account is still real — the two commands that hand it over are printed.

**Their agents are one command**, and it is the one to reach for rather than assembling an
account, a role and a token by hand:

```console
$ subroutine agent create sam --profile collaborator --project web --write web
```

`--profile` says what the agent is *for* and expands into the rest — `worker` owns one project,
`collaborator` reads several and writes one, `observer` only reads. It prints an environment
line, and **that line is half the work**: an agent that can run a shell reaches the instance
both through the tools its editor wired up and by running `subroutine` itself, and those
resolve credentials separately. Setting the variable where the agent starts covers both, so its
work is not attributed half to it and half to you.

`source_url` in `GET /v1/meta` says where the source of *this* instance can be had. **Nothing
in the licence requires that of you** — it is a promise the product makes to whoever is using
it, and it is a setting, so somebody running a fork can point at theirs.

## Search

`q` looks in titles, descriptions, document bodies and the comments on an item. Every word you
give it has to appear, in any order and in any of those places. A query that is **just a
number** finds the item with that ref as well as everything mentioning it, whether or not it is
finished — so `subroutine search 862` is how you find what has been said about `#862`.

By default a search is a substring scan — honest at personal scale, and measured at 20,000
tasks a search matching nothing took **119 ms**, growing with the backlog. On PostgreSQL,
`search_backend = "native"` in `config.toml` puts it behind a real index and the same search
takes **1 ms**. No migration beyond the ordinary `subroutine db upgrade`, and turning it off
again is a configuration change and nothing else.

**It is off by default because it changes what a search finds, not only how fast.** `seed`
starts finding *seeded* and *seeding*, and `curs` still finds *cursor* — but **`ursor` stops
finding *cursor***, because matching the middle of a word is the one thing an index cannot do,
and a very common word stops narrowing rather than being required. Keep the default if you rely
on either. On SQLite it is simply not available, and asking for it there is not an error: you
get the scanning implementation and are told so rather than left to assume.

**Ask `GET /v1/meta` what this instance can do** rather than inferring it. Merging tasks and
documents into one list, paging through it, and ordering deferred work last are covered in
`GET /v1/docs/agent`.

## Documentation

- **[docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)** — the seven ways to reach an instance, organised
  by which one you are. Start here if somebody has handed you an address and a token.
- **[docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md)** — running it as a service, end to end.
- **[CHANGELOG.md](https://github.com/simonholliday/subroutine/blob/main/CHANGELOG.md)** — what changed, and which releases need a database
  migration. That last part is checked rather than remembered: CI refuses a release that moves
  the schema without saying so, so you can plan the upgrade instead of discovering it.
- **[docs/errors.md](https://github.com/simonholliday/subroutine/blob/main/docs/errors.md)** — every error code the API can return. Generated from
  the registry, so it cannot drift from the code.
- **[docs/design.md](https://github.com/simonholliday/subroutine/blob/main/docs/design.md)** — the design this was built from: data model, API,
  permissions, agent design, and the reasoning behind each. Frozen, and wrong in places — the
  code is the truth. It is here because the code cites it about two thousand times and a
  citation nobody can follow is worse than none.
- **`GET /v1/docs/agent`** — the guide an agent should read first, written for that reader.
- **[SECURITY.md](https://github.com/simonholliday/subroutine/blob/main/SECURITY.md)** — how to report a vulnerability privately, and what is in
  scope. Not through an issue: an issue is public from the moment it is filed.

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
