"""What "ready to start" means, in one place.

An item can be un-startable for three unrelated reasons, and a caller choosing what to do
next needs to skip all three without caring which applies:

* it is **finished** — done or cancelled;
* it is **blocked** — something unfinished must land first (§5.7's ``blocks``);
* it is **deferred** — ``start_at`` is in the future, and §6.5 says a task is not actionable
  before it.

**None of that is expressible as a priority.** ``priority_score`` is a scalar and the first
two are a graph and a clock — folding either into the number would make the number mean two
things and rank badly at both. So readiness is a *filter*, and the ordering stays what it was.

Written after a week of choosing work here by hand. Every reordering in that week was of the
form "X makes Y worse if done in the wrong order" — the adapter inheriting a bad default, a
list tool that could not rank — and each was recorded as a ``blocks`` link *after* the
decision, because nothing read them. The order actually followed was topological and the only
one on offer was the scalar.
"""

import datetime
import typing

import sqlalchemy

import subroutine.db.models.vocabulary
import subroutine.db.models.work


def unblocked (model: type[typing.Any]) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items nothing unfinished is blocking.

	A ``blocks`` link runs source → target, so an item's blockers are the *sources* of links
	pointing at it. Only tasks can block: a document has no state that could finish, so a
	``derives_from`` to a specification would otherwise block every task in the project
	forever.

	**Finished is read off ``completed_at``, not off the status vocabulary.** §10.7's
	invariant 5 makes that column non-null exactly when the category is ``done`` or
	``cancelled``, so it answers the same question without joining a table an installation is
	free to rename rows in.
	"""

	link = subroutine.db.models.work.Link
	blocker = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	kind = subroutine.db.models.vocabulary.LinkType

	return ~sqlalchemy.exists(
		sqlalchemy.select(link.id)
		.join(kind, kind.id == link.link_type_id)
		.join(
			blocker,
			sqlalchemy.and_(blocker.id == link.source_id, link.source_type == "task"),
		)
		.where(
			link.target_type == "task",
			link.target_id == model.id,
			link.deleted_at.is_(None),
			kind.key == "blocks",
			blocker.completed_at.is_(None),
			blocker.deleted_at.is_(None),
		)
		.correlate(model)
	)


def undeferred (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items whose defer instant has passed, or that have none.

	``now`` is passed in rather than read here so that one request resolves every relative
	comparison against a single instant — the same rule ``domain.tasks`` follows, and the
	reason a task cannot be deferred and ready in one listing.
	"""

	return sqlalchemy.or_(model.start_at.is_(None), model.start_at <= now)


def ready (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that can actually be started.

	The three clauses together. ``completed`` is left to the caller's own
	``include_completed``, which every listing already has — repeating it here would give two
	parameters an argument about the same rows.
	"""

	return sqlalchemy.and_(unblocked(model), undeferred(model, now=now))
