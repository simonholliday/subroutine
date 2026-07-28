"""Creating and moving projects — the container for work and the unit of permission.

A project is a node in a tree, and moving one takes its whole subtree with it. That is the
only genuinely awkward operation in this module, and it is awkward for a reason worth
keeping: the materialised path that makes "everything under here" cheap to read is what
makes moving expensive to write, and reads outnumber moves by a very wide margin.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.mixins
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.errors

#: What a template writes into ``project.settings``, and nothing else (SPEC.md §6.12).
#: Templates are seed-time only: they set defaults and then have no further effect, so a
#: project stays reconfigurable and no template is a cage.
TEMPLATES: dict[str, dict[str, typing.Any]] = {
	"personal": {
		"visible_status_keys": ["open", "done"],
		"require_verification_to_complete": False,
	},
	"software": {
		"visible_status_keys": [
			"open",
			"in_progress",
			"blocked",
			"needs_input",
			"done",
			"cancelled",
		],
		"require_verification_to_complete": True,
	},
	"blank": {
		"visible_status_keys": ["open", "done"],
		"require_verification_to_complete": False,
	},
}


def create (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	key: str,
	title: str,
	description: str | None = None,
	parent: subroutine.db.models.project.Project | None = None,
	template: str = "blank",
	visibility: str = "public",
	owner_id: uuid.UUID | None = None,
	is_inbox: bool = False,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Create a project, placed in the tree and stamped with its template's defaults."""

	normalized_key = normalize_key(key)

	if not normalized_key:
		raise subroutine.errors.ValidationError(
			"A project needs a short key, like 'SR' or 'HOME'.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{key!r} contains nothing usable as a key.",
					hint="Keys are short and uppercase; they become the first half of every ref.",
				)
			],
		)

	if template not in TEMPLATES:
		raise subroutine.errors.ValidationError(
			f"There is no {template!r} project template.",
			errors=[
				subroutine.errors.FieldError(
					field="template",
					code="invalid_field_value",
					message=f"Unknown template {template!r}.",
					hint=f"Available templates: {', '.join(sorted(TEMPLATES))}.",
				)
			],
		)

	if visibility not in subroutine.db.mixins.PROJECT_VISIBILITIES:
		raise subroutine.errors.ValidationError(
			f"A project is public or private, not {visibility!r}.",
			errors=[
				subroutine.errors.FieldError(
					field="visibility",
					code="invalid_field_value",
					message=f"Unknown visibility {visibility!r}.",
					hint=f"Valid values: {', '.join(subroutine.db.mixins.PROJECT_VISIBILITIES)}.",
				)
			],
		)

	if parent is not None and parent.workspace_id != workspace_id:
		raise subroutine.errors.ValidationError(
			"A parent project must be in the same workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="parent_id",
					code="invalid_field_value",
					message="That project belongs to a different workspace.",
				)
			],
		)

	_refuse_duplicate_key(session, workspace_id, normalized_key)

	project = subroutine.db.models.project.Project(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		parent_id=None if parent is None else parent.id,
		visibility=visibility,
		key=normalized_key,
		title=title,
		description=description,
		status_id=default_status(session, workspace_id).id,
		owner_id=owner_id,
		is_inbox=is_inbox,
		template=template,
		settings=dict(TEMPLATES[template]),
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(project, parent, max_depth=max_depth)

	session.add(project)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"key": {"from": None, "to": normalized_key}, "title": {"from": None, "to": title}},
		actor=actor,
	)
	session.flush()

	return project


def move (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	parent: subroutine.db.models.project.Project | None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> int:
	"""Move a project and everything under it, returning how many rows were rewritten."""

	previous_parent = project.parent_id
	previous_path = project.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.project.Project, project, parent, max_depth=max_depth
	)

	if moved == 0:
		return 0

	project.parent_id = None if parent is None else parent.id
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.MOVED,
		changes={
			"parent_id": {"from": previous_parent, "to": project.parent_id},
			"path": {"from": previous_path, "to": project.path},
			"descendants_rewritten": {"from": None, "to": moved - 1},
		},
		actor=actor,
	)
	session.flush()

	return moved


def default_status (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> subroutine.db.models.vocabulary.Status:
	"""Return the status a new project is given."""

	model = subroutine.db.models.vocabulary.Status

	status = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == "project",
			model.is_default.is_(True),
		)
		.order_by(model.position)
	).first()

	if status is None:
		raise subroutine.errors.ValidationError(
			"This workspace has no default project status.",
			code="invalid_status",
			hint="Seed the workspace, or mark one project status as the default.",
		)

	return status


def normalize_key (key: str) -> str:
	"""Return the stored form of a project key: short, uppercase, alphanumeric."""

	return "".join(character for character in key.strip().upper() if character.isalnum())[:16]


def _refuse_duplicate_key (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> None:
	"""Raise if a live project in this workspace already uses this key."""

	model = subroutine.db.models.project.Project

	existing = session.scalars(
		sqlalchemy.select(model.id).where(
			model.workspace_id == workspace_id, model.key == key, model.deleted_at.is_(None)
		)
	).first()

	if existing is not None:
		raise subroutine.errors.Conflict(
			f"A project with the key {key!r} already exists here.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="duplicate_key",
					message=f"The key {key!r} is already in use in this workspace.",
					hint="Keys become the first half of every ref, so they have to be unique.",
				)
			],
		)
