"""Recording what happened, in the same transaction as the thing that happened.

SPEC.md §10.7 invariant 9: every entity mutation emits at least one ``event`` row, written
inside the caller's transaction. That "inside" is the whole point — an event dispatched
afterwards can be lost when the mutation is rolled back, or recorded for a change that
never landed, and either way the audit trail becomes something you have to corroborate
rather than something you can read.

One table serves four purposes: the audit trail, the activity feed, the change feed
clients poll for what happened while they were away, and the outbox a webhook dispatcher
will later drain. That is why the cost is paid on every write from the first migration
rather than added when someone wants a feed.
"""

import datetime
import enum
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.errors


class EventAction(enum.StrEnum):
	"""What was done to an entity.

	Stored as text rather than a database enum, and open by design: a later feature adds
	its verbs here without a migration. The values are read by clients, so they are as
	stable as the error codes.
	"""

	CREATED = "created"
	UPDATED = "updated"
	DELETED = "deleted"
	RESTORED = "restored"
	MOVED = "moved"

	#: A workspace was stocked with its vocabulary, or an upgrade added to it. Carries the
	#: seed version and the per-kind counts rather than one event per row.
	SEEDED = "seeded"

	#: Reserved for the completion work in slice 2. Until something emits them, a status
	#: change is recorded as an ordinary `updated` with the status in its `changes` —
	#: which is accurate, just less specific than these will be.
	STATUS_CHANGED = "status_changed"
	COMPLETED = "completed"


