"""Allocating the human-readable identifiers people actually use.

``#42`` is what goes in a commit message, a chat log and a sentence, so it has to be
short, stable and unambiguous. Three properties follow, and each costs something:

* **A plain integer, unique per workspace, across tasks *and* documents.** Both draw from
  one counter on the workspace, so a ref names exactly one thing whichever table it lives
  in (SPEC.md §6.2). There is no prefix, because a prefix has to name something and
  whatever it names is something the item can then be moved out of — leaving an
  identifier that either changes (and is not an identifier) or lies about where the item
  is.
* **Immutable, and never reused.** The counter only goes up, and nothing rewrites a ref
  once it is assigned. A number somebody memorised while working on something goes on
  meaning it.
* **Not gap-free.** A rolled-back create burns a number. Closing that gap would mean
  serialising every create in a workspace behind one lock, which is a real cost to avoid
  an imaginary problem: nobody minds that ``#41`` does not exist.
"""

import dataclasses
import re
import uuid

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.orm.util

import subroutine.db.models.identity

#: The sigil a ref is written with in prose and printed with in listings. Bare on input,
#: because ``#`` opens a comment in every POSIX shell (SPEC.md §12.2a).
SIGIL = "#"

#: What divides the parts of an address (SPEC.md §13.7). A slash, so the grammar reads as
#: the relative path it is and matches the ``subroutine:work/acme/42`` markdown target
#: rather than inventing a second spelling beside it.
SEPARATOR = "/"

#: The largest ref the column can hold. Checked here rather than left to the database, for
#: exactly the reason :data:`subroutine.domain.durations.MAX_MINUTES` exists: the two
#: backends disagree about an overflow and *neither* of them disagrees quietly. Asking for
#: ref 2147483648 raised ``NumericValueOutOfRange`` on PostgreSQL and ``OverflowError`` on
#: SQLite — both unhandled, both a 500 where the honest answer is "no such item".
#:
#: Python integers have no ceiling, so a bound the parser does not impose is a bound
#: nothing imposes until a driver refuses the query.
MAX_REF = 2_147_483_647

#: A ref as somebody might type it: ``42`` or ``#42``. Anchored at both ends — this is for
#: reading a whole argument, not for finding references inside running text, which is
#: :mod:`subroutine.domain.mentions` and a different problem.
#:
#: No leading zero, and no bare ``0``, which keeps this in step with that other pattern:
#: ``#007`` is left as prose there, so ``007`` must not resolve here. One spelling per ref,
#: in both directions.
_TYPED = re.compile(r"\A#?([1-9][0-9]*)\Z")


def allocate (session: sqlalchemy.orm.Session, workspace_id: uuid.UUID) -> int:
	"""Claim the next ref in a workspace.

	One statement, and safe under concurrent creation on both backends: the row is locked
	for the duration of the update, so a second caller waits and then reads the value the
	first one left. Read-then-write in Python would hand both callers the same number.
	"""

	# Anything pending on this workspace must land before the counter moves, or the flush
	# that follows could overwrite the row this statement just updated.
	session.flush()

	model = subroutine.db.models.identity.Workspace

	statement = (
		sqlalchemy.update(model)
		.where(model.id == workspace_id)
		.values(next_ref_number=model.next_ref_number + 1)
		.returning(model.next_ref_number)
	)
	updated = session.scalar(statement)

	if updated is None:
		raise LookupError(f"Workspace {workspace_id} no longer exists; no ref could be allocated.")

	# Any in-memory copy still believes the old value. Taken from the identity map rather
	# than with ``session.get``, which would fetch a row nobody asked for on the hottest
	# path in the application just to mark one of its columns stale.
	loaded = session.identity_map.get(
		sqlalchemy.orm.util.identity_key(model, workspace_id), None
	)

	if loaded is not None:
		session.expire(loaded, ["next_ref_number"])

	# RETURNING hands back the value *after* the increment, so the number just claimed is
	# the one below it. The counter is named for what it holds next, not what it gave out.
	return updated - 1


