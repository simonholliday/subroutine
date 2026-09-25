# Subroutine

**A self-hosted, agent-native task and decision tracker for people and coding agents working on complex projects.**

Designed from the ground up so that a coding agent is a first-class user rather than an add-on:
agents get accounts of their own, can file and claim work, and document findings. An agent
learns the tool while using it.

## Setup takes about a minute, and that is it

**Four commands, and most of the minute is waiting for downloads.**

```console
$ uv tool install subroutine
$ subroutine init
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

The first two put the program on your machine and make your instance; the last two give Claude
Code the tools and the skill. **Adding the marketplace needs Git**, because that command clones
a repository - the one prerequisite here that is Claude Code's rather than ours.

**Then start a fresh Claude Code session and tell it *"we use Subroutine now"*. That sentence is
the setup.**

The skill that ships with the plugin has a section for exactly that moment, and its first
instruction is to **ask only what cannot be undone and state the rest**. So your agent looks for
an existing project before making one, proposes a key instead of interviewing you about it,
skips the workspace question when there is only one workspace, marks the checkout so later
sessions do not have to work out where they are, and writes a pointer into whichever file your
agent reads at startup, so the next session finds all of it without being told.

From there you talk to your agent rather than to Subroutine: *"file that as a bug"*, *"what can
I actually start?"*, *"what did we decide about retries?"*, *"write down why we rejected the
other approach."* You never type a ticket, and you never have to learn a command to get any of
what follows.

### Keeping an eye on the work

You will want to see what is happening, though. Run `subroutine serve`, then
`subroutine login link` for a link that signs your browser in, and leave the browser on a second
screen.

The page refreshes itself every few seconds, so you watch your agent work as it works: items
appearing as it files them, a card crossing the board as it claims something and starts, a
question landing on your agenda when it hits something only you can settle. You read what it
decided, in its own words, without interrupting it to ask.

And when somebody else needs to see the work - a colleague, a client, whoever is asking how it
is going - it is the same page, and they need nothing installed to open it.

## What that makes possible

Every decision, finding and dead end stays beside the work, so an agent opening a task three
weeks later starts with the context rather than inventing it. That is the whole bet: your agents
begin each session knowing nothing, and the more of what you have already settled they can
reach, the less they invent. What it buys:

**An agent per project, handing work to each other.** One agent files work for another, with the
reasoning attached; the second picks it up, comments, and hands it back. Every item carries who
did what, so a chain of three agents and a person reads as one record rather than four accounts
of the same afternoon.

**Research that outlives the session that did it.** Run four agents at a question in parallel
and have each write down what it found. Those become documents - a decision, a finding, a dead
end - and a dead end is worth as much as a decision, because the next agent stops before
spending a day proving it again.

**Conclusions that reach the work without anybody remembering to link them.** File a decision
against a parent and everything filed beneath it inherits it, nearest first, labelled with where
it came from. The agent that opens a leaf three weeks later reads the rule that binds it under
*Read first*, having done nothing to find it.

**A roadmap that stops the tangents.** An agent that asks *what should I do next* gets the plan
rather than whatever it thought of thirty seconds ago. Work that is blocked says what by, so
*what can I actually start* has a real answer.

**A question that waits for you instead of being guessed.** An agent that hits something only
you can settle marks it and moves on. It arrives on your agenda, you answer once, and the answer
stays on the item for whichever agent picks the work up next - including one that will not exist
for a month.

**Several projects that have to agree.** Work in one project can block work in another, and a
decision taken in one can bind all of them. That is not hypothetical here: see
[It runs on itself](#it-runs-on-itself) below.

**A record of everything, in plain words.** A journal of what happened over any period, by
whom - people and agents alike, each agent showing the person it answers to.

## An agent that picks up its own work

Nothing above needs you in the loop. With Claude Code, this is a standing instruction:

```
/loop Check Subroutine for anything assigned to me, or unassigned in +web, that is ready to
start. Claim it, set it in progress, and do it. Comment with what you found, then hand it
back. If a decision is missing, file a question and assign it to me rather than guessing.
```

It wakes, asks what is startable, claims one so nobody else takes it, works, writes down what
happened, and releases it. A claim is a lease rather than a lock, so an agent that dies does not
strand the work - the claim expires and the next one can take it.

## Why an agent needs no teaching

*Setup takes about a minute* is the half a person sees. This is the half underneath it: an
agent meets Subroutine as four things at once - a database, an HTTP API, an MCP server and a
skill - and none of them has to be taught to it, by you or by anybody.

- **A refusal is a lesson.** An error names the field, says what was wrong with the value, and
  lists the ones that would work. An agent that gets a call wrong gets told how to get it right,
  rather than getting a stack trace or a shrug.
- **The vocabulary is published, not memorised.** Statuses, types and fields are whatever your
  workspace calls them, and `/v1/meta` says so. The filter and search grammar compiles to that
  same registry, so an agent generates a query instead of remembering a list of parameter names
  somebody wrote down once.
- **There is a guide written for an agent**, at `/v1/docs/agent`, and every worked call in
  `/v1/docs/examples` is executed by the test suite - so an example that stopped working fails
  the build rather than misleading a reader.
- **The tool surface is a deliberate budget**: a small set, and one of them reaches any route
  the others do not. Every name an agent has to be taught is context spent for ever, where a
  grammar it can discover costs nothing.
- **Conventions travel with the work.** `subroutine://conventions` carries everything in force
  in your workspace - decisions, specifications, designs and dead ends - and any MCP client can
  read it before its first write.