def record (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
	action: str,
	subject_type: str | None = None,
	subject_id: uuid.UUID | None = None,
	changes: dict[str, typing.Any] | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Event:
	"""Append one event to the change feed.

	``actor`` is optional because some writes have no principal behind them: seeding, a
	migration's data fix, and ``subroutine init`` all happen before anyone has logged in.
	Recording those as system actions is more honest than attributing them to whoever
	happened to run the command.

	``subject_*`` names what the event happened *on* when that is something other than the
	entity itself, and only comments pass it today — see ``selected`` for what reads it.
	"""

	event = subroutine.db.models.activity.Event(
		workspace_id=workspace_id,
		actor_user_id=None if actor is None else actor.user.id,
		actor_token_id=None if actor is None or actor.token is None else actor.token.id,
		entity_type=entity_type,
		entity_id=entity_id,
		subject_type=subject_type,
		subject_id=subject_id,
		action=action,
		changes=None if changes is None else jsonable(changes),
	)
	session.add(event)

	return event


def changes_between (
	before: dict[str, typing.Any], after: dict[str, typing.Any]
) -> dict[str, typing.Any]:
	"""Return the fields that differ, as ``{"field": {"from": …, "to": …}}``.

	Only what actually changed: an update that sets a field to the value it already held
	should not appear in the feed as though something happened, or every client polling
	for real changes has to filter them back out.
	"""

	differences: dict[str, typing.Any] = {}

	for field, new_value in after.items():
		old_value = before.get(field)

		if old_value != new_value:
			differences[field] = {"from": jsonable(old_value), "to": jsonable(new_value)}

	return differences


def selected (
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	entity_type: str | None = None,
	entity_id: uuid.UUID | None = None,
	upper_bound: datetime.datetime | None = None,
	since: int | None = None,
	visible: sqlalchemy.ColumnElement[bool] | None = None,
	actor_token_id: uuid.UUID | None = None,
) -> sqlalchemy.Select[tuple[subroutine.db.models.activity.Event]]:
	"""Return the statement both readers of this table are built on (SPEC.md §5.11a).

	**One builder, and the upper bound is a parameter** — that is the whole design, and it is
	the thing that stops a per-entity history being written as "the feed with a filter".
	``GET /v1/changes`` will pass a watermark of ``now() - 1s``, because it is *resumable*:
	``seq`` is allocated at insert and becomes visible at commit, so a reader that advances
	its cursor past an uncommitted number never sees that event again. A **history** passes
	nothing, because it is not resumable — ask again and the row is still there — and a
	history that inherited the watermark would show nothing for a comment written a moment
	ago, which a person meets in the first minute and reads as a lost write.

	The ordering is the caller's, deliberately: a history runs newest-first and the feed runs
	forwards, and both are served by an index the schema already carries
	(``ix_event_workspace_id_entity_type_entity_id_seq`` and ``ix_event_workspace_id_seq``).

	**This narrows by workspace and nothing else, and that is not an oversight.** An event is
	exactly as visible as the entity it describes, and for a history the caller has *already*
	resolved that entity through ``readable_tasks``/``_projects``/``_documents`` — resolving
	it is the permission check, so re-deriving one here would be a second copy of a rule the
	route has already applied. The feed has no such resolution to lean on and will have to
	compose those predicates itself; §5.11a says so, and it is why the histories came first.

	**The last three arguments belong to the feed alone**, and are stated here so that both
	readers are still built by one function rather than two that agree for a while:

	* ``since`` is a ``seq`` and is **inclusive**. §5.11 fixes cursors as
	  "inclusive-with-dedupe" because a client that persists its cursor before it has finished
	  processing a page must not lose the page — one duplicated row per poll buys that, and
	  every event carries a stable ``id`` to dedupe on.
	* ``visible`` is :func:`subroutine.domain.scoping.visible_events`. It is a *parameter* so
	  that this stays a builder rather than a policy — :func:`feed` is the one place that
	  decides a feed always narrows, and a history always does not.
	* ``actor_token_id`` answers "what did *I* do" (`#158`) — **the credential, not the user**.
	  An agent with its own service-account token wants what it did, not what the person who
	  issued it did from a laptop.

	**Naming an entity asks for what happened *to* it, which is not the same as what was
	recorded *against* it.** Commenting on ``#42`` writes an event whose entity is the comment,
	so a query matching only ``entity_id`` reported that nothing had happened to an item
	somebody had just written a paragraph about — both ways of asking blind at once, since
	``updated_at`` deliberately does not move for a comment either (``#52``). The subject pair
	is the join that fixes it, and matching it here rather than at the route means the feed
	inherits the same answer instead of inventing a second one.
	"""

	model = subroutine.db.models.activity.Event
	statement = sqlalchemy.select(model).where(model.workspace_id.in_(workspace_ids))

	if entity_type is not None and entity_id is not None:
		statement = statement.where(
			sqlalchemy.or_(
				sqlalchemy.and_(model.entity_type == entity_type, model.entity_id == entity_id),
				sqlalchemy.and_(model.subject_type == entity_type, model.subject_id == entity_id),
			)
		)

	else:
		if entity_type is not None:
			statement = statement.where(model.entity_type == entity_type)

		if entity_id is not None:
			statement = statement.where(model.entity_id == entity_id)

	if upper_bound is not None:
		statement = statement.where(model.created_at <= upper_bound)

	if since is not None:
		statement = statement.where(model.seq >= since)

	if visible is not None:
		statement = statement.where(visible)

	if actor_token_id is not None:
		statement = statement.where(model.actor_token_id == actor_token_id)

	return statement


#: How far behind the clock the newest reportable event sits. §5.11 fixes the value because it
#: is client-visible: a caller polling more often than this sees nothing new, and one reasoning
#: about freshness needs to know the feed is deliberately a second stale.
#:
#: **In the domain rather than in the route**, because two clients answer this question — the
#: HTTP endpoint and ``clients.local`` — and a watermark that existed in only one of them would
#: mean the same instance losing events over one transport and not the other. That divergence
#: is what S3-07 removed for tasks and what ``views.py`` sits outside ``api/`` to prevent.
WATERMARK = datetime.timedelta(seconds=1)

#: The lowest ``seq`` a cursor can name. Written down rather than spelled ``1`` at the two
#: places that need it, because a caller sending ``0`` is told this number and would otherwise
#: be told it by a literal nobody had connected to the column it describes.
FIRST_SEQ = 1


def feed (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None = None,
	mine: bool = False,
	newest: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.activity.Event]]:
	"""Return the change feed's statement — ordered, watermarked and narrowed (§5.11a).

	Everything :func:`selected` leaves to the caller, this decides, because for a feed the
	answers are not a caller's to choose: it always runs forwards, it always withholds the last
	second, and it is always narrowed to what the principal may see. A history is the opposite
	on all three counts, and keeping them one function is how they would come to agree only for
	a while.

	``mine`` is ``#158``'s ``?actor=me``. **A caller with no token gets nothing**, rather than
	everything: a session-authenticated principal has no ``actor_token_id`` on anything it
	wrote, so matching on a null token would quietly widen the filter to every system-written
	row — and the belief being tested is precisely "these are the things I did".
	"""

	model = subroutine.db.models.activity.Event
	token_id = None if principal.token is None else principal.token.id

	statement = selected(
		workspace_ids=workspace_ids,
		upper_bound=subroutine.db.types.utcnow() - WATERMARK,
		since=since,
		visible=subroutine.domain.scoping.visible_events(
			principal, workspace_ids=workspace_ids
		),
		actor_token_id=token_id if mine else None,
	)

	if mine and token_id is None:
		statement = statement.where(sqlalchemy.false())

	# **`newest` reads the tail, and :func:`page` turns it the right way up again.** A feed is
	# defined forwards and stays that way in every answer; this is only about which end of a
	# long history the *first* call lands on. Without it somebody meeting an instance with
	# thousands of events is shown its first afternoon and has to page to reach this morning,
	# which is not what "what has changed" asks.
	return statement.order_by(model.seq.desc() if newest else model.seq.asc())


