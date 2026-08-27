---
name: subroutine
description: Track work in Subroutine — file tasks, find what can actually be started, record what happened, and write down conclusions the next session will need. **Read this before the first subroutine_* call of a session, including a read-only one.** It carries conventions the tool descriptions do not — how to open so you are not answering from a stale snapshot, how to ask for work that can actually be started, what a title has to say, and which end of a blocks link is the blocker. Also when the user asks what to work on, reports a problem, asks you to log or file something, finishes a piece of work, wonders where something was left, asks whether Subroutine is connected or what is in it, or says this project uses Subroutine — and for adopting Subroutine in a project that does not use it yet.
---

# Working with Subroutine

Subroutine is a task and project tracker that a person and an agent use as equals. What you
write in it is attributed to you, addressable by a number, and still there after your context
is gone — which is the whole reason to spend calls on it.

**If the `subroutine_*` tools are not available, stop and say so — and establish which plugin
is installed before diagnosing anything.** There are two, they fail for different reasons, and
every remedy for one is a wasted evening on the other. Four causes, and one command separates
them:

```
claude mcp list
```

- **`subroutine`** starts Subroutine as a program on the machine your client runs on, so it
  needs the program installed there.
- **`subroutine-remote`** connects to a server by address and installs nothing, so it needs an
  address and a token.

**Rule out the browser first, because it is the only cause nobody can fix.** A client running on
claude.ai has no machine to start a program on, and it does not read either of these plugins at
all — reaching an instance from there is a *connector*, set up in Claude's own settings against
a publicly reachable address. So on the web the tools are absent by construction, however
anything is configured, and every remedy below is wasted effort. Say that plainly rather than
beginning a diagnosis; Claude Code and the desktop apps are where these work.

**If `claude mcp list` says the server is connected and this session still has no tools, the
session started before the plugin was configured.** Reload the window, or start a new session.
MCP servers are attached when a session begins and are not rebuilt when configuration changes
underneath them, so a session opened before somebody filled in the address and token keeps the
tool list it was born with — **which is the ordinary state in the minutes right after anybody
sets this up**, and therefore the likeliest moment to meet it.

Check this before anything below. It is the cheapest remedy and the only one where every other
signal says the setup is fine: `claude mcp list` runs in a *new* process, so it reports the
configuration as it is now, correctly, while describing a session that no longer matches it. A
`✔ Connected` beside absent tools reads as "connected but broken" and is not — it means "connected
now, and this session predates now".

**With `subroutine-remote`, read the line `claude mcp list` prints and pass it on.** The client
names the actual fault — a token the server rejected, an address with no MCP endpoint on it, a
server that did not answer — and quotes the instance's own words back. That sentence is written
for the person who has to fix it. Do not translate it into a guess, and do not offer any of the
installation advice below: there is nothing to install.

**With `subroutine`, the failure is silent and needs that command to see at all.** Installing a
plugin and starting its server are separate moments and only the first one reports, so "not
installed" and "installed where the editor cannot see it" look identical from in here — no
tools, and no error. `✘ Failed to connect` beside `plugin:subroutine:subroutine` means the
program was not found on the `PATH` the editor passes down, which is nearly always a virtualenv.
Say so, and offer the two ways out: install it as a tool so it is on the `PATH` for good, or
point the plugin at the copy that already exists.

```
uv tool install subroutine     # or: pipx install subroutine
subroutine init
```

`/plugin configure subroutine` takes the absolute path instead — `<venv>/bin/subroutine` — for
somebody who would rather not install it twice. If no server is listed at all, the plugin itself
is not installed or is disabled.

**If the tools are there but every call fails, read what the failure says — it names the
remedy.** This is the ordinary case on a fresh local install and it is not the one above: the
MCP server starts perfectly well against an instance nobody has created yet, so the tools appear
and then refuse. `no Subroutine instance has been set up here yet.` means the user needs to run
`subroutine init` once; a schema message means `subroutine db upgrade`. Pass the failure on
verbatim rather than diagnosing it — the sentence is written for them.

