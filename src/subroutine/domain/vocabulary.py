"""Curating the words a workspace uses — `#826`.

Until this existed, ``Status``, ``ItemType``, ``LinkType`` and ``Tag`` rows were written by
``db.seed`` and by nothing else, on any surface. So an installation could not add, rename or
remove one — and ``tag:write``, ``status:write`` and ``link_type:write`` were three published
permissions that gated nothing. An operator removing ``status:write`` from a role would have
found it changed nothing at all.

**Item types are deliberately not here.** `#906` handed `#826` a question nobody has answered —
*what are the fixed categories of an item type* — and until there is one, adding a type would
mean adding something no client can branch on. ``Status`` publishes a fixed ``category`` beside
its renameable key for exactly that reason, and ``ItemType`` has no equivalent yet.

## Three rules this module exists to hold

**A key is renameable and a category is not.** ``category`` is settable when a status is created
and never afterwards. It is the machine meaning — what "everything not finished" resolves to
without knowing the local vocabulary — and moving an existing status between categories would
silently change what every task already in it *means*, including to readiness and the agenda.
Adding that later is easy; taking it back would not be.

**Exactly one default per entity type** — docs/design.md §10.7 invariant 6, which nothing
enforced. Two defaults is not a cosmetic mess: :func:`subroutine.domain.tasks.create` asks for
*the* default and would get whichever the database returned first, so the same call would file
two tasks into different statuses on two days.

**Nothing in use is removed.** The foreign keys are ``ondelete="RESTRICT"``, so the database
already refuses — as an ``IntegrityError``, which reaches a caller as a 500 naming a constraint.
This refuses first, in the caller's terms, and says how many rows are in the way.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.mixins
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.patch
import subroutine.domain.settings
import subroutine.domain.tags
import subroutine.domain.text
import subroutine.errors
import subroutine.permissions

#: The width docs/design.md §10.6 gives a key, enforced here so an over-long one names itself
#: rather than arriving as a driver error on PostgreSQL and silent truncation on SQLite (§10.3).
MAX_KEY_LENGTH = 64

#: The same, for the human-readable half.
MAX_LABEL_LENGTH = 128

#: Where a new row goes when the caller does not say. Far enough past the seeded rows that an
#: installation's own additions sort after ours without having to renumber anything.
_APPENDED = 1000


def _refuse_a_key_that_is_not_one (key: str, *, field: str) -> str:
	"""Return the key, or refuse a shape that cannot be sent back.

	A key travels in a request body and in a query string, so it may not carry whitespace —
	and it is what a client branches on where a category is absent, so an empty one is
	unusable. This is deliberately looser than a project key (§5.4), which is a path segment.
	"""

	cleaned = key.strip()

	if not cleaned or cleaned != key or any(character.isspace() for character in cleaned):
		raise subroutine.errors.ValidationError(
			f"{key!r} cannot be used as a key.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message="A key has no spaces and is not empty.",
					hint="It is what a caller sends back — 'in_review' rather than 'In review'.",
				)
			],
		)

	if len(cleaned) > MAX_KEY_LENGTH:
		raise subroutine.errors.ValidationError(
			f"That key is longer than {MAX_KEY_LENGTH} characters.",
			errors=[
				subroutine.errors.FieldError(
					field=field, code="invalid_field_value", message="Too long."
				)
			],
		)

	return cleaned


def _refuse_a_label_that_is_not_one (label: str, *, field: str) -> str:
	"""Return the label, or refuse an empty or over-long one."""

	cleaned = label.strip()

	if not cleaned:
		raise subroutine.errors.ValidationError(
			"A label cannot be empty.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message="This is what a person reads; it has to say something.",
				)
			],
		)

	if len(cleaned) > MAX_LABEL_LENGTH:
		raise subroutine.errors.ValidationError(
			f"That label is longer than {MAX_LABEL_LENGTH} characters.",
			errors=[
				subroutine.errors.FieldError(
					field=field, code="invalid_field_value", message="Too long."
				)
			],
		)

	return cleaned


def _refuse_a_duplicate_key (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str | None,
	key: str,
	field: str,
	excluding: uuid.UUID | None = None,
) -> None:
	"""Refuse a key this workspace already uses, before the constraint does.

	**Not the oracle `#161` forbids.** That rule is about a uniqueness check leaking the
	existence of something the caller cannot see; a vocabulary is workspace-wide and anybody
	who may write one may already list them all, so there is nothing here to leak.
	"""

	if entity_type is None:
		model: typing.Any = subroutine.db.models.vocabulary.LinkType
		where = [model.workspace_id == workspace_id, model.key == key]

	else:
		model = subroutine.db.models.vocabulary.Status
		where = [
			model.workspace_id == workspace_id,
			model.entity_type == entity_type,
			model.key == key,
		]

	if excluding is not None:
		where.append(model.id != excluding)

	if session.scalars(sqlalchemy.select(model).where(*where)).first() is None:
		return

	raise subroutine.errors.Conflict(
		f"This workspace already has {key!r}.",
		errors=[
			subroutine.errors.FieldError(
				field=field, code="duplicate_key", message="Pick another key, or edit that one."
			)
		],
	)


def _in_use (session: sqlalchemy.orm.Session, *, column: typing.Any, value: uuid.UUID) -> int:
	"""Return how many rows point at this vocabulary entry."""

	# A count rather than an existence check, because the refusal says how many: "3 tasks" is
	# actionable where "it is in use" sends somebody looking.
	return int(
		session.scalar(sqlalchemy.select(sqlalchemy.func.count()).where(column == value)) or 0
	)


def _refuse_removing_something_in_use (holders: dict[str, int], *, what: str) -> None:
	"""Refuse a removal the database would refuse anyway, in the caller's words.

	The foreign keys are ``ondelete="RESTRICT"``, so this is not the safety — the database is.
	What this adds is a sentence somebody can act on instead of an ``IntegrityError`` surfacing
	as a 500 naming a constraint, which is `#46`'s shape.
	"""

	using = {name: count for name, count in holders.items() if count}

	if not using:
		return

	counted = ", ".join(f"{count} {name}" for name, count in sorted(using.items()))

	raise subroutine.errors.InUse(
		f"{what} is still being used by {counted}.",
		hint="Move them to something else first, and then remove it.",
	)


def statuses (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str | None = None,
) -> list[subroutine.db.models.vocabulary.Status]:
	"""Return this workspace's statuses, in the order a client should show them."""

	model = subroutine.db.models.vocabulary.Status
	where = [model.workspace_id == workspace_id]

	if entity_type is not None:
		where.append(model.entity_type == entity_type)

	return list(
		session.scalars(
			sqlalchemy.select(model).where(*where).order_by(model.entity_type, model.position)
		)
	)