def page (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	size: int,
	since: int | None = None,
	mine: bool = False,
	newest: bool = False,
) -> tuple[list[subroutine.db.models.activity.Event], bool]:
	"""Return one page of the feed, **always oldest first**, and whether more follow.

	The one place the ``newest`` reversal is undone, so that no caller can return a feed
	running backwards and no two callers can disagree about how ``has_more`` was counted.

	``has_more`` means "there is another page in the direction you are reading" — later events
	on an ordinary call, *earlier* ones when ``newest`` is set. Either way the page itself
	reads forwards, and its last row is the number to resume from.

	**And the one place ``since`` overrules ``newest``** (`#310`). Both transports worked that
	rule out for themselves — ``newest=newest and since is None``, written twice — which is the
	shape this module exists to prevent: the watermark and the ``410`` were moved here so that
	a feed could not behave differently over HTTP and locally, and a third rule was left behind
	in both routes. The equivalence suite exercised each argument alone and never together, so
	a divergence in exactly the combination the rule is about would have passed.
	"""

	# `since` says where the caller has got to, which `newest` cannot improve on and would
	# contradict by skipping everything in between.
	newest = newest and since is None

	statement = feed(
		principal, workspace_ids=workspace_ids, since=since, mine=mine, newest=newest
	)
	rows = list(session.scalars(statement.limit(size + 1)))
	has_more = len(rows) > size
	rows = rows[:size]

	if newest:
		rows.reverse()

	return rows, has_more


def refuse_unusable_cursor (
	session: sqlalchemy.orm.Session,
	*,
	since: int | None,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> None:
	"""Refuse a cursor that names nothing, or one pointing further back than this instance holds.

	**Two refusals, and they must be in that order** (`#309`). ``since`` is a ``seq`` and the
	first one is 1, so ``since=0`` names nothing — and, read as a cursor, it is below every
	surviving event and therefore looks exactly like one that expired. That is what happened:
	the route checked ``since >= 1`` for itself and ``clients.local`` did not, so an
	uninitialised cursor — zero, the ordinary default in most languages — got a ``422`` over
	HTTP and, locally, a ``410`` announcing that events had been pruned on an instance that has
	never pruned anything. A refusal that states a cause it has not established is worse than a
	vague one, and this one sent the reader looking for a retention policy that does not exist.

	Both live here rather than in either caller because §5.11a's whole reason for this module is
	that a feed must not answer differently over two transports.

	§5.11 retains events for ``events_retention_days`` and requires ``410 cursor_expired``
	below that floor, so a client resyncs rather than being handed a page that silently omits
	everything pruned in between — the one failure a feed must never have, because it looks
	exactly like nothing having happened.

	**The expiry test is "did events below this point exist and go", not "is this old".** A
	caller resuming from seq 5 on an instance that still holds seq 1 is simply behind, and
	behind is what the feed is for.

	**That half is still unreachable, and honestly so.** Nothing prunes yet (`#251`), so the
	oldest surviving event is the first one ever written and no *legal* cursor can fall below
	it. The path is built and tested by deleting rows, and goes live the day retention does.
	"""

	if since is None:
		return

	if since < FIRST_SEQ:
		raise subroutine.errors.ValidationError(
			f"'since' is a seq and the first one is {FIRST_SEQ}, so {since} names nothing.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="since",
					code="invalid_field_value",
					message="Send the seq of the last event you processed, or omit 'since' "
					"to start from the oldest event still held.",
				)
			],
		)

	model = subroutine.db.models.activity.Event
	oldest = session.scalar(
		sqlalchemy.select(sqlalchemy.func.min(model.seq)).where(
			model.workspace_id.in_(workspace_ids)
		)
	)

	if oldest is None or since >= oldest:
		return

	raise subroutine.errors.CursorExpired(
		f"Events before seq {oldest} are no longer held, so what happened since {since} "
		f"cannot be reported in full.",
		hint="Ask again without 'since' to resync from the oldest event still kept.",
	)


