# Connecting to Subroutine

There are seven ways to reach a Subroutine instance, and the one that is right for you follows
from two questions: **where does your work live**, and **who is asking for it** — you in a
browser, you at a terminal, an agent in your editor, or the calendar application you already
keep your week in.

This page is organised by the answer. Find yourself in the table, read that one section, and
ignore the rest. [docs/hosting.md](hosting.md) is the other end of most of them — it is for
whoever is standing the server up, and if that is also you, read it first.

| Your work lives | You are | Read |
| --- | --- | --- |
| On a server | In a browser | [Just a web page](#just-a-web-page) |
| On this machine | At a terminal | [Just this machine](#just-this-machine) |
| On a server | At a terminal | [Your terminal here, your work there](#your-terminal-here-your-work-there) |
| On this machine | An agent in your editor | [An agent, on the machine holding the work](#an-agent-on-the-machine-holding-the-work) |
| On someone else's server | An agent in your editor | [An agent, with nothing installed](#an-agent-with-nothing-installed) |
| On a public server | Claude on the web | [Claude on the web](#claude-on-the-web) |
| Anywhere | Your calendar application | [Your work in your calendar](#your-work-in-your-calendar) |

**Two of these can be true at once and that is normal.** Your own list on this laptop and your
team's on a server is one arrangement, not two — `subroutine agenda` asks every instance you can
reach and merges the answers, so the dentist and the stand-up land in one list. Reading spans
everything; only writing has to pick.

## Just a web page

**Nothing to install, and nothing to configure.** If somebody runs an instance and has given
you an account on it, they can hand you a **sign-in link** — one address that signs you in and
then works no more. Open it, and you are in.

Ask them for `subroutine login link --username <you>`. What arrives looks like this:

```
https://subroutine.example.com/signin?link=…
```

**It is good for half an hour and works once.** If it has gone stale by the time you get to it,
that is ordinary — ask for another. Once it has signed you in, the browser stays signed in for a
fortnight, and you sign in again the same way after that.

**Treat it like a password while it is alive**, because for those thirty minutes it is one:
anybody who has the link can become you. It travels in a web address, so it reaches whatever
carried the message, and it is worth not pasting into anywhere that keeps a history.

**A token is not a substitute.** If you have been given something starting `sr_`, that is for a
terminal or an agent, and pasting it into a browser will not sign you in. Ask for a link
instead — they are different credentials for different doors, and having one does not get you
the other.

**What you can do there**: read and add work, change what an item says, move it through its
statuses, comment, link items together, and search. If you also want a terminal or an agent,
every section below still applies and you want a token as well as a link.

## What to ask for, if somebody else runs the instance

Three things, and the third is often forgotten:

1. **The address.** For a terminal, the instance's base address —
   `https://subroutine.example.com`. For an agent, the same with `/mcp` on the end.
2. **A token.** It starts `sr_`. It says who you are, so what you file is attributed to you
   rather than to whoever set the server up, and it decides what you are allowed to do. It is
   shown once, by the person issuing it, and stored nowhere — so if it is lost, the answer is a
   new one rather than a lookup.
3. **The workspace, if the instance holds more than one.** A workspace is a wall between two
   bodies of work — a client, a company, a side project. Most instances have one and you will
   never hear the word. On an instance with several, a session that has not been told which one
   it is in has its first read refused, and an agent has no way to guess.

Nothing else. There is no account to create on your side, no key to exchange, and no
configuration file you have to write by hand.

## Just this machine

**You are the only person who needs this, your work stays on your own disk, and nothing is
served to anybody.** This is the ordinary case and it is the one to start from.

```console
$ uv tool install subroutine    # or: pipx install subroutine
$ subroutine init
$ subroutine add "Call the dentist before Sunday"
$ subroutine
```

**What it needs:** Python 3.11 or newer, and nothing else. The database is a SQLite file under
your own data directory, made by `init`. There is no server, no port, no token and no login —
the file permissions on that database are what protect it.

**You know it worked** when `subroutine` prints the task you just added.

**If it does not:** `subroutine doctor` prints where this installation keeps its configuration,
its database and its state, and says whether they are coherent. Run it before believing anything
else about the machine.

You will not meet the words *workspace*, *instance* or *connection* on this path, and you never
have to. They are what the next four sections are about.

## Your terminal here, your work there

**Somebody runs Subroutine on a server — your company, or you on a machine that is always on —
and you want it in your own terminal, beside whatever is already on this laptop.**

Install the program here, then add the server as a *connection*:

```console
$ uv tool install subroutine    # or: pipx install subroutine
$ subroutine connections add work --url https://subroutine.example.com
Token for work:
Reached Acme Ltd as jo, in acme.
Added work to …/config.toml
Its token is in …/credentials.toml, readable only by you.
```

It asks for the token, reaches the instance with it, and writes nothing until both work. A
mistyped address or a revoked credential is refused there and then, rather than becoming a line
of failure the next time you list something. **The name it reports back is the one that
instance knows you by** — which is the only thing that confirms you pasted the token you meant
to, since a token carries no clue about whose it is.

**What it needs:** the program on this machine, the address, and a token from whoever runs the
instance.

**You know it worked** when `subroutine list` shows the server's work with an address you can
type back:

```console
$ subroutine list
  Local
              #1  Pay the gas bill

  work
    work/acme/#1  Fix the deploy script
```

**The name — `work` here — is yours.** It becomes the first part of every address that
instance's items print as, and two people reaching one server may call it different things.

**Your own database does not go anywhere.** It is a connection too, called `local`, and it is
still where `subroutine add` files things — unless this machine has no list of its own, in which
case `connections add` points writes at the server and says so. `subroutine use work` moves them
either way, and it never changes what you can *see*: reads always span everything you can reach,
which is what makes switching safe.

**If it does not:** `subroutine connections` lists what this machine reaches and, for each,
which of the four places its token came from. It is worth knowing about because it stays out of
`subroutine --help` until a second connection exists — which is to say, until the thing you are
checking has already worked. `connections add` is hidden alongside it, which is why this page
names them both.

## An agent, on the machine holding the work

**Your work is on this machine and you want your coding agent to plan, file and close it.**

```console
$ uvx subroutine init
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**What it needs: [uv](https://docs.astral.sh/uv/getting-started/installation/), and not
Subroutine.** Your editor starts the plugin through `uvx`, which fetches the package on first
use and caches it — roughly five seconds once, then a fraction of a second. Nothing is
permanently installed and nothing has to be on your `PATH`.

**Already ran `uv tool install subroutine`?** That copy is used instead of a download, so the
two arrangements do not fight. **Running from a checkout or a virtualenv?** The plugin cannot
point at it — `uvx` takes the package name as its first argument and there is no way to omit
that — so use `claude mcp add subroutine -- /path/to/subroutine mcp` instead, which is better
for development anyway: the plugin's copy is cached and lags until you refresh it.

**And Git, for the third command.** The marketplace is a repository and `claude plugin
marketplace add` clones it, so without a `git` binary that step refuses before anything of ours
runs. A machine already set up to install Python packages nearly always has it.

**This one runs a program on your machine, so it does not work in a browser.** Claude Code and
the desktop apps can start it; claude.ai cannot, because there is nothing on that side to start
anything on. The plugin still installs and still reports success, and the only sign of a problem
is an absence — so it is worth knowing in advance rather than diagnosing.

**You know it worked** when `claude mcp list` shows the server connected, or when you ask the
agent to run `subroutine_whoami` and it answers. Installing a plugin and starting its server are
separate moments and only the first one reports, so the second is worth checking once.

**If you keep more than one instance** — your own and a client's, say — the plugin's *Which
instance* field takes the name of a connection you have already set up, and the section above is
how you set one up.

## An agent, with nothing installed

**Somebody else runs Subroutine, they have given you an address and a token, and you want your
agent working against it this afternoon.** This is the freelancer's case, and it needs nothing
on your machine at all — no Python, no package, no configuration file.

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine-remote@subroutine
```

Then fill in two fields — the address, ending in `/mcp`, and your token. **In a terminal**, run
`claude`, then `/plugin` inside the session, and choose the plugin. Once set they are read by
every session, editor included.

**That terminal is not optional, and this is the step that catches people out.** `/plugin` is
not available in the VS Code extension, and `claude plugin` has no `configure` subcommand —
run `claude plugin --help` if you want to check that for yourself, and it is worth a look,
because that is a claim about somebody else's program and it may stop being true. So a plugin
can be installed from the editor and cannot be set up there, and **nothing says so**:
the install reports success, the fields are simply never asked for, and the only evidence is
that no tools appear.

If you have no terminal at all, the values are ordinary settings and you can write them
yourself. In `~/.claude/settings.json`:

```json
{
  "pluginConfigs": {
    "subroutine-remote@subroutine": {
      "options": {
        "url": "https://subroutine.example.com/mcp?workspace=projects",
        "token": "the token you were given"
      }
    }
  }
}
```

**Two things to know before you do.** That file is not a secret store — your token sits in it
in plain text, which is the same trade as `credentials.toml` and worth a deliberate decision
rather than a discovery. And this is where the values *land* rather than a documented
interface, so `/plugin` is the route that will keep working. Verified on Linux with the
editor reading a plugin configured exactly this way.

**Then reload the window, or start a new session.** MCP servers are attached when a session
begins, so one that was already open when you configured the plugin keeps the tool list it
started with — everything will look correctly set up and there will be no tools.

**You know it worked** when `claude mcp list` shows the server connected *and* the agent can run
`subroutine_whoami`. Check both: a session that predates its configuration shows `✔ Connected`
and has no tools, which reads as a broken product and is not one.

**What it needs on the instance's side:** an address and a token. That is the whole list, and it
is the part somebody else hands you.

**On your own machine you need Claude Code and Git** — the marketplace is a repository, and
`claude plugin marketplace add` clones it. Nothing of Subroutine's is installed: no Python, no
package, no configuration file of ours.

**Your editor connects from *this* machine**, so an instance on your own network or behind a
VPN is as reachable as a public one. Your editor stores the token, not Subroutine, and where it
puts it depends on the editor and the machine — on Windows it is a file under your home
directory. Treat it as you would any password there; if it is exposed, ask for a new one rather
than moving this one somewhere safer.

**If the instance holds more than one workspace, the address has to say which one.** Put it on
the end:

```
https://subroutine.example.com/mcp?workspace=acme
```

Without it, the agent's first read comes back refused — *"This request could be about any of
several workspaces, so it needs to say which"* — with the workspaces it can reach listed. It can
recover by naming one on every call, but it will do that for the whole session and the next
session will start over. One word in the address settles it permanently. **Ask which workspace
your work belongs in at the same time as you ask for the token.**

**You know it worked** when the agent can answer "who am I on this instance?" — it has a
`subroutine_whoami` tool for exactly that, and the answer names the account the token belongs to
and the workspaces it reaches.

**If it does not:** an empty address is *not* an error. The plugin sits idle and this session
simply has no Subroutine tools, so it can be installed before anybody has told you where to
point it. A wrong token or a wrong address both report clearly in the editor; a token is not
something to edit around, so ask for a new one rather than guessing.

**Like every Claude Code plugin, this one runs in the editor and the desktop apps and not on
the web.** The transport is different from the section above — this one needs nothing installed
— but that particular limit is the same.

### Another MCP client

The plugin is a convenience, not the mechanism. The instance speaks MCP itself over ordinary
HTTP, so any client that takes a URL and a header can reach it: point it at the same `/mcp`
address with `Authorization: Bearer sr_…`. There is no session to establish and nothing to
install on either side.

**What such a client does not get is the plugin's skill** — the working practice for using this
well, which ships with the plugin rather than with the instance. The instance offers four
documents as MCP resources instead: a guide written for an agent arriving with nothing, worked
examples, this installation's own vocabulary, and the decisions this workspace has taken. Those
are enough to work from. The skill is the part that says how to work *well*, and it reaches
Claude Code and the desktop apps only.

## Claude on the web

**You want your instance in claude.ai, or in a desktop app talking to it directly, as a
connector.** This is not built yet, and saying so is more useful than a page that implies
otherwise.

It is a different problem from the ways above rather than a bigger one. A connector's traffic
comes *from Anthropic's servers* rather than from your machine, so:

- the instance has to be reachable from the public internet — a laptop or a machine behind a
  VPN can never be one;
- the credential cannot be a token you paste, because it is not your machine holding it.

That makes it an authorisation flow rather than a field in a settings box, which is why it is
its own piece of work rather than a variation on the section above.

**Until then**, the plugin in [An agent, with nothing installed](#an-agent-with-nothing-installed)
reaches exactly the same instance from Claude Code and the desktop apps, with the same token, and
needs nothing installed either.

## Your work in your calendar

**A seventh way in, and the only one that is not really a way *in*.** Anything here with a date
can appear in Google Calendar, Apple Calendar, Outlook or Thunderbird, beside the rest of your
week — so a deadline you filed at a terminal turns up on your phone without you doing anything
else about it.

You need a terminal once, to make the subscription:

```
subroutine calendar create "My work"
```

That prints one address ending `.ics`. Paste it into whatever you keep your diary in, under
whatever it calls *subscribe to a calendar* or *add by URL*. From then on it updates on its own,
every quarter of an hour or so, and you never touch it again.

**Nothing comes back.** Moving an event in your calendar changes nothing here, and deleting one
there does not complete anything. That is the trade for it working in every calendar
application without an account: the feed is a **copy**, kept up to date, and the work still
lives here.

**The address is a password.** Anybody who has it can read everything the feed shows, for as
long as it works, and nobody here can tell that they are — a fetch from somewhere unexpected
looks exactly like one from your phone. So paste it into the calendar application and nowhere
else, and if it gets out:

```
subroutine calendar reset <reference>
```

which gives that subscription a new address and stops the old one that instant. The
subscription keeps its name and its scope; you re-add it in your calendar and carry on.
`subroutine calendar revoke <reference>` stops one for good.

**It is shown once.** Nothing recovers it afterwards, including the instance — what is kept is
a fingerprint. If you lose it before you have subscribed, reset the feed and paste the new one.

**Narrow it if the whole workspace is too much**, which it usually is:

```
subroutine calendar create "The web rebuild" --project ui
subroutine calendar create "Just mine" --mine
subroutine calendar create "Deadlines" --type bug --type feature
```

`--project` takes everything filed under that project too. `--mine` shows only what is assigned
to you. `--expires` stops a feed working on a day you name, which is worth setting for anything
temporary.

**What shows up**: an item's start, an item's deadline, and both where it has both — the day you
meant to do it and the day it is due are different facts, so a calendar showing one would hide
the other. A deadline reads `Due: <title>`. An item that repeats on a fixed schedule arrives as
a repeating event, so your calendar draws the whole series without this instance sending four
hundred copies of it.

**A week of the recent past, and a year or so ahead.** The past is kept on purpose: most
calendar applications delete an event the moment a feed stops sending it, so dropping finished
work would erase a meeting from your calendar's history the moment you ticked it off.

**It shows what you can see, asked afresh every time.** Losing access to a project takes it out
of the feed the same day. There is no way to make a feed of somebody else's work.

**If `subroutine calendar create` says it has no address to give you**, the instance has not
been told its own — ask whoever runs it to set `public_url`, then reset the feed. It is not
broken; it simply cannot say where it lives. And if the command is refused outright, feeds may
be turned off on that instance, which is a decision its operator is entitled to make.

## Which is which, if you have lost track

- **`subroutine doctor`** — what this machine's installation is, and whether it holds together.
- **`subroutine connections`** — every instance this machine reaches, and where each token came
  from. No token is ever printed.
- **`subroutine whoami`** — who you are on the instance you are pointed at, and which versions
  of the plugin, the program and the instance are in play. It says so when two of them disagree.
- **`claude mcp list`** — whether your editor actually started a Subroutine server, which is a
  different question from whether the plugin installed.
- **`subroutine explain connecting`** — the short version of this page, without leaving the
  terminal.