def _only_one_default (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	keeping: uuid.UUID,
) -> None:
	"""Clear every other default for this entity type — §10.7 invariant 6.

	**Nothing enforced this and two defaults were reachable.** It matters because
	``tasks.create`` asks for *the* default: with two, the status a task lands in depends on
	which row the database returns first, so the same call files two tasks differently on two
	days and nothing reports it.
	"""

	model = subroutine.db.models.vocabulary.Status

	session.execute(
		sqlalchemy.update(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == entity_type,
			model.id != keeping,
			model.is_default.is_(True),
		)
		.values(is_default=False)
	)


def create_status (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	key: str,
	label: str,
	category: str,
	is_default: bool = False,
	position: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.Status:
	"""Add a status to this workspace's vocabulary."""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.STATUS_WRITE, workspace_id=workspace_id
		)

	if entity_type not in subroutine.db.mixins.STATUS_ENTITY_TYPES:
		raise subroutine.errors.ValidationError(
			f"{entity_type!r} is not something that has statuses.",
			errors=[
				subroutine.errors.FieldError(
					field="entity_type",
					code="invalid_field_value",
					message=f"One of: {', '.join(sorted(subroutine.db.mixins.STATUS_ENTITY_TYPES))}.",
				)
			],
		)

	# **The two vocabularies are refused against each other by name.** A document's statuses
	# map onto `draft`/`current`/`superseded`/`archived` and a task's onto
	# `todo`/`in_progress`/`done`/`cancelled`, because a superseded specification is not
	# "done" — so `STATUS_CATEGORIES` is the union and is the wrong thing to check against.
	# `documents.statuses_in_category` refuses the other set the same way.
	allowed = (
		subroutine.db.mixins.DOCUMENT_STATUS_CATEGORIES
		if entity_type == "document"
		else subroutine.db.mixins.TASK_STATUS_CATEGORIES
	)

	if category not in allowed:
		raise subroutine.errors.ValidationError(
			f"{category!r} is not a status category for a {entity_type}.",
			errors=[
				subroutine.errors.FieldError(
					field="category",
					code="invalid_field_value",
					message=f"One of: {', '.join(allowed)}.",
					hint="The category is the fixed meaning; the key is yours to choose.",
				)
			],
		)

	cleaned = _refuse_a_key_that_is_not_one(key, field="key")

	_refuse_a_duplicate_key(
		session,
		workspace_id=workspace_id,
		entity_type=entity_type,
		key=cleaned,
		field="key",
	)

	status = subroutine.db.models.vocabulary.Status(
		workspace_id=workspace_id,
		entity_type=entity_type,
		key=cleaned,
		label=_refuse_a_label_that_is_not_one(label, field="label"),
		category=category,
		is_default=is_default,
		position=_APPENDED if position is None else position,
	)

	session.add(status)
	session.flush()

	if is_default:
		_only_one_default(
			session, workspace_id=workspace_id, entity_type=entity_type, keeping=status.id
		)

	return status


