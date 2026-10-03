"""Events older than an operator's retention floor move to the archive - `#251`, decision `#4233`.

**Moved, never deleted.** The change feed reads the live table, so its floor moves and a cursor
below it is answered ``410 cursor_expired`` (docs/design.md §5.11); everything that reads history
reads both tables, through :data:`subroutine.domain.events.HISTORY`, so nothing a reader relies on
is lost to the floor.

**Nothing moves unless an operator set a floor**, ``events_retention_days``: by default every
event stays in the feed for ever (decision `#1835`). Two things move them once it is set, and
both come here: ``subroutine db archive``, which an operator can put on a timer, and a serving
instance, at most once a day while somebody is using it (:class:`Keeper`).
"""

import dataclasses
import datetime
import logging
import threading
import typing

import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.system
import subroutine.db.types

#: How many events one transaction moves. Bounded so that a first run over a long history stays
#: within the statement timeout a served instance puts on its sessions, and so that a failure part
#: of the way leaves what it moved moved, and recorded.
STEP = 5_000

#: How often a serving instance moves events of its own accord, at most.
EVERY = datetime.timedelta(days=1)

_logger = logging.getLogger("subroutine.retention")


@dataclasses.dataclass(frozen=True)
class Archived:
	"""What a run moved: how many events, and the highest ``seq`` the archive now holds."""

	moved: int
	through: int | None


def archived_through (session: sqlalchemy.orm.Session) -> int | None:
	"""Return the highest ``seq`` ever moved to the archive, or ``None`` if none has been."""

	return session.scalar(
		sqlalchemy.select(subroutine.db.models.system.Instance.events_archived_through)
	)


def due (
	session: sqlalchemy.orm.Session, *, days: int, now: datetime.datetime
) -> int | None:
	"""Return the highest ``seq`` a run should move, or ``None`` when nothing is old enough.

	**Never the newest event**, so the live table is never emptied. SQLite numbers the next row one
	past the highest the table holds, so an emptied one would begin again at 1 and hand out numbers
	the archive already holds; and a copy re-seats PostgreSQL's sequence from the live table's
	highest the same way (`db/transfer.py`).

	**Only a run of old events** (`#4296`, decision `#4305`): the answer is the ``seq`` just below
	the oldest live event not yet old enough, so everything at or below it is old. It was the highest
	old ``seq``, and everything beneath moved whether old or not - and a merge brings old events in
	at high numbers, so the next run took every recent event out of the feed. Accepted with it: one
	event dated in the future holds back everything after it until the clock passes it.
	"""

	live = subroutine.db.models.activity.Event

	# **Nothing is that old** (`#4295`, M15 of the cold review of 2026-10-03): a floor past the
	# calendar's start overflowed, and ended `db archive` in a traceback and the background run in
	# a dead thread. The setting has lower bounds only, deliberately (`#1559`), so it is read here.
	try:
		floor = now - datetime.timedelta(days=days)

	except OverflowError:
		return None

	newest = session.scalar(sqlalchemy.select(sqlalchemy.func.max(live.seq)))

	if newest is None:
		return None

	young = session.scalar(
		sqlalchemy.select(sqlalchemy.func.min(live.seq)).where(live.created_at >= floor)
	)
	below = newest if young is None else min(young, newest)

	return session.scalar(sqlalchemy.select(sqlalchemy.func.max(live.seq)).where(live.seq < below))


