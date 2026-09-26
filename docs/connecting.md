# Connecting to Subroutine

There are seven ways to reach a Subroutine instance, and the one that is right for you follows
from two questions: **where does your work live**, and **who is asking for it** - you in a
browser, you at a terminal, an agent in your editor, or the calendar application you already
keep your week in.

This page is organised by the answer. Find yourself in the table, read that one section, and
ignore the rest. [docs/hosting.md](hosting.md) is the other end of most of them - it is for
whoever is standing the server up, and if that is also you, read it first.

| Your work lives | You are | Read |
| --- | --- | --- |
| On a server | In a browser, or as an app on a phone | [Just a web page](#just-a-web-page) |
| On this machine | At a terminal | [Just this machine](#just-this-machine) |
| On a server | At a terminal | [Your terminal here, your work there](#your-terminal-here-your-work-there) |
| On this machine | An agent in your editor | [An agent, on the machine holding the work](#an-agent-on-the-machine-holding-the-work) |
| On someone else's server | An agent in your editor | [An agent, with nothing installed](#an-agent-with-nothing-installed) |
| On a public server | Claude on the web | [Claude on the web](#claude-on-the-web) |
| Anywhere | Your calendar application | [Your work in your calendar](#your-work-in-your-calendar) |

**Wherever a token is involved, [Where your token is
kept](#where-your-token-is-kept-and-what-removes-it) compares the places it can live**, and what
can take it away - which, for a token in a plugin's field, includes signing out of Claude Code.

**Two of these can be true at once and that is normal.** Your own list on this laptop and your
team's on a server is one arrangement, not two - `subroutine agenda` asks every instance you can
reach and merges the answers, so the dentist and the stand-up land in one list. Reading spans
everything; only writing has to pick.

## Just a web page

**Nothing to install, and nothing to configure.** If somebody runs an instance and has given
you an account on it, they can hand you a **sign-in link** - one address that signs you in and
then works no more. Open it, and you are in.

Ask them for `subroutine login link --username <you>`. What arrives looks like this:

```
https://subroutine.example.com/signin?link=…
```

**It is good for half an hour and works once.** If it has gone stale by the time you get to it,
that is ordinary - ask for another.

**After that the browser stays signed in while you keep using it.** A fortnight of not touching
it ends the session and you sign in again the same way; using it puts the fortnight back. So a
browser you open most days never asks again, and a device somebody else has stops working two
weeks after they took it.

**Treat it like a password while it is alive**, because for those thirty minutes it is one:
anybody who has the link can become you. It travels in a web address, so it reaches whatever
carried the message, and it is worth not pasting into anywhere that keeps a history.

**A token is not a substitute.** If you have been given something starting `sr_`, that is for a
terminal or an agent, and pasting it into a browser will not sign you in. Ask for a link
instead - they are different credentials for different doors, and having one does not get you
the other.

**What you can do there**: read and add work, change what an item says, move it through its
statuses, comment, link items together, and search. If you also want a terminal or an agent,
every section below still applies and you want a token as well as a link.

**Unless your account is a *viewer*, in which case you read and nothing else.** The page is
drawn from what you are allowed to do, so the controls for writing are **absent** rather than
present and refused - no capture box, no *Complete*, no *Edit*, no comment box. If that is not
what you expected, it is a role and not a fault: ask whoever set you up.

**A read-only account is also how a wall display or a kiosk is done**, which is worth knowing if
you are the one being asked to set a screen up rather than the one using it. That is in
[hosting.md](hosting.md#a-screen-that-only-reads), because it is the instance owner's end.

### On a phone or tablet

**The same address installs as an app.** You get an icon on the home screen, a window of its
own with no address bar and no tabs, and an entry in the app switcher beside everything else.
There is nothing to download from a store and nothing to sign up for - the instance serves what
the browser needs to do it.

**Sign in first, then install**, and it takes five steps:

1. **Get a sign-in link** - `subroutine login link --username <you>`, run wherever the instance
   is. It works once and lasts half an hour.

2. **Open the browser you want the app in, and paste the link into its address bar.** Copy it
   rather than tapping it: a tapped link opens in whatever the device treats as the default
   browser, and the link is used up by whichever browser opens it.

3. **Check you can see your work.** The app inherits this browser's session, so this is what
   there is to get right.

4. **Install it from the browser's own menu.** It is called **Install**, **Install app** or
   **Add to Home Screen** depending on the browser, and it is never a control on the page.

5. **Open it from the home screen.** It comes up signed in, in a window of its own, with no
   address bar.

**If your browser's menu offers nothing, try another one.** Browsers differ about when they
will install a page, and about what they make when they do - some write a bookmark that opens in
a tab rather than an app with a window of its own. There is nothing to change on this end and
nothing you have done wrong.

**It needs the network, exactly like the page does.** Everything you see comes from the
instance, so there is nothing stored on the device and no offline mode.

**Use a browser that remembers you.** Firefox Focus and anything else that erases cookies when
you leave will need a fresh sign-in link every time, and a link works once and lasts half an
hour.

**What we have checked ourselves**: Brave on an Android tablet installs it and it works, and a
real Chromium reads the manifest without complaint. Other browsers we have not tested - tell us
what yours does, either way.

**Signing out is the same as in a tab**: **Sign out** in the menu under your name, or ask whoever
runs the instance to run `subroutine login revoke` for you, which ends every browser and every
installed app you are signed in on at once. **Removing the icon does not sign you out.**

## What to ask for, if somebody else runs the instance

Three things, and the third is often forgotten:

1. **The address.** For a terminal, the instance's base address -
   `https://subroutine.example.com`. For an agent, the same with `/mcp` on the end.
2. **A token.** It starts `sr_`. It says who you are, so what you file is attributed to you
   rather than to whoever set the server up, and it decides what you are allowed to do. It is
   shown once, by the person issuing it, and stored nowhere - so if it is lost, the answer is a
   new one rather than a lookup.
3. **The workspace, if the instance holds more than one.** A workspace is a wall between two
   bodies of work - a client, a company, a side project. Most instances have one and you will
   never hear the word. On an instance with several, a session that has not been told which one
   it is in has its first read refused, and an agent has no way to guess.

Nothing else. There is no account to create on your side, no key to exchange, and no
configuration file you have to write by hand.

## Where your token is kept, and what removes it

**A token can be kept in seven places, and they differ in what reads it and in what can take it
away.** The sections below set most of them up. This one compares them, so that the choice is
made on purpose rather than by whichever section you read first.

| Kept in | Put there by | Read by | Removed by | Choose it for |
| --- | --- | --- | --- | --- |
| `credentials.toml`, in Subroutine's configuration directory | `subroutine connections add`, or `--store` on `token create` and `agent create` | the terminal, and the `subroutine` plugin's tools | only you, by editing the file | yourself at a terminal, and one agent for the whole machine |
| A project's `.claude/settings.local.json` | `subroutine agent create <name> --here` | everything Claude Code starts in that project: the agent's shell and the `subroutine` plugin's tools | deleting the file, or `git clean -x` in that checkout | an agent of that project's own |
| `SUBROUTINE_TOKEN_<CONNECTION>` in the environment | you: a shell profile, a CI secret, a container | whatever is started with it | whatever set that environment up | scripts, CI and containers |
| A password manager, through `token_command` | `subroutine connections add --token-command` | the terminal, and the `subroutine` plugin's tools | your password manager | a token you already keep in one |
| The `subroutine` plugin's *Agent token* field | `/plugin`, in a Claude Code terminal session | that plugin's tools only | **Claude Code**, in three ways, below | little: leave it empty |
| `subroutine-remote`'s *Your token* field | `/plugin`, in a Claude Code terminal session | that plugin's tools only | **Claude Code**, in the same three ways | a machine with nothing of Subroutine installed |
| Claude Code's own list of servers, in `~/.claude.json` | `claude mcp add --transport http … --header "Authorization: Bearer sr_…"`, as [hosting.md](hosting.md) gives it | that one server's tools | `claude mcp remove`; signing out leaves it | a client without the plugin, where plain text in that file and the token in your shell's history are acceptable |

**Claude Code keeps a plugin's token field, not Subroutine**, beside its own sign-in - on Linux
and Windows, in a file in your home directory, `.claude/.credentials.json`. And Claude Code
deletes it:

- **when you sign out of Claude Code** - `/logout`, `claude auth logout`, or signing out in
  your editor. That removes every plugin's token at once, and signing in again does not bring
  any of them back;
- **when you uninstall the plugin**, even with `--keep-data`;
- **when you remove the plugin's marketplace**, for every plugin in it.

**Updating the plugin keeps it.** We checked every update to the plugin published from 21 to 23
September 2026, on four versions of Claude Code, and the token was there after each one and was
sent as before.

**What an emptied field looks like depends on the plugin.** `subroutine-remote` sends no token,
so every call is refused and Claude Code reports *Server rejected the configured `Authorization`
header (HTTP 401)*, followed by the instance's own sentence - on a current instance, *No token
came with this request*. Enter the same token again with `/plugin` in a Claude Code terminal
session. Ask for a new one only if you no longer have it. **The `subroutine` plugin goes on
working**, as whoever this machine's own credentials name for the connection, which may be you
rather than the agent the token belonged to. It says so, in `subroutine_whoami` and in the
answer to its first write, the first time it starts after the field was emptied.

**So keep an agent's identity out of the plugin's field.** Give each project an agent of its own
with `--here` ([A different agent in each project](#a-different-agent-in-each-project)), or the
whole machine one with `agent create --store`. Neither place is touched by anything Claude
Code does to its plugins or to your sign-in, and both reach the agent's shell as well as its
tools, where the field reaches the tools alone.

**With `subroutine-remote` the field is the only place that plugin reads.** Keep the token
where you keep your passwords, so that entering it again is a paste rather than a request. If
this machine can have [uv](https://docs.astral.sh/uv/getting-started/installation/), the
`subroutine` plugin reaching the instance as a connection is sturdier: [Your terminal here,
your work there](#your-terminal-here-your-work-there) stores the token in `credentials.toml`,
and [With subroutine-remote](#with-subroutine-remote) is how to switch.

**Nothing shows you a plugin's field.** Claude Code leaves a token field out of `/config`,
whether it is set or not. `claude mcp list` says whether each server connected, and
`subroutine_whoami` says who the tools are. Those two answer the question without anybody
reading a credential.

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
your own data directory, made by `init`. There is no server, no port, no token and no login -
the file permissions on that database are what protect it.

**You know it worked** when `subroutine` prints the task you just added.

**If it does not:** `subroutine doctor` prints where this installation keeps its configuration,
its database and its state, and says whether they are coherent. Run it before believing anything
else about the machine.

You will not meet the words *workspace*, *instance* or *connection* on this path, and you never
have to. They are what the next four sections are about.

## Your terminal here, your work there

**Somebody runs Subroutine on a server - your company, or you on a machine that is always on -
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
instance knows you by** - which is the only thing that confirms you pasted the token you meant
to, since a token carries no clue about whose it is.

**What it needs:** the program on this machine, the address, and a token from whoever runs the
instance.

**You know it worked** when `subroutine list` shows the server's work:

```console
$ subroutine list
  Local
              #1  Pay the gas bill

  work
    work/acme/#1  Fix the deploy script
```

**If this machine has nothing of its own, it looks different and that is right.** Somebody
given a token who installs the program and never runs `init` has one connection, so there is
nothing to tell apart and nothing to prefix - the list is bare, with no `Local` heading:

```console
$ subroutine list
   #1  Fix the deploy script
```

`connections add` says so at the time, on a line the transcript above does not have because
that machine had a list already:

```console
New work goes to work now, because this machine has no list of its own - and nothing here
will look for one.
```

**The name - `work` here - is yours.** It becomes the first part of every address that
instance's items print as, and two people reaching one server may call it different things.

**Your own database does not go anywhere.** It is a connection too, called `local`, and it is
still where `subroutine add` files things - unless this machine has no list of its own, in which
case `connections add` points writes at the server and says so. `subroutine use work` moves them
either way, and it never changes what you can *see*: reads always span everything you can reach,
which is what makes switching safe.

**If it does not:** `subroutine connections` lists what this machine reaches and, for each,
which of the four places its token came from. It is worth knowing about because it stays out of
`subroutine --help` until a second connection exists - which is to say, until the thing you are
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
use and caches it - roughly five seconds once, then a fraction of a second. Nothing is
permanently installed and nothing has to be on your `PATH`.

**Already ran `uv tool install subroutine`?** That copy is used instead of a download, so the
two arrangements do not fight. **Running from a checkout or a virtualenv?** The plugin cannot
point at it - `uvx` takes the package name as its first argument and there is no way to omit
that - so use `claude mcp add subroutine -- /path/to/subroutine mcp` instead, which is better
for development anyway: the plugin's copy is cached and lags until you refresh it.

**And Git, for the third command.** The marketplace is a repository and `claude plugin
marketplace add` clones it, so without a `git` binary that step refuses before anything of ours
runs. A machine already set up to install Python packages nearly always has it.

**This one runs a program on your machine, so it does not work in a browser.** claude.ai cannot
start it, because there is nothing on that side to start anything on. The plugin still installs
and still reports success, and the only sign of a problem is an absence - so it is worth knowing
in advance rather than diagnosing.

**Claude Code is tested; a desktop app that can start a local program is not.** We have not
driven that combination, and this page used to say we had. It is the only route we know of for
somebody who will not use a terminal, so it is worth trying and worth telling us about - but do
not plan an afternoon around it on our word.

**You know it worked** when `claude mcp list` shows the server connected, or when you ask the
agent to run `subroutine_whoami` and it answers. Installing a plugin and starting its server are
separate moments and only the first one reports, so the second is worth checking once.

**If you keep more than one instance** - your own and a client's, say - the plugin's *Which
instance* field takes the name of a connection you have already set up, and the section above is
how you set one up.

**One command gives each project's agent a name of its own**, instead of yours - [A different
agent in each project](#a-different-agent-in-each-project).

## A different agent in each project

**Give each repository you work in an agent of its own**, so that work done in `web` is recorded
as the web agent's, is kept to that project, and can be revoked without touching the rest.

**Until you do, an agent works as you.** Everything it files, closes and comments on is recorded
under your name - or under this machine's agent, if one was recorded with `agent create --store`.

**What it needs:** Claude Code and the `subroutine` plugin from [An agent, on the machine holding
the work](#an-agent-on-the-machine-holding-the-work); the `subroutine` program, reaching the
instance as you, which [Your terminal here, your work there](#your-terminal-here-your-work-there)
sets up for a server; and the right to make accounts there, which `subroutine whoami` lists as
`instance:user_create`. Without that, [somebody who has it makes the
agent](#if-somebody-else-makes-the-agent).

It works whether the work is on this machine or on a server you reach as a connection. **With
`subroutine-remote` it reaches only the shell.** That plugin starts nothing: the editor sends the
plugin's token itself, and a plugin's settings apply to every project at once, so its tools go on
as that token whatever the project says. The `subroutine` plugin runs the program, which reads the
project's own settings - which is why this uses them, and why [switching the project to
it](#with-subroutine-remote) gives the tools the agent too.

### Ask your agent

In a Claude Code session in the project, ask for it: *give this project an agent of its own*. The
plugin's skill offers it anyway, the first time it finds itself working as you. The agent runs the
command below, says what it changed, and asks you to reload the window. The credential never
passes through its conversation, because the command prints nothing secret.

**If the session already acts as an agent** - this machine's, from `agent create --store`, say -
the new agent answers to that one, and a question it hands back reaches it before you. That can
only happen where the agent may make accounts. `agent create` then says so, and prints the one
command that makes you the parent instead, which only a person can run:

```console
Account parent: claude. Answers to jo.
A question it hands back goes to claude first. To take it on,
jo runs 'subroutine user transfer web --to jo' - an agent cannot.
```

### Or run it yourself

In the project's directory - the one you open Claude Code in:

```console
$ subroutine agent create web --workspace acme --here
Added .claude/settings.local.json to …/web/.gitignore, so git keeps the credential out of the repository. Commit that.
Created service account web, with the contributor role.

Checked, by presenting it: web (agent), in acme (comment:read, comment:write, project:read, task:read, task:write, workspace:read)
Account parent: jo.

Written to …/web/.claude/settings.local.json as SUBROUTINE_TOKEN_LOCAL, readable only by you.
Reload the window, or start a new Claude Code session there, before anything else:
one already open may act as web in its shell and as before in its tools.
Then 'subroutine whoami' names web, and 'subroutine_whoami' does too
where the 'subroutine' plugin runs the tools. Where 'subroutine-remote' runs
them, switch this directory to 'subroutine', then reload:
  claude plugin enable subroutine@subroutine --scope local
  claude plugin disable subroutine-remote@subroutine --scope local
That needs 'subroutine' installed here, and uv; docs/connecting.md has the rest.
```

- **`web`** is what the agent is called. Naming it after its project keeps the pair obvious.
- **`--workspace`** is the workspace the project is in - `whoami` lists yours - and pins the
  credential to it. It goes after `create`, spelled out in full.
- **`--here`** does the rest. It writes the credential into this directory's
  `.claude/settings.local.json`, under the variable Subroutine reads for this connection. Claude
  Code gives that file's `env` to everything it starts in the project - the agent's shell as well
  as the `subroutine` plugin's server - so one line covers both of the ways an agent reaches an
  instance, and [an agent that can also run a shell](hosting.md#an-agent-that-can-also-run-a-shell)
  is why both matter. It makes the repository ignore the file, adding the line to `.gitignore` if it needs
  one, and prints nothing secret. **If it cannot finish, it refuses before a credential is made** -
  a settings file that is not valid JSON, say, or one the repository already tracks - leaving at
  most the line it added to `.gitignore`. If the settings file cannot be written once the
  credential exists, it shows you the credential rather than lose it.
- **`Account parent`** is who answers for the agent: whoever ran the command, which here is you.
- **As written, the agent can do what its account's role allows anywhere in that workspace.** To
  keep it to its own project, add `--profile worker --project web`; to let it read a neighbour and
  change only its own, `--profile collaborator --project web --project api --write web`. [Saying
  what the credential is for](hosting.md#saying-what-the-credential-is-for) has all four profiles,
  and `--title` names the credential if `web agent` is not what you want to read later.

**Run it again to replace a credential that was lost or seen.** It gives the agent a new one, names
the one it replaced, and says how to revoke that.

### With subroutine-remote

**`subroutine-remote` cannot take a project's credential, so switch the project to the
`subroutine` plugin, which can.** Claude Code's own commands do it.

**Once on the machine**, install the `subroutine` plugin and turn it off, so every other project
keeps `subroutine-remote`:

```console
$ claude plugin install subroutine@subroutine
$ claude plugin disable subroutine@subroutine
```

**Then in the project's directory**, with `agent create --here` before or after:

```console
$ claude plugin enable subroutine@subroutine --scope local
$ claude plugin disable subroutine-remote@subroutine --scope local
```

Then reload the window. `--scope local` writes both choices into the project's
`.claude/settings.local.json` and installs nothing, so `claude plugin update subroutine@subroutine`
still updates the plugin once for the whole machine.

**Or move the whole machine to it**, which is simpler when most projects will have an agent of
their own: every project then works the way the rest of this section describes, with nothing to
switch per project.

```console
$ claude plugin install subroutine@subroutine
$ claude plugin disable subroutine-remote@subroutine
```

**The catch is every project without an agent of its own.** Its tools stop presenting the token
you gave `subroutine-remote`, and present the `subroutine` plugin's own `token` option instead -
or, with that left blank, the program's credential for the connection, which is this machine's
`--store` agent where there is one. Give the option a token for the same account to keep those
projects as they were, and ask `subroutine_whoami` in one of them afterwards.

**The `subroutine` plugin needs uv**, since it starts the program with `uvx` - [An agent, on the
machine holding the work](#an-agent-on-the-machine-holding-the-work) has the rest. With its
options left blank it uses this machine's default connection, so run `--here` against that one:
`subroutine connections` says which it is.

### Check it

**Reload the window, or start a new session there, before the agent does anything else.** A
session already open may take the file up in its shell straight away while its tools go on with
what they started with, so until the reload it is two people at once - and in an editor, a new
conversation may not be a new session. Then ask the agent to run `subroutine whoami` in its
shell, and to call `subroutine_whoami` through its tools. **Both must name the project's agent**,
on their first line - the tools only where they come from the `subroutine` plugin:

```console
$ subroutine whoami
web (agent), via token 'web agent' (11117b8a…).
Account parent: jo.
Narrowed to workspace 'acme'.
```

**Both, because they are separate.** The shell and the plugin's server each find a credential on
their own. One right and the other wrong puts half the agent's work under another name - and a
spot check that finds the right name in one place calls that a success.

**If either names somebody else**, ask the agent to run `subroutine connections` in that session.
The project's variable is named there when it arrived:

```console
$ subroutine connections
local  sqlite:///…/subroutine.db  SUBROUTINE_TOKEN_LOCAL  in use, default
```

Anything else in that column means the session never received it: it was opened in another
directory than the one holding `.claude/`, or it has not been reloaded since the file was written.
**If the shell names the agent and the tools do not**, the tools are still running with what they
started with, and reloading the window restarts them. **If a reload changes nothing**, the tools are
`subroutine-remote`'s: `subroutine_whoami`'s version line names a plugin and no program, and that
plugin presents one token in every project. [Switching the project to the `subroutine`
plugin](#with-subroutine-remote) is the fix.

**Then ask the same in a session anywhere else** - your home directory will do. It should name
you, or this machine's `--store` agent: the credential stays where it was put. Whoever it names is
who the agent is in every project you have not set up this way.

### If somebody else makes the agent

Without `instance:user_create`, ask whoever runs the instance to run `subroutine agent create web
--workspace acme` - without `--here`, since the credential has to travel - and to send you what it
prints. Then do by hand what `--here` does, in the project's directory:

1. **Make the repository ignore the file.** Add `.claude/settings.local.json` to its `.gitignore`
   and commit that. `git check-ignore -v .claude/settings.local.json` then prints a line beginning
   `.gitignore`. A rule anywhere else - `.git/info/exclude`, or a file in your home directory -
   protects this clone or this machine, and not the repository.

2. **Write the file.** Run `mkdir -p .claude`, then open `.claude/settings.local.json` in an
   editor. If it is new, this is its whole content, with both placeholders replaced; if it holds
   settings already, add the `env` entry beside them:

   ```json
   {
     "env": {
       "SUBROUTINE_TOKEN_<CONNECTION>": "sr_…"
     }
   }
   ```

   `<CONNECTION>` is **your** name for the connection, as `subroutine connections` lists it,
   upper-cased, with anything that is not a letter or a digit as an underscore: `work` makes
   `SUBROUTINE_TOKEN_WORK`, and `my-work` makes `SUBROUTINE_TOKEN_MY_WORK`. **Any other name is
   ignored without a word** - the one in the administrator's output included, which is named for
   their machine. Use an editor rather than a command, which would keep the credential in your
   shell's history.

3. **Check it, and keep it private**, without printing it:

   ```console
   $ python3 -m json.tool .claude/settings.local.json > /dev/null && echo valid
   valid
   $ chmod 600 .claude/settings.local.json
   ```

   A network drive may ignore `chmod`, and then anybody who can read the checkout can read the
   credential.

Then check it as above.

**It answers to whoever made it**, which the *Account parent* line in what they sent you names.
For it to answer to you, they run `subroutine user transfer web --to <you>` - only a person who
may make accounts can.

### A checkout more than one machine opens

On a shared drive, every machine that opens the checkout reads the same file, and `--here` names
the variable after **this** machine's name for the connection. Where another machine calls the
connection something else, add a line for that name as well, as in step 2 above.

### What it does not do

- **It is a record and a bound, not a wall.** The project's credential makes the right name the
  default and bounds what that name can touch, and [Giving an agent a
  token](hosting.md#giving-an-agent-a-token) is where the bound is set. It cannot stop an agent
  that runs commands from finding a different credential and presenting that instead.
- **An agent kept to one project cannot write anywhere else**, a comment on another project's
  item included. Where one agent's work genuinely spans two, give it `--write` for each rather
  than widening it back to everything.
- **Only what Claude Code starts in that project reads the file.** A terminal or a scheduled job
  it did not start, and `subroutine-remote`, go on as before.

### To undo it

- **Remove the `env` entry and start a new session.** That project goes back to acting as you, or
  as this machine's agent.
- **If you switched plugins**, remove the `enabledPlugins` entries from the same file, and the
  project goes back to the plugins the rest of the machine uses. A whole machine goes back with
  `claude plugin enable subroutine-remote@subroutine` and
  `claude plugin disable subroutine@subroutine`.
- **`subroutine token list`** shows each credential's prefix, whose it is and when it was last
  used, and **`subroutine token revoke <prefix>`** stops one working everywhere at once.

## An agent, with nothing installed

**Somebody else runs Subroutine, they have given you an address and a token, and you want your
agent working against it this afternoon.** This is the freelancer's case, and it needs nothing
on your machine at all - no Python, no package, no configuration file.

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine-remote@subroutine
```

Then fill in two fields - the address, ending in `/mcp`, and your token. **In a terminal**, run
`claude`, then `/plugin` inside the session, and choose the plugin. Once set they are read by
every session, editor included.

**That terminal is not optional, and this is the step that catches people out.** `/plugin` is
not available in the VS Code extension, and `claude plugin` has no `configure` subcommand -
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

**Two things to know before you do.** That file is not a secret store - your token sits in it
in plain text, which is the same trade as `credentials.toml` and worth a deliberate decision
rather than a discovery. And this is where the values *land* rather than a documented
interface, so `/plugin` is the route that will keep working. Verified on Linux with the
editor reading a plugin configured exactly this way. A token written there also survives
signing out of Claude Code, which one entered with `/plugin` does not.

**Then reload the window, or start a new session.** MCP servers are attached when a session
begins, so one that was already open when you configured the plugin keeps the tool list it
started with - everything will look correctly set up and there will be no tools.

**You know it worked** when `claude mcp list` shows the server connected *and* the agent can run
`subroutine_whoami`. Check both: a session that predates its configuration shows `✔ Connected`
and has no tools, which reads as a broken product and is not one.

**What it needs on the instance's side:** an address and a token. That is the whole list, and it
is the part somebody else hands you.

**On your own machine you need Claude Code and Git** - the marketplace is a repository, and
`claude plugin marketplace add` clones it. Nothing of Subroutine's is installed: no Python, no
package, no configuration file of ours.

**Your editor connects from *this* machine**, so an instance on your own network or behind a
VPN is as reachable as a public one. Claude Code keeps the token, not Subroutine, beside its
own sign-in - on Linux and Windows, in a file in your home directory. **It deletes the token
when you sign out of Claude Code**, uninstall the plugin or remove its marketplace, so keep a
copy where you keep your passwords - [Where your token is
kept](#where-your-token-is-kept-and-what-removes-it) has the rest. Treat it as you would any
password; if it is exposed, ask for a new one rather than moving this one somewhere safer.

**If your token reaches more than one workspace, the address has to say which one.** A token
pinned to one workspace needs nothing here, however many the instance holds - whoever issues it
chooses, with `subroutine token create --workspace`. Otherwise put it on the end:

```
https://subroutine.example.com/mcp?workspace=acme
```

Without it, the agent's first read comes back refused - *"This request could be about any of
several workspaces, so it needs to say which"* - with the workspaces it can reach listed. It can
recover by naming one on every call, but it will do that for the whole session and the next
session will start over. One word in the address settles it permanently. **Ask which workspace
your work belongs in at the same time as you ask for the token.**

**You know it worked** when the agent can answer "who am I on this instance?" - it has a
`subroutine_whoami` tool for exactly that, and the answer names the account the token belongs to
and the workspaces it reaches.

**If it does not:** an empty address is *not* an error. The plugin sits idle and this session
simply has no Subroutine tools, so it can be installed before anybody has told you where to
point it. A wrong token or a wrong address both report clearly in the editor. **A token that
worked and is now refused may have been emptied rather than revoked** - signing out of Claude
Code does that, above - so enter it again first, and ask for a new one only if that is refused
too.

**This one is tested in Claude Code and nowhere else.** The transport is different from the
section above - this one needs nothing installed - but the same caution applies to where it
runs. It does not run on the web, which is structural. Whether a desktop app will take an HTTP
plugin configured with a pasted token we have not driven, and the section on
[Claude on the web](#claude-on-the-web) is why we doubt it.

### Another MCP client

The plugin is a convenience, not the mechanism. The instance speaks MCP itself over ordinary
HTTP, so any client that takes a URL and a header can reach it: point it at the same `/mcp`
address with `Authorization: Bearer sr_…`. There is no session to establish and nothing to
install on either side.

**What such a client does not get is the plugin's skill** - the working practice for using this
well, which ships with the plugin rather than with the instance. The instance offers four
documents as MCP resources instead: a guide written for an agent arriving with nothing, worked
examples, this installation's own vocabulary, and the decisions this workspace has taken. Those
are enough to work from. The skill is the part that says how to work *well*, and it ships with
the plugin - so it reaches wherever the plugin does, which is Claude Code for certain and the
desktop apps untested (above).

## Claude on the web

**You want your instance in claude.ai, or in a desktop app talking to it directly, as a
connector.** This is not built yet, and saying so is more useful than a page that implies
otherwise.

It is a different problem from the ways above rather than a bigger one. A connector's traffic
comes *from Anthropic's servers* rather than from your machine, so:

- the instance has to be reachable from the public internet - a laptop or a machine behind a
  VPN can never be one;
- the credential cannot be a token you paste, because it is not your machine holding it.

That makes it an authorisation flow rather than a field in a settings box, which is why it is
its own piece of work rather than a variation on the section above.

**Until then, there is one route and it is not the one that looks easiest.** The two plugins
work differently, they are not interchangeable, and only one of them has been driven end to end
by us:

| | What it is | Where it is known to work |
| --- | --- | --- |
| [An agent, with nothing installed](#an-agent-with-nothing-installed) | an HTTP server, reached with a token you paste | **Claude Code - tested.** Not the desktop apps: a connector there wants an authorisation flow rather than a pasted token, which is the whole of what is unbuilt above. |
| [An agent, on the machine holding the work](#an-agent-on-the-machine-holding-the-work) | a program started on your own machine | **Claude Code - tested.** A desktop app that can start a local program, **untested by us** - if you try it, we would like to know. |

**So somebody who does not use a terminal has one path**: install `uv`, then the local one. It
is a real path and it is not a nothing-to-install path, and this page said otherwise until
2026-08-27 - which is the sentence somebody would have spent an afternoon on. A plugin that
cannot connect reports success and shows no tools, so the failure is an absence rather than an
error and nothing would have said what went wrong.

**Marked as untested rather than quietly asserted.** Everything else on this page is a command
somebody ran; a claim about what another program can do is the same promise one step out, and
this page has no way to keep it. Saying which half we have driven is worth more than a sentence
that is confidently wrong.

## Your work in your calendar

**A seventh way in, and the only one that is not really a way *in*.** Anything here with a date
can appear in Google Calendar, Apple Calendar, Outlook or Thunderbird, beside the rest of your
week - so a deadline you filed at a terminal turns up on your phone without you doing anything
else about it.

You need a terminal once, to make the subscription:

```
subroutine calendar create "My work"
```

That prints one address ending `.ics`. Paste it into whatever you keep your diary in, under
whatever it calls *subscribe to a calendar* or *add by URL*. From then on it updates on its own,
every quarter of an hour or so, and you never touch it again.

**Make it with a credential nothing narrows.** A feed reads with its owner's own sight rather
than with the narrowing on the credential that made it, so one narrowed to a project, to some
permissions or to one workspace is refused - *"A bounded credential cannot mint a calendar
feed"* - and one that expires can only make a feed that stops no later than it does. Narrow the
feed itself instead, as below.

**Nothing comes back.** Moving an event in your calendar changes nothing here, and deleting one
there does not complete anything. That is the trade for it working in every calendar
application without an account: the feed is a **copy**, kept up to date, and the work still
lives here.

**The address is a password.** Anybody who has it can read everything the feed shows, for as
long as it works, and nobody here can tell that they are - a fetch from somewhere unexpected
looks exactly like one from your phone. So paste it into the calendar application and nowhere
else, and if it gets out:

```
subroutine calendar reset <reference>
```

which gives that subscription a new address and stops the old one that instant. The
subscription keeps its name and its scope; you re-add it in your calendar and carry on.
`subroutine calendar revoke <reference>` stops one for good.

**It is shown once.** Nothing recovers it afterwards, including the instance - what is kept is
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

**What shows up**: an item's start, an item's deadline, and both where it has both - the day you
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
been told its own - ask whoever runs it to set `public_url`, then reset the feed. It is not
broken; it simply cannot say where it lives. And if the command is refused outright, feeds may
be turned off on that instance, which is a decision its operator is entitled to make.

## Which is which, if you have lost track

- **`subroutine doctor`** - what this machine's installation is, and whether it holds together.
- **`subroutine connections`** - every instance this machine reaches, and where each token came
  from. No token is ever printed.
- **`subroutine whoami`** - who you are on the instance you are pointed at, and which versions
  of the plugin, the program and the instance are in play. It says so when two of them disagree.
- **`claude mcp list`** - whether your editor actually started a Subroutine server, which is a
  different question from whether the plugin installed.
- **`subroutine explain connecting`** - the short version of this page, without leaving the
  terminal.
