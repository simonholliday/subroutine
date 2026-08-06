# Connecting to Subroutine

There are five ways to reach a Subroutine instance, and the one that is right for you follows
from two questions: **where does your work live**, and **who is asking for it** — you at a
terminal, or an agent in your editor.

This page is organised by the answer. Find yourself in the table, read that one section, and
ignore the rest. [docs/hosting.md](hosting.md) is the other end of most of them — it is for
whoever is standing the server up, and if that is also you, read it first.

| Your work lives | You are | Read |
| --- | --- | --- |
| On this machine | At a terminal | [Just this machine](#just-this-machine) |
| On a server | At a terminal | [Your terminal here, your work there](#your-terminal-here-your-work-there) |
| On this machine | An agent in your editor | [An agent, on the machine holding the work](#an-agent-on-the-machine-holding-the-work) |
| On someone else's server | An agent in your editor | [An agent, with nothing installed](#an-agent-with-nothing-installed) |
| On a public server | Claude on the web | [Claude on the web](#claude-on-the-web) |

**Two of these can be true at once and that is normal.** Your own list on this laptop and your
team's on a server is one arrangement, not two — `subroutine today` asks every instance you can
reach and merges the answers, so the dentist and the stand-up land in one list. Reading spans
everything; only writing has to pick.

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
$ uv tool install subroutine    # or: pipx install subroutine
$ subroutine init
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine@subroutine
```

**What it needs:** Subroutine installed **and on your `PATH`**, which is what `uv tool install`
and `pipx install` guarantee and a virtualenv does not. Your editor starts `subroutine` itself,
so it has to be able to find it. If the tools do not appear, that is nearly always why.

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

Then open its settings and fill in two fields: the address, ending in `/mcp`, and your token.

**What it needs:** an address and a token. That is the whole list.

**Your editor connects from *this* machine**, so an instance on your own network or behind a
VPN is as reachable as a public one. The token goes to your system keychain rather than to a
settings file.

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

It is a different problem from the four above rather than a bigger one. A connector's traffic
comes *from Anthropic's servers* rather than from your machine, so:

- the instance has to be reachable from the public internet — a laptop or a machine behind a
  VPN can never be one;
- the credential cannot be a token you paste, because it is not your machine holding it.

That makes it an authorisation flow rather than a field in a settings box, which is why it is
its own piece of work rather than a variation on the section above.

**Until then**, the plugin in [An agent, with nothing installed](#an-agent-with-nothing-installed)
reaches exactly the same instance from Claude Code and the desktop apps, with the same token, and
needs nothing installed either.

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