def format_ref (ref: int) -> str:
	"""Return the way a ref is written for a reader: ``#42``."""

	return f"{SIGIL}{ref}"


def parse_ref (text: str) -> int | None:
	"""Read a ref somebody typed, or ``None`` if it is not one.

	``42`` and ``#42`` are the same request. The sigil is accepted rather than required
	because a shell eats it: ``subroutine done #42`` arrives with no argument at all.

	``None`` covers everything that cannot *be* a ref, which includes a number too large for
	one (:data:`MAX_REF`) and a zero-padded one. Callers turn that into "no such item", so
	an out-of-range lookup is a 404 rather than a query the database refuses — see
	:data:`MAX_REF` for what that cost before it was bounded. A body field that wants to
	*validate* a reference rather than resolve one says so in its own error;
	``api/schemas.Reference`` does that.
	"""

	match = _TYPED.match(text.strip())

	if match is None:
		return None

	ref = int(match.group(1))

	return None if ref > MAX_REF else ref


@dataclasses.dataclass(frozen=True)
class Address:
	"""A ref as somebody wrote it, with however much context they chose to give.

	SPEC.md §13.7's grammar, read **relatively, nearest scope first** — the same way a
	filesystem path is:

	* ``42`` — the current context's 42.
	* ``acme/42`` — workspace ``acme``, on whatever connection is current.
	* ``work/acme/42`` — fully qualified, and the form printed whenever a shorter one would
	  be ambiguous.

	Two parts therefore mean *workspace*, never *connection*, because a workspace is the
	nearer enclosing scope. That has to be a stated rule rather than a guess: with two
	components there is nothing in the text itself to tell one from the other.
	"""

	ref: int
	workspace: str | None = None
	connection: str | None = None


def parse_address (text: str) -> Address | None:
	"""Read an address somebody typed, or ``None`` if it is not one.

	Only the *shape* is read here. Whether the named workspace exists, is readable, or is the
	one holding that ref is the caller's business — this module has no session and no
	principal, and deciding "is it visible" without one is how a lookup ends up unnarrowed.
	"""

	parts = [part.strip() for part in text.strip().split(SEPARATOR)]

	if len(parts) > 3 or any(not part for part in parts):
		return None

	ref = parse_ref(parts[-1])

	if ref is None:
		return None

	names = parts[:-1]

	return Address(
		ref=ref,
		workspace=names[-1] if names else None,
		connection=names[0] if len(names) == 2 else None,
	)


def format_address (ref: int, *, workspace: str | None = None) -> str:
	"""Return the way an item is written for a reader who may need the wider context.

	``#42`` on its own when that resolves, ``acme/#42`` when it would not. Printing the
	shortest form that *resolves* is the rule that makes a listing safe to copy from: a bare
	number beside an item in another workspace is an invitation to act on the wrong one.
	"""

	shown = format_ref(ref)

	return shown if workspace is None else f"{workspace}{SEPARATOR}{shown}"


#: There was a ``find(session, workspace_id, ref)`` here, and it was **deleted on
#: 2026-07-29** rather than kept for later. It resolved any ref in a workspace to an id with
#: no visibility narrowing whatever, which was safe only while its one caller authorised
#: what it got — and that caller went away when the CLI's lookup moved to
#: ``scoping.readable_tasks``. What remained was an unnarrowed resolver in the domain layer
#: with nothing calling it and a written exemption in ``tests/test_scoping.py`` vouching for
#: a caller that no longer existed: precisely the thing the next person needing "resolve a
#: ref" would have found and trusted.
#:
#: Resolve a ref through ``domain.scoping`` (a caller-facing lookup) or
#: ``domain.mentions.resolve`` (indexing text the author already wrote). If a future
#: cross-connection resolver needs something like the old ``find``, it starts from a
#: narrowed statement — not from this module.
