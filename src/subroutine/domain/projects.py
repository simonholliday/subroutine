"""Creating and moving projects — the container for work and the unit of permission.

A project is a node in a tree, and moving one takes its whole subtree with it. That is the
only genuinely awkward operation in this module, and it is awkward for a reason worth
keeping: the materialised path that makes "everything under here" cheap to read is what
makes moving expensive to write, and reads outnumber moves by a very wide margin.
"""

import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.addressing
import subroutine.db.mixins
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.text
import subroutine.errors
import subroutine.permissions

#: The shape a project key must take. It is deliberately identical to the ref half of
#: ``subroutine.domain.mentions.REF_PATTERN``, and that is the whole reason it is
#: constrained: a key becomes the first part of every ref the project mints, and a ref the
#: mention scanner cannot match is a ref that is silently invisible to backlinks for the
#: life of the project.
#:
#: The cost is that a key must be ASCII — 'CAFÉ' is refused. Titles, descriptions, tags and
#: comments are all fully Unicode; only this one identifier is not, because it is the piece
#: that ends up in commit messages, chat and URLs, where being typeable matters more.
KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9]{0,15}")

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

	title = subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"), field="title", limit=512
	)
	_permitted(session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=workspace_id)

	normalized_key = normalize_key(key)

	if not KEY_PATTERN.fullmatch(normalized_key):
		raise subroutine.errors.ValidationError(
			f"{key!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{key!r} contains nothing usable as a key."
					if not normalized_key
					else f"{normalized_key!r} is not a usable key.",
					hint="A key starts with a letter A-Z and continues with letters and "
					"digits, up to 16 characters — 'SR', 'HOME', 'WEB2'.",
				)
			],
		)

	# A key becomes a path segment, and some segments belong to an endpoint. Refused here
	# rather than at the API, because the alternative is a project that exists, is listed,
	# and cannot be opened — and because the CLI can create one without an API in sight.
	if subroutine.addressing.is_reserved_word(normalized_key):
		reserved = ", ".join(sorted(subroutine.addressing.RESERVED_PATH_WORDS))

		raise subroutine.errors.ValidationError(
			f"{normalized_key!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{normalized_key!r} is reserved: a project keyed that way "
					f"would share an address with one of this API's own endpoints.",
					hint=f"Reserved keys are: {reserved}. Any other key is fine.",
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

	_permitted(
		session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=project.workspace_id
	)


	previous_parent = project.parent_id
	previous_path = project.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.project.Project, project, parent, max_depth=max_depth
	)

	if moved == 0:
		return 0

	project.parent_id = None if parent is None else parent.id

	# `version` is the ETag (SPEC.md §8.9), so anything a client can read has to move it.
	# `reparent` rewrote `path` and `depth` on this row and every descendant with one Core
	# UPDATE, which sets no version — so the descendants are bumped here too, or a client
	# holding an ETag for a child cannot tell that the child's path changed.
	project.version += 1
	project.updated_by = None if actor is None else actor.user.id

	model = subroutine.db.models.project.Project
	session.execute(
		sqlalchemy.update(model)
		.where(
			model.workspace_id == project.workspace_id,
			subroutine.domain.hierarchy.subtree(model, project),
			model.id != project.id,
		)
		.values(version=model.version + 1, updated_by=project.updated_by)
		.execution_options(synchronize_session=False)
	)
	session.expire_all()
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


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	permission: str,
	*,
	workspace_id: uuid.UUID,
) -> None:
	"""Check that an actor may do this, or raise. ``None`` is an internal caller.

	See ``domain.tasks._permitted`` for why the ``None`` case is a skip and what stops it
	being a silent hole.
	"""

	if actor is None:
		return

	subroutine.domain.authorization.authorize(
		session, actor, permission, workspace_id=workspace_id
	)


def normalize_key (key: str) -> str:
	"""Return the stored form of a project key: trimmed and upper-cased, nothing more.

	Deliberately *not* a filter. An earlier version dropped any character outside the
	allowed set, which turned ``'CAFÉ'`` into the perfectly valid key ``'CAF'`` — the user
	asked for one project and silently got another. Case-folding is the only change anyone
	would expect to be made on their behalf; everything else is refused by
	:func:`create` with an explanation, which is the honest half of the same job.
	"""

	return key.strip().upper()


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