def update_status (
	session: sqlalchemy.orm.Session,
	status: subroutine.db.models.vocabulary.Status,
	*,
	key: str = subroutine.domain.patch.UNSET,
	label: str = subroutine.domain.patch.UNSET,
	is_default: bool = subroutine.domain.patch.UNSET,
	position: int = subroutine.domain.patch.UNSET,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.Status:
	"""Rename or reposition a status.

	**``category`` is not here and that is the decision** — see this module's docstring. It is
	settable once, when the status is created.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.STATUS_WRITE,
			workspace_id=status.workspace_id,
		)

	if key is not subroutine.domain.patch.UNSET:
		cleaned = _refuse_a_key_that_is_not_one(key, field="key")

		_refuse_a_duplicate_key(
			session,
			workspace_id=status.workspace_id,
			entity_type=status.entity_type,
			key=cleaned,
			field="key",
			excluding=status.id,
		)

		if cleaned != status.key:
			_rename_in_settings(session, status.workspace_id, was=status.key, now=cleaned)

		status.key = cleaned

	if label is not subroutine.domain.patch.UNSET:
		status.label = _refuse_a_label_that_is_not_one(label, field="label")

	if position is not subroutine.domain.patch.UNSET:
		status.position = position

	if is_default is not subroutine.domain.patch.UNSET:
		if not is_default and status.is_default:
			raise subroutine.errors.ValidationError(
				"Something has to be the default.",
				errors=[
					subroutine.errors.FieldError(
						field="is_default",
						code="invalid_field_value",
						message="Make another status the default instead; this one stops being it.",
					)
				],
			)

		status.is_default = is_default

		if is_default:
			_only_one_default(
				session,
				workspace_id=status.workspace_id,
				entity_type=status.entity_type,
				keeping=status.id,
			)

	session.flush()

	return status


def delete_status (
	session: sqlalchemy.orm.Session,
	status: subroutine.db.models.vocabulary.Status,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Remove a status nothing is in."""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.STATUS_WRITE,
			workspace_id=status.workspace_id,
		)

	if status.is_default:
		raise subroutine.errors.ValidationError(
			"That is the default status, so a new item would have nowhere to go.",
			errors=[
				subroutine.errors.FieldError(
					field="is_default",
					code="invalid_field_value",
					message="Make another status the default first.",
				)
			],
		)

	_refuse_removing_something_in_use(
		{
			"tasks": _in_use(
				session, column=subroutine.db.models.work.Task.status_id, value=status.id
			),
			"documents": _in_use(
				session, column=subroutine.db.models.work.Document.status_id, value=status.id
			),
			"projects": _in_use(
				session,
				column=subroutine.db.models.project.Project.status_id,
				value=status.id,
			),
		},
		what=f"{status.label!r}",
	)

	_forget_in_settings(session, status.workspace_id, key=status.key)
	session.delete(status)
	session.flush()


def link_types (
	session: sqlalchemy.orm.Session, *, workspace_id: uuid.UUID
) -> list[subroutine.db.models.vocabulary.LinkType]:
	"""Return this workspace's link types."""

	model = subroutine.db.models.vocabulary.LinkType

	return list(
		session.scalars(
			sqlalchemy.select(model).where(model.workspace_id == workspace_id).order_by(model.key)
		)
	)


def _refuse_a_category_that_is_not_one (category: str) -> str:
	"""Refuse a link-type category outside the vocabulary, naming the ones there are.

	**In the service rather than left to the CHECK constraint** — a CHECK is not input
	validation. It would arrive as a driver error naming no field on PostgreSQL, and on SQLite
	it might not fire at all, where this names the field and lists the alternatives.

	Its sibling for statuses is inline in :func:`create_status`, because that one picks between
	two vocabularies by entity type. A link type has one.
	"""

	if category in subroutine.db.mixins.LINK_TYPE_CATEGORIES:
		return category

	raise subroutine.errors.ValidationError(
		f"{category!r} is not a link category.",
		errors=[
			subroutine.errors.FieldError(
				field="category",
				code="invalid_field_value",
				message=f"One of: {', '.join(subroutine.db.mixins.LINK_TYPE_CATEGORIES)}.",
				hint=(
					"The category is what the program concludes from the relation; the key is "
					"yours to name. 'gating' holds work up, 'ordering' says which comes first "
					"without holding anything up, 'governing' says one binds the other, and "
					"'describing' says only that they are connected."
				),
			)
		],
	)


