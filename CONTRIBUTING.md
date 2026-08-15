# Contributing to Subroutine

## We are not seeking code contributions yet

Thank you for thinking of it, and please do not spend your time on a pull request we would
have to turn down. Subroutine is early: the core is still moving, the backlog is planned some
way ahead, and there is not yet a stable enough surface to review outside work against
fairly. Accepting patches now would mostly mean asking people to rewrite them.

That will change once the shape has settled, and this file will say so when it does.

**Two things are very welcome in the meantime:**

- **Bug reports.** If something is wrong, or a message sent you the wrong way, please open an
  issue. That is the most useful thing anybody outside the project can do right now.
- **Telling us it does not fit.** If you tried it and stopped, we would rather know why than
  not. An issue saying "I wanted X and there was no way to do it" is worth more than a guess.

**A vulnerability is the exception, and please treat it as one.** An issue is public from the
moment it is filed, so it is the one report that must not start there —
[SECURITY.md](SECURITY.md) says where it should go instead.

The rest of this file is kept for two reasons: an unsolicited pull request still needs the
licence agreement below if it is ever taken, and the conventions are worth having if you are
reading the code to decide whether to trust it.

## The paperwork, if a change is ever agreed

**Every contributor agrees to the [Contributor Licence Agreement](CLA.md) before their
first pull request is merged.** Say so in the pull request:

> I have read the CLA document and I hereby agree to its terms.

That is the whole process — there is nothing to print, sign or post.

It exists because Subroutine is offered under FSL-1.1-ALv2 *and* under a commercial
licence by agreement. Offering the second one means being able to grant rights in all of
the code, and a contribution that arrived under the FSL alone could not be included in it,
because the FSL grants rights for a Permitted Purpose and selling a service is not one. The
CLA solves that by having you grant those rights up front. **You keep your copyright** —
it is a licence, not an assignment, and you remain free to do anything you like with your
own work.

If your employer owns what you write at work, please make sure someone with authority to
do so agrees on their behalf before you contribute.

## Getting set up

Subroutine targets Python 3.11 and later.

```console
$ git clone https://github.com/simonholliday/subroutine
$ cd subroutine
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e '.[dev,postgres]'
$ pytest
```

That runs the suite against SQLite. To run it against both backends — which CI does, and
which you should before opening a pull request — you need a PostgreSQL you can create
databases on:

```console
$ export SUBROUTINE_TEST_POSTGRES_ADMIN_URL=postgresql+psycopg:///postgres
$ pytest
```

That variable is only needed if your server is somewhere other than the default, which is
a local Unix socket (`postgresql+psycopg:///postgres`) — the suite tries PostgreSQL without
being asked. If it cannot be reached, that half of the suite **skips**, so a laptop without
PostgreSQL can still run the tests. In CI, `SUBROUTINE_TEST_REQUIRE_POSTGRES=1` turns those
skips into failures — a green build there means both backends really ran.

Each run creates and drops its own database, named with a random suffix, so two `pytest`
processes on one machine do not destroy each other's schema.

Before pushing:

```console
$ ruff check .
$ mypy src tests scripts
$ pytest
```

### The commit hooks

Optional, and worth it if you are working against a Subroutine instance that holds this
project's own backlog:

```console
$ python scripts/install_hooks.py
```

Two things then happen. A commit message has to cite an item that exists — written `SR#42`,
never a bare `#42`, because GitHub auto-links that to *this repository's* issues and the link
resolves, so nobody can see it is about something else. And after the commit lands, the sha is
written back onto every item it cites, so "what closed #46" and "what did `abc1234` do" are
both answerable from the instance rather than only from a checkout.

A commit that changes nothing but comments needs no item. For anything else the exemption is
taken deliberately:

```console
$ git commit --no-verify
```

**The installer puts a shim outside the working tree and points `core.hooksPath` at it**,
rather than writing into `.git/hooks`. That is not tidiness: a working tree on a filesystem
that forces its permission bits — a CIFS or SMB share mounted `file_mode=0666`, which is where
this project is developed — cannot hold an executable file at all, and **git skips a hook it
cannot execute without saying anything**. The shim runs the tracked hook by path, so editing
`hooks/` takes effect immediately and there is no copy to go stale.

## Conventions

These are unusual enough to be worth stating plainly, because a linter will not tell you
about most of them.

**Indentation is tabs, never spaces.** Strictly. The one exception is
`src/subroutine/db/migrations/versions/`, which Alembic generates space-indented and
which is left in Alembic's conventions rather than half-converted to ours.

**Ruff is a linter here, never a formatter.** Run `ruff check`. Do not run `ruff format`
— it would convert the whole codebase to spaces and strip the space in `def foo (x)`.

**Imports are `import x` only, never `from x import y`,** and things are called by their
fully-qualified names: `sqlalchemy.select`, not `select`. The exceptions are
`from __future__ import annotations` and imports inside `if typing.TYPE_CHECKING:`.

Note the consequence: `import a.b.c` binds only `a`, so Ruff's unused-import check cannot
see a stale `import subroutine.x.y`, and in the other direction `a.b.c.thing` resolves
through somebody else's import. `tests/test_imports.py` checks both directions, so the
suite will tell you — but it is worth knowing why a linter never will.

**Function definitions take a space before the parenthesis, calls do not.** `def foo (x)`
and `foo(x)`. It marks a definition apart from a call at a glance.

**Docstrings are mandatory** on every function, and blank lines separate paragraphs of
code the way they separate paragraphs of prose — a guard clause is followed by a blank
line, and so is a shift from validating to acting.

**Type hints are mandatory and use PEP 604**: `str | None`, `list[str]`, not
`typing.Optional[str]`. `mypy --strict` passes across the whole tree and must keep doing
so.

**Every test runs against SQLite *and* PostgreSQL.** The engine fixture is parameterised;
please do not add a test that silently covers one backend only. The bugs this catches —
event ordering, NULL sort order, `LIKE` case sensitivity, string-length enforcement,
collation — are invisible on SQLite by construction.

**Schema changes go through Alembic.** `Base.metadata.create_all` is for tests. A CI
check asserts that `--autogenerate` produces an empty diff against the models, and a
second check compares CHECK constraints, which autogenerate does not look at.

**User-facing text is read by people setting up a to-do list**, not only by Python
developers. CLI output, error messages and docstrings describe outcomes in the user's
terms, and errors say what to do next.

## Pull requests

Small and focused beats large and comprehensive. If you are planning something
substantial, please open an issue first so we can agree the shape of it before you spend
the time.

Include tests. Include the CLA sentence if it is your first contribution.
