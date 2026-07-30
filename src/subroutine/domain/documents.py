"""Specifications, designs, notes, decisions, findings and dead ends.

The sibling of a task, and the reason there are two entities rather than one (SPEC.md
§5.6): a bug is done or not done and carries an assignee, a deadline and an estimate; a
specification is never "done" — it is draft, then active, then superseded — and has an
owner rather than a worker. Half the columns differ, and splitting on that keeps both
models honest.

So there is deliberately **no** ``due_at``, ``planned_for``, ``estimate_minutes`` or
``assignee_id`` here. "The spec must be signed off by Friday" is a *task* of type ``chore``
that ``documents`` the spec, which keeps the deadline in the agenda where a deadline belongs
and means no scheduling query ever has to exclude a document.

What is shared is shared completely: the ref space, the project tree, permissions
(``task:*`` — a document is a work item under the same rules as the task beside it), the
event feed, and the mention index.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.mentions
import subroutine.domain.patch
import subroutine.domain.refs
import subroutine.domain.text
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: SPEC.md §6.10, matching tasks.
MAX_TITLE_LENGTH = 512

#: The status a document moves to when something supersedes it. A category rather than a
#: key, for the reason every other status lookup here uses one: an installation renames
#: them freely.
SUPERSEDED_CATEGORY = "superseded"


def create (
	session: sqlalchemy.orm.Session,
	*,
	project: subroutine.db.models.project.Project,
	title: str,
	body: str | None = None,
	type_key: str = "note",
	status_key: str | None = None,
	parent: subroutine.db.models.work.Document | None = None,
	owner_id: uuid.UUID | None = None,
	supersedes: subroutine.db.models.work.Document | None = None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Write a document into a project, allocating its ref and recording that it happened."""

	cleaned_title = _clean_title(title)

	if parent is not None and parent.project_id != project.id:
		raise subroutine.errors.ValidationError(
			"A section belongs to the same project as the document it is part of.",
			errors=[
				subroutine.errors.FieldError(
					field="parent_id",
					code="invalid_field_value",
					message="That document is in a different project.",
				)
			],
		)

	workspace_id = project.workspace_id

	_permitted(session, actor, subroutine.permissions.TASK_WRITE, project=project)

	item_type = item_type_for(session, workspace_id, type_key)
	status = status_for(session, workspace_id, status_key)

	if supersedes is not None and supersedes.workspace_id != workspace_id:
		raise subroutine.errors.ValidationError(
			"A document can only supersede one in the same workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="supersedes_id",
					code="invalid_field_value",
					message="That document belongs to a different workspace.",
				)
			],
		)

	ref = subroutine.domain.refs.allocate(session, workspace_id)

	document = subroutine.db.models.work.Document(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		project_id=project.id,
		parent_id=None if parent is None else parent.id,
		type_id=item_type.id,
		ref=ref,
		title=cleaned_title,
		body=body,
		status_id=status.id,
		owner_id=owner_id,
		supersedes_id=None if supersedes is None else supersedes.id,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(document, parent, max_depth=max_depth)

	session.add(document)
	session.flush()

	if supersedes is not None:
		_retire(session, supersedes, document, actor=actor)

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=workspace_id,
		source_type="document",
		source_id=document.id,
		texts=(document.title, document.body),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"ref": {"from": None, "to": ref}, "title": {"from": None, "to": cleaned_title}},
		actor=actor,
	)
	session.flush()

	return document