None of that is AI. It is an API designed on the assumption that its most frequent reader will
be a program that has never seen it before and will not remember it next time.

## It runs on itself

Subroutine has tracked its own development since its third day: **1,191 of the 1,225 commits
since then** cite the item they implement, and the commit hash is written back onto that item,
so *what closed this* and *what did that commit do* are both answerable.

It is not the only thing in there. At the time of writing (25th September 2026) the workspace it
is tracked in holds **419 open items across 17 projects** and **610 written-up documents** -
238 findings, 129 decisions, 95 designs, 83 notes, 39 specifications and 26 dead ends -
covering this tracker, four audio applications, two MIDI libraries, a networking framework, two
websites and some infrastructure.

The audio projects are the case that explains the rest. They are separate programs that have to
agree with each other: they reach a shared service over one protocol, and they read the same
instrument definitions. A finding filed in one of them reads

> *the two device matchers already disagree on exact-match precedence*

which is the kind of thing nobody notices until two agents working in two repositories both
think they are right. It was found, written down and settled in one place, and both projects
inherited the answer.

## What it actually is

Self-hosted, with no account to make and no model running anywhere in it. SQLite by default, and
PostgreSQL when you outgrow it. An HTTP API first, published as OpenAPI, with the CLI, the
browser and your agents all clients of it.

Three ways in, and they compose:

- **MCP**, for your agents - over stdio or straight from the server, with nothing installed.
- **A terminal**, for you. `subroutine add "call the dentist tomorrow at 2pm"` reads the date
  out of the sentence.
- **A browser**, for you and for everyone who is never going to use a terminal - an agenda, a
  list, a drag-and-drop board, and the whole of an item.

Your own to-do list fits in the same install without being filed like work.

### About Claude

Subroutine speaks MCP, so any agent that does can use it. **In practice it has only been tested
with Claude Code**, every example here is Claude Code, and the packaged plugin is a Claude Code
plugin.

One piece is Claude's alone: the **skill**, which teaches an agent the conventions a tool
description has no room for. Nothing else loads a skill file today. What is not Claude-only is
`subroutine://conventions`, an MCP resource carrying what binds the workspace - any MCP client
can read it, and should before its first write.

## Beyond the first minute

The four commands above are the whole install. Three things are worth knowing once you are past
them, and none is needed on day one.

**No uv?** Its installer is one line and needs no Python, or `pipx install subroutine` does the
same job. If the shell cannot find `subroutine` afterwards, `uv tool update-shell` fixes it, and
you will need a fresh terminal.

