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

import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.dates
import subroutine.domain.ordering
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

	#: What is already started — status category ``in_progress`` (`#853`). Between *today* and
	#: the rest, because work somebody is in the middle of is neither scheduled nor a candidate
	#: to pick up, and an agenda that could not say so left an agent unable to see its own
	#: half-finished work (`#841`).
	in_progress: tuple[subroutine.db.models.work.Task, ...] = ()

	#: How many undated tasks there are in total, which is usually more than were returned.
	#: Carried so a client can say "and 14 more" rather than implying the list is complete.
	unscheduled_total: int = 0

	@property
	def is_empty (self) -> bool:
		"""Report whether there is nothing at all to show."""

		return not (
			self.overdue
			or self.today
			or self.in_progress
			or self.upcoming
			or self.unscheduled
		)


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

	base = _visible(session, principal, workspace_ids, until=day_end)

	# **Uncapped, and bounded by nothing — which is not the reason `#888` gave** (`#927` M-18,
	# Simon's decision of 2026-08-17). That item declined a cap on `in_progress` and said in
	# passing that *"`overdue` and `today` are unlimited too and are naturally bounded by
	# dates"*. Dates do not bound this: a deadline that has passed goes on having passed, so
	# this bucket grows with however much you are late on and has no ceiling at all —
	# `in_progress`'s bound, workers times leases, does not apply here.
	#
	# **It stays uncapped anyway, on `#888`'s other argument**, which is the one that carries:
	# hiding work misleads the reader into starting something else, and that is worse for late
	# work than for anything on the page. Measured before deciding: 2 overdue, 1 today, on the
	# instance this project runs on.
	#
	# **What would change it, and what the change must look like.** A backlog large enough that
	# a day's agenda is unreadable — every row here renders through `views.task`, and this is
	# also MCP's `subroutine_list(today=true)`, where §13's context economy is a first-order
	# cost. If it ever comes to that, `#888` already fixed the shape: a cap must *say* it is
	# one, count what is hidden and offer a way to see it all, which is exactly what
	# `unscheduled_total` is below. Do not add a bare `.limit()`.
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
				# **Compared against the end of the day, not against the day** (`#854`).
				# This used to read `planned_for <= day`, a `DATE` against a `date`; the
				# column is an instant now, so the boundary has to be one too or every
				# comparison is an instant against midnight in whichever zone the driver
				# guessed. Everything that has begun by tonight belongs to today, which is
				# what `<=` said before and still says.
				#
				# **So a start date in the past stays in today's bucket, and that is
				# deliberate** (`#927` M-18, Simon's decision of 2026-08-17). It is the
				# `starts_at` analogue of `overdue` above: work you meant to begin and did
				# not is work for today, every day, until you do it or move it.
				#
				# **Narrowing this to starts falling *within* today would lose the task
				# entirely**, which is why the obvious fix is the wrong one — `undated`
				# below is `starts_at IS NULL AND due_at IS NULL`, so a task with a start
				# and no deadline is in no other bucket at all. The agenda would stop
				# mentioning it, in silence, and `list` would become the only place it
				# appears — which is a worse answer than showing it every day.
				model.starts_at <= day_end,
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

	# **What is already started, between the day's work and everything else** (`#853`). Read
	# off the status *category* rather than a key, because a workspace may rename the row —
	# `in_progress` is one of the five categories §6.5 fixes, and the key beside it is not.
	#
	# **Deliberately unlimited, unlike `unscheduled` below** — Simon's decision of 2026-08-14,
	# `#888`: *"a user viewing their own agenda should see all in-progress items. Hiding some
	# risks misleading the user. They may start others instead of finishing items we didn't
	# show them."*
	#
	# **Measured before deciding**, because the cold review raised it as unbounded and the word
	# is doing a lot of work: 2 in-progress against 179 unscheduled on the served instance. The
	# argument is what the two are bounded *by* rather than the numbers — `unscheduled` grows
	# with the backlog and has no ceiling at all, where this is bounded by how many workers
	# there are times how much each holds at once, which §14.11's leases keep small on purpose.
	#
	# **What would change it is team size**, since every bucket here is scoped by readability
	# rather than by assignee. If it ever does, the shape is already beside it: Simon's
	# condition was that a cap must *say* it is one, count what is hidden and offer a way to
	# see it all, which is exactly what `unscheduled_total` is.
	started = _run(
		session,
		base.join(
			subroutine.db.models.vocabulary.Status,
			subroutine.db.models.vocabulary.Status.id == model.status_id,
		).where(subroutine.db.models.vocabulary.Status.category == "in_progress"),
		_ranked(),
	)
	started = tuple(task for task in started if task.id not in seen)
	seen.update(task.id for task in started)

	upcoming: tuple[subroutine.db.models.work.Task, ...] = ()

	if horizon_days is not None:
		horizon = _boundary(day + datetime.timedelta(days=horizon_days), timezone, end=True)

		upcoming = _run(
			session,
			base.where(
				sqlalchemy.or_(
					sqlalchemy.and_(model.due_at > day_end, model.due_at <= horizon),
					sqlalchemy.and_(model.starts_at > day_end, model.starts_at <= horizon),
				)
			),
			sqlalchemy.asc(model.due_at).nullslast(),
			sqlalchemy.asc(model.starts_at).nullslast(),
		)
		upcoming = tuple(task for task in upcoming if task.id not in seen)

	undated = base.where(model.starts_at.is_(None), model.due_at.is_(None))

	if seen:
		undated = undated.where(model.id.not_in(seen))

	# **Ordered by rank, which is what makes this section worth reading** (`#853`). It was
	# `position` — a column `#28` records as written by nothing — and then `created_at`, so a
	# person with two hundred captured tasks got the twenty oldest and `!1/1 tidy the desk`
	# sat above `!5/5 renew the passport`. With no planned days and two deadlines across this
	# project's 172 open tasks, **this bucket *is* the agenda**, so the answer to "what should
	# I work on" was "whatever you wrote down first".
	unscheduled = _run(
		session,
		undated.limit(unscheduled_limit),
		_ranked(),
	)
	total = session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(undated.subquery())
	)

	return Agenda(
		date=day,
		timezone=timezone,
		overdue=overdue,
		today=today,
		in_progress=started,
		upcoming=upcoming,
		unscheduled=unscheduled,
		unscheduled_total=total or 0,
	)