def update (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	title: str = subroutine.domain.patch.UNSET,
	body: str | None = subroutine.domain.patch.UNSET,
	status_key: str = subroutine.domain.patch.UNSET,
	owner_id: uuid.UUID | None = subroutine.domain.patch.UNSET,
	supersedes: subroutine.db.models.work.Document | None = subroutine.domain.patch.UNSET,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Change a document, recording only what actually changed.

	Anything left at ``UNSET`` is untouched; passing ``None`` clears the field (§8.3).
	**Everything is validated before anything is assigned**, for the reason ``tasks.update``
	gives: a rejected update must leave the row exactly as it was, because the caller holds a
	live session it may still commit.
	"""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		project=session.get(subroutine.db.models.project.Project, document.project_id),
		workspace_id=document.workspace_id,
	)
	subroutine.domain.versions.require(document, expected_version, noun="This document")

	# Validation pass. Nothing below this point may raise.
	cleaned_title: typing.Any = (
		subroutine.domain.patch.UNSET
		if title is subroutine.domain.patch.UNSET
		else _clean_title(title)
	)
	status: typing.Any = (
		subroutine.domain.patch.UNSET
		if status_key is subroutine.domain.patch.UNSET
		else status_for(session, document.workspace_id, status_key)
	)

	if (
		supersedes is not subroutine.domain.patch.UNSET
		and supersedes is not None
		and supersedes.id == document.id
	):
		raise subroutine.errors.Conflict(
			"A document cannot supersede itself.",
			code="cycle_detected",
			errors=[
				subroutine.errors.FieldError(
					field="supersedes_id",
					code="cycle_detected",
					message="That is this document.",
				)
			],
		)

	if (
		owner_id is not subroutine.domain.patch.UNSET
		and owner_id is not None
		and session.get(subroutine.db.models.identity.User, owner_id) is None
	):
		raise subroutine.errors.ValidationError(
			"That owner does not exist.",
			errors=[
				subroutine.errors.FieldError(
					field="owner_id", code="not_found", message=f"No user with id {owner_id}."
				)
			],
		)

	# Assignment pass.
	changes: dict[str, typing.Any] = {}
	previous_text = (document.title, document.body)

	for field, value in (
		("title", cleaned_title),
		("body", body),
		("owner_id", owner_id),
		("status_id", None if status is subroutine.domain.patch.UNSET else status.id),
	):
		if value is subroutine.domain.patch.UNSET:
			continue

		if field == "status_id" and status is subroutine.domain.patch.UNSET:
			continue

		existing = getattr(document, field)

		if existing == value:
			continue

		setattr(document, field, value)
		changes[field] = {"from": existing, "to": value}

	if supersedes is not subroutine.domain.patch.UNSET:
		wanted = None if supersedes is None else supersedes.id

		if document.supersedes_id != wanted:
			changes["supersedes_id"] = {"from": document.supersedes_id, "to": wanted}
			document.supersedes_id = wanted

	if not changes:
		return document

	document.version += 1
	document.updated_by = None if actor is None else actor.user.id

	if "title" in changes or "body" in changes:
		document.content_updated_at = subroutine.db.types.utcnow()

	session.flush()

	if supersedes not in (subroutine.domain.patch.UNSET, None) and "supersedes_id" in changes:
		_retire(session, supersedes, document, actor=actor)

	if (document.title, document.body) != previous_text:
		subroutine.domain.mentions.synchronize(
			session,
			workspace_id=document.workspace_id,
			source_type="document",
			source_id=document.id,
			texts=(document.title, document.body),
		)

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)
	session.flush()

	return document


def delete (
	session: sqlalchemy.orm.Session,
	document: subroutine.db.models.work.Document,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Document:
	"""Move a document to the trash, where it stays recoverable (SPEC.md §6.9)."""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_DELETE,
		project=session.get(subroutine.db.models.project.Project, document.project_id),
		workspace_id=document.workspace_id,
	)
	subroutine.domain.versions.require(document, expected_version, noun="This document")

	if document.deleted_at is not None:
		return document

	document.deleted_at = now if now is not None else subroutine.db.types.utcnow()

	# **The version moves, because a delete is a change.** §8.9's promise is that a change is
	# based on the state you read, and a version that stands still across a soft delete breaks
	# it silently: read at v3, somebody trashes it, and `expected_version: 3` still passes — so
	# you edit a deleted item believing nothing happened. `projects.delete` did this and the
	# other two did not, which is what kept the gap invisible.
	document.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=document.workspace_id,
		entity_type="document",
		entity_id=document.id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return document


def status_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.Status:
	"""Return a document status by key, or the workspace's default when none is named."""

	return typing.cast(
		subroutine.db.models.vocabulary.Status,
		_vocabulary(
			session,
			subroutine.db.models.vocabulary.Status,
			workspace_id,
			key,
			field="status",
			noun="status",
		),
	)


def item_type_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.ItemType:
	"""Return a document type by key, or the workspace's default when none is named."""

	return typing.cast(
		subroutine.db.models.vocabulary.ItemType,
		_vocabulary(
			session,
			subroutine.db.models.vocabulary.ItemType,
			workspace_id,
			key,
			field="type",
			noun="type",
		),
	)


def _vocabulary (
	session: sqlalchemy.orm.Session,
	model: typing.Any,
	workspace_id: uuid.UUID,
	key: str | None,
	*,
	field: str,
	noun: str,
) -> typing.Any:
	"""Look one vocabulary row up by key, or fall back to the workspace's default.

	One function for both tables because the two lookups differ only in which table they
	read: both are workspace-scoped, both carry an ``entity_type`` discriminator, and both
	have to name the valid alternatives when they fail (SPEC.md §5.5).
	"""

	statement = sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.entity_type == "document"
	)

	if key is None:
		found = session.scalars(
			statement.where(model.is_default.is_(True)).order_by(model.position)
		).first()

	else:
		found = session.scalars(statement.where(model.key == key)).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "document"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		f"This workspace has no default document {noun}."
		if key is None
		else f"There is no document {noun} called {key!r} here.",
		code="invalid_status" if noun == "status" else "invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="not_found",
				message=f"No document {noun} with key {key!r} exists in this workspace.",
				hint=f"Valid keys here: {', '.join(available)}."
				if available
				else "This workspace's vocabulary is incomplete.",
			)
		],
	)


