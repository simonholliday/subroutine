"""Taking a task, and giving it back — docs/design.md §14.11, item ``#350``.

**A lease, not a lock.** Agents die mid-task routinely — a context ends, a process is killed,
a machine reboots — and a hard lock would strand the work permanently, with no way back except
somebody noticing and intervening. A lease expires on its own, and an expired one is *ignored*
rather than cleaned up: nothing has to run, and a task nobody renewed is simply available
again.

**What this is for, and what it is not.** It stops two workers taking the same item off the
same ranked listing — which is a live problem, because ``?ready=true&order=-priority_score``
deliberately answers the same for everybody, so two agents asking the obvious question collide
by construction. The cost of that collision is not a merge conflict, which git handles: it is
two workers doing the same work, and one of them finding out at the end.

It is **not** a lock on a file, and does not pretend to be. Two agents holding claims on
different tasks can still edit the same module. That is a different problem with a different
answer.

It is **not** assignment either. ``assignee_id`` says who *should* do something and is somebody
else's decision; a claim says who *is doing it right now* and is the worker's own. They are
independent on purpose — an agent may claim an unassigned task, and an assignee who has not
started has not claimed.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.readiness
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: How long a lease lasts, and the most anybody may ask for — **both in `subroutine.config`,
#: which is the only place they can bound the setting as well as the argument** (`#358`).
#:
#: They were declared here and the setting was not held to them: `claim_lease_minutes` went
#: through `_lease` unchecked, so an instance configured with `0` gave every claim an expiry on
#: the instant it was taken — claiming succeeded, printed a confirmation, and did nothing, for
#: every worker at once. Aliased rather than re-declared so this module still reads as the
#: place a lease is decided.
#:
#: The setting itself was `#247`'s family until this module existed: declared, printed by
#: `config show`, and read by nothing.
DEFAULT_LEASE_MINUTES = subroutine.config.DEFAULT_LEASE_MINUTES
MAX_LEASE_MINUTES = subroutine.config.MAX_LEASE_MINUTES


def held_by (
	task: subroutine.db.models.work.Task, *, now: datetime.datetime | None = None
) -> uuid.UUID | None:
	"""Return who currently holds this task, or ``None`` if nobody effectively does.

	**Expiry is answered here rather than by a column**, so that every reader agrees. A row
	whose ``claim_expires_at`` has passed still carries the id of whoever held it last — that
	is deliberate, because "who was working on this" is worth keeping — and this is what makes
	it stop *meaning* anything.
	"""

	moment = now or subroutine.db.types.utcnow()

	if task.claimed_by_id is None or task.claim_expires_at is None:
		return None

	return None if task.claim_expires_at <= moment else task.claimed_by_id


def claim (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	minutes: int | None = None,
	settings: subroutine.config.Settings | None = None,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal,
) -> subroutine.db.models.work.Task:
	"""Take a lease on a task, or renew one this actor already holds.

	Refuses with :class:`~subroutine.errors.Conflict` when somebody else holds it, naming who
	and until when — the two facts that decide what the caller does next. Taking a task
	somebody else is working on is not something to resolve on their behalf.

	**Renewing is the same call.** An agent that is still working says so by claiming again,
	and does not need to know whether its lease has minutes or seconds left. The claimed
	instant stays where it was, so how long this has been in hand is not lost by renewing.

	**Needs ``task:write``.** Claiming is a statement about work in progress and reserves the
	right to do it, so the permission that lets somebody change the task is the one that lets
	them take it — anything narrower would let a reader park work they cannot then do.

	**Taken in one statement, because reading and then writing is not taking it** (`#354`).
	The first version of this read the holder, decided in Python, and then wrote — so two
	workers both saw nobody holding it, both wrote, and both were told they had it. Reproduced
	with two connections: 2 of 2 succeeded, and the row held the first. The second worker then
	does the work it believes it reserved, which is precisely the collision this whole module
	exists to prevent, arriving only under the contention that is the reason for it.

	So the condition lives in the ``WHERE`` clause and the answer is the row count. On
	PostgreSQL the second statement blocks on the row and re-evaluates against what the first
	committed; on SQLite the writers are serialised and the same re-evaluation happens. Neither
	needs ``FOR UPDATE``, which is why this shape was chosen over one.
	"""

	moment = now or subroutine.db.types.utcnow()

	# **Before the conflict is reported, not after.** Whether somebody else is working on this
	# is a fact about the workspace, and a caller who may not touch the task should not learn it.
	_permitted(session, actor, task)

	subroutine.domain.versions.require(task, expected_version, noun="task")

	model = subroutine.db.models.work.Task
	mine = sqlalchemy.and_(
		subroutine.domain.readiness.held(model, now=moment),
		model.claimed_by_id == actor.user.id,
	)
	statement = (
		sqlalchemy.update(model)
		.where(
			model.id == task.id,
			# The listing's own predicate, rather than a second copy of it. What a worker is
			# shown as free to start and what it is allowed to take are then one rule.
			subroutine.domain.readiness.unclaimed(model, now=moment, by=actor.user.id),
		)
		.values(
			claimed_by_id=actor.user.id,
			# **Renewing keeps the instant it was first taken**, so how long this has been in
			# hand is not lost by saying so again. Decided in SQL because the alternative is a
			# read taken before the update, which is the thing that was wrong here.
			claimed_at=sqlalchemy.case((mine, model.claimed_at), else_=moment),
			claim_expires_at=moment + datetime.timedelta(
				minutes=_lease(minutes, settings)
			),
			# **The version moves, for `delete`'s reason.** A claim changes what a caller may
			# safely do next, so a version that stood still across one would let somebody edit
			# on the strength of a read taken before the work was taken.
			version=model.version + 1,
		)
	)

	# Typed as a plain Result, but DML always yields a cursor result and only that carries the
	# row count — which here is the whole answer.
	taken = typing.cast(
		"sqlalchemy.CursorResult[typing.Any]", session.execute(statement)
	)

	# The statement went round the ORM, so the loaded object is stale until it is told so.
	session.expire(task)

	if taken.rowcount != 1:
		raise subroutine.errors.Conflict(
			"Somebody else is working on this.",
			hint=_who_holds_it(session, task),
		)

	# Read back rather than assumed: the statement decided both of these, and reconstructing
	# them here would be the same read-then-believe that #354 was.
	renewed = task.claimed_at != moment
	expires = task.claim_expires_at

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.CLAIMED,
		changes={
			"renewed": renewed,
			"expires_at": None if expires is None else expires.isoformat(),
		},
		actor=actor,
	)
	session.flush()

	return task


def release (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal,
) -> subroutine.db.models.work.Task:
	"""Give a task back, so somebody else can take it.

	Releasing something nobody holds is not an error and records nothing — a caller tidying up
	after itself should not have to check first, and an event saying a lease that had already
	expired was released would be noise in the one place the record is meant to be read.

	**Anybody with ``task:write`` may release, not only the holder.** That is deliberate: the
	case it exists for is an agent that died holding a lease, and requiring its credential to
	give the work back would mean the one principal who cannot act is the one whose action is
	needed. The lease expiring is the ordinary remedy; this is the impatient one.
	"""

	moment = now or subroutine.db.types.utcnow()

	_permitted(session, actor, task)

	subroutine.domain.versions.require(task, expected_version, noun="task")

	if held_by(task, now=moment) is None:
		return task

	# **Cleared entirely rather than expired in place.** A row that kept its holder with a past
	# expiry is indistinguishable from one whose lease simply ran out, and those are different
	# facts: somebody gave this back. The event is what records who held it.
	task.claimed_by_id = None
	task.claimed_at = None
	task.claim_expires_at = None
	task.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.RELEASED,
		actor=actor,
	)
	session.flush()

	return task


def _lease (minutes: int | None, settings: subroutine.config.Settings | None) -> int:
	"""Return how many minutes a lease should last, refusing one nobody could mean."""

	if minutes is None:
		return (
			DEFAULT_LEASE_MINUTES
			if settings is None
			else settings.claim_lease_minutes
		)

	if 1 <= minutes <= MAX_LEASE_MINUTES:
		return minutes

	raise subroutine.errors.ValidationError(
		f"A lease of {minutes} minutes is not one this can take.",
		errors=[
			subroutine.errors.FieldError(
				field="minutes",
				code="invalid_field_value",
				message=f"{minutes} is outside 1 to {MAX_LEASE_MINUTES}.",
				hint="A lease is a promise that the work comes back if the worker does not, "
				f"so it is bounded at {MAX_LEASE_MINUTES} minutes. Renew instead of asking "
				"for longer.",
			)
		],
	)


def _who_holds_it (
	session: sqlalchemy.orm.Session, task: subroutine.db.models.work.Task
) -> str:
	"""Name the holder and when the lease runs out, for a refusal somebody can act on.

	**Both facts, because either alone leaves the caller stuck.** A name without a time gives
	them nobody to wait for; a time without a name gives them nothing to ask.

	**"Somebody since deleted" describes a state the schema forbids**, and that is worth saying
	rather than leaving as an apparent case. ``claimed_by_id`` carries ``ondelete="SET NULL"``,
	so a hard delete clears the column and every other kind of leaving is a soft delete that
	keeps the name readable — the branch exists because ``session.get`` is typed ``User | None``
	and the type checker is owed an answer, not because anybody can reach it. Found by writing
	the test for it (`#360`), which the foreign key refused outright.
	"""

	holder = session.get(subroutine.db.models.identity.User, task.claimed_by_id)
	who = "somebody since deleted" if holder is None else holder.username
	until = "" if task.claim_expires_at is None else (
		f", until {task.claim_expires_at.isoformat(timespec='minutes')}"
	)

	return (
		f"{who} claimed it{until}. Wait for the lease to run out, or ask them — "
		f"'subroutine release <ref>' takes it back if they have finished with it."
	)


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	task: subroutine.db.models.work.Task,
) -> None:
	"""Refuse an actor who may not change this task."""

	subroutine.domain.authorization.authorize(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		workspace_id=task.workspace_id,
		project=session.get(subroutine.db.models.project.Project, task.project_id),
	)
