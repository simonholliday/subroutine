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

import subroutine.db.models.activity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.errors
import subroutine.permissions

#: The entity types a link may join. ``verification`` is in the schema so a bug can derive
#: from a failing test (§14), and is not creatable through this module until those exist.
LINKABLE = ("task", "document")

#: The category a workspace's own *precedes* would carry: it says which of a pair comes first
#: and holds nothing up. Seeded on nothing (`#1151` is whether it should be), and named here
#: because :data:`SEQUENCING` is the only thing that reads it.
ORDERING = "ordering"

#: What a relation is when it says only that two items are connected — no sequence, no binding.
#: Named because the ring refusal offers one as the alternative, and offering it by *key* is
#: what `#1158` was: advice naming a relation the workspace may not have.
DESCRIBING = "describing"

#: The relation :func:`proposals` builds an edge of, by key rather than by category — the one
#: place a key is still read on purpose, and the reason is at the call site: a proposal
#: *constructs* a link where every other rule *interprets* one, and the two governing relations
#: run opposite ways round.
PROPOSED_TYPE = "documents"

#: What a relation has to *be* for a document at one end of it to bind the other (§5.7,
#: decision `#1157`). A category rather than the key ``documents``, which is what this said
#: until `#1156` measured what that costs: a workspace renaming the key kept the words and
#: lost the behaviour, so *Read first* went empty while the link still read *Documents*.
GOVERNING = "governing"

#: The categories whose rings are a contradiction — anything that asserts which of a pair comes
#: first (decision `#1157`).
#:
#: **Two rather than one, and they nest.** ``gating`` says the source must finish before the
#: target can start; ``ordering`` says only that it comes first. A ring of the second holds no
#: work up and is still a statement that cannot be true — *A before B before A* — which is what
#: `#1154` was: a workspace's own *precedes* could contradict itself and nothing said so.
#:
#: Read by ``domain.readiness`` for the narrower question, which takes ``gating`` alone — and
#: **built from that module's own name for it**, so the nesting is structural rather than two
#: literals that happen to agree. `#1156` is the record of what two agreeing literals cost.
SEQUENCING = frozenset({subroutine.domain.readiness.GATING, ORDERING})


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

	#: What the type *is* — decision `#1157`. Carried beside the key because every rule about a
	#: relation reads this and none may read the key, which a workspace renames freely.
	link_category: str
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
	acted_on: End | None = None,
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
		#
		# **`acted_on` is the item somebody was looking at, which is the source on every path
		# but one** (`#816`). Simon's rule, settling `#815`'s question 3: *the action occurs on
		# the item which is edited to add the link*. An inverse link — `#16 blocked by #17` — is
		# stored as `#17 blocks #16`, because the row records a direction and there is only one
		# of it; the person was on `#16`. So the row and the event deliberately name different
		# items here, and that is not a disagreement: **the row says what is true and the event
		# says what somebody did.**
		#
		# Defaulted to the source rather than made required, because every other caller is a
		# path where the two are the same and passing it would be ceremony.
		subject_type=(acted_on or source).entity_type,
		subject_id=(acted_on or source).id,
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

	if link_type.category not in SEQUENCING:
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

	# **What the two sequencing categories share is *order*, so that is what the sentence says**
	# (`#1158`). It said "cannot block", which is true of `gating` and of nothing else — the rule
	# moved off the key with `#1157` and the wording did not. The relation's own title is quoted
	# beside it, so this reads correctly whatever a workspace calls the thing.
	raise subroutine.errors.Conflict(
		f"{here} cannot come before {there} under {link_type.title!r}, "
		f"because {there} already comes before {here}.",
		code="cycle_detected",
		errors=[
			subroutine.errors.FieldError(
				field="target",
				code="cycle_detected",
				message=f"The chain that comes back is {written}.",
			)
		],
		hint=_why_a_ring_is_wrong(session, link_type, workspace_id=workspace_id),
	)


