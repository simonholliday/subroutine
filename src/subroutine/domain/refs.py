"""Allocating the human-readable identifiers people actually use.

``SR-42`` is what goes in a commit message, a chat log and a sentence, so it has to be
short, stable and unambiguous. Three properties follow, and each costs something:

* **Unique per workspace, across tasks *and* documents.** Both draw from one counter on
  the project, so a ref names exactly one thing whichever table it lives in (SPEC.md §6.2).
* **Immutable.** A task that moves to another project keeps its original ref, so the
  number belongs to the project that *minted* it — ``origin_project_id`` — and not to
  wherever the task currently sits. Keying on the current project would collide the moment
  the destination mints its own number 42, from an entirely legitimate move.
* **Not gap-free.** A rolled-back create burns a number. Closing that gap would mean
  serialising every create in a project behind one lock, which is a real cost to avoid an
  imaginary problem: nobody minds that ``SR-41`` does not exist.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.work


def allocate (
	session: sqlalchemy.orm.Session, project: subroutine.db.models.project.Project
) -> tuple[str, int]:
	"""Claim the next ref in a project, returning ``(ref, number)``.

	One statement, and safe under concurrent creation on both backends: the row is locked
	for the duration of the update, so a second caller waits and then reads the value the
	first one left. Read-then-write in Python would hand both callers the same number.
	"""

	# Anything pending on this project must land before the counter moves, or the flush
	# that follows could overwrite the row this statement just updated.
	session.flush()

	model = subroutine.db.models.project.Project

	statement = (
		sqlalchemy.update(model)
		.where(model.id == project.id)
		.values(next_ref_number=model.next_ref_number + 1)
		.returning(model.next_ref_number)
	)
	updated = session.scalar(statement)

	if updated is None:
		raise LookupError(f"Project {project.id} no longer exists; no ref could be allocated.")

	# RETURNING hands back the value *after* the increment, so the number just claimed is
	# the one below it. The counter is named for what it holds next, not what it gave out.
	number = updated - 1

	# The in-memory object still believes the old value, and would write it back.
	session.expire(project, ["next_ref_number"])

	return format_ref(project.key, number), number


def format_ref (project_key: str, number: int) -> str:
	"""Return the ref a project key and number combine into."""

	return f"{project_key}-{number}"


def split_ref (ref: str) -> tuple[str, int] | None:
	"""Split a ref into its project key and number, or ``None`` if it is not one."""

	key, separator, digits = ref.rpartition("-")

	if not separator or not key or not digits.isdigit():
		return None

	return key, int(digits)


def find (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, ref: str
) -> tuple[str, uuid.UUID] | None:
	"""Resolve a ref to ``(entity_type, id)``, or ``None`` if nothing answers to it.

	Tasks and documents share one ref space, so both are searched. A soft-deleted item
	still resolves: a reference to something in the trash is more useful than a dangling
	one, and the caller can see the deletion for itself.
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