**On a server somebody else runs, the same rule applies and the remedies are not yours to
offer.** A refusal naming a permission means the token you were given does not carry it, and an
ambiguous-workspace refusal names the workspaces it could have meant — which the person who
gave you the address can put right by adding `?workspace=` to it. Report the sentence and who
needs to act on it; do not attempt to reconfigure someone else's instance.

**Whichever of these it is, do not work around it by writing to a file instead.** A tracker
nobody can reach is worse than an honest failure, and a to-do list in a scratch file is a
tracker nobody can reach.

`GET /v1/docs/agent` on any served instance is the reference for the HTTP API. This page is
about *when* to reach for it and what good practice looks like — it does not repeat the API.

## Adopting Subroutine in a project that does not use it yet

If the user has just installed Subroutine, or says "we use Subroutine now", set the project up
before doing anything else. **Ask only what cannot be undone; state the rest and proceed.** A
setup interview is how a tool loses the person who just installed it.

1. **Look before creating.** `subroutine_project` with no arguments. If something already
   covers this repository, use it. A duplicate project is invisible until somebody files into
   the one nobody reads.

2. **Propose a key and say what it is.** Lower case, letters and digits, and **a hyphen between
   words** — `claude-test` rather than `claudetest`. It starts with a letter and is at most 32
   characters. Derive it from the title, or from the repository directory name where that reads
   better. State it and proceed — a key can be renamed later, and a checkout marked with step 6
   follows the rename, so this is not a decision to stop over.

   The hyphen is worth spending a character on: a key is a path segment and will be part of a
   URL, so it is read far more often than it is typed.

   ```
   subroutine_project(key="website-redesign", title="Website redesign")
   ```

   What a rename *does* cost is addresses somebody has already written down — a `+web` in a
   note, a URL, the key in another checkout's marker. The old key then stops working, loudly.
   So it is worth a sentence if they seem to be choosing a name they will regret, and it is
   not worth a question. Renaming is the person's to do, not yours: it is a command that
   counts what will break and asks first.

3. **Do not ask which workspace unless there is more than one.** A fresh install has exactly
   one. If there are several, ask — and say why: **this is the one thing here that cannot be
   undone.** Items are numbered per workspace and a project cannot be moved between them, so
   the wrong workspace means starting again rather than renaming.

4. **Propose a parent, do not ask for one.** Placement in the project tree can be changed
   later, so state where it is going and put it there. Ask only when more than one existing
   project is a plausible parent.

5. **Ask about privacy only when it can matter** — that is, when the instance has more than one
   account. Use `--private` if they want it; the creator keeps access either way, and it can be
   changed later.

6. **Record which project this checkout is, so later sessions do not have to guess.** This is
   the step that makes everything after it reliable — without it, a session starting in this
   directory has no way to tell which of the instance's projects the work belongs to.

   ```
   subroutine use --here --project web
   ```

   **`use` is not listed by `subroutine --help`, and it does exist.** Neither are `claim`,
   `release` or `connections`. They are held back from the first thing a newcomer reads, not
   removed — `subroutine explain connecting` names them. Do not conclude from an absent line in
   `--help` that a command in this page is gone; run it, or ask `explain`.

   **This one needs the command line**, so a session connected to a server by address cannot do
   it. Name the project on each call instead — `+web` in a captured line — and say that a
   marker is worth adding by somebody who has `subroutine` installed in this checkout.

   That writes a small `.subroutine` file at the repository root. Say that you have written it
   and that it is safe to commit — it names a project, not a credential. From then on, work
   added anywhere under this directory goes to that project unless a line says `+other`.

7. **Do not import an existing to-do list unless asked.** Filing thirty items out of a
   `TODO.md` is a large write that is tedious to undo and that nobody requested.

8. **Write the pointer into the project's agent file** — `CLAUDE.md`, `AGENTS.md`, whichever it
   already uses. One line naming the project key is enough. Without it the next session does
   not know adoption happened, and adopts again.

## Day to day

**Open by asking what changed.** Your context is a snapshot and it does not decay — nothing
will tell you that something you read is now stale, so you will answer from it confidently and
be wrong:

```
subroutine_changes()
```

