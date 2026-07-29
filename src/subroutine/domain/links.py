"""Typed relationships between work items.

One stored edge, displayed from both ends. ``blocks``/``blocked by`` is a single row and
the link type carries the inverse label, so nothing has to keep two rows agreeing with each
other (SPEC.md §5.7).

The ends are polymorphic — task or document, in any combination — which is what lets a task
derive from the specification that called for it without a table per pairing. That is the
capability the whole slice is for: writing a spec into the system and deriving the work
from it.

**Permission is the parent's.** A link is not a thing with rules of its own; creating one
needs ``task:write`` on *both* ends, because a link is a change to both. Reading follows
the same principle: an end the caller cannot see is not reported, and never as a refusal —
saying "there is a link to something you may not see" discloses exactly what §7.3a's
existence rule protects.
"""

import dataclasses
import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.errors
import subroutine.permissions

#: The entity types a link may join. ``verification`` is in the schema so a bug can derive
#: from a failing test (§14), and is not creatable through this module until those exist.
LINKABLE = ("task", "document")


@dataclasses.dataclass(frozen=True)
class End:
	"""One end of a link, resolved to a row this caller may actually see."""

	entity_type: str
	id: uuid.UUID
	ref: int
	title: str
	project_id: uuid.UUID


@dataclasses.dataclass(frozen=True)
class Related:
	"""A link as seen from one end: the type, the direction, and what is at the other end.

	``label`` is already the right way round. A caller looking at the blocking task sees
	"Blocks"; a caller looking at the blocked one sees "Blocked by", off the same row.
	"""

	id: uuid.UUID
	link_type: str
	label: str
	direction: str
	other: End
	created_at: datetime.datetime


def create (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	source: End,
	target: End,
	link_type_key: str,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Link:
	"""Join two work items, or return the link that already joins them.

	Idempotent by (source, target, type): asking twice is not an error, because a client
	retrying a request it is unsure landed should not have to find out by getting a
	conflict. A symmetric type is stored once in the direction it was asked for; reading
	handles both ends.
	"""

	link_type = _link_type(session, workspace_id, link_type_key)

	if source.entity_type == target.entity_type and source.id == target.id:
		raise subroutine.errors.ValidationError(
			"Nothing can be linked to itself.",
			errors=[
				subroutine.errors.FieldError(
					field="target",
					code="invalid_field_value",
					message=f"{subroutine.domain.refs.format_ref(source.ref)} is the item this "
					"link starts from.",
				)
			],
		)

	# Both ends, because a link is a change to both. A caller who may write to the spec but
	# not to the private project the task lives in may not join the two.
	for end in (source, target):
		_permitted(session, actor, workspace_id, end)

	model = subroutine.db.models.work.Link

	existing = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id,
			model.source_type == source.entity_type,
			model.source_id == source.id,
			model.target_type == target.entity_type,
			model.target_id == target.id,
			model.link_type_id == link_type.id,
			model.deleted_at.is_(None),
		)
	).first()

	if existing is not None:
		return existing

	link = subroutine.db.models.work.Link(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		source_type=source.entity_type,
		source_id=source.id,
		target_type=target.entity_type,
		target_id=target.id,
		link_type_id=link_type.id,
		created_by=None if actor is None else actor.user.id,
	)
	session.add(link)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="link",
		entity_id=link.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={
			"link_type": {"from": None, "to": link_type.key},
			"source": {"from": None, "to": source.ref},
			"target": {"from": None, "to": target.ref},
		},
		actor=actor,
	)
	session.flush()

	return link