def _why_a_ring_is_wrong (
	session: sqlalchemy.orm.Session,
	link_type: subroutine.db.models.vocabulary.LinkType,
	*,
	workspace_id: uuid.UUID,
) -> str:
	"""Say what is actually wrong with this ring, and what to do instead.

	**The two categories are wrong in different ways and the hint said only one of them**
	(`#1158`). *Neither could ever be started* is the cost of a ``gating`` ring and is **false**
	of an ``ordering`` one, which holds nothing up by definition — so it told somebody their work
	was stuck when it was not, and sent them to withdraw a link that was costing them nothing.

	**The alternative is resolved rather than spelled.** It named ``relates_to``, a key, in the
	hint of the function `#1157` had just moved off keys — so a workspace that renamed it was
	advised to use a relation it does not have. Offered by title, and the clause is dropped
	where the workspace has nothing to offer, because a suggestion that refuses is worse than
	none.
	"""

	wrong = (
		"Neither could ever be started."
		if link_type.category == subroutine.domain.readiness.GATING
		else "A sequence cannot come back to where it started."
	)

	# **Symmetric, and that is not a refinement — it is what the sentence claims.** Ordering by
	# key alone offered `Duplicates`, which is `describing` and is a specific assertion about a
	# pair rather than a neutral one: *connected without saying which is first* is exactly what
	# `is_symmetric` means, and nothing else here means it. Caught by the test asserting the
	# words rather than the status.
	instead = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.LinkType)
		.where(
			subroutine.db.models.vocabulary.LinkType.workspace_id == workspace_id,
			subroutine.db.models.vocabulary.LinkType.category == DESCRIBING,
			subroutine.db.models.vocabulary.LinkType.is_symmetric.is_(True),
		)
		.order_by(subroutine.db.models.vocabulary.LinkType.key)
	).first()

	if instead is None:
		return f"{wrong} Withdraw a link in that chain."

	return (
		f"{wrong} Withdraw a link in that chain, or join them with {instead.title!r}, "
		f"which says they are connected without saying which is first."
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
	acted_on: End | None = None,
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
		# The subject an unlink names, and it is the same question the creation answers
		# (`#252`, `#816`). Read off the row when nobody says otherwise — an unlink names the
		# link, and the item it was hung on is what decides who may know it went away — but a
		# reader withdrawing an *incoming* link is standing on the target, and recording the
		# source would attribute their work to an item they never opened.
		subject_type=link.source_type if acted_on is None else acted_on.entity_type,
		subject_id=link.source_id if acted_on is None else acted_on.id,
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
				link_category=kind.category,
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


@dataclasses.dataclass(frozen=True)
class Proposed:
	"""A link the writing already implies and nobody has confirmed (`#1137`).

	**It is not a :class:`Related` and must never be rendered as one.** A citation in prose
	is evidence that a link belongs, not the link — *this contradicts `#1131`* is written the
	same way as *this follows `#1131`*, and a graph that filled itself from that would answer
	*what governs this* with edges nobody agreed to. So this carries no id, because there is
	no row: it is a suggestion, and confirming it is an ordinary ``create``.
	"""

	link_type: str
	label: str
	direction: str
	other: End

	#: What the citation was, in a reader's words — *this names it*, *a comment here names it*.
	#: Carried because a proposal a person cannot check is one they can only accept or ignore.
	because: str


#: How a citation is described, by where it was written and which way round it runs.
_BECAUSE = {
	("outgoing", False): "this names it",
	("outgoing", True): "a comment here names it",
	("incoming", False): "it names this",
	("incoming", True): "a comment there names this",
}


def _citations (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifier: uuid.UUID,
) -> dict[tuple[str, uuid.UUID], tuple[str, bool]]:
	"""Return everything this item's prose names or is named by, and how.

	Keyed by the other end, valued by the direction of the citation and whether it was
	written in a comment rather than in the item itself. **The nearest evidence wins**: an
	item's own words are a stronger statement than a comment on it, and both directions are
	stronger than nothing, so a pair cited more than one way is described by the first rule
	that matched rather than by however the rows happened to sort.

	A comment is resolved to the item it is on, exactly as :func:`mentions.backlinks` does —
	a comment has no ref for a reader to open, so a proposal naming one would be unactionable.
	"""

	mention = subroutine.db.models.work.Mention
	comment = subroutine.db.models.activity.Comment
	mine = sqlalchemy.select(comment.id).where(
		comment.entity_type == entity_type,
		comment.entity_id == identifier,
		comment.deleted_at.is_(None),
	)
	found: dict[tuple[str, uuid.UUID], tuple[str, bool]] = {}

	# Ordered so that the strongest description of a pair is written last and wins. The
	# dictionary is keyed by the pair, so a later row for one already seen replaces it.
	for direction, in_a_comment, clause, other in (
		(
			"incoming",
			True,
			sqlalchemy.and_(
				mention.target_type == entity_type,
				mention.target_id == identifier,
				mention.source_type == "comment",
			),
			None,
		),
		(
			"outgoing",
			True,
			sqlalchemy.and_(mention.source_type == "comment", mention.source_id.in_(mine)),
			"target",
		),
		(
			"incoming",
			False,
			sqlalchemy.and_(
				mention.target_type == entity_type,
				mention.target_id == identifier,
				mention.source_type != "comment",
			),
			"source",
		),
		(
			"outgoing",
			False,
			sqlalchemy.and_(
				mention.source_type == entity_type, mention.source_id == identifier
			),
			"target",
		),
	):
		rows = session.scalars(
			sqlalchemy.select(mention).where(mention.workspace_id == workspace_id, clause)
		).all()

		for row in rows:
			if other is None:
				# A comment naming this item: the citing item is whatever the comment is on,
				# which needs the comment row rather than the mention.
				on = session.get(comment, row.source_id)

				if on is None or on.deleted_at is not None:
					continue

				key = (on.entity_type, on.entity_id)
			elif other == "source":
				key = (row.source_type, row.source_id)
			else:
				key = (row.target_type, row.target_id)

			if key == (entity_type, identifier):
				continue

			found[key] = (direction, in_a_comment)

	return found


def proposals (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifier: uuid.UUID,
) -> list[Proposed]:
	"""Return the governing links this item's citations suggest and nobody has made.

	`#1137`. *What governs this* answers from typed links only (`#1124` Q2), because *near* is
	not *governs* and answering the second under the first's name spends the trust the feature
	exists to earn. The cost of that decision is a cold start: on a fresh install nothing is
	typed, so the answer is empty for ever unless somebody knows to reach for a link type.

	**The mention index is evidence that already exists.** If a task's description cites a
	decision, somebody wrote that deliberately. So a citation *proposes* the link, a person or
	an agent confirms it, and the answer stays typed-links-only exactly as decided.

	Three narrowings, and each is the difference between a proposal and noise:

	* **Only a governing document at the other end.** ``documents.GOVERNS`` — a decision, a
	  specification, a design or a dead end. A finding describes and does not bind, and
	  proposing that one governs anything would be the classifier saying something it does not.
	  **The status is deliberately not asked**, unlike :func:`governing`: that answers *what is
	  in force over this*, and this answers *what the link should be*. A draft decision is one
	  somebody is about to agree, and a superseded one is a true statement about how this work
	  came to be what it is.
	* **Only a pair nothing already joins.** Any link at all, of any type: if two items are
	  already related, somebody has looked at this pair, and proposing an edge over the top of
	  their answer is arguing with them.
	* **Only ends this caller may see**, through the same ``_ends`` every other link read
	  uses. §6.15's rule is that a citation from somewhere invisible is omitted rather than
	  reported as hidden — *something you cannot see mentioned this* discloses that activity
	  exists and explains nothing.
	"""

	cited = _citations(
		session, workspace_id=workspace_id, entity_type=entity_type, identifier=identifier
	)

	if not cited:
		return []

	joined = {
		(link.target_type, link.target_id)
		if link.source_type == entity_type and link.source_id == identifier
		else (link.source_type, link.source_id)
		for link, _kind in _touching(
			session,
			workspace_id=workspace_id,
			entity_type=entity_type,
			identifiers=[identifier],
		)
	}
	documents = _governing(
		session,
		{key[1] for key in cited if key[0] == "document"} | {identifier},
	)

	# **Which end governs is decided by type and never by which one wrote the citation.** A
	# decision naming the work it settles and a task naming the decision it follows are the
	# same fact written from two ends, and reading direction off the prose would make them
	# opposite answers.
	if entity_type == "document" and identifier in documents:
		outgoing = True
		wanted = {
			key for key in cited if key not in joined and key[1] not in documents
		}
	else:
		outgoing = False
		wanted = {
			key
			for key in cited
			if key not in joined and key[0] == "document" and key[1] in documents
		}

	if not wanted:
		return []

	# **The one site that names a key on purpose, and it is the exception that proves `#1157`'s
	# rule.** Every other rule about a relation *interprets* an existing link and reads the
	# category; this one **constructs** a proposed one, and a category cannot say which end a
	# document goes at.
	#
	# Both governing relations run opposite ways round — a decision `documents` a task, and a
	# task `derives_from` a specification — and the direction here is computed from which end
	# did the citing. So picking whichever governing type came back first would produce a
	# proposal that is correctly labelled and points the wrong way.
	#
	# The cost of staying keyed is bounded and is the right way for a *suggestion* to fail: a
	# workspace that has renamed this offers no proposals, rather than offering wrong ones.
	kind = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.LinkType).where(
			subroutine.db.models.vocabulary.LinkType.workspace_id == workspace_id,
			subroutine.db.models.vocabulary.LinkType.key == PROPOSED_TYPE,
		)
	).first()

	if kind is None:
		# **A workspace may delete a link type** (`#826`), and one that has deleted this has
		# said it does not use the relation. Proposing links of a type it cannot make would be
		# offering work that refuses.
		return []

	visible = {
		(end.entity_type, end.id): end
		for entity in {key[0] for key in wanted}
		for end in _ends(
			session,
			principal,
			workspace_id=workspace_id,
			entity_type=entity,
			identifiers={key[1] for key in wanted if key[0] == entity},
		)
	}
	found = [
		Proposed(
			link_type=kind.key,
			# Read from the near item's point of view, like every other label here: something
			# documents *this* when the document is at the far end, and *this* documents
			# something when the caller is standing on the document.
			label=kind.title if outgoing else kind.inverse_title,
			direction="outgoing" if outgoing else "incoming",
			other=visible[key],
			because=_BECAUSE[cited[key]],
		)
		for key in wanted
		if key in visible
	]

	return sorted(found, key=lambda one: one.other.ref)