class Described(typing.NamedTuple):
	"""How an item is named to a reader, as against how it is keyed."""

	#: ``None`` for the things that carry no ref — a project, a workspace (§6.2).
	ref: int | None
	title: str


def descriptions (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.activity.Event],
) -> dict[uuid.UUID, Described]:
	"""Return the ref and title of whatever each of ``rows`` is *about*, keyed by that id.

	**"About" is the subject when there is one and the entity otherwise.** An event recording
	a comment names the comment as its entity, which has no ref and no title and is not what a
	reader wants to be told; its subject is the item somebody wrote on, which is. Deciding that
	here rather than in each client is what keeps a CLI, an agent and a future browser saying
	the same thing about the same row.

	**Batched, because the alternative is `#39`'s N+1 by another name.** Three queries per
	page whatever its size, and a page of fifty events touching one task asks about that task
	once.

	Anything unresolvable is simply absent from the map — a workspace, a link, an item hard to
	reach — and the view reports null rather than inventing a name. Nothing here re-checks
	visibility: these rows have already been narrowed by :func:`feed` or resolved through a
	subject, and an event a caller may read is one whose item they may read by construction.
	"""

	wanted: dict[str, set[uuid.UUID]] = {"task": set(), "document": set(), "project": set()}

	for row in rows:
		kind = row.subject_type or row.entity_type
		identifier = row.subject_id or row.entity_id

		if kind in wanted:
			wanted[kind].add(identifier)

	task = subroutine.db.models.work.Task
	document = subroutine.db.models.work.Document
	project = subroutine.db.models.project.Project
	found: dict[uuid.UUID, Described] = {}

	# Written out per model rather than looped over a tuple of them: the three do not share a
	# base that declares `ref` and `title`, and a loop only type-checks by widening to `Base`
	# and then reaching for attributes it cannot promise are there.
	if wanted["task"]:
		for one in session.scalars(sqlalchemy.select(task).where(task.id.in_(wanted["task"]))):
			found[one.id] = Described(ref=one.ref, title=one.title)

	if wanted["document"]:
		for paper in session.scalars(
			sqlalchemy.select(document).where(document.id.in_(wanted["document"]))
		):
			found[paper.id] = Described(ref=paper.ref, title=paper.title)

	if wanted["project"]:
		for folder in session.scalars(
			sqlalchemy.select(project).where(project.id.in_(wanted["project"]))
		):
			found[folder.id] = Described(ref=None, title=folder.title)

	return found


def jsonable (value: typing.Any) -> typing.Any:
	"""Convert a value into something the JSON column can hold.

	UUIDs and datetimes are the two that appear constantly and serialise nowhere by
	default. Anything else unrecognised becomes its string form rather than raising: an
	event that records a change imperfectly is worth more than a mutation that fails
	because its audit record could not be written.
	"""

	if value is None or isinstance(value, str | int | float | bool):
		return value

	if isinstance(value, uuid.UUID):
		return str(value)

	if isinstance(value, datetime.datetime | datetime.date):
		return value.isoformat()

	if isinstance(value, list | tuple | set | frozenset):
		return [jsonable(item) for item in value]

	if isinstance(value, dict):
		return {str(key): jsonable(item) for key, item in value.items()}

	return str(value)