Keep the `seq` it prints last and pass it back as `since` next time. It is inclusive, so you
will see that one again; ignore what you already have. `mine=true` narrows it to what your own
credential did, which is how you pick up your own unfinished work rather than everybody's.

**Know whose credential you are writing with, before you write anything.** Ask once at the
start of a session:

```
subroutine_whoami()
```

It names the account, the credential by its title, what that credential is limited to, and the
versions of everything in play. One machine commonly holds more than one credential — the
person's own, and one per agent — so the answer is not obvious and is not something to assume.
Three answers are worth acting on:

- **A person's name where you expected an agent's** means your work is being recorded as
  theirs. Say so rather than carrying on: attribution is the reason a person hands over work
  they would otherwise supervise, and it is silent when it is wrong.
- **`No workspace here can be read with this credential`** means the credential reaches
  nothing. Every other command will report that as an empty instance, which reads as "there is
  no work" rather than "you cannot see it".
- **A line after the versions** names a mismatch worth acting on. There are two, and they are
  different problems: *the program and the instance disagree* means one of them has a field the
  other does not; *the plugin is older than the program* means this skill and the plugin's
  settings describe an earlier version of these tools.

**That is why the versions are printed at all.** The plugin, the program and the instance
upgrade separately, so you may be holding a tool description written for a program that has not
been updated, or talking to an instance that has. You cannot tell a capability that does not
exist from one that is merely too old from where you sit, and guessing wrongly costs the person
an hour. Report the line as it stands and let them fix it; refreshing any of them is theirs to
do, not yours.

**Three numbers that are not identical is normal, and there is deliberately no line about it.**
The plugin's version moves whenever its own contents change, so it runs ahead of the program
between releases by design. Only the mismatches above are said out loud.

**But silence has two causes and only one of them is agreement.** A version this cannot put in
order — a build from source, `0.8.2.dev45+g1234567` — is not compared at all, and prints nothing,
exactly as agreement does. So no line means *either* nothing is wrong *or* the question could not
be answered. If any of the three carries a `.dev` or a `+`, read the numbers yourself rather than
taking the silence as an all-clear.

Go back to the whole answer whenever a tool does something you did not expect: an argument
refused, a field missing, a capability you have read about here that does not seem to be there.

**And if you can also run shell commands, ask twice.** The tools and the shell resolve
credentials independently — the tools use whatever the plugin was configured with, the shell
uses what the command line finds, and nothing reconciles them. So run this as well:

```
subroutine whoami
```

**Two different answers is a real and common misconfiguration.** It has been measured: one
agent, one session, one connection, writing as a bounded service account through its tools and
as a superuser through its shell. It is worse than plainly acting as the operator, because it
is partial — anyone spot-checking finds the agent's own name on the half that went through the
tools, and concludes the setup worked.

If the two disagree, say so rather than picking one. The fix is the person's and it is one
line — a `SUBROUTINE_TOKEN_<CONNECTION>` in the environment their editor starts from, which
both halves read — but they cannot fix a split nobody has told them about, and you are the only
one positioned to see it.

**Ask what can be started, not what exists.** This is the one thing Subroutine answers that a
list of tasks does not:

```
subroutine_list(ready=true, order="-priority_score")
```

`ready` leaves out anything blocked by unfinished work and anything deferred to a later date.
Without it you get a backlog in priority order, which includes things nobody can act on yet.

**Ask what happened, not only what is left.** A date field takes `.gte`, `.gt`, `.lt` and
`.lte`, and the value is the same date grammar a write accepts:

```
subroutine_list(filter={"created_at.gte": "yesterday"})
subroutine_list(filter={"completed_at.gte": "start_of_week"})
```

Two entries make a range, and it narrows alongside `project`, `ready` and the rest rather than
replacing them. This is the question to ask at the start of a session about work you did in the
last one — `subroutine_changes` answers what *moved*, and this answers what a period contains.

For *worked on* rather than *changed*, ask `touched_at` — it reads the event feed, so a comment
or a status change counts where `updated_at` would say nothing happened:

```
subroutine_list(filter={"touched_at.gte": "yesterday"})
subroutine_list(filter={"touched_at.gte": "start_of_week", "touched_by.eq": "si"})
```

