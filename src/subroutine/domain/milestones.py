"""What counts toward a milestone, and how much of it is done — decision `#3391`.

A milestone is an item whose type is in the ``target`` category, which
:data:`subroutine.domain.readiness.TARGET` names because readiness is what keeps one from ever
being offered as work. **What counts toward it is an ``includes`` link**, from the milestone to
the work, in a link category of its own: :data:`COUNTING`.

**Only what shows a milestone's contents reads ``includes``** — its progress, its plan in
``show``, the question put when all of it is done, and the roadmap. Readiness never does, so no
work is hidden or marked *Blocker* for being included, and that is why these rules are a module
of their own rather than clauses in :mod:`subroutine.domain.readiness`.

**Imported by :mod:`subroutine.domain.links` and :mod:`subroutine.domain.tasks` alike, so it
imports neither.** ``documents`` reads ``tasks`` at import time and ``links`` imports
``documents``, so a rule that ``tasks`` calls could not live in ``links`` without a cycle that
breaks whichever of the two is imported first.
"""

import dataclasses
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.errors

#: The link-type category of ``includes`` — decision `#3391`, whose name Simon confirmed on
#: 2026-09-23. The source counts the target toward its own progress: a milestone, and the work
#: it is reached by.
#:
#: **The category and never the key** (`#1157`), so a workspace that renames ``includes`` keeps
#: every rule written against it, and one that adds a relation of its own under it gets them.
COUNTING = "counting"


@dataclasses.dataclass(frozen=True)
class Progress:
	"""How much of what one milestone includes is finished — `#3396` — as one reader sees it.

	**The two counts are the reader's, and the two flags are the milestone's** (Simon, 2026-09-25,
	`#3597`). A count narrowed to what somebody can see keeps a row and the milestone's own page
	saying the same, and discloses nothing; whether it includes more, and whether all of it is
	done, are facts about the work - so an outsider is never told *all done* while something they
	cannot see is open, which is the false completion readiness refuses for a hidden blocker.
	**Never how much is unseen**: that is the bound readiness draws, *that* and never *how many*.
	"""

	#: How many pieces of work it includes, of those this reader can see, that are neither
	#: withdrawn nor in the trash.
	included: int

	#: How many of those are finished.
	done: int

	#: Whether it includes work this reader cannot see - never how much.
	unseen: bool = False

	#: Whether every piece of it is finished, seen or not, and there is at least one.
	finished: bool = False

	@property
	def all_done (self) -> bool:
		"""Report whether it includes something and every piece of it is finished.

		**Including nothing is not being done** — `#3395`'s placeholder a year out. Nothing in it
		is unfinished, and asking a person whether it has been reached the day it was made is
		noise.
		**Asked of all of it**, seen or not (`#3597`), since it is what puts *close it?* to a person.
		"""

		return self.finished


#: What a row that includes nothing reports — every row but a milestone's.
NOTHING = Progress(included=0, done=0)


def is_one (session: sqlalchemy.orm.Session, task_id: uuid.UUID) -> bool:
	"""Report whether this task is a milestone, which its type's category decides."""

	kind = subroutine.db.models.vocabulary.ItemType
	task = subroutine.db.models.work.Task

	category = session.scalar(
		sqlalchemy.select(kind.category)
		.join(task, task.type_id == kind.id)
		.where(task.id == task_id)
	)

	return category == subroutine.domain.readiness.TARGET


