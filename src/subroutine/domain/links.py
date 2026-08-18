"""Typed relationships between work items.

One stored edge, displayed from both ends. ``blocks``/``blocked by`` is a single row and
the link type carries the inverse label, so nothing has to keep two rows agreeing with each
other (docs/design.md §5.7).

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

#: The one link type that says which of a pair comes first, and so the one where a ring of
#: them means work nobody can start. Named here rather than spelled at the two places that
#: read it, because ``domain.readiness`` is the other and the two have to agree about which
#: edges sequence work.
_SEQUENCING = "blocks"


@dataclasses.dataclass(frozen=True)
class End:
	"""One end of a link, resolved to a row this caller may actually see."""

	entity_type: str
	id: uuid.UUID
	ref: int
	title: str
	project_id: uuid.UUID

	#: The row itself, so that a view can render this end **through the renderer it already
	#: has for that kind** rather than resolving a status, a type, a project address and two
	#: usernames a second way (`#970`). What crosses the wire is still a curated subset — a
	#: link's end is not a whole item and must not carry every item's prose — but the subset
	#: is a *projection of one rendering* instead of a parallel one, which is the difference
	#: between a shape that cannot disagree with a row and one that quietly does.
	#:
	#: **`#583`/`#674`'s lesson applied before the drift rather than after it.** Four
	#: renderings of a link line had already diverged when this was written; the fix that
	#: sticks is having one of them.
	row: "subroutine.db.models.work.Task | subroutine.db.models.work.Document | None" = None

	#: Whether the thing at this end is finished (`#210`). Carried because a link is how
	#: `#84` models a milestone — "an item whose blockers are its contents" — and a list of
	#: contents that cannot say which are done is a list nobody can read a milestone off. Every
	#: end used to arrive without it, so ``subroutine show 85`` reported forty-eight completed
	#: blockers as forty-eight outstanding ones.
	#:
	#: **Only a task can be finished.** ``readiness.unblocked`` says so and this agrees: a
	#: document has no state that could finish, so an end that is one is never complete rather
	#: than being judged by a status it does not have.
	is_complete: bool = False


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


@dataclasses.dataclass(frozen=True)
class Edge:
	"""A link as the stored fact it is: this one, joined to that one, this way round.

	The counterpart to :class:`Related`, and the difference is the vantage point rather than
	the contents. ``Related`` answers "what is #13 joined to", so it has a direction and an
	inverted label; an ``Edge`` answers "what joins these items", where there is no single
	item to be looking from — a page holding both ends of ``#12 blocks #13`` has two vantage
	points and the link is still one row.

	So ``label`` is the forward title only. A client wanting "blocked by" reads it from the
	target's side, which is the same inversion the link type already carries and not a second
	place to get it wrong.
	"""

	id: uuid.UUID
	link_type: str
	label: str
	source: End
	target: End
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
	conflict.

	**A symmetric type is idempotent by the *unordered* pair** (`#575`), so asking from
	either end returns the one row that already says it. This sentence used to claim that
	and the query compared the ordered pair, so the far end stored a duplicate.
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

	_refuse_a_loop(
		session,
		workspace_id=workspace_id,
		source=source,
		target=target,
		link_type=link_type,
	)

	model = subroutine.db.models.work.Link

	joins = sqlalchemy.and_(
		model.source_type == source.entity_type,
		model.source_id == source.id,
		model.target_type == target.entity_type,
		model.target_id == target.id,
	)

	if link_type.is_symmetric:
		# **The pair is unordered by definition, so either direction is the same fact**
		# (`#575`). Comparing the ordered pair is right for `blocks` — *A blocks B* and
		# *B blocks A* are different claims, and a contradictory pair at that — and wrong
		# here, where the forward and inverse labels are one word. Linking from the far
		# end stored a second row and the item then rendered two identical lines, which a
		# reader cannot tell apart because a link carries no ref.
		#
		# **Matched rather than refused.** Somebody linking from the other end has made a
		# correct statement and should not be told it is a mistake; they get the row that
		# already says it, which is what idempotence means everywhere else here.
		#
		# Symmetry was honoured on the *reading* side all along — `views.Link` arrives with
		# the label already the right way round — and on one half of the pair only, which
		# is this codebase's signature shape rather than a special case of it.
		joins = sqlalchemy.or_(
			joins,
			sqlalchemy.and_(
				model.source_type == target.entity_type,
				model.source_id == target.id,
				model.target_type == source.entity_type,
				model.target_id == source.id,
			),
		)

	existing = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id,
			joins,
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
		# **The item the link hangs off, so the event can be scoped** (`#252`). Without it
		# `entity_id` names a link row and nothing can decide who may see the event — which is
		# why the change feed excluded link events entirely until now. This is the pair
		# `domain.comments` already uses, and `scoping.visible_events` narrows on it without
		# knowing what kind of thing wrote it.
		subject_type=source.entity_type,
		subject_id=source.id,
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


def _refuse_a_loop (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	source: End,
	target: End,
	link_type: subroutine.db.models.vocabulary.LinkType,
) -> None:
	"""Refuse a ``blocks`` link that would close a ring, naming the chain that closes it.

	**Because a ring is silent.** ``A blocks B blocks A`` leaves both items permanently
	un-ready with nothing anywhere saying why: each is correctly reported as blocked, each
	blocker is correctly reported as unfinished, and the only way out is for somebody to
	notice by hand that the work cannot start. ``errors.py`` has described
	``cycle_detected`` as covering "a chain of blocking links" since the registry was
	written, and nothing produced one — a refusal published and never raised.

	**``blocks`` alone**, because it is the only type :mod:`subroutine.domain.readiness`
	reads: ``relates_to`` and ``documents`` describe a pair rather than sequencing it, and a
	ring of them holds nothing up. Task to task alone for the same reason — a document has
	no state that could finish.
	"""

	if link_type.key != _SEQUENCING:
		return

	if source.entity_type != "task" or target.entity_type != "task":
		return

	chain = _blocks_reaching(
		session,
		workspace_id=workspace_id,
		start=target.id,
		looking_for=source.id,
		link_type_id=link_type.id,
	)

	if chain is None:
		return

	written = " → ".join(
		subroutine.domain.refs.format_ref(ref) for ref in _refs_for(session, chain)
	)
	here = subroutine.domain.refs.format_ref(source.ref)
	there = subroutine.domain.refs.format_ref(target.ref)

	raise subroutine.errors.Conflict(
		f"{here} cannot block {there}, because {there} already blocks {here}.",
		code="cycle_detected",
		errors=[
			subroutine.errors.FieldError(
				field="target",
				code="cycle_detected",
				message=f"The chain that comes back is {written}.",
			)
		],
		hint="Neither could ever be started. Withdraw a link in that chain, or join them "
		"with 'relates_to', which says they are connected without saying which is first.",
	)


def _blocks_reaching (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	start: uuid.UUID,
	looking_for: uuid.UUID,
	link_type_id: uuid.UUID,
) -> list[uuid.UUID] | None:
	"""Return the chain of live ``blocks`` links from one task to another, or ``None``.

	Breadth-first, **one query per level rather than one per node**, so a chain three deep
	costs three statements however wide it is. Measured on this instance's own backlog when
	the ordering was designed: 20 live blocking edges across 172 open tasks, deepest
	transitive reach 3. It terminates on the visited set rather than on a depth limit, so a
	graph that grows deeper is answered correctly rather than approximately.
	"""

	model = subroutine.db.models.work.Link
	came_from: dict[uuid.UUID, uuid.UUID] = {}
	frontier = [start]
	seen = {start}

	while frontier:
		edges = session.execute(
			sqlalchemy.select(model.source_id, model.target_id).where(
				model.workspace_id == workspace_id,
				model.link_type_id == link_type_id,
				model.source_type == "task",
				model.target_type == "task",
				model.source_id.in_(frontier),
				model.deleted_at.is_(None),
			)
		).all()

		frontier = []

		for came, reached in edges:
			if reached in seen:
				continue

			seen.add(reached)
			came_from[reached] = came

			if reached == looking_for:
				return _walked_back(came_from, start=start, end=reached)

			frontier.append(reached)

	return None


def _walked_back (
	came_from: dict[uuid.UUID, uuid.UUID], *, start: uuid.UUID, end: uuid.UUID
) -> list[uuid.UUID]:
	"""Return the path from ``start`` to ``end``, read out of how each node was reached."""

	chain = [end]

	while chain[-1] != start:
		chain.append(came_from[chain[-1]])

	return list(reversed(chain))


def _refs_for (
	session: sqlalchemy.orm.Session, identifiers: list[uuid.UUID]
) -> list[int]:
	"""Return the refs of these tasks, in the order they were given.

	Read in one statement rather than per item: the chain is short, and a loop of queries in
	the middle of building a refusal is the kind of thing that only shows up when the refusal
	fires, which is the one time it must not be slow.
	"""

	model = subroutine.db.models.work.Task
	found: dict[uuid.UUID, int] = dict(
		session.execute(
			sqlalchemy.select(model.id, model.ref).where(model.id.in_(identifiers))
		).tuples().all()
	)

	return [found[identifier] for identifier in identifiers if identifier in found]


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
		# The same subject as the creation, read off the row rather than off a caller's
		# argument: an unlink names the link, and the item it was hung on is what decides
		# who may know it went away (`#252`).
		subject_type=link.source_type,
		subject_id=link.source_id,
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

	rows = _touching(
		session, workspace_id=workspace_id, entity_type=entity_type, identifiers=[identifier]
	)
	ends = _ends_by_key(session, principal, workspace_id=workspace_id, rows=rows)
	found: list[Related] = []

	for link, kind in rows:
		outgoing = link.source_type == entity_type and link.source_id == identifier
		other_type = link.target_type if outgoing else link.source_type
		other_id = link.target_id if outgoing else link.source_id
		other = ends.get((other_type, other_id))

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


def edges (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifiers: typing.Sequence[uuid.UUID],
) -> list[Edge]:
	"""Return the links touching any of these items, once each, as source-to-target pairs.

	**Not :func:`around` in a loop, and not only for the query count.** A link is stored
	once and :func:`around` reports it from whichever end was asked about, which is right for
	one item and wrong for a set: a page holding both ends of ``#12 blocks #13`` would report
	that link twice, in opposite directions, and a caller building a graph would have to
	notice they were the same row. An edge names its two ends, so it is the same fact however
	many of the items it touches are on the page.

	Three queries whatever the page size — one for the links, one per entity type for the
	ends they reach. The obvious implementation is one ``around`` per item, which is N+1
	inside the request that exists to remove N+1, and quietly N+M once ``around`` resolves
	each end separately.

	``label`` is the forward title only. There is no inverse here because there is no vantage
	point to invert for; a client that wants "blocked by" reads it off the target.
	"""

	if not identifiers:
		return []

	rows = _touching(
		session, workspace_id=workspace_id, entity_type=entity_type, identifiers=identifiers
	)
	ends = _ends_by_key(session, principal, workspace_id=workspace_id, rows=rows)
	found: list[Edge] = []

	for link, kind in rows:
		source = ends.get((link.source_type, link.source_id))
		target = ends.get((link.target_type, link.target_id))

		# **Both ends, not just the far one.** A link is only as visible as the things it
		# joins, and here neither end is guaranteed to be one of the items asked about.
		if source is None or target is None:
			continue

		found.append(
			Edge(
				id=link.id,
				link_type=kind.key,
				label=kind.title,
				source=source,
				target=target,
				created_at=link.created_at,
			)
		)

	return found


def _touching (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifiers: typing.Collection[uuid.UUID],
) -> list[tuple[typing.Any, typing.Any]]:
	"""Return the link rows touching any of these items, with their types, oldest first."""

	model = subroutine.db.models.work.Link
	link_type = subroutine.db.models.vocabulary.LinkType
	wanted = set(identifiers)

	return [
		(link, kind)
		for link, kind in session.execute(
			sqlalchemy.select(model, link_type)
			.join(link_type, link_type.id == model.link_type_id)
			.where(
				model.workspace_id == workspace_id,
				model.deleted_at.is_(None),
				sqlalchemy.or_(
					sqlalchemy.and_(
						model.source_type == entity_type, model.source_id.in_(wanted)
					),
					sqlalchemy.and_(
						model.target_type == entity_type, model.target_id.in_(wanted)
					),
				),
			)
			.order_by(model.created_at)
		).all()
	]


def _ends_by_key (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	rows: typing.Sequence[tuple[typing.Any, typing.Any]],
) -> dict[tuple[str, uuid.UUID], End]:
	"""Return every end these links reach that this caller may see, keyed by type and id.

	Both ends of every row are gathered before any of them is looked up, so this is one
	query per entity type rather than one per link. An end that is missing from the result is
	one the caller cannot see, and its link goes with it.
	"""

	reached: dict[str, set[uuid.UUID]] = {}

	for link, _kind in rows:
		for side_type, side_id in (
			(link.source_type, link.source_id),
			(link.target_type, link.target_id),
		):
			reached.setdefault(side_type, set()).add(side_id)

	return {
		(end.entity_type, end.id): end
		for kind, of_kind in reached.items()
		for end in _ends(
			session, principal, workspace_id=workspace_id, entity_type=kind, identifiers=of_kind
		)
	}


def _ends (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifiers: typing.Collection[uuid.UUID],
) -> list[End]:
	"""Return the items of one type this caller may see, from one narrowed statement."""

	if entity_type not in LINKABLE or not identifiers:
		return []

	model: typing.Any = (
		subroutine.db.models.work.Task
		if entity_type == "task"
		else subroutine.db.models.work.Document
	)

	return [
		End(
			entity_type=entity_type,
			id=row.id,
			ref=row.ref,
			title=row.title,
			project_id=row.project_id,
			row=row,
			# Read off `completed_at`, not off the status vocabulary: invariant 5 makes that
			# column non-null exactly when the category is done or cancelled, so it answers
			# the same question without joining a table an installation may rename rows in.
			is_complete=entity_type == "task" and row.completed_at is not None,
		)
		for row in session.scalars(
			_visible(principal, workspace_id=workspace_id, entity_type=entity_type).where(
				model.id.in_(identifiers)
			)
		)
	]


def _visible (
	principal: subroutine.domain.authentication.Principal | None,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
) -> typing.Any:
	"""Return the statement selecting the items of one type this caller may see.

	One definition, reached by every path here. Two copies of "which items may this caller
	see" is the pair that comes to disagree, and this project has already paid for that once:
	the agenda kept its own copy of project visibility and the two answers differed.
	"""

	if principal is None:
		# No principal means an internal caller with no narrowing to apply.
		model: typing.Any = (
			subroutine.db.models.work.Task
			if entity_type == "task"
			else subroutine.db.models.work.Document
		)

		return sqlalchemy.select(model).where(model.workspace_id == workspace_id)

	if entity_type == "task":
		return subroutine.domain.scoping.readable_tasks(
			principal,
			workspace_ids=[workspace_id],
			include_deleted=True,
			include_archived=True,
			include_templates=True,
		)

	return subroutine.domain.scoping.readable_documents(
		principal, workspace_ids=[workspace_id], include_deleted=True, include_archived=True
	)


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

	found = _ends(
		session,
		principal,
		workspace_id=workspace_id,
		entity_type=entity_type,
		identifiers=[identifier],
	)

	return found[0] if found else None


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