**Take the task before you touch anything, and say when you start.** Two calls around the
work, in this order, every time:

```
subroutine_claim(ref=42)                      first, before any other change
subroutine_update(ref=42, status="in_progress")   when you actually begin
                                              … the work …
subroutine_done(ref=42)                       which hands the claim back with it
```

**This used to say "if anybody else works from this list", and that condition is why nobody
ever did it.** An agent alone on an instance reads it as false — and it was, until it was not.
By the time a second worker exists, the habit needed to have been there already. So it is
unconditional now: you cannot see who else is about to pick this up, and that is the whole
reason the mechanism exists.

**A claim and a status are two different facts, and both are worth saying.** The claim says
*somebody has this right now* — a lease, so `ready=true` hides it from other workers and
`claimed_by` shows your name beside the item. The status says *work has begun*, which is not
the same thing: you may claim an item in order to read it and decide it is not for you, and
then nothing was in progress at all. Nobody derives either from the other.

**It expires by itself, and working on it keeps it alive.** Every write to something you are
holding — an edit, a status change, a comment — pushes the lease out, so an agent that is
working never has to think about it and an agent that stopped stops renewing. Nothing is
stranded if your context ends first, which it will. A claim you find on somebody else's item
may already have run out; you are told who holds it and until when, which is the answer to what
you do next.

**Finishing hands it back**, so there is no separate act at the end. That used to be a third
call and it is gone deliberately: an obligation falling at the end of a session is one nobody
attends, because the end of a session is compaction or a killed process rather than a moment
anybody is present for. `subroutine_claim(ref=42, release=true)` is still there for work you are
putting down without finishing.

**When you need an answer from a person, park the question rather than asking in the
conversation.** A conversation ends and takes the question with it; an item does not.

```
subroutine_update(ref=42, status="needs_input")
subroutine_comment(ref=42, body="Which way round should the flag read? Both work; the second
                                 matches the CLI.")
```

It goes to the top of that person's agenda under *Waiting on you*, and the answer is on the
item when you — or a different agent, days later — come back to it. Then move the status on and
carry on with something else in the meantime; a question you are waiting on is not a reason to
stop.

**Look before you file.** Searching costs one call and a duplicate costs somebody an afternoon
of wondering which of two items is the real one:

```
subroutine_search(q="deploy script")
```

It reads titles *and* what was written about them, and every word you give has to appear —
in any order, in either field. So a half-remembered description finds it. Use it before
creating anything, and whenever the user refers to something you have no record of — it is
usually already there.

**Read one before acting on it.** A listing is titles; `subroutine_show(ref=42)` is the whole
item, with what it is linked to and everything anybody has recorded against it:

```
subroutine_show(ref=42)
```

That record is the point. Somebody — possibly you, last week — wrote down why this was
attempted and what happened, and reading it is cheaper than repeating it. A ref names a task
*or* a document, so this is also how you read a decision somebody pointed you at.

**Filing something is a complete act on its own.** Most of the time you file work you are
about to do, so it is visible while it is happening rather than afterwards — but the commoner
case in a working conversation is that somebody has just told you about a problem, and what
they want is for it to be *written down*.

**A report is not an assignment.** When somebody describes a defect, file it and say you have,
with its number. Do not begin fixing it unless they asked you to. The words that most often
get misread are the ones about *importance* — "urgent", "this is a blocker", "we shouldn't
ship with this" — and every one of those is a fact about the tracker rather than an instruction
to act. They tell you what to put in `!4/2`; they say nothing about who does the work or when.

If you genuinely cannot tell whether you were handed a report or a job, file it first and ask.
Filing is cheap and reversible; a change nobody asked for costs somebody a review, and if they
had already given the work to a person or another agent it collides with them silently — a
claim (below) tells you somebody *has* an item, and nothing tells you an item was meant for
them.

One line carries the detail:

```
subroutine_add(text="Fix the deploy script by friday !4/2 ~2h #ops +web")
```

`by friday` is a deadline, `!4/2` is importance and urgency out of five, `~2h` an estimate,
`#ops` a tag, `+web` the project. Whatever it read is echoed back, so check that line — it is
the only confirmation that `+web` was understood rather than left in the title.

