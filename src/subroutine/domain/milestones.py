"""What counts toward a milestone, and when all of it is done — decision `#3391`.

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

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.errors

#: The link-type category of ``includes`` — decision `#3391`, whose name Simon confirmed on
#: 2026-09-23. The source counts the target toward its own progress: a milestone, and the work
#: it is reached by.
#:
#: **The category and never the key** (`#1157`), so a workspace that renames ``includes`` keeps
#: every rule written against it, and one that adds a relation of its own under it gets them.
COUNTING = "counting"


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


def _includes (
	model: type[typing.Any], *, now: datetime.datetime, unfinished: bool
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching an item that includes live work, or unfinished live work.

	**Live is :func:`subroutine.domain.readiness._live_blocks_edge`'s rule, read for this
	relation**: the link is not withdrawn, and the work it reaches is neither in the trash nor
	in a project that is. A piece of work somebody deleted is out of a milestone's count on every
	surface (`#1403`'s rule for blockers), so it must be out of this question too.
	"""

	link = sqlalchemy.orm.aliased(subroutine.db.models.work.Link)
	kind = sqlalchemy.orm.aliased(subroutine.db.models.vocabulary.LinkType)
	part = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	filed_in = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)

	narrowed = (
		[sqlalchemy.not_(subroutine.domain.readiness.over(part, now=now))] if unfinished else []
	)

	return sqlalchemy.exists(
		sqlalchemy.select(link.id)
		.join(kind, kind.id == link.link_type_id)
		.join(part, sqlalchemy.and_(part.id == link.target_id, link.target_type == "task"))
		.join(filed_in, filed_in.id == part.project_id)
		.where(
			link.source_type == "task",
			link.source_id == model.id,
			link.deleted_at.is_(None),
			kind.category == COUNTING,
			part.deleted_at.is_(None),
			filed_in.deleted_at.is_(None),
			*narrowed,
		)
		.correlate(model)
	)


def included_done (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching an unfinished milestone all of whose work is over — `#3395`.

	**`#1615`'s question, put for a milestone.** Progress is derived and completion stays an
	act (decision `#3391`, restating `#84`): a milestone never completes itself, because that is
	a write nobody made, it credits whoever closed the last piece with a decision they did not
	take, and it does not reverse when something is added later. So when everything it includes
	is done, what changes is that the milestone says so, and a person decides.

	**Four clauses and each is load-bearing**, as :func:`subroutine.domain.readiness.
	every_sub_task_is_done`'s three are. It must be a milestone, because only a milestone's
	count reads ``includes``; it must not be over, because once somebody has decided there is no
	question left; it must include something, or an empty placeholder a year out would be
	marked the day it was made; and nothing it includes may be unfinished.
	"""

	return sqlalchemy.and_(
		subroutine.domain.readiness.is_target(model),
		sqlalchemy.not_(subroutine.domain.readiness.over(model, now=now)),
		_includes(model, now=now, unfinished=False),
		sqlalchemy.not_(_includes(model, now=now, unfinished=True)),
	)


def included_done_among (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	now: datetime.datetime,
) -> set[uuid.UUID]:
	"""Return which of these tasks are milestones whose included work is all done — `#3395`.

	:func:`subroutine.domain.readiness.finished_underneath_among`'s shape, for its reason: one
	``EXISTS`` scan for a whole page rather than a question per row, which is `#39`'s N+1.

	**Not narrowed by visibility**, exactly as that one is not. Whether the work a milestone
	includes is finished is a fact about that work rather than about the reader, and counting
	only what somebody can see would say the question is ready to be answered when it is not.
	"""

	wanted = set(identifiers)

	if not wanted:
		return set()

	model = subroutine.db.models.work.Task

	return set(
		session.scalars(
			sqlalchemy.select(model.id).where(model.id.in_(wanted), included_done(model, now=now))
		)
	)


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