def _governing (
	session: sqlalchemy.orm.Session, identifiers: typing.Collection[uuid.UUID]
) -> set[uuid.UUID]:
	"""Return which of these documents are of a type that binds rather than describes.

	``documents.GOVERNS`` — a decision, a specification, a design or a dead end. A finding
	states what was learnt and a note states something worth keeping; neither is a rule, and
	proposing that one governs anything would have the classifier saying something it does not.
	"""

	if not identifiers:
		return set()

	item_type = subroutine.db.models.vocabulary.ItemType

	return set(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Document.id)
			.join(item_type, item_type.id == subroutine.db.models.work.Document.type_id)
			.where(
				subroutine.db.models.work.Document.id.in_(set(identifiers)),
				item_type.key.in_(subroutine.domain.documents.GOVERNS),
			)
		).all()
	)


@dataclasses.dataclass(frozen=True)
class Governs:
	"""A document in force that a typed link says binds one item (`#1119`).

	``link_type`` is kept because the two ways of saying it are different sentences: a
	``documents`` link is *this decision settles that work*, and a ``derives_from`` link is
	*this work comes out of that specification*. A reader deciding what to read first is
	served by knowing which they are looking at.
	"""

	link_type: str
	document: End


#: **Was a set of keys and is now a category** (decision `#1157`). The two seeded relations it
#: used to name — ``documents`` and ``derives_from`` — are exactly the two the migration files
#: under :data:`GOVERNING`, so nothing about which links bind has changed; what changed is that
#: the rule survives a workspace renaming either of them, which `#1156` measured that it did not.
#:
#: `#1124` Q2, Simon's: project ancestry and the mention index answer *what is near this*,
#: which is a different claim, and a feature answering the second under the first's name
#: teaches a reader to distrust it. So a typed link is the whole of the evidence, and it is why
#: the answer is empty until somebody has said something — which `#1137` is what makes likely.


