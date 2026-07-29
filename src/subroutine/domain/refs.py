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

import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.orm.util

import subroutine.db.models.identity
import subroutine.db.models.work

#: The sigil a ref is written with in prose and printed with in listings. Bare on input,
#: because ``#`` opens a comment in every POSIX shell (SPEC.md §12.2a).
SIGIL = "#"

#: A ref as somebody might type it: ``42`` or ``#42``. Anchored at both ends — this is for
#: reading a whole argument, not for finding references inside running text, which is
#: :mod:`subroutine.domain.mentions` and a different problem.
_TYPED = re.compile(r"\A#?(\d+)\Z")


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
	"""

	match = _TYPED.match(text.strip())

	if match is None:
		return None

	return int(match.group(1))


def find (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, ref: int
) -> tuple[str, uuid.UUID] | None:
	"""Resolve a ref to ``(entity_type, id)``, or ``None`` if nothing answers to it.

	Tasks and documents share one ref space, so both are searched — in that order, which
	is the documented tie-break should the allocator ever be bypassed (SPEC.md Appendix A).
	A soft-deleted item still resolves: a reference to something in the trash is more
	useful than a dangling one, and the caller can see the deletion for itself.
	"""

	candidates: tuple[tuple[str, typing.Any], ...] = (
		("task", subroutine.db.models.work.Task),
		("document", subroutine.db.models.work.Document),
	)

	for entity_type, model in candidates:
		found = session.scalars(
			sqlalchemy.select(model.id).where(model.workspace_id == workspace_id, model.ref == ref)
		).first()

		if found is not None:
			return entity_type, found

	return None
