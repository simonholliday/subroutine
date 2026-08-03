"""What "ready to start" means, in one place.

An item can be un-startable for three unrelated reasons, and a caller choosing what to do
next needs to skip all three without caring which applies:

* it is **finished** — done or cancelled;
* it is **blocked** — something unfinished must land first (§5.7's ``blocks``);
* it is **deferred** — ``start_at`` is in the future, and §6.5 says a task is not actionable
  before it;
* it is **claimed by somebody else** — another worker has a live lease on it (§14.11, `#350`).

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
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.errors


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


#: What a caller may say about deferred work, and the default. **Three values rather than a
#: boolean**, which is a departure from §8.4's ``include_completed`` family and is worth the
#: departure: ``only`` is what lets a listing *report* what it is hiding without a second
#: notion of counting, and "what have I got parked?" is a question somebody asks directly.
#:
#: ``include`` is the default, so **no existing caller sees a change** — Simon's decision of
#: 2026-07-31. §6.5's "default views hide it entirely" is read as being about views a person
#: reads, which is what a view is; an API listing is not one, and ``?ready=true`` already
#: answers the API's version of the question explicitly and opt-in. Changing a published
#: default would break clients in order to say something they can already ask for.
DEFERRAL = ("include", "exclude", "only")

DEFAULT_DEFERRAL = "include"


def deferred (
	model: type[typing.Any], *, now: datetime.datetime, choice: str
) -> sqlalchemy.ColumnElement[bool] | None:
	"""Return the predicate for one of :data:`DEFERRAL`, or ``None`` to narrow nothing.

	``None`` rather than a tautology, so a caller can tell "no narrowing was asked for" from
	"narrow by something that happens to match everything" — the same distinction §8.3 makes
	between an omitted field and a null one.
	"""

	if choice == "exclude":
		return undeferred(model, now=now)

	if choice == "only":
		return ~undeferred(model, now=now)

	return None


def refuse_unknown_deferral (choice: str) -> str:
	"""Return the choice, or refuse with the ones that would have worked.

	Shared by both transports so that a bad value is refused identically whether it arrived as
	``?deferred=`` or as a CLI flag, and named as the same field.
	"""

	chosen = choice.strip().lower()

	if chosen not in DEFERRAL:
		raise subroutine.errors.ValidationError(
			f"{choice!r} is not a way to treat deferred work.",
			errors=[
				subroutine.errors.FieldError(
					field="deferred",
					code="invalid_field_value",
					message=f"The choices are: {', '.join(DEFERRAL)}.",
					hint="'include' is the default and needs no parameter at all; 'exclude' "
					"hides work whose start date has not arrived; 'only' shows just that work.",
				)
			],
		)

	return chosen


def held (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items somebody currently holds a live lease on.

	The SQL form of :func:`subroutine.domain.claims.held_by`, and deliberately the only one:
	:func:`subroutine.domain.claims.claim` narrows its own conditional update with
	:func:`unclaimed` rather than writing a second copy, so the rule that decides whether a
	listing hides a task *is* the rule that decides whether a claim is refused. §6.3a's warning
	about a predicate and a row-reader disagreeing applies to this pair with the stakes raised —
	a disagreement here is two workers on one task rather than a page boundary.

	**Null-safe in both columns, and it was not** (`#362`). A row carrying a holder and no
	expiry is not reachable through any endpoint, but ``NOT (a AND b)`` is *null* rather than
	true when ``b`` is null — so without the explicit test that row vanished from every listing
	while ``held_by`` said nobody held it. The two readings have to agree about a state neither
	of them can produce, because the thing that produces it will be something nobody is thinking
	about at the time.
	"""

	return sqlalchemy.and_(
		model.claimed_by_id.is_not(None),
		model.claim_expires_at.is_not(None),
		model.claim_expires_at > now,
	)


def unclaimed (
	model: type[typing.Any], *, now: datetime.datetime, by: uuid.UUID | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items no *other* worker holds a live lease on.

	**Your own claim does not hide your own work.** An agent that claims a task and then asks
	what it can start would otherwise lose the thing it just took — which is the one behaviour
	that would make claiming a trap rather than a tool.

	An expired lease matches, with no cleanup: a claim nobody renewed simply stops counting,
	which is what makes a lease a lease.

	``by`` is ``None`` for a caller with no principal, where every live claim belongs to
	somebody else.
	"""

	live = held(model, now=now)

	if by is None:
		return sqlalchemy.not_(live)

	return sqlalchemy.or_(sqlalchemy.not_(live), model.claimed_by_id == by)


def ready (
	model: type[typing.Any], *, now: datetime.datetime, by: uuid.UUID | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that can actually be started.

	The four clauses together. ``completed`` is left to the caller's own
	``include_completed``, which every listing already has — repeating it here would give two
	parameters an argument about the same rows.

	**The claim clause is the one that is about the *viewer* rather than the work**, and that
	is worth naming because this module's other predicates are deliberately not. ``unblocked``
	reads blocker tasks without narrowing by visibility precisely because readiness is a fact
	about the work; a claim is not, and cannot be — "can I start this" has a different answer
	for the agent holding the lease than for anybody else. So ``by`` is passed rather than
	assumed.

	**Required rather than defaulted** (`#361`). It defaulted to ``None``, which is the
	strictest reading, for a caller with no principal — and there is no such caller: both
	listings have an actor. A future one that forgot the argument would hide an agent's own
	claimed work from that agent, which is the one behaviour this module says would make
	claiming a trap rather than a tool, and nothing would have looked wrong. ``None`` is still
	*expressible*, so a genuinely anonymous reader is still describable; it just has to be said.
	"""

	return sqlalchemy.and_(
		unblocked(model), undeferred(model, now=now), unclaimed(model, now=now, by=by)
	)
