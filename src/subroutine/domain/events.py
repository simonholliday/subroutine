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
import subroutine.domain.authentication


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
	changes: dict[str, typing.Any] | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.activity.Event:
	"""Append one event to the change feed.

	``actor`` is optional because some writes have no principal behind them: seeding, a
	migration's data fix, and ``subroutine init`` all happen before anyone has logged in.
	Recording those as system actions is more honest than attributing them to whoever
	happened to run the command.
	"""

	event = subroutine.db.models.activity.Event(
		workspace_id=workspace_id,
		actor_user_id=None if actor is None else actor.user.id,
		actor_token_id=None if actor is None or actor.token is None else actor.token.id,
		entity_type=entity_type,
		entity_id=entity_id,
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
	"""

	model = subroutine.db.models.activity.Event
	statement = sqlalchemy.select(model).where(model.workspace_id.in_(workspace_ids))

	if entity_type is not None:
		statement = statement.where(model.entity_type == entity_type)

	if entity_id is not None:
		statement = statement.where(model.entity_id == entity_id)

	if upper_bound is not None:
		statement = statement.where(model.created_at <= upper_bound)

	return statement


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