def create_link_type (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	key: str,
	title: str,
	inverse_title: str,
	category: str,
	is_symmetric: bool = False,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.LinkType:
	"""Add a way two items can relate.

	**``is_symmetric`` is settable here and nowhere else**, for the same reason a status
	category is: it decides how every edge already stored reads from each end, so changing it
	later would rewrite the meaning of existing data rather than the wording of it.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.LINK_TYPE_WRITE, workspace_id=workspace_id
		)

	cleaned = _refuse_a_key_that_is_not_one(key, field="key")

	_refuse_a_duplicate_key(
		session, workspace_id=workspace_id, entity_type=None, key=cleaned, field="key"
	)

	kind = subroutine.db.models.vocabulary.LinkType(
		workspace_id=workspace_id,
		key=cleaned,
		title=_refuse_a_label_that_is_not_one(title, field="title"),
		inverse_title=_refuse_a_label_that_is_not_one(inverse_title, field="inverse_title"),
		category=_refuse_a_category_that_is_not_one(category),
		is_symmetric=is_symmetric,
	)

	session.add(kind)
	session.flush()

	return kind


def update_link_type (
	session: sqlalchemy.orm.Session,
	kind: subroutine.db.models.vocabulary.LinkType,
	*,
	key: str = subroutine.domain.patch.UNSET,
	title: str = subroutine.domain.patch.UNSET,
	inverse_title: str = subroutine.domain.patch.UNSET,
	category: str = subroutine.domain.patch.UNSET,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.LinkType:
	"""Rename a link type, reword either end of it, or say what it does."""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.LINK_TYPE_WRITE, workspace_id=kind.workspace_id
		)

	if key is not subroutine.domain.patch.UNSET:
		cleaned = _refuse_a_key_that_is_not_one(key, field="key")

		_refuse_a_duplicate_key(
			session,
			workspace_id=kind.workspace_id,
			entity_type=None,
			key=cleaned,
			field="key",
			excluding=kind.id,
		)

		kind.key = cleaned

	if title is not subroutine.domain.patch.UNSET:
		kind.title = _refuse_a_label_that_is_not_one(title, field="title")

	if inverse_title is not subroutine.domain.patch.UNSET:
		kind.inverse_title = _refuse_a_label_that_is_not_one(
			inverse_title, field="inverse_title"
		)

	# **Changeable, where a status category is not**, and the asymmetry is the point. A status
	# category decides how every row already stored reads; this decides what the program
	# concludes from an edge, and a workspace whose own relation came out of `#1157`'s migration
	# as `describing` has to be able to say what it actually is.
	if category is not subroutine.domain.patch.UNSET:
		kind.category = _refuse_a_category_that_is_not_one(category)

	session.flush()

	return kind


def delete_link_type (
	session: sqlalchemy.orm.Session,
	kind: subroutine.db.models.vocabulary.LinkType,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Remove a link type nothing is joined by.

	**Not in docs/design.md §5.5's table, which names only ``GET/POST /v1/link-types``.** Built
	anyway, because a create with no matching remove is the shape `#704` is: a workspace could
	add a link type and never be rid of it, and finding that out costs somebody a row in the
	database they cannot reach.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.LINK_TYPE_WRITE, workspace_id=kind.workspace_id
		)

	_refuse_removing_something_in_use(
		{"links": _in_use(
			session, column=subroutine.db.models.work.Link.link_type_id, value=kind.id
		)},
		what=f"{kind.title!r}",
	)

	session.delete(kind)
	session.flush()


def create_tag (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	name: str,
	description: str | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.Tag:
	"""Declare a tag before anybody uses it, and say what it means here.

	**A tag is still made by being used** (§5.8) — ``tags.ensure`` is untouched and takes no
	actor, deliberately, because applying a label is part of a write the caller was already
	permitted. This is the other door: declaring one in advance, with a description, which is
	the only place a workspace can write down what its own label means (`#905`).
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.TAG_WRITE, workspace_id=workspace_id
		)

	tag = subroutine.domain.tags.ensure(session, workspace_id=workspace_id, names=[name])[0]

	if description is not None:
		tag.description = description.strip() or None

	session.flush()

	return tag