**The line is the title. Everything you know that the title cannot hold goes in
`description`, in the same call:**

```
subroutine_add(
    text="Cache the connection roster !3/2 +web",
    description="Measured at 400ms a call, four calls a listing. The roster changes only when config.toml does.",
)
```

Write it while you are filing, not afterwards. You have the most context about a piece of work
at the moment you decide it exists, and a title alone is rarely enough for the next reader —
who is usually you, without any of the session this came from.

**The type is a promise about what the title says.** Get these two agreeing or a listing
stops being scannable — the type is the column somebody reads to know whether a line describes
a fault or a plan.

| Type | The title says |
| --- | --- |
| `bug` | what is wrong — *"A date more than a year away renders as if it were this year"* |
| `feature`, `task`, `chore` | what will be true when it is done — *"Highlight the search term where it matched"* |
| `spike` | the question — *"Settle whether search should read comments"* |
| `decision`, `finding`, `spec` | the conclusion — *"Blocked is tracked; waiting is a defer with a reason"* |

The failure to avoid is a problem statement filed as a feature: *"Nothing measures what the API
can do and the clients cannot"* reads as a defect and claims to be a plan. Two other reasons
beyond scannability, and the second is the one that decided it here:

- Your motivation is not lost by an outcome-shaped title, because it belongs in the
  description — which is one field away and is where somebody looks next.
- **A problem-shaped title rots.** "The guide's 8 KB budget is exhausted" was true and is not;
  the budget is 15 KB. It is on a finished item, so nobody will ever re-read it. A title
  stating a *condition* becomes false when the condition changes, silently and permanently. A
  title stating an *outcome* cannot.

If you find out later that something is not what you filed it as, say so — `type` is settable
on both `subroutine_add` and `subroutine_update`. What something is often becomes clear only
after it has been looked at, so reclassifying is normal rather than an admission.

**Comment as you go, especially when something fails.** A comment is what happened.

```
subroutine_comment(ref=42, body="Reproduced on 3.11 only. The fix in #38 does not apply here.")
```

A `#38` in the body is a *reference*, not a link: it shows on item 38 under *Referred to
by*, so the two find each other later. Where the item cited is one that governs — a
decision, a specification, a design, a dead end — `subroutine_show` goes further and
offers the typed link for you to confirm, with the call that makes it. Confirm it: an
unconfirmed suggestion is not what `ready=true` reads.

If you wrote one you should not have, `subroutine_comment(ref=42, body="…", remove=true)` takes
it back out — named by some of its words, because a comment has no number of its own. Matching
more than one is refused rather than guessed at. **Do not use it to tidy history**: a comment
that turned out to be wrong is worth more standing beside the correction than removed, because
"we thought X" is half of why the next session should not think X. Withdraw duplication and
mistakes, not the record of having been wrong.

**Write a document when you conclude something.** A comment is what happened; a document is
what you concluded — and the test is simple: *would the next person need to read it?* Decisions,
findings, designs and dead ends are all worth more than the hour they cost.

```
subroutine_document(title="Why we dropped the queue", type="decision",
                    body="It added an operational surface nobody wanted. …")
```

Dead ends especially. "We tried X and it does not work because Y" is the single most valuable
thing to leave behind, because without it the next session will try X.

**Say which project it belongs in.** Pass `project="web"`. Without one it lands in the Inbox,
which is where things go when nobody decided — fine for a quick capture, wrong for a conclusion
somebody will go looking for. It can be moved later, so this is worth a moment and not worth a
question.

**Here, or on disk?** A conclusion the next *session* needs is a document here. A thing a
*program* reads — a specification whose sections code and tests address by number, a config
file, a README — stays a file. The test is who reads it, not how important it is. And one thing
lives in one place, never both: two copies drift, and nobody can tell which one is stale.

**Ask before writing anything sensitive.** A document in a private project is visible only to
that project's members; one anywhere else is visible to everybody who can reach the workspace.
If a conclusion names a client, a rate, a person or a credential, ask which project it belongs
in rather than choosing for yourself — publishing cannot be undone.

