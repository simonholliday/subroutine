"""Labels, created the first time somebody uses one.

``#health`` in a captured line should not require a separate tag-management step, from a
person or from an agent (docs/design.md §5.8). So there is no "create tag" command in the personal
path at all: applying a tag that does not exist creates it, and that is the only way tags
come into being until a UI offers to rename one.

Matching is on a normalised form — lower-cased, whitespace collapsed — so ``#Health`` and
``#health`` are one tag, while the name is stored as first written so a UI can show it the
way its author meant it.
"""

import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.text
import subroutine.errors

#: docs/design.md §10.6's column width. Enforced here so an over-long tag names itself rather than
#: arriving as a driver error on PostgreSQL and as silent success on SQLite (§10.3).
MAX_NAME_LENGTH = 128

_WHITESPACE = re.compile(r"\s+")


def normalize (name: str) -> str:
	"""Return the form two tags are considered the same by."""

	return _WHITESPACE.sub(" ", name).strip().lower()


def refuse_a_reference (name: str) -> None:
	"""Refuse a tag whose name is entirely digits.

	``#`` means both things: a tag in quick capture (§6.13) and a reference to an item in
	prose (§6.15). They stay apart because a reference is *all* digits and a tag is not —
	so a tag named "42" could never be written with its own sigil, and ``#42`` in anybody's
	description would go on pointing at task 42 instead.

	Enforced here rather than only in the two parsers because a rule that lives in a regex is
	a rule the next entry point does not have.

	**Public, and it was not** (`#1167`). This was private on the belief that :func:`ensure`
	is the one function every tag passes through — which is true of every tag that is
	*created* and false of one that is *renamed*. ``vocabulary.update_tag`` is the second
	door; it went round this for as long as it existed, and its own comment said it did not.
	"""

	if not name.isdigit():
		return

	raise subroutine.errors.ValidationError(
		f"{name!r} cannot be used as a tag.",
		errors=[
			subroutine.errors.FieldError(
				field="tags",
				code="invalid_field_value",
				message=f"A tag made only of digits would be indistinguishable from a "
				f"reference to item #{name}.",
				hint="Add a letter — 'q3' rather than '3' — or, if you meant to refer to "
				f"item #{name}, write that in the description instead.",
			)
		],
	)


def ensure (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	names: typing.Sequence[str],
) -> list[subroutine.db.models.vocabulary.Tag]:
	"""Return the tags with these names, creating any that do not exist yet.

	Order follows ``names``, and duplicates collapse — ``#health #Health`` is one tag, not
	an error, because refusing it would be pedantry about a convenience feature.
	"""

	model = subroutine.db.models.vocabulary.Tag

	wanted: dict[str, str] = {}

	for name in names:
		cleaned = subroutine.domain.text.fit(
			subroutine.domain.text.require(name, field="tags", label="tag"),
			field="tags",
			limit=MAX_NAME_LENGTH,
			label="tag",
		)
		key = normalize(cleaned)

		refuse_a_reference(key)
		wanted.setdefault(key, cleaned)

	if not wanted:
		return []

	existing = {
		tag.name_normalized: tag
		for tag in session.scalars(
			sqlalchemy.select(model).where(
				model.workspace_id == workspace_id, model.name_normalized.in_(wanted)
			)
		)
	}

	resolved = []

	for key, written in wanted.items():
		tag = existing.get(key)

		if tag is None:
			tag = model(
				id=subroutine.db.types.new_uuid(),
				workspace_id=workspace_id,
				name=written,
				name_normalized=key,
			)
			session.add(tag)

		resolved.append(tag)

	session.flush()

	return resolved


class Joined (typing.NamedTuple):
	"""How one kind of item is joined to the tags on it."""

	#: The association model — ``TaskTag``, ``DocumentTag``.
	rows: typing.Any

	#: Its column naming the item, so a query can be written once for both.
	owner: typing.Any


#: Which kinds carry tags, keyed by the item's own model — `#819`.
#:
#: **One tag vocabulary across both, decided with Simon on 2026-08-12**, and it is what the
#: schema already assumed: both association tables reference ``tag.id``, and a tag is scoped to
#: a *workspace* rather than to a kind. So ``#health`` on a document and ``#health`` on a task
#: are the same tag, unlike a status or an item type, which §5.5 keeps per kind.
#:
#: **Keyed by the item rather than passed as a pair**, so a caller hands over the thing it has
#: and cannot put a document's id into a task's join table. That mattered enough to shape the
#: signatures: every function below takes the item.
#:
#: ``document_tag`` has existed since the initial migration and was read and written by nothing
#: until this — the second signature defect of this codebase (`#247`, `#251`, `#303`, `#443`),
#: and the largest instance of it so far, because the table was not merely unused: the guard
#: written to notice exactly that carried an excuse for it naming a field that did not exist
#: (`#820`).
JOINS: dict[typing.Any, Joined] = {
	subroutine.db.models.work.Task: Joined(
		rows=subroutine.db.models.work.TaskTag,
		owner=subroutine.db.models.work.TaskTag.task_id,
	),
	subroutine.db.models.work.Document: Joined(
		rows=subroutine.db.models.work.DocumentTag,
		owner=subroutine.db.models.work.DocumentTag.document_id,
	),
}