def progress_among (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	reader: subroutine.domain.authentication.Principal | None,
) -> dict[uuid.UUID, Progress]:
	"""Return how much of what each of these includes is done, in one grouped statement — `#3396`.

	**One count gives a row both of the things it says**: *2 of 5 included done* while work is
	open, and *Included done* once all of it is (`#3395`), which is :attr:`Progress.all_done`
	on a milestone nobody has completed. A row with no entry includes nothing, and only a
	milestone can include anything, because :mod:`subroutine.domain.links` refuses the link from
	anything else.

	**Live is :func:`subroutine.domain.readiness._live_blocks_edge`'s rule, read for this
	relation**: the link is not withdrawn, and the work it reaches is neither in the trash nor in
	a project that is. A piece of work somebody deleted is out of a milestone's count on every
	surface, as a deleted blocker is (`#1403`).

	**Finished means completed, the column ``show`` reads**, so the count on a row and the count
	on the milestone's own page cannot disagree about one milestone. That is narrower than
	:func:`subroutine.domain.readiness.over`, which also calls an occasion finished once its day
	has gone by: an event somebody counted toward a milestone stays unfinished here until
	somebody says otherwise, which is what ``show`` has always said of it.

	**Counted as ``reader`` sees it, and flagged as the milestone is** (Simon, 2026-09-25,
	`#3597`) - see :class:`Progress`. The counts go through ``scoping.task_seen_by``, which is
	the rule every listing narrows by, so a row counts exactly the work its Links heading lists.
	Whether all of it is done is counted without narrowing, for readiness's reason: it is a fact
	about the work, and a count narrowed to one reader would say the question is ready to be
	answered when it is not. ``None`` is nobody reading, and counts everything as seen.
	"""

	wanted = set(identifiers)

	if not wanted:
		return {}

	link = subroutine.db.models.work.Link
	kind = subroutine.db.models.vocabulary.LinkType
	part = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	# **The table itself rather than an alias**, because ``task_seen_by`` is written over it, as
	# every listing's narrowing is. Nothing else in the statement joins a project.
	filed_in = subroutine.db.models.project.Project
	seen = sqlalchemy.true() if reader is None else subroutine.domain.scoping.task_seen_by(reader)
	holder = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	typed = sqlalchemy.orm.aliased(subroutine.db.models.vocabulary.ItemType)

	counted = session.execute(
		sqlalchemy.select(
			link.source_id,
			sqlalchemy.func.count(link.id),
			# ``COUNT`` of a column counts the rows where it is set.
			sqlalchemy.func.count(part.completed_at),
			# And the same two of what this reader can see: a ``CASE`` with no ``ELSE`` is null, and
			# null is what ``COUNT`` skips.
			sqlalchemy.func.count(sqlalchemy.case((seen, link.id))),
			sqlalchemy.func.count(sqlalchemy.case((seen, part.completed_at))),
		)
		.join(kind, kind.id == link.link_type_id)
		.join(part, sqlalchemy.and_(part.id == link.target_id, link.target_type == "task"))
		.join(filed_in, filed_in.id == part.project_id)
		# **Only a milestone's count** (`#3596`): a counting link from anything else is refused where
		# links are made, and one that got there another way gave an ordinary row a count its own
		# page did not show.
		.join(holder, holder.id == link.source_id)
		.join(typed, typed.id == holder.type_id)
		.where(
			link.source_type == "task",
			link.source_id.in_(wanted),
			link.deleted_at.is_(None),
			kind.category == COUNTING,
			typed.category == subroutine.domain.readiness.TARGET,
			part.deleted_at.is_(None),
			filed_in.deleted_at.is_(None),
		)
		.group_by(link.source_id)
	)

	return {
		source: Progress(
			included=seen_included,
			done=seen_done,
			unseen=included > seen_included,
			finished=0 < included == done,
		)
		for source, included, done, seen_included, seen_done in counted
	}


def refuse_to_stop_including (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	becoming: subroutine.db.models.vocabulary.ItemType,
) -> None:
	"""Refuse to make a milestone that includes work into anything else — `#3395`.

	**Only a milestone includes, by every route**, which is `#1246`'s lesson: that item refused
	an event's deadline at creation and found the same state reachable by retyping. Making an
	``includes`` from anything but a milestone is refused where links are made; without this, a
	milestone retyped afterwards would go on including work that no rule reads, its count gone
	from every surface and its links still there.

	**Refused rather than cleared**, because withdrawing somebody's links as a side effect of a
	type change is a second write they did not ask for. The refusal says what to do instead.
	"""

	if becoming.category == subroutine.domain.readiness.TARGET:
		return

	# **Asked of a milestone only** (`#3596`): a task that is not one has nothing to stop being,
	# and counting its links refused an ordinary task's retype as *a milestone* once a relation
	# it used had been moved into ``counting``.
	if not is_one(session, task.id):
		return

	link = subroutine.db.models.work.Link
	kind = subroutine.db.models.vocabulary.LinkType

	included = session.scalar(
		sqlalchemy.select(sqlalchemy.func.count(link.id))
		.join(kind, kind.id == link.link_type_id)
		.where(
			link.source_type == "task",
			link.source_id == task.id,
			link.deleted_at.is_(None),
			kind.category == COUNTING,
		)
	) or 0

	if not included:
		return

	noun = "item" if included == 1 else "items"

	raise subroutine.errors.ValidationError(
		f"{subroutine.domain.refs.format_ref(task.ref)} includes other work, so it cannot stop "
		"being a milestone.",
		code="invalid_field_value",
		hint="Withdraw what it includes first, or leave it a milestone.",
		errors=[
			subroutine.errors.FieldError(
				field="type",
				code="invalid_field_value",
				message=(
					f"`type` cannot become {becoming.key!r} while it includes {included} {noun}."
				),
			)
		],
	)