**Finish by leaving the trail.** Before your context ends: comment on what you touched, write a
document for anything you decided, and mark done what is done. `subroutine_done(ref=42)`.

## When the tools do not cover it

`subroutine_call_api(method="PATCH", path="/v1/documents/42", body={"title": "…"})` reaches any
route your credential already allows. Use it for the thing you cannot otherwise do — and reach
for a named tool first, every time you have one.

**That is not politeness, it is the difference between a call that works and a call that is
right.** The tools carry conventions the API does not enforce. `subroutine_add` reads a whole
line — `Fix the boiler by friday !4/2 ~2h #home +sr` — and `POST /v1/tasks` will happily take
`{"title": "Fix the boiler", "importance": 4}` instead. Both succeed. The second quietly stops
using the grammar, sets no deadline because nobody parsed "by friday", and nothing anywhere
reports it. A raw call is the one place this tool surface cannot help you.

So: **the tools are a budget, and the command line is the whole product.** There are fourteen
tools because each one costs context in every session whether you call it or not — not because
the product does fourteen things. **If `subroutine` is on your `PATH`**, `subroutine --help` and
`subroutine explain <topic>` are complete, and setting anything up — `subroutine token create`
for another agent, for instance — exists only there. Check before recommending one: a session
connected to a server by address has the whole tool surface and no command line at all, and
`subroutine_call_api` is its escape hatch instead.

**Two things to read before constructing a call.** `subroutine://meta` is this workspace's
vocabulary — status keys, item types, what each listing filters and sorts by. The keys are
renameable, so `done` may be called something else here and guessing is how you get a 422.
`subroutine://docs/examples` is a worked request for each common act, every one of them executed
by the project's own test suite.

**Three routes are deliberately out of reach**: creating a workspace, renaming one, and moving a
project. Each is consequential, none can be undone, and the command line counts what will change
and asks first — which a tool call cannot do here yet. The refusal names the command to run.

## Things worth knowing

- **A number means one item, for ever.** `#42` is allocated once and never reused, and it names
  a task *or* a document. Never address anything by its position in a list.

  **The number space is Subroutine's, so do not mint your own.** When you write anything else —
  a review, a design note, a summary in chat — do not number its sections and call them findings
  or items. A reader holding "finding 3" and `#3` cannot tell them apart, and you will not be
  there to explain. Name the sections, and cite the real ref wherever one exists.

  **You usually cannot cite the refs on the first pass**, because you do not know what is worth
  filing until you have written it up. So write it with names, file from it, then go back once
  and put the refs in. That pass is what turns a write-up into the index into the tracker, which
  is what somebody reading it in six months actually wants — and it is the pass that shows you
  which findings you never filed.
- **Blocked is a link, not a status.** Say it with a link rather than by setting a status — a
  link resolves itself when the other side finishes, and it is what `ready` reads.

  **`ref` is the blocker.** `subroutine_link(ref=42, type="blocks", other=43)` means *42 blocks
  43*, so 43 is the one that disappears from `ready`. Read it as the sentence it spells: "42
  blocks 43". If what you have in mind is "this work depends on that work", the thing it depends
  *on* goes in `ref`.
  **`other` takes several.** `subroutine_link(ref=42, type="blocks", other=[43, 44, 45])` makes
  three links in one call — same `ref`, same type, one per target. Laying out a plan is when
  this matters: one measured project needed 37 links, which is 37 round trips one at a time.
  Every number is read before any link is written, so a bad one leaves nothing half-made.

- **Waiting on something outside the system is a deferral with a reason**:
  `subroutine_update(ref=42, defer="now+7d")` and a comment saying what you are waiting for.
  The link above resolves itself; an external wait does not, so it needs the reason in prose
  and a date to look again.
- **Do not close somebody else's work** without being asked, and do not edit their comments —
  a comment is attributed prose. Add your own.

## This is a default, not a rule

The practice above is what the tool is shaped for, not a policy anybody has to keep. If the
project has its own conventions — in `CLAUDE.md`, in a contributing guide, or just in what the
user tells you — those win. Say so once and follow them.