def governing (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	identifier: uuid.UUID,
) -> list[Governs]:
	"""Return the documents in force that a typed link says govern one item.

	``subroutine://conventions`` narrowed to a single item, and the framing is worth keeping:
	that resource answers *what binds anybody working in this workspace*, and this answers
	*what binds whoever picks this up*. The second is the question `#1035` §4.2 said would
	change how somebody works — **which 5% of the corpus do I need for this task, and what
	tells me** — and today the only thing that answers it is a file on one machine.

	Three rules, each taken from the resource this narrows rather than invented here:

	* **In force, not merely of the right type** (`#1036`). A superseded decision is not a
	  rule, and a draft one is not yet. The status *category* decides it, so a workspace that
	  has renamed ``active`` still gets an answer.
	* **A governing type**, from ``documents.GOVERNS``. A finding states what was learnt and a
	  note states something worth keeping; neither binds, and a ``derives_from`` link to one is
	  a real relationship that is not this question.
	* **Titles and refs, never bodies.** §6.14 makes a document's title state its conclusion,
	  so the list is readable on its own and a reader fetches only the one they need. A reading
	  list that inlined its reading would be the cost it exists to remove.

	Newest first, by ref. A ref is allocated in creation order within a workspace (§6.2), so
	that is the same ordering as newest-first and stays deterministic where ``created_at``
	would not — two documents written in one transaction share an instant.
	"""

	rows = [
		(link, kind)
		for link, kind in _touching(
			session,
			workspace_id=workspace_id,
			entity_type=entity_type,
			identifiers=[identifier],
		)
		if kind.category == GOVERNING
	]

	if not rows:
		return []

	far: dict[uuid.UUID, str] = {}

	for link, kind in rows:
		outgoing = link.source_type == entity_type and link.source_id == identifier
		other_type = link.target_type if outgoing else link.source_type
		other_id = link.target_id if outgoing else link.source_id

		if other_type == "document" and other_id != identifier:
			far.setdefault(other_id, kind.key)

	binding = _in_force(session, workspace_id=workspace_id, identifiers=set(far))

	if not binding:
		return []

	found = [
		Governs(link_type=far[end.id], document=end)
		for end in _ends(
			session,
			principal,
			workspace_id=workspace_id,
			entity_type="document",
			identifiers=binding,
		)
	]

	return sorted(found, key=lambda one: one.document.ref, reverse=True)


def _in_force (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	identifiers: typing.Collection[uuid.UUID],
) -> set[uuid.UUID]:
	"""Return which of these documents both bind and are still current.

	**Two questions and neither answers the other** (`#1036`). The *type* says whether this
	kind of document binds anybody; the *status category* says whether this one still does.
	Asking only the first lists superseded decisions as rules; asking only the second lists
	every current note.
	"""

	if not identifiers:
		return set()

	document = subroutine.db.models.work.Document
	item_type = subroutine.db.models.vocabulary.ItemType
	status = subroutine.db.models.vocabulary.Status

	return set(
		session.scalars(
			sqlalchemy.select(document.id)
			.join(item_type, item_type.id == document.type_id)
			.join(status, status.id == document.status_id)
			.where(
				document.workspace_id == workspace_id,
				document.deleted_at.is_(None),
				document.id.in_(set(identifiers)),
				item_type.key.in_(subroutine.domain.documents.GOVERNS),
				status.category == subroutine.domain.documents.CURRENT_CATEGORY,
			)
		).all()
	)


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