def remove (
	session: sqlalchemy.orm.Session,
	link: subroutine.db.models.work.Link,
	*,
	now: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Link:
	"""Withdraw a link. Soft, and idempotent."""

	if link.deleted_at is not None:
		return link

	for entity_type, identifier in (
		(link.source_type, link.source_id),
		(link.target_type, link.target_id),
	):
		end = resolve(session, actor, workspace_id=link.workspace_id, entity_type=entity_type, identifier=identifier)

		if end is not None:
			_permitted(session, actor, link.workspace_id, end)

	link.deleted_at = now if now is not None else subroutine.db.types.utcnow()
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=link.workspace_id,
		entity_type="link",
		entity_id=link.id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return link


def around (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifier: uuid.UUID,
) -> list[Related]:
	"""Return every link touching one item, from that item's point of view.

	Both directions in one list, each already labelled the way round the caller is looking
	at it. **An end the caller cannot see is dropped**, not reported as hidden: a link is
	only as visible as the thing at the other end of it.
	"""

	model = subroutine.db.models.work.Link
	link_type = subroutine.db.models.vocabulary.LinkType

	rows = session.execute(
		sqlalchemy.select(model, link_type)
		.join(link_type, link_type.id == model.link_type_id)
		.where(
			model.workspace_id == workspace_id,
			model.deleted_at.is_(None),
			sqlalchemy.or_(
				sqlalchemy.and_(model.source_type == entity_type, model.source_id == identifier),
				sqlalchemy.and_(model.target_type == entity_type, model.target_id == identifier),
			),
		)
		.order_by(model.created_at)
	).all()

	found: list[Related] = []

	for link, kind in rows:
		outgoing = link.source_type == entity_type and link.source_id == identifier
		other_type = link.target_type if outgoing else link.source_type
		other_id = link.target_id if outgoing else link.source_id

		other = resolve(
			session,
			principal,
			workspace_id=workspace_id,
			entity_type=other_type,
			identifier=other_id,
		)

		if other is None:
			continue

		found.append(
			Related(
				id=link.id,
				link_type=kind.key,
				# A symmetric type reads the same from both ends, so it keeps its own title
				# rather than being given an inverse it does not have.
				label=kind.title if outgoing or kind.is_symmetric else kind.inverse_title,
				direction="outgoing" if outgoing else "incoming",
				other=other,
				created_at=link.created_at,
			)
		)

	return found


def resolve (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifier: uuid.UUID,
) -> End | None:
	"""Return one end of a link, or ``None`` when this caller cannot see it.

	Narrowed through ``domain.scoping``, so an item in a private project is invisible here
	exactly as it is everywhere else.
	"""

	if entity_type not in LINKABLE:
		return None

	model: typing.Any = (
		subroutine.db.models.work.Task
		if entity_type == "task"
		else subroutine.db.models.work.Document
	)
	statement: typing.Any

	if principal is None:
		# No principal means an internal caller with no narrowing to apply.
		statement = sqlalchemy.select(model).where(model.workspace_id == workspace_id)

	elif entity_type == "task":
		statement = subroutine.domain.scoping.readable_tasks(
			principal,
			workspace_ids=[workspace_id],
			include_deleted=True,
			include_archived=True,
			include_templates=True,
		)

	else:
		statement = subroutine.domain.scoping.readable_documents(
			principal, workspace_ids=[workspace_id], include_deleted=True, include_archived=True
		)

	row = session.scalars(statement.where(model.id == identifier)).first()

	if row is None:
		return None

	return End(
		entity_type=entity_type,
		id=row.id,
		ref=row.ref,
		title=row.title,
		project_id=row.project_id,
	)


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	workspace_id: uuid.UUID,
	end: End,
) -> None:
	"""Check that an actor may change the item at one end of a link."""

	if actor is None:
		return

	subroutine.domain.authorization.authorize(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		workspace_id=workspace_id,
		project=session.get(subroutine.db.models.project.Project, end.project_id),
	)


def _link_type (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.vocabulary.LinkType:
	"""Return a link type by key, naming the valid ones when there is no such thing."""

	model = subroutine.db.models.vocabulary.LinkType

	found = session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace_id, model.key == key)
	).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(model.workspace_id == workspace_id)
		)
	)

	raise subroutine.errors.ValidationError(
		f"There is no link type called {key!r} here.",
		errors=[
			subroutine.errors.FieldError(
				field="link_type",
				code="not_found",
				message=f"No link type with key {key!r} exists in this workspace.",
				hint=f"Valid link types here: {', '.join(available)}.",
			)
		],
	)
