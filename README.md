# Subroutine

**Project management for people and agents, in equal measure.**

> ⚠️ **Early development.** The specification is settled; the code is being built.
> **The personal to-do list works, and so does the HTTP API** — quick capture, an agenda,
> projects, tasks, documents and the links between them, scoped tokens, and a `serve` that
> refuses an unsafe bind. What is not built yet is most of what makes it interesting for an
> agent over *weeks* rather than minutes: comments, session handoffs, recorded decisions,
> verification evidence and claims are specified and not written. The specification and the
> implementation plan are not published yet.

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
good personal to-do list: three commands from install to ticking something off. Take away
every human and you have a substrate agents can plan, claim and verify work in. Neither is
a degraded mode of the other.

What that is *for* — and this part will keep changing as agents do — is continuity and
accountability. An agent leaves a handoff: what it did, what it verified, what it decided,
and what turned out to be a dead end, so the next session doesn't start cold or re-propose
something you already ruled out. A project can refuse to let a task be closed without
passing evidence attached to it. Every action is attributed, so you can see what happened
while you weren't watching — and an agent's token can be scoped narrower than your own,
because an agent you can't bound isn't one you can trust.

Of that, what works **now** is attribution, scoped tokens, and documents linked to the tasks
they came from. The handoff, the recorded decision and the evidence gate are written down in
full and not yet built; the warning at the top of this file is the honest boundary.

Free, open source, self-hosted. SQLite by default with no configuration, PostgreSQL when
you outgrow it. Your data stays yours.

---

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

No server, no token, no configuration. When you want an agent involved, or a second
person, the same install grows an HTTP API:

```console
$ subroutine token create --service-account claude
  Created service account claude, with the contributor role.

  sr_d78d5d93_hU5ak4GqR_E2GyX2lC0Zq8Mz5JA1kbm-byrlb5hXEfY

  That is the only time it is shown. Store it now.

$ subroutine serve
  Serving on http://127.0.0.1:8471 — the agent guide is at /v1/docs/agent.
```

`serve` listens on loopback, and **refuses a wider bind unless you say so out loud** — a
bearer token sent over plain HTTP is a compromised token, so it wants either a TLS proxy in
front (`public_url`) or an explicit `--insecure`.

Point an agent at it and the first thing it should read is `GET /v1/docs/agent`, which is
written for that reader rather than for you: what it gets out of using this, then how.

And if you keep your own list here and your team's on a company server, both are just
*connections* — one `subroutine today` shows the dentist and the stand-up together, and each
row prints an address you can type back:

```console
$ subroutine today
  Today
    #7             Pay the gas bill
    work/acme/#12  Fix the deploy script
```

## Documentation

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
