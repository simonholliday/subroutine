---
name: subroutine
description: Track work in Subroutine — file tasks, find what can actually be started, record what happened, and write down conclusions the next session will need. **Read this before the first subroutine_* call of a session, including a read-only one.** It carries conventions the tool descriptions do not — how to open so you are not answering from a stale snapshot, how to ask for work that can actually be started, what a title has to say, and which end of a blocks link is the blocker. Also when the user asks what to work on, asks you to log or file something, finishes a piece of work, wonders where something was left, asks whether Subroutine is connected or what is in it, or says this project uses Subroutine — and for adopting Subroutine in a project that does not use it yet.
---

# Working with Subroutine

Subroutine is a task and project tracker that a person and an agent use as equals. What you
write in it is attributed to you, addressable by a number, and still there after your context
is gone — which is the whole reason to spend calls on it.

**If the `subroutine_*` tools are not available, stop and say so — and do not guess which of the
two causes it is.** The plugin configures the tools but does not install the program, so either
it is not installed, or it is installed somewhere the editor cannot see. Both look identical
from here: no tools, and no error, because installing a plugin and starting its server are
separate moments and only the first one reports. One command tells them apart:

```
claude mcp list
```

`✘ Failed to connect` beside `plugin:subroutine:subroutine` means the program was not found on
the `PATH` the editor passes down — nearly always a virtualenv. Say so, and offer the two ways
out: install it as a tool so it is on the `PATH` for good, or point the plugin at the copy that
already exists.

```
uv tool install subroutine     # or: pipx install subroutine
subroutine init
```

`/plugin configure subroutine` takes the absolute path instead — `<venv>/bin/subroutine` — for
somebody who would rather not install it twice. If the server is not listed at all, the plugin
itself is not installed or is disabled.

**If the tools are there but every call fails, read what the failure says — it names the
remedy.** This is the ordinary case on a fresh install and it is not the one above: the MCP
server starts perfectly well against an instance nobody has created yet, so the tools appear
and then refuse. `no Subroutine instance has been set up here yet.` means the user needs to run
`subroutine init` once; a schema message means `subroutine upgrade`. Pass the failure on
verbatim rather than diagnosing it — the sentence is written for them.

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

2. **Propose a key and say what it is.** Derive it from the repository directory name:
   uppercase, letters and digits only, starting with a letter, at most sixteen characters.
   State it and proceed — a key can be renamed later, and a checkout marked with step 6
   follows the rename, so this is not a decision to stop over.

   ```
   subroutine_project(key="WEB", title="Website redesign")
   ```

   What a rename *does* cost is addresses somebody has already written down — a `+WEB` in a
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
   subroutine use --here --project WEB
   ```

   That writes a small `.subroutine` file at the repository root. Say that you have written it
   and that it is safe to commit — it names a project, not a credential. From then on, work
   added anywhere under this directory goes to that project unless a line says `+OTHER`.

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
between releases by design. Only the mismatches above are said out loud — so if you see the
numbers differ and no line beneath them, nothing is wrong and there is nothing to report.

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

**If anybody else works from this list, take the task before you start it.**

```
subroutine_claim(ref=42)
```

Nothing enforces this and nothing has to: a claim is how you say "I am on this" to workers who
cannot see your screen. `ready=true` then hides it from them and never from you. It expires by
itself, so say it again if you are still going, and nothing is stranded if your context ends
first — which it will.

`subroutine_claim(ref=42, release=true)` gives it back when you stop without finishing. If
somebody else holds it you are told who and until when, which is the answer to what you do
next.

**Look before you file.** Searching costs one call and a duplicate costs somebody an afternoon
of wondering which of two items is the real one:

```
subroutine_search(q="deploy script")
```

It reads titles *and* what was written about them, so a half-remembered phrase from a
description will find it. Use it before creating anything, and whenever the user refers to
something you have no record of — it is usually already there.

**Read one before acting on it.** A listing is titles; `subroutine_show(ref=42)` is the whole
item, with what it is linked to and everything anybody has recorded against it:

```
subroutine_show(ref=42)
```

That record is the point. Somebody — possibly you, last week — wrote down why this was
attempted and what happened, and reading it is cheaper than repeating it. A ref names a task
*or* a document, so this is also how you read a decision somebody pointed you at.

**File the work before you start it**, so it is visible while it is happening rather than
afterwards. One line carries the detail:

```
subroutine_add(text="Fix the deploy script by friday !4/2 ~2h #ops +WEB")
```

`by friday` is a deadline, `!4/2` is importance and urgency out of five, `~2h` an estimate,
`#ops` a tag, `+WEB` the project. Whatever it read is echoed back, so check that line — it is
the only confirmation that `+WEB` was understood rather than left in the title.

**The line is the title. Everything you know that the title cannot hold goes in
`description`, in the same call:**

```
subroutine_add(
    text="Cache the connection roster !3/2 +WEB",
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

A `#38` in the body becomes a link on item 38, so the two find each other later.

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

**Say which project it belongs in.** Pass `project="WEB"`. Without one it lands in the Inbox,
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
line — `Fix the boiler by friday !4/2 ~2h #home +SR` — and `POST /v1/tasks` will happily take
`{"title": "Fix the boiler", "importance": 4}` instead. Both succeed. The second quietly stops
using the grammar, sets no deadline because nobody parsed "by friday", and nothing anywhere
reports it. A raw call is the one place this tool surface cannot help you.

So: **the tools are a budget, and the command line is the whole product.** There are fourteen
tools because each one costs context in every session whether you call it or not — not because
the product does fourteen things. If you have a shell, `subroutine --help` and `subroutine
explain <topic>` are complete, and `subroutine doc edit 42` is how a document is revised.

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
