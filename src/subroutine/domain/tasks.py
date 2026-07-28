"""Creating and editing the thing the whole system exists to hold.

Everything the foundations built meets here: a ref is allocated from the project's
counter, a path is placed in the subtask tree, an event is recorded, and whatever the
description refers to is indexed — all inside one transaction, so a task never exists
without its ref or its history.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.mentions
import subroutine.domain.refs
import subroutine.errors


class _Unset:
	"""The absence of a value, as distinct from ``None``.

	SPEC.md §8.3: on an update, a field that is absent is left alone and a field set to
	``null`` is cleared. Collapsing those two into one would make it impossible to ever
	clear a due date.
	"""

	def __repr__ (self) -> str:
		"""Describe the sentinel in a way that reads clearly in a signature."""

		return "UNSET"


UNSET: typing.Any = _Unset()


def create (
	session: sqlalchemy.orm.Session,
	*,
	project: subroutine.db.models.project.Project,
	title: str,
	description: str | None = None,
	type_key: str = "task",
	status_key: str | None = None,
	parent: subroutine.db.models.work.Task | None = None,
	assignee_id: uuid.UUID | None = None,
	importance: int | None = None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Create a task in a project, allocating its ref and recording that it happened."""

	if not title.strip():
		raise subroutine.errors.ValidationError(
			"A task needs a title.",
			code="missing_field",
			errors=[
				subroutine.errors.FieldError(
					field="title", code="missing_field", message="A task needs a title."
				)
			],
		)

	if parent is not None and parent.project_id != project.id:
		raise subroutine.errors.ValidationError(
			"A subtask belongs to the same project as its parent.",
			errors=[
				subroutine.errors.FieldError(
					field="parent_task_id",
					code="invalid_field_value",
					message="That task is in a different project.",
				)
			],
		)

	workspace_id = project.workspace_id
	item_type = _item_type(session, workspace_id, type_key)
	status = _status(session, workspace_id, status_key)

	ref, number = subroutine.domain.refs.allocate(session, project)

	task = subroutine.db.models.work.Task(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		project_id=project.id,
		parent_task_id=None if parent is None else parent.id,
		type_id=item_type.id,
		ref=ref,
		number=number,
		origin_project_id=project.id,
		title=title.strip(),
		description=description,
		status_id=status.id,
		assignee_id=assignee_id,
		importance=importance,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(task, parent, max_depth=max_depth)

	session.add(task)
	session.flush()

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=workspace_id,
		source_type="task",
		source_id=task.id,
		texts=(task.title, task.description),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"ref": {"from": None, "to": ref}, "title": {"from": None, "to": task.title}},
		actor=actor,
	)
	session.flush()

	return task


def update (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	title: str = UNSET,
	description: str | None = UNSET,
	status_key: str = UNSET,
	assignee_id: uuid.UUID | None = UNSET,
	importance: int | None = UNSET,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Change a task, recording only what actually changed.

	Anything left at ``UNSET`` is untouched; passing ``None`` clears the field. An update
	that changes nothing writes no event, so the change feed stays a record of changes
	rather than of requests.
	"""

	before = _snapshot(task)
	touches_content = False

	if title is not UNSET:
		task.title = title.strip()
		touches_content = True

	if description is not UNSET:
		task.description = description
		touches_content = True

	if status_key is not UNSET:
		task.status_id = _status(session, task.workspace_id, status_key).id
		touches_content = True

	if assignee_id is not UNSET:
		task.assignee_id = assignee_id

	if importance is not UNSET:
		task.importance = importance

	changes = subroutine.domain.events.changes_between(before, _snapshot(task))

	if not changes:
		return task

	# `updated_at` moves on any write; `content_updated_at` moves only when the *meaning*
	# changed. That distinction is what lets a verification know whether it is stale, and
	# stops a repositioning from invalidating evidence (SPEC.md §6.1).
	if touches_content:
		task.content_updated_at = subroutine.db.types.utcnow()

	task.version += 1
	task.updated_by = None if actor is None else actor.user.id
	session.flush()

	if title is not UNSET or description is not UNSET:
		subroutine.domain.mentions.synchronize(
			session,
			workspace_id=task.workspace_id,
			source_type="task",
			source_id=task.id,
			texts=(task.title, task.description),
		)

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)
	session.flush()

	return task


def _snapshot (task: subroutine.db.models.work.Task) -> dict[str, typing.Any]:
	"""Return the fields an update may change, for comparison afterwards."""

	return {
		"title": task.title,
		"description": task.description,
		"status_id": task.status_id,
		"assignee_id": task.assignee_id,
		"importance": task.importance,
	}


def _item_type (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.vocabulary.ItemType:
	"""Return a task type by key, or list the ones this workspace has."""

	model = subroutine.db.models.vocabulary.ItemType

	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id, model.entity_type == "task", model.key == key
		)
	).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "task"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		f"There is no task type called {key!r} here.",
		errors=[
			subroutine.errors.FieldError(
				field="type",
				code="not_found",
				message=f"No task type with key {key!r} exists in this workspace.",
				hint=f"Types here: {', '.join(available)}." if available else None,
			)
		],
	)


def _status (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.Status:
	"""Return a task status by key, or the workspace's default when none is named."""

	model = subroutine.db.models.vocabulary.Status

	statement = sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.entity_type == "task"
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
				model.workspace_id == workspace_id, model.entity_type == "task"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		"This workspace has no default task status."
		if key is None
		else f"There is no task status called {key!r} here.",
		code="invalid_status",
		errors=[
			subroutine.errors.FieldError(
				field="status",
				code="not_found",
				message=f"No task status with key {key!r} exists in this workspace."
				if key is not None
				else "No task status is marked as the default.",
				hint=f"Statuses here: {', '.join(available)}." if available else None,
			)
		],
	)
