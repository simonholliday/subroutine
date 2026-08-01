---
name: subroutine
description: Track work in Subroutine — file tasks, find what can actually be started, record what happened, and write down conclusions the next session will need. Use when the user asks what to work on, asks you to log or file something, finishes a piece of work, wonders where something was left, or says this project uses Subroutine. Also covers adopting Subroutine in a project that does not use it yet.
---

# Working with Subroutine

Subroutine is a task and project tracker that a person and an agent use as equals. What you
write in it is attributed to you, addressable by a number, and still there after your context
is gone — which is the whole reason to spend calls on it.

**If the `subroutine_*` tools are not available, stop and say so.** The plugin configures them
but does not install the program. Tell the user:

```
pip install subroutine
subroutine init
```

**If the tools are there but every call fails, read what the failure says — it names the
remedy.** This is the ordinary case on a fresh install and it is not the one above: the MCP
server starts perfectly well against an instance nobody has created yet, so the tools appear
and then refuse. `no Subroutine instance has been set up here yet.` means the user needs to run
`subroutine init` once; a schema message means `subroutine upgrade`. Pass the failure on
verbatim rather than diagnosing it — the sentence is written for them.

If it is installed but the tools still fail with nothing useful, it is almost certainly on a
path Claude Code cannot see — a virtualenv, usually. `/plugin configure subroutine` takes the
full path to the `subroutine` command. Do not work around it by writing to a file instead; a
tracker nobody can reach is worse than an honest failure.

`GET /v1/docs/agent` on any served instance is the reference for the HTTP API. This page is
about *when* to reach for it and what good practice looks like — it does not repeat the API.

## Adopting Subroutine in a project that does not use it yet

If the user has just installed Subroutine, or says "we use Subroutine now", set the project up
before doing anything else. **Ask only what cannot be undone; state the rest and proceed.** A
setup interview is how a tool loses the person who just installed it.

1. **Look before creating.** `subroutine_project` with no arguments. If something already
   covers this repository, use it. A duplicate project is invisible until somebody files into
   the one nobody reads.

2. **Propose a key and confirm it — this is the one question always worth asking.** A project
   key cannot be changed afterwards and becomes part of how every item is addressed. Derive it
   from the repository directory name: uppercase, letters and digits only, starting with a
   letter, at most sixteen characters. Say what you propose and let them correct it.

   ```
   subroutine_project(key="WEB", title="Website redesign")
   ```

3. **Do not ask which workspace unless there is more than one.** A fresh install has exactly
   one. If there are several, ask — and say why: items are numbered per workspace, and a
   project cannot be moved between them.

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

7. **Write the pointer into the project's agent file** — `CLAUDE.md`, `AGENTS.md`, whichever it
   already uses. One line naming the project key is enough. Without it the next session does
   not know adoption happened, and adopts again.

## Day to day

**Ask what can be started, not what exists.** This is the one thing Subroutine answers that a
list of tasks does not:

```
subroutine_list(ready=true, order="-priority_score")
```

`ready` leaves out anything blocked by unfinished work and anything deferred to a later date.
Without it you get a backlog in priority order, which includes things nobody can act on yet.

**File the work before you start it**, so it is visible while it is happening rather than
afterwards. One line carries the detail:

```
subroutine_add(text="Fix the deploy script by friday !4/2 ~2h #ops +WEB")
```

`by friday` is a deadline, `!4/2` is importance and urgency out of five, `~2h` an estimate,
`#ops` a tag, `+WEB` the project. Whatever it read is echoed back, so check that line — it is
the only confirmation that `+WEB` was understood rather than left in the title.

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

**Write a document when you conclude something.** A comment is what happened; a document is
what you concluded — and the test is simple: *would the next person need to read it?* Decisions,
findings, designs and dead ends are all worth more than the hour they cost.

```
subroutine_document(title="Why we dropped the queue", type="decision",
                    body="It added an operational surface nobody wanted. …")
```

Dead ends especially. "We tried X and it does not work because Y" is the single most valuable
thing to leave behind, because without it the next session will try X.

**Finish by leaving the trail.** Before your context ends: comment on what you touched, write a
document for anything you decided, and mark done what is done. `subroutine_done(ref=42)`.

## Things worth knowing

- **A number means one item, for ever.** `#42` is allocated once and never reused, and it names
  a task *or* a document. Never address anything by its position in a list.
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