def _joined (item: typing.Any) -> Joined:
	"""Return how this item is joined to its tags, refusing a kind that carries none."""

	found = JOINS.get(type(item))

	if found is None:
		raise TypeError(f"{type(item).__name__} does not carry tags")

	return found


def apply_to (
	session: sqlalchemy.orm.Session,
	item: typing.Any,
	tags: typing.Sequence[subroutine.db.models.vocabulary.Tag],
) -> None:
	"""Attach tags to an item, skipping any it already carries.

	Idempotent rather than clever: the join row's primary key would refuse a duplicate, and
	a caller re-applying a tag has not done anything wrong.
	"""

	if not tags:
		return

	join = _joined(item)

	already = set(
		session.scalars(
			sqlalchemy.select(join.rows.tag_id).where(join.owner == item.id)
		)
	)

	for tag in tags:
		if tag.id in already:
			continue

		session.add(join.rows(**{join.owner.key: item.id, "tag_id": tag.id}))
		already.add(tag.id)

	session.flush()


def on (
	session: sqlalchemy.orm.Session, item: typing.Any
) -> list[subroutine.db.models.vocabulary.Tag]:
	"""Return an item's tags, in a stable order."""

	tag = subroutine.db.models.vocabulary.Tag
	join = _joined(item)

	return list(
		session.scalars(
			sqlalchemy.select(tag)
			.join(join.rows, join.rows.tag_id == tag.id)
			.where(join.owner == item.id)
			.order_by(tag.name_normalized)
		)
	)


def names_for (
	session: sqlalchemy.orm.Session,
	kind: typing.Any,
	identifiers: typing.Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
	"""Return the tag names on each of these items, as one query keyed by id.

	The batched form of :func:`on`, for rendering a page. Calling that one per row is fifty
	queries for a listing of fifty, and a listing is the thing this program does most.

	**The only one that takes the kind rather than the item**, because it is handed ids — a
	renderer has already loaded its rows and knows what they are, and asking it for one item
	back just to read its class would be a query to save an argument.

	Only the names are read. A renderer needs the word, never the row, and loading whole tag
	objects to take one string off each is a cost paid on every page.
	"""

	wanted = {identifier for identifier in identifiers if identifier is not None}

	if not wanted:
		return {}

	tag = subroutine.db.models.vocabulary.Tag
	join = JOINS[kind]

	rows = session.execute(
		sqlalchemy.select(join.owner, tag.name, tag.name_normalized)
		.join(tag, tag.id == join.rows.tag_id)
		.where(join.owner.in_(wanted))
	).all()

	found: dict[uuid.UUID, list[tuple[str, str]]] = {}

	for owner_id, name, normalized in rows:
		found.setdefault(owner_id, []).append((normalized, name))

	# **Sorted here, not by the database.** PostgreSQL's collation on this machine is
	# `en_GB.UTF-8` and does not sort byte-wise, so `ORDER BY name_normalized` put `ähnlich`
	# before `apple` there and after `zebra` on SQLite — measured. `tags` is a published API
	# field and a compact-line column, so the same task rendered `#ähnlich #apple` on one
	# deployment and `#apple #ähnlich` on the other, and the transport-equivalence test could
	# not see it because both of its sides run on one backend (docs/design.md §10.3).
	return {
		owner_id: [name for _key, name in sorted(pairs)] for owner_id, pairs in found.items()
	}


def set_on (
	session: sqlalchemy.orm.Session,
	item: typing.Any,
	tags: typing.Sequence[subroutine.db.models.vocabulary.Tag],
) -> None:
	"""Make an item's tags exactly these, adding what is missing and removing what is not.

	**Replaces rather than adds**, which is what §8.3 means by a field on a ``PATCH``: every
	other field there is assigned, not merged, and a ``tags`` that merged would be the only
	one a caller could not use to *remove* anything. An empty sequence therefore clears them,
	which is the same statement as sending ``null`` for a scalar.

	The counterpart is :func:`apply_to`, which is additive and is what quick capture wants —
	``#health`` in a captured line adds a tag to whatever is already there.

	Rows are added and deleted rather than the set being rebuilt, so a tag an item already
	carries keeps its join row. Nothing depends on that yet; it will the moment anything
	records when a tag was applied.
	"""

	join = _joined(item)
	wanted = {tag.id for tag in tags}

	already = set(
		session.scalars(sqlalchemy.select(join.rows.tag_id).where(join.owner == item.id))
	)

	for tag_id in already - wanted:
		session.execute(
			sqlalchemy.delete(join.rows).where(join.owner == item.id, join.rows.tag_id == tag_id)
		)

	for tag in tags:
		if tag.id not in already:
			session.add(join.rows(**{join.owner.key: item.id, "tag_id": tag.id}))

	session.flush()


def names_on (
	session: sqlalchemy.orm.Session, item: typing.Any
) -> list[str]:
	"""Return an item's tag names, ordered, for comparing one state against another.

	Its own function because an event's before-and-after has to be built the same way twice,
	and a sorted list of names is what makes "did the tags change" a value comparison rather
	than a set of join rows nobody can diff.
	"""

	return [tag.name for tag in on(session, item)]
