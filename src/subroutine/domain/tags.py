"""Labels, created the first time somebody uses one.

``#health`` in a captured line should not require a separate tag-management step, from a
person or from an agent (SPEC.md §5.8). So there is no "create tag" command in the personal
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

#: SPEC.md §10.6's column width. Enforced here so an over-long tag names itself rather than
#: arriving as a driver error on PostgreSQL and as silent success on SQLite (§10.3).
MAX_NAME_LENGTH = 128

_WHITESPACE = re.compile(r"\s+")


def normalize (name: str) -> str:
	"""Return the form two tags are considered the same by."""

	return _WHITESPACE.sub(" ", name).strip().lower()


def _refuse_a_reference (name: str) -> None:
	"""Refuse a tag whose name is entirely digits.

	``#`` means both things: a tag in quick capture (§6.13) and a reference to an item in
	prose (§6.15). They stay apart because a reference is *all* digits and a tag is not —
	so a tag named "42" could never be written with its own sigil, and ``#42`` in anybody's
	description would go on pointing at task 42 instead.

	Enforced here rather than only in the two parsers because this is the one function every
	tag passes through, whatever created it. The capture grammar already declines to read
	``#42`` as a tag, so nothing reaches this today — but the API will grow a ``tags`` field,
	and a rule that lives only in a regex is a rule the next entry point does not have.
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

		_refuse_a_reference(key)
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


def apply_to_task (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	tags: typing.Sequence[subroutine.db.models.vocabulary.Tag],
) -> None:
	"""Attach tags to a task, skipping any it already carries.

	Idempotent rather than clever: the join row's primary key would refuse a duplicate, and
	a caller re-applying a tag has not done anything wrong.
	"""

	if not tags:
		return

	model = subroutine.db.models.work.TaskTag

	already = set(
		session.scalars(
			sqlalchemy.select(model.tag_id).where(model.task_id == task.id)
		)
	)

	for tag in tags:
		if tag.id in already:
			continue

		session.add(model(task_id=task.id, tag_id=tag.id))
		already.add(tag.id)

	session.flush()


def for_task (
	session: sqlalchemy.orm.Session, task: subroutine.db.models.work.Task
) -> list[subroutine.db.models.vocabulary.Tag]:
	"""Return a task's tags, in a stable order."""

	tag = subroutine.db.models.vocabulary.Tag
	join = subroutine.db.models.work.TaskTag

	return list(
		session.scalars(
			sqlalchemy.select(tag)
			.join(join, join.tag_id == tag.id)
			.where(join.task_id == task.id)
			.order_by(tag.name_normalized)
		)
	)


def names_for_tasks (
	session: sqlalchemy.orm.Session, task_ids: typing.Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
	"""Return the tag names on each of these tasks, as one query keyed by task id.

	The batched form of :func:`for_task`, for rendering a page. Calling that one per row is
	fifty queries for a listing of fifty, and a listing is the thing this program does most.

	Only the names are read. A renderer needs the word, never the row, and loading whole tag
	objects to take one string off each is a cost paid on every page.
	"""

	wanted = {identifier for identifier in task_ids if identifier is not None}

	if not wanted:
		return {}

	tag = subroutine.db.models.vocabulary.Tag
	join = subroutine.db.models.work.TaskTag

	rows = session.execute(
		sqlalchemy.select(join.task_id, tag.name)
		.join(tag, tag.id == join.tag_id)
		.where(join.task_id.in_(wanted))
		.order_by(join.task_id, tag.name_normalized)
	).all()

	found: dict[uuid.UUID, list[str]] = {}

	for task_id, name in rows:
		found.setdefault(task_id, []).append(name)

	return found