**Give each project's agent an account of its own** - ask your agent to, or run
`subroutine agent create web --workspace projects --here` in the project's directory - and every
change it makes carries its name and a credential narrower than yours, rather than being filed
as you. With the `subroutine-remote` plugin that covers the agent's shell and not its tools,
until the project is switched to the `subroutine` plugin. On one laptop with one person this
does not matter; the moment there are two agents it does.

**Sign in from anywhere else** with `subroutine login link`, which prints a link that works once
and lasts half an hour. Put the instance on a machine your team can reach and the same browser
page is how everybody else sees the work.

---

# Reference

Everything above is the argument. The rest of this page is the detail, and where a program can
check it, it is checked rather than remembered: a row in the table below marked built that the
agent guide, `GET /v1/docs/agent`, calls unbuilt fails the build before anybody reads it.

## What is built, and what is planned

Every row marked **Built** works today and is covered by tests. Every row marked Planned is
specified and not built - named here because a tool that overstates itself wastes your afternoon.

### The work itself

| | |
| --- | --- |
| Tasks and documents, sharing one numbering scheme | **Built** |
| Projects, sub-projects and workspaces | **Built** |
| Priorities - importance × urgency, ranked in bands | **Built** |
| One prioritised project per workspace, whose work rises without hiding anybody else's | **Built** |
| Deadlines, planned days, and deferring until later | **Built** |
| Something that lasts - a start and a real end, rather than one moment | **Built** |
| Events - a birthday, a booked fortnight, a code freeze: what happens to you, never due or overdue | **Built** |
| Reminders - *two weeks before my sister's birthday*, asked once and carried by your calendar | **Built** |
| `blocks` dependencies, and `--ready` to filter by them | **Built** |
| A fixed meaning on every relation, so the words are yours to rename | **Built** |
| Milestones - never offered as work, counting what they include, and read in date order as a roadmap | **Built** |
| Comments (what happened) and documents (what you concluded) | **Built** |
| A dead end recorded as a document, so an idea is only tried once | **Built** |
| *Read first* - which written conclusions govern this particular item | **Built** |
| Proposed links, read out of what the item itself says | **Built** |
| A record of what was checked, against the state of the code it ran on | **Built** |
| Tags, custom statuses and per-workspace vocabulary, editable through the API | **Built** |
| Search across titles, descriptions, document bodies and comments | **Built** |
| Search served by an index, with ranking - PostgreSQL, opt-in | **Built** |
| Capture grammar - `Fix the deploy script by friday !4/2 ~2h #ops +web` | **Built** |
| Moving a task to another project, or under a different parent | **Built** |
| Recurring tasks - `--repeat "every month on the 30th"`, from a captured line or the browser | **Built** |
| Acceptance criteria and completion gates | Planned |
| Handing a working session from one agent to the next | Planned |
| Ordering a backlog by hand | Planned |
| Attachments | Planned |
| Time tracking - `~2h` records an estimate; it does not track one | Planned |

### People and agents

| | |
| --- | --- |
| Delegation - assign work to a person or an agent, and ask what is assigned to you | **Built** |
| Sub-agents, with an accountability chain that ends at a person | **Built** |
| Claims - a lease that renews as the task is written to, and is given back when it is done | **Built** |
| A question parked for a person, on their agenda until they answer | **Built** |
| Handing work back - a question goes to whoever assigned the work, and the answer comes back | **Built** |
| An agenda that says what you are waiting on, and who is waiting on you | **Built** |
| `subroutine agent create` - an account, a role and a credential in one act | **Built** |
| Service accounts, and credentials narrower than your own | **Built** |
| Per-workspace roles; credentials scoped to a single project | **Built** |
| Deactivate a person and their agents stop with them | **Built** |
| Every change attributed to a principal, permanently | **Built** |
| What one account has been doing, through whatever credential | **Built** |
| A journal of what happened, in plain words - a workspace's or one item's, in the browser too | **Built** |
| Email sign-in - today the link is printed at a terminal | Planned |
| Notifications and webhooks | Planned |

### Ways in