def _retire (
	session: sqlalchemy.orm.Session,
	superseded: subroutine.db.models.work.Document,
	by: subroutine.db.models.work.Document,
	*,
	actor: subroutine.domain.authentication.Principal | None,
) -> None:
	"""Move a superseded document to the status that says so (SPEC.md §6.14).

	Done here rather than left to the caller because the two facts are one fact: a document
	that has been superseded and still reads as ``active`` is a document somebody will act
	on. If the workspace has removed its ``superseded`` status, the link stands and the
	status does not move — an installation is allowed to edit its own vocabulary, and
	refusing the whole operation over it would be worse.
	"""

	model = subroutine.db.models.vocabulary.Status
	replacement = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == superseded.workspace_id,
			model.entity_type == "document",
			model.category == SUPERSEDED_CATEGORY,
		)
		.order_by(model.position)
	).first()

	if replacement is None or superseded.status_id == replacement.id:
		return

	previous = superseded.status_id
	superseded.status_id = replacement.id
	superseded.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=superseded.workspace_id,
		entity_type="document",
		entity_id=superseded.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes={
			"status_id": {"from": previous, "to": replacement.id},
			"superseded_by": {"from": None, "to": by.ref},
		},
		actor=actor,
	)


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	permission: str,
	*,
	project: subroutine.db.models.project.Project | None = None,
	workspace_id: uuid.UUID | None = None,
) -> None:
	"""Check that an actor may do this, or raise. ``None`` is an internal caller.

	See ``domain.tasks._permitted`` for why the ``None`` case is a skip and what stops it
	being a silent hole.
	"""

	if actor is None:
		return

	scope = workspace_id if project is None else project.workspace_id

	if scope is None:
		raise ValueError("A workspace or a project is needed to check a permission against.")

	subroutine.domain.authorization.authorize(
		session, actor, permission, workspace_id=scope, project=project
	)


def _clean_title (title: str) -> str:
	"""Return a usable document title, or refuse with a reason."""

	return subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"),
		field="title",
		limit=MAX_TITLE_LENGTH,
	)
