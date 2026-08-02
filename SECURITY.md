# Security

## Reporting a vulnerability

**Please do not open an issue.** An issue is public from the moment it is filed, which for a
vulnerability is the one thing that must not happen first.

Use **[GitHub's private vulnerability reporting](https://github.com/simonholliday/subroutine/security/advisories/new)**
— the *Report a vulnerability* button on the Security tab. It is private between you and the
maintainer, it needs no email address on either side, and it keeps the whole conversation with
the eventual advisory.

If that is not available to you, write to **simon.holliday@protonmail.com** with `Subroutine` in
the subject.

**What helps:** the version (`subroutine --version`), whether it was reached over the HTTP API
or the CLI, and the smallest sequence that shows the problem. A proof of concept is welcome and
not required — a clear description of the flaw is worth more than a working exploit.

## What to expect

Subroutine is maintained by one person, so these are honest intentions rather than a
service-level agreement:

- **An acknowledgement within a few days.** If you have heard nothing in a week, assume it went
  astray and send a reminder.
- **An assessment, and the reasoning.** If it turns out not to be a vulnerability you will be
  told why, rather than left waiting.
- **Credit in the advisory** unless you would rather not be named.

Please give a reasonable window before disclosing publicly. If a fix is taking longer than you
think reasonable, say so — a deadline you have stated is easier to work to than one nobody
mentioned.

## What is in scope

The code in this repository, and the current release on PyPI.

Things that are worth reporting even though they may look like design decisions, because the
boundary is exactly where mistakes hide:

- **A credential doing more than it should.** A token that is wider than the person who issued
  it, or that survives something meant to end it.
- **Reading or writing across a boundary** — another workspace, a private project, another
  user's items.
- **Anything that reaches the database or the filesystem through an input** that was not
  supposed to.
- **A refusal that discloses.** "Forbidden" where the answer should be "not found" tells a
  stranger something exists; the API is written to avoid that and a place it does not is a bug.

## What is not

- **Anything requiring an account you were given.** A member of a workspace can read that
  workspace; that is the product working.
- **`serve` bound to a wider interface with `--insecure`.** It refuses without TLS in front of
  it and says why; overriding that is a deliberate act.
- **A self-hosted instance's own configuration** — file permissions, a database on a shared
  volume, a reverse proxy that terminates TLS and forwards over a network you do not trust.
- **Dependency advisories with no reachable path here.** Do report one if you can show the path.

## Supported versions

**The most recent release**, which is the only version that gets a fix. Subroutine is `0.x`: it
is early, the surface is still moving, and pretending to maintain several lines at once would be
a promise nobody could keep. Upgrading is `uv tool upgrade subroutine` or `pipx upgrade
subroutine` — whichever you installed it with — and then `subroutine upgrade` for the database.

## One thing this project cannot do for you

**Subroutine authenticates with bearer tokens and stores their SHA-256 hashes.** A token is
shown once, at creation, and is unrecoverable afterwards by design. Nothing here can tell you
what a token was, and nobody who has the database can either — but anybody who can *read* an
instance's database can read every item in it. The database is a secret. `docs/hosting.md` says
where it should live and what should be backing it up.
