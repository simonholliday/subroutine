"""What may be done to an item in the trash, or beneath anything in it (§6.9, decision `#4532`).

**Read it, restore it, or delete it again - and withdraw what hangs off it.** A row in the trash is
somebody saying they are done with it, so work on it waits until it is taken back out (`#3935`): a
comment on a trashed item was refused by name from the start, while an edit, a completion, a skip, a
claim and a move answered as though it were live - and finishing a trashed occurrence of a repeat
brought the next one. What is beneath it is out of sight with it (`#4091`), and refused the same.

**One gate, at the permission check** (`#4548`): ``authorization.authorize_on`` refuses every
write to a task or a document in or beneath the trash, with :func:`refuse_reaching`'s sentence, so
no writer has to remember to ask. Seven asked here by hand, and comments kept a copy of their own.

**Three acts are not refused, because they withdraw what hangs off the item and leave the item as
it is** (Q10 of the cold review of 2026-10-05): editing or deleting a comment on it - changing one
is not adding to its record (`#3920`) - releasing a lease on it, since the case a release exists
for is an agent that died holding one, and removing a link to or from it, which is still its
owner's to take back (`#4429`). Releasing and changing a comment pass ``doing=None`` to the gate.
**A link's ends reach it as ends, not as items, so the gate does not hold them**: removing a link
asks nothing of the trash, and making one asks :func:`refuse_reaching` itself, once it has answered
a link already made (`#3798`).

**What is beneath the trash takes none of them** (Simon, `#4653`): it is hidden with its container
(`#4091`), so nothing is done through it until that is restored, and each door that meets it says
where it is. A link to it can still be removed from its live end (`#4429`).

**What is done to an item from elsewhere** - filing or moving something under it - asks
:func:`refuse_reaching` of the item it reaches, which is not the one being written (`#4096`).
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.errors

Item = subroutine.db.models.work.Task | subroutine.db.models.work.Document


def refusal (ref: int, *, doing: str) -> subroutine.errors.ValidationError:
	"""Return the sentence refusing ``doing`` to an item in the trash.

	``doing`` is the whole clause after *so*: *#42 is in the trash, so* ``it cannot be changed``.
	Public for a caller that has to say it before it asks (`#4005`): ``document edit`` finds a
	document trash included and runs the editor before it saves, so the domain's refusal arrived
	after the typing and the text was gone. The terminal asks first, in these same words.
	"""

	return subroutine.errors.ValidationError(
		f"{subroutine.domain.refs.format_ref(ref)} is in the trash, so {doing}.",
		hint="Restore it first if you meant to keep working on it.",
	)


def refuse_reaching (session: sqlalchemy.orm.Session, row: Item, *, doing: str) -> None:
	"""Refuse ``doing`` to an item in the trash, or beneath one - decisions `#4096` and `#4532`.

	**The gate ``authorization.authorize_on`` asks for every write**, and what is done to an item
	from elsewhere asks it directly. ``doing`` is the whole clause after *so*: *#42 is in the trash,
	so nothing can be filed under it.* An item beneath one in the trash is refused with
	:func:`hidden`'s sentence, which names the one to restore, since decision `#4091` hides it with
	its container.
	"""

	if row.deleted_at is not None:
		raise refusal(row.ref, doing=doing)

	out_of_sight = _out_of_sight(session, row)

	if out_of_sight is not None:
		raise out_of_sight


def hidden (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	wanted: str,
) -> subroutine.errors.ValidationError | None:
	"""Return why an item asked for is out of sight beneath the trash, or ``None`` - `#4091`.

	**Asked only once a lookup has found nothing**, so a row a lookup finds is answered exactly as
	it was. Deleting an item hides what is beneath it, and a lookup that then said *There is no
	#39* sent its reader to ``list --trash``, which does not list it either: it was not deleted,
	only hidden. This finds it as the reader may - their visibility and their credential's scope
	still apply, through the statements every lookup starts from - and says where it is.
	"""

	ref = subroutine.domain.refs.parse_ref(wanted)

	try:
		identifier = uuid.UUID(wanted.strip()) if ref is None else None

	except ValueError:
		return None

	statements: tuple[tuple[type[Item], sqlalchemy.Select[typing.Any]], ...] = (
		(
			subroutine.db.models.work.Task,
			subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=[workspace_id],
				include_deleted=True,
				include_archived=True,
				include_templates=True,
				include_beneath_trash=True,
			),
		),
		(
			subroutine.db.models.work.Document,
			subroutine.domain.scoping.readable_documents(
				actor,
				workspace_ids=[workspace_id],
				include_deleted=True,
				include_archived=True,
				include_beneath_trash=True,
			),
		),
	)

	for model, statement in statements:
		found = session.scalars(
			statement.where(model.ref == ref if ref is not None else model.id == identifier)
		).first()

		if found is not None:
			return _out_of_sight(session, found)

	return None


def _out_of_sight (
	session: sqlalchemy.orm.Session, row: Item
) -> subroutine.errors.ValidationError | None:
	"""Say what in the trash ``row`` is beneath, nearest first, or ``None`` when nothing is."""

	kind = type(row)
	shown = subroutine.domain.refs.format_ref(row.ref)

	# **Its own kind first, because that is nearer**: a sub-task is filed in its parent's project,
	# so whatever task or document it is beneath sits inside the projects above it. Cast because a
	# select over a union of two mapped classes is typed as their common base.
	above = typing.cast(
		Item | None,
		session.scalars(
			sqlalchemy.select(kind)
			.where(
				kind.workspace_id == row.workspace_id,
				kind.deleted_at.is_not(None),
				kind.id != row.id,
				sqlalchemy.literal(row.path).like(kind.path.concat("%")),
			)
			.order_by(sqlalchemy.func.length(kind.path).desc())
		).first(),
	)

	if above is not None:
		container = subroutine.domain.refs.format_ref(above.ref)

		return subroutine.errors.ValidationError(
			f"{shown} is beneath {container}, which is in the trash.",
			hint=f"Restore {container} to bring it back, with everything beneath it.",
		)

	model = subroutine.db.models.project.Project
	filed_in = session.get(model, row.project_id)

	if filed_in is None:
		return None

	binned = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == row.workspace_id,
			model.deleted_at.is_not(None),
			sqlalchemy.literal(filed_in.path).like(model.path.concat("%")),
		)
		.order_by(sqlalchemy.func.length(model.path).desc())
	).first()

	if binned is None:
		return None

	address = subroutine.domain.projects.paths_for(session, {binned.id}).get(binned.id, binned.key)
	where = "in" if binned.id == filed_in.id else "beneath"

	return subroutine.errors.ValidationError(
		f"{shown} is {where} the project {address}, which is in the trash.",
		hint=f"Restore the project {address} to bring it back, with everything in it.",
	)