def move (
	session: sqlalchemy.orm.Session, *, through: int, limit: int = STEP
) -> Archived:
	"""Move the oldest ``limit`` live events at or below ``through``, and record how far it got.

	**Recorded on the instance in the same transaction** as the rows move, so the number a cursor
	is refused by and the rows it stands for cannot disagree. A database with no instance row has
	nowhere to record it, and moves nothing.
	"""

	instance = session.scalar(sqlalchemy.select(subroutine.db.models.system.Instance))

	if instance is None:
		return Archived(moved=0, through=None)

	live = typing.cast(sqlalchemy.Table, subroutine.db.models.activity.Event.__table__)

	# **The numbers moved, named once and used for both halves** (`#4295`): copied by number and
	# deleted by range, an event committed in between was deleted and never copied.
	moving: list[int] = list(
		session.scalars(
			sqlalchemy.select(live.c.seq)
			.where(live.c.seq <= through)
			.order_by(live.c.seq)
			.limit(limit)
		)
	)

	if not moving:
		return Archived(moved=0, through=instance.events_archived_through)

	reached = moving[-1]
	names = [column.name for column in live.columns]
	session.execute(
		subroutine.db.models.activity.ARCHIVE.insert().from_select(
			names, sqlalchemy.select(*[live.c[name] for name in names]).where(live.c.seq.in_(moving))
		)
	)
	# Typed as a plain Result, but DML always yields a cursor result and only that carries the count.
	deleted = typing.cast(
		"sqlalchemy.CursorResult[typing.Any]",
		session.execute(sqlalchemy.delete(live).where(live.c.seq.in_(moving))),
	)
	moved = int(deleted.rowcount)

	# **Raised in the database, never written from what was read** (`#4295`): a run that read the
	# row before another moved further and committed wrote its own lower number over it, on both
	# backends - SQLite's reads hold no snapshot, so locking the read does nothing there.
	system = typing.cast(sqlalchemy.Table, subroutine.db.models.system.Instance.__table__)
	session.execute(
		sqlalchemy.update(system).values(
			events_archived_through=sqlalchemy.case(
				(
					sqlalchemy.or_(
						system.c.events_archived_through.is_(None),
						system.c.events_archived_through < reached,
					),
					reached,
				),
				else_=system.c.events_archived_through,
			)
		)
	)
	session.expire(instance)

	return Archived(moved=moved, through=instance.events_archived_through)


def run (
	factory: typing.Callable[[], sqlalchemy.orm.Session], *, days: int, now: datetime.datetime
) -> Archived:
	"""Move every event older than ``days``, one :data:`STEP` to a transaction, and say how many.

	``factory`` opens a session for each step, which is committed before the next begins.
	"""

	with factory() as session:
		target = due(session, days=days, now=now)
		through = archived_through(session)

	moved = 0

	while target is not None and (through is None or through < target):
		with factory() as session:
			step = move(session, through=target)
			session.commit()

		if step.moved == 0:
			break

		moved += step.moved
		through = step.through

	return Archived(moved=moved, through=through)


class Keeper:
	"""Move events past an operator's retention floor in the background, at most once a day.

	**On :class:`subroutine.releases.Watch`'s terms, and for its reason**: started in the background
	of an authenticated request and never waited on, so an instance nobody is using does nothing at
	all - there is still no timer. Built only when ``events_retention_days`` is set.

	**Per process.** Two workers can start a run together, and the second then finds the first's
	rows already moved and is refused by the archive's key, which is logged and tried again a day on.
	"""

	def __init__ (
		self,
		*,
		days: int,
		factory: typing.Callable[[], sqlalchemy.orm.Session],
		clock: typing.Callable[[], datetime.datetime] = subroutine.db.types.utcnow,
		every: datetime.timedelta = EVERY,
	) -> None:
		"""Prepare to move events older than ``days``, having moved nothing yet."""

		self._days = days
		self._factory = factory
		self._clock = clock
		self._every = every
		self._lock = threading.Lock()
		self._attempted: datetime.datetime | None = None

		#: The run in flight or last started, so a test can wait for it. A request never does.
		self.moving: threading.Thread | None = None

	def archive_if_due (self) -> threading.Thread | None:
		"""Start a run in the background unless one was attempted within :data:`EVERY`.

		The attempt is recorded before the thread starts, under the lock, so two requests arriving
		together start one run between them.
		"""

		now = self._clock()

		with self._lock:
			if self._attempted is not None and now - self._attempted < self._every:
				return None

			self._attempted = now
			moving = threading.Thread(
				target=self._archive, args=(now,), name="subroutine-retention", daemon=True
			)
			self.moving = moving

		moving.start()

		return moving

	def _archive (self, at: datetime.datetime) -> None:
		"""Run once, and say in the server's log what moved or why nothing could."""

		try:
			archived = run(self._factory, days=self._days, now=at)

		except sqlalchemy.exc.SQLAlchemyError as failure:
			_logger.warning(
				"Moving events older than %d days to the archive failed, and is tried again in a "
				"day: %s",
				self._days,
				failure,
			)

			return

		# **Anything else is said in the log too** (`#4295`): a thread that dies prints through the
		# thread hook and nothing through the server's log, so an operator never hears of it.
		except Exception:
			_logger.exception(
				"Moving events older than %d days to the archive stopped unexpectedly, and is tried "
				"again in a day.",
				self._days,
			)

			return

		if archived.moved:
			_logger.info(
				"Moved %d events older than %d days to the archive.", archived.moved, self._days
			)