def update_tag (
	session: sqlalchemy.orm.Session,
	tag: subroutine.db.models.vocabulary.Tag,
	*,
	name: str = subroutine.domain.patch.UNSET,
	description: str | None = subroutine.domain.patch.UNSET,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.vocabulary.Tag:
	"""Rename a tag, or say what it means in this workspace."""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.TAG_WRITE, workspace_id=tag.workspace_id
		)

	if name is not subroutine.domain.patch.UNSET:
		cleaned = subroutine.domain.text.fit(
			subroutine.domain.text.require(name, field="name", label="tag"),
			field="name",
			limit=subroutine.domain.tags.MAX_NAME_LENGTH,
			label="tag",
		)
		normalized = subroutine.domain.tags.normalize(cleaned)

		# **Through `ensure`'s own rule rather than a second copy of it** — a name that is
		# entirely digits is a reference and not a tag (§6.15), and that rule lives in one
		# function every tag passes through whatever created it.
		if normalized != tag.name_normalized:
			existing = session.scalars(
				sqlalchemy.select(subroutine.db.models.vocabulary.Tag).where(
					subroutine.db.models.vocabulary.Tag.workspace_id == tag.workspace_id,
					subroutine.db.models.vocabulary.Tag.name_normalized == normalized,
				)
			).first()

			if existing is not None:
				raise subroutine.errors.Conflict(
					f"This workspace already has a tag called {existing.name!r}.",
					errors=[
						subroutine.errors.FieldError(
							field="name",
							code="duplicate_key",
							message="Two tags cannot share a name; merging them is not this.",
						)
					],
				)

		tag.name = cleaned
		tag.name_normalized = normalized

	if description is not subroutine.domain.patch.UNSET:
		tag.description = None if description is None else description.strip() or None

	session.flush()

	return tag


def delete_tag (
	session: sqlalchemy.orm.Session,
	tag: subroutine.db.models.vocabulary.Tag,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Remove a tag, and with it every application of it.

	**No "in use" refusal here, unlike a status**, and §5.5's table says so by omission —
	*Rename / delete*, where the status row carries *(fails if in use)*. Removing a label
	*means* taking it off the things it is on; refusing until somebody had untagged every item
	by hand would make the command useless exactly when it is wanted.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.TAG_WRITE, workspace_id=tag.workspace_id
		)

	for association in (
		subroutine.db.models.work.TaskTag,
		subroutine.db.models.work.DocumentTag,
	):
		session.execute(sqlalchemy.delete(association).where(association.tag_id == tag.id))

	session.delete(tag)
	session.flush()


def _rename_in_settings (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, *, was: str, now: str
) -> None:
	"""Carry a renamed status key into every setting that names it.

	``hidden_statuses`` stores **keys**, on a workspace and on any project (`#1029`). A rename
	that left them behind would not fail — it would quietly stop hiding, which is the worst of
	the three outcomes because nothing reports it and the list still looks configured.
	"""

	_rewrite_hidden(session, workspace_id, change=lambda keys: [
		now if key == was else key for key in keys
	])


def _forget_in_settings (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, *, key: str
) -> None:
	"""Drop a removed status key from every setting that names it."""

	_rewrite_hidden(session, workspace_id, change=lambda keys: [
		held for held in keys if held != key
	])


def _rewrite_hidden (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	*,
	change: typing.Callable[[list[str]], list[str]],
) -> None:
	"""Apply a change to ``hidden_statuses`` wherever this workspace stores one.

	**The whole map is replaced, never mutated in place** — SQLAlchemy does not watch inside a
	JSON column, so ``settings[key] = value`` is silently never written (`#42`). And it is
	written only when the value actually changes, or an identical dict would still mark the row
	dirty and move ``updated_at``.
	"""

	held = subroutine.domain.settings.HIDDEN_STATUSES.key
	workspace = session.get(subroutine.db.models.identity.Workspace, workspace_id)
	rows: list[typing.Any] = [] if workspace is None else [workspace]

	rows.extend(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.project.Project).where(
				subroutine.db.models.project.Project.workspace_id == workspace_id
			)
		)
	)

	for row in rows:
		stored = row.settings or {}
		keys = stored.get(held)

		if not isinstance(keys, list):
			continue

		wanted = change([str(key) for key in keys])

		if wanted == keys:
			continue

		row.settings = {**stored, held: wanted}