def _ranked () -> sqlalchemy.UnaryExpression[typing.Any]:
	"""Return the ordering a section of candidates is read in: best first.

	The same rule `?order=-priority_score` applies, so the agenda and a ranked listing cannot
	disagree about which item is the one to start — §6.3a's bands, NULLS LAST in both
	directions so unranked work sorts after ranked work rather than before it.
	"""

	return sqlalchemy.desc(subroutine.domain.ordering.RANKING).nullslast()


def _visible (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	*,
	until: datetime.datetime,
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
	- **not deferred past the end of this day** — ``snoozed_until`` beyond it means "don't show me
	  this yet" (§6.5).

	**``until`` is the end of the day being shown, not the current instant, and that is the
	whole of `#771`.** It was ``now``, so a dentist appointment at 14:00 was hidden from the
	morning's agenda — from *every* bucket at once, which is why a workspace holding one open
	task reported ``unscheduled_total`` of zero. The capture grammar made it systematic rather
	than rare, because a time of day was written into the *defer* column: ``Dentist appointment
	at 2pm`` deferred itself until two o'clock, so every appointment written with a time was
	invisible until it began. `#854` moved that to ``starts_at``, which hides nothing — so this
	guard now protects against a deliberate defer alone rather than against the grammar.

	**A defer hides something until a day, not until an o'clock.** The agenda is a day view, so
	its horizon is that day: a task starting later today belongs to today, and one starting
	tomorrow does not. ``starts_at`` of today is the reader saying *this belongs to this day*,
	and a defer inside the same day may not overrule it.

	:func:`subroutine.domain.readiness.undeferred` is unchanged and keeps comparing against an
	instant, because ``?ready=`` asks *what can I start now* — a different question, to which an
	appointment at 14:00 is honestly "not yet".
	"""

	model = subroutine.db.models.work.Task

	return (
		subroutine.domain.scoping.readable_tasks(
			principal, workspace_ids=workspace_ids, include_completed=False
		)
		.where(subroutine.domain.readiness.undeferred(model, now=until))
		# **Every row carries the ordering value, because a merged agenda re-sorts in Python**
		# (`#853`). Two of the buckets are ranked, and `subroutine today` asks one connection
		# per place and merges the answers — so the rank has to survive the wire or the merge
		# sorts on nulls and silently keeps whichever connection answered first. The
		# expression is a plain `CASE` over two columns, so this costs nothing a sort by it
		# was not paying anyway.
		.options(
			sqlalchemy.orm.with_expression(model.rank, subroutine.domain.ordering.RANKING)
		)
	)


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
