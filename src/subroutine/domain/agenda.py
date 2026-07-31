"""What am I doing today — the one question a personal to-do list has to answer well.

Four named buckets rather than a flat list, because a person's day has structure and
because a flat list loses the most common kind of personal task (SPEC.md §8.6). The
buckets in priority order:

``overdue``      a deadline that has already passed
``today``        planned for today or earlier, or due at some point today
``upcoming``     due or planned inside a look-ahead window
``unscheduled``  no dates at all — "buy milk"

**The ``unscheduled`` bucket is what makes quick capture worth having.** Most personal
tasks are captured with no date, and without a bucket for them they would never appear in
the agenda at any point, ever — a to-do list you can write to and not read from. §13.5b
tests for it directly.

**``upcoming`` is off unless asked for, and the CLI always asks.** The API keeps it behind
``include=upcoming`` so that a client can reason about the window it gets; the CLI renders
a seven-day look-ahead by default because a to-do list that shows nothing when something is
due on Friday is one nobody keeps using. The two are not in tension — one is a transport,
the other is a product — but they were in outright contradiction until a review caught it,
so the reason is written down in §8.6 and again here.
"""

import dataclasses
import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.dates
import subroutine.domain.readiness
import subroutine.domain.schedule
import subroutine.domain.scoping

#: How many undated tasks the agenda shows before it stops. A person with two hundred
#: captured-and-forgotten tasks does not want all of them every morning; they want the
#: reminder that the pile exists, which is what :attr:`Agenda.unscheduled_total` is for.
DEFAULT_UNSCHEDULED_LIMIT = 20

#: The CLI's default look-ahead. Not the API's — it has none (SPEC.md §8.6).
DEFAULT_HORIZON_DAYS = 7


@dataclasses.dataclass(frozen=True)
class Agenda:
	"""One day's work, in buckets, with enough context to render it honestly."""

	date: datetime.date
	timezone: str

	overdue: tuple[subroutine.db.models.work.Task, ...]
	today: tuple[subroutine.db.models.work.Task, ...]
	upcoming: tuple[subroutine.db.models.work.Task, ...]
	unscheduled: tuple[subroutine.db.models.work.Task, ...]

	#: How many undated tasks there are in total, which is usually more than were returned.
	#: Carried so a client can say "and 14 more" rather than implying the list is complete.
	unscheduled_total: int = 0

	@property
	def is_empty (self) -> bool:
		"""Report whether there is nothing at all to show."""

		return not (self.overdue or self.today or self.upcoming or self.unscheduled)


