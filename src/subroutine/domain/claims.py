"""Taking a task, and giving it back — SPEC.md §14.11, item ``#350``.

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
import subroutine.errors
import subroutine.permissions

#: What a lease lasts when nobody says. Long enough that an agent doing real work is not
#: renewing constantly, short enough that a dead one frees its task within a coffee break.
#: ``claim_lease_minutes`` in the configuration overrides it — and until this module existed
#: that setting was declared, printed by ``config show`` and read by nothing, which is the
#: family `#247`, `#251` and `#303` belong to.
DEFAULT_LEASE_MINUTES = 30

#: The longest lease anybody may ask for. A lease is a promise that the work comes back if the
#: worker does not, so an unbounded one is a lock wearing a lease's clothes.
MAX_LEASE_MINUTES = 60 * 24


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
	"""

	moment = now or subroutine.db.types.utcnow()
	holder = held_by(task, now=moment)

	_permitted(session, actor, task)

	if holder is not None and holder != actor.user.id:
		raise subroutine.errors.Conflict(
			"Somebody else is working on this.",
			hint=_who_holds_it(session, task),
		)

	renewed = holder == actor.user.id
	task.claimed_by_id = actor.user.id
	task.claim_expires_at = moment + datetime.timedelta(
		minutes=_lease(minutes, settings)
	)

	if not renewed:
		task.claimed_at = moment

	# **The version moves, for `delete`'s reason.** A claim changes what a caller may safely do
	# next, so a version that stood still across one would let somebody edit on the strength of
	# a read taken before the work was taken.
	task.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.CLAIMED,
		changes={
			"renewed": renewed,
			"expires_at": task.claim_expires_at.isoformat(),
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