| | |
| --- | --- |
| HTTP API - OpenAPI at `/v1/openapi.json`, for any viewer you like | **Built** |
| CLI, progressive - a shopping list needs none of the above | **Built** |
| Web interface - add, edit, complete, comment, link, hand over, set a repeat, write a document | **Built** |
| Markdown rendering, and a link to any item that you can send somebody | **Built** |
| Sign-in links, revocable from the command line | **Built** |
| MCP over stdio (`subroutine mcp`) and over HTTP (`POST /mcp`) | **Built** |
| Two Claude Code plugins - one local, one needing nothing installed | **Built** |
| `subroutine setup claude` - a hook that gives back what an agent is still holding | **Built** |
| Multiple connections merged into one agenda | **Built** |
| The agenda as the browser's front page | **Built** |
| Settings for you, a workspace, a project and the installation, in the browser | **Built** |
| Calendar feeds - subscribe Google, Apple or Outlook to your work, your events and your reminders | **Built** |
| A board in the browser, with drag-and-drop between columns | **Built** |
| A calendar view | Planned |

### Running it

| | |
| --- | --- |
| SQLite and PostgreSQL - anything that touches a database is tested against both | **Built** |
| Migrations, with releases that announce a schema change in advance | **Built** |
| Backups to wherever you point them, verified where they land | **Built** |
| Restore, as a recovery or as a clone | **Built** |
| Separate profiles on one machine | **Built** |
| Deleting a workspace, and bringing it back with every number intact | **Built** |
| `subroutine doctor` - whether this machine's installation is coherent | **Built** |
| Being told when the program, the plugin or the instance is out of date - opt-in | **Built** |
| Copying an instance between SQLite and PostgreSQL | **Built** |
| Single-command deployment from a compose file | Planned |

---

## Where the rest of it is written

**Reaching an instance somebody else runs is [docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)**, which
is organised by which of seven situations you are in rather than by how the software is built.
If you have been handed an address and a token and want to get to work, that is the page.

- **[docs/connecting.md](https://github.com/simonholliday/subroutine/blob/main/docs/connecting.md)** - the seven ways to reach an instance, organised
  by which one you are.
- **[docs/hosting.md](https://github.com/simonholliday/subroutine/blob/main/docs/hosting.md)** - running it as a service, end to end.
- **[docs/errors.md](https://github.com/simonholliday/subroutine/blob/main/docs/errors.md)** - every error code the API can return, generated from
  the registry the code uses.
- **[docs/osc.md](https://github.com/simonholliday/subroutine/blob/main/docs/osc.md)** - sending what happens in a workspace to music
  software, which most people will never need.
- **[CHANGELOG.md](https://github.com/simonholliday/subroutine/blob/main/CHANGELOG.md)** - what changed, and which releases need a database
  migration.

These five are moving to the guide, which is why the links above point at the repository rather
than at the site.

## A few commands worth knowing

`subroutine help` lists them all and `subroutine explain dates` covers the ideas behind them.

```console
$ subroutine add "call the dentist tomorrow at 2pm"
$ subroutine agenda
$ subroutine list --ready
$ subroutine search "retries"
$ subroutine show 42
$ subroutine done 42
$ subroutine doctor
```

## Documentation

Full documentation, including a guide in four parts that walks through every way Subroutine is
reached, run and worked in: `https://subsystem.co/subroutine/`

- The guide: `https://subsystem.co/subroutine/guide/`
- HTTP API reference: `https://subsystem.co/subroutine/http-api/`
- Command-line reference: `https://subsystem.co/subroutine/command-line/`
- MCP reference: `https://subsystem.co/subroutine/mcp/`

Every page there is generated from the release it documents, so it cannot drift from the code.

## Contributing

Pull requests are welcome and need the CLA sentence in [CLA.md](https://github.com/simonholliday/subroutine/blob/main/CLA.md).
[CONTRIBUTING.md](https://github.com/simonholliday/subroutine/blob/main/CONTRIBUTING.md) has the rest.

## Licence

Subroutine is under FSL-1.1-ALv2, plus a commercial licence by agreement. Run it, modify it, fork it, for any
purpose **except competing with it**: no commercial product or service built from it that
substitutes for it or does substantially the same job. Internal use at any size is free for
ever, and so is professional services work. Each release becomes Apache-2.0 two years after it
ships. It is not OSI open source.