def build (
	session: sqlalchemy.orm.Session,
	*,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	now: datetime.datetime,
	timezone: str,
	date: datetime.date | None = None,
	horizon_days: int | None = None,
	unscheduled_limit: int = DEFAULT_UNSCHEDULED_LIMIT,
) -> Agenda:
	"""Return the agenda for one day, in the caller's timezone.

	``horizon_days`` of ``None`` omits the ``upcoming`` bucket entirely, which is the API's
	default; the CLI passes :data:`DEFAULT_HORIZON_DAYS`.

	**The buckets are disjoint**, in the order they are listed above. A task that is both
	overdue and planned for today belongs in ``overdue`` — it is the more urgent truth about
	it, and showing one task twice in a five-line summary makes the summary useless.
	"""

	day = date or subroutine.domain.schedule.local_date(now, timezone)
	model = subroutine.db.models.work.Task

	day_start = _boundary(day, timezone, end=False)
	day_end = _boundary(day, timezone, end=True)

	base = _visible(session, principal, workspace_ids, now=now)

	overdue = _run(
		session,
		base.where(model.due_at.is_not(None), model.due_at < day_start),
		sqlalchemy.asc(model.due_at),
	)

	seen = {task.id for task in overdue}

	today = _run(
		session,
		base.where(
			sqlalchemy.or_(
				sqlalchemy.and_(model.planned_for.is_not(None), model.planned_for <= day),
				sqlalchemy.and_(model.due_at >= day_start, model.due_at <= day_end),
			)
		),
		# NULLs last explicitly. SQLite sorts them first by default and PostgreSQL last, so
		# the undated-but-planned tasks would appear at opposite ends of this list depending
		# on which backend answered (SPEC.md §10.3).
		sqlalchemy.asc(model.due_at).nullslast(),
		sqlalchemy.asc(model.position),
	)
	today = tuple(task for task in today if task.id not in seen)
	seen.update(task.id for task in today)

	upcoming: tuple[subroutine.db.models.work.Task, ...] = ()

	if horizon_days is not None:
		horizon = _boundary(day + datetime.timedelta(days=horizon_days), timezone, end=True)
		limit = day + datetime.timedelta(days=horizon_days)

		upcoming = _run(
			session,
			base.where(
				sqlalchemy.or_(
					sqlalchemy.and_(model.due_at > day_end, model.due_at <= horizon),
					sqlalchemy.and_(model.planned_for > day, model.planned_for <= limit),
				)
			),
			sqlalchemy.asc(model.due_at).nullslast(),
			sqlalchemy.asc(model.planned_for).nullslast(),
		)
		upcoming = tuple(task for task in upcoming if task.id not in seen)

	undated = base.where(model.planned_for.is_(None), model.due_at.is_(None))

	unscheduled = _run(
		session,
		undated.limit(unscheduled_limit),
		sqlalchemy.asc(model.position),
		sqlalchemy.asc(model.created_at),
	)
	total = session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(undated.subquery())
	)

	return Agenda(
		date=day,
		timezone=timezone,
		overdue=overdue,
		today=today,
		upcoming=upcoming,
		unscheduled=unscheduled,
		unscheduled_total=total or 0,
	)


def _visible (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	*,
	now: datetime.datetime,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return the select every bucket narrows: live, unfinished, actionable, visible work.

	Everything about *who may see what* — the workspace scope, project visibility and the
	token's project scope — comes from :func:`subroutine.domain.scoping.readable_tasks`,
	which is the one copy of those rules (§7.3). The agenda kept its own until the slice-2
	review found two copies disagreeing about whether privacy reaches a private project's
	children; a third copy is not the lesson to take from that.

	What is left here is what the *agenda* means, as opposed to what the caller may read:

	- **not finished** — ``completed_at`` is non-null exactly when the status category is
	  done or cancelled (invariant 5), so this needs no join to the status;
	- **not deferred** — ``start_at`` in the future means "don't show me this yet" (§6.5).
	"""

	model = subroutine.db.models.work.Task

	return subroutine.domain.scoping.readable_tasks(
		principal, workspace_ids=workspace_ids, include_completed=False
	).where(subroutine.domain.readiness.undeferred(model, now=now))


def _run (
	session: sqlalchemy.orm.Session,
	statement: sqlalchemy.Select[tuple[subroutine.db.models.work.Task]],
	*order: sqlalchemy.UnaryExpression[typing.Any],
) -> tuple[subroutine.db.models.work.Task, ...]:
	"""Execute one bucket's query with a deterministic order.

	``created_at`` is appended as the final tie-break so that two tasks with identical
	sort keys do not swap places between calls — a list that reorders itself while nothing
	changed is one nobody trusts.
	"""

	model = subroutine.db.models.work.Task

	return tuple(
		session.scalars(statement.order_by(*order, sqlalchemy.asc(model.created_at)))
	)


def _boundary (day: datetime.date, timezone: str, *, end: bool) -> datetime.datetime:
	"""Return the first or last instant of a local day, as UTC.

	The same boundaries §6.5 stores an all-day deadline at, so a task due all-day today
	lands inside today's window rather than one microsecond outside it.
	"""

	moment = subroutine.domain.dates.LAST_MICROSECOND if end else datetime.time.min

	return datetime.datetime.combine(
		day, moment, tzinfo=subroutine.domain.dates.zone(timezone)
	).astimezone(datetime.UTC)
