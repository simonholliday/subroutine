"""What "ready to start" means, in one place.

An item can be un-startable for several unrelated reasons, and a caller choosing what to do
next needs to skip all of them without caring which applies:

* it is **finished** — done or cancelled;
* it is **blocked** — something unfinished must land first (§5.7's ``blocks``);
* it is **deferred** — ``snoozed_until`` is in the future, and §6.5 says a task is not
  actionable before it. **Only this field hides a row**: a ``starts_at`` in the future is an
  appointment or an intended day, and hiding those was `#854`'s whole defect;
* it is **claimed by somebody else** — another worker has a live lease on it (§14.11, `#350`);
* it is **in a project that is not running** — on hold, finished or abandoned (`#983`).

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

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.errors

#: The link-type category that holds work up — decision `#1157`, and the narrowest of the three
#: questions a category answers. Owned here rather than in :mod:`subroutine.domain.links` because
#: this is the rule that decides what ``--ready`` hides, and `#1156` is what happens when it and
#: the ring refusal are two literals that merely happen to agree: ``links.SEQUENCING`` is built
#: from this name, so the two cannot part company.
GATING = "gating"


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
	filed_in = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
	kind = subroutine.db.models.vocabulary.LinkType

	return ~sqlalchemy.exists(
		sqlalchemy.select(link.id)
		.join(kind, kind.id == link.link_type_id)
		.join(
			blocker,
			sqlalchemy.and_(blocker.id == link.source_id, link.source_type == "task"),
		)
		.join(filed_in, filed_in.id == blocker.project_id)
		.where(
			link.target_type == "task",
			link.target_id == model.id,
			*_live_blocks_edge(link, kind, blocker, filed_in),
		)
		.correlate(model)
	)


def blocking (model: type[typing.Any]) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that are holding something unfinished up.

	**The mirror of :func:`unblocked`, and `#569` is the mirror of `#425`.** That item made
	work that *cannot be started* visible; nothing made the work *doing the blocking* visible,
	so a board showed the urgent item marked ``blocked`` and said nothing at all about the
	five-minute errand holding it up — which was the only thing on the board worth doing. A
	rule aimed at one direction of a symmetric problem never fires for the other.

	Same edges, read the other way: a ``blocks`` link runs source → target, so what an item
	holds up are the *targets* of links leaving it.
	"""

	link = subroutine.db.models.work.Link
	held = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	filed_in = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
	kind = subroutine.db.models.vocabulary.LinkType

	return sqlalchemy.exists(
		sqlalchemy.select(link.id)
		.join(kind, kind.id == link.link_type_id)
		.join(held, sqlalchemy.and_(held.id == link.target_id, link.target_type == "task"))
		.join(filed_in, filed_in.id == held.project_id)
		.where(
			link.source_type == "task",
			link.source_id == model.id,
			*_live_blocks_edge(link, kind, held, filed_in),
		)
		.correlate(model)
	)


def _live_blocks_edge (
	link: type[typing.Any], kind: type[typing.Any], other: typing.Any, filed_in: typing.Any
) -> tuple[sqlalchemy.ColumnElement[bool], ...]:
	"""Return what makes a ``blocks`` link count: it is live and its far end is unfinished.

	**Read in both directions and stated once.** :func:`unblocked` asks what is holding a row
	up and :func:`blocking` asks what a row is holding up; the rule about which edges are real
	is the same, mirrored, so each caller supplies only the clauses saying which end it is
	standing at. ``other`` is the task at the far end, whichever end that is, and ``filed_in``
	is the project it is filed in.
	"""

	return (
		link.deleted_at.is_(None),
		# **What the relation *is*, never what it is called** (decision `#1157`). This compared
		# `kind.key` to the literal `blocks` until `#1156`, which measured what that costs: a
		# workspace renaming the key kept every label — the item page still said *Blocked by* —
		# and lost the filter, so a blocked task appeared in `--ready` at the same moment its own
		# page said it could not start.
		#
		# **`gating` alone, and that is narrower than the ring refusal.** `links.SEQUENCING` also
		# takes `ordering`, which asserts a sequence and holds nothing up — Simon's distinction on
		# `#1154`, that a code review preceding a first-contact pass is an opinion rather than a
		# dependency. A relation like that must not take work off anybody's list.
		kind.category == GATING,
		# Finished work is neither held up nor holding anything up. Without this a shipped
		# release would go on marking everything that ever blocked it.
		other.completed_at.is_(None),
		other.deleted_at.is_(None),
		# **And the project it is filed in is still there.** `projects.delete` does not touch
		# its tasks — "every listing joins the project and excludes deleted ones, so they
		# leave the visible world with it" — so a task in a binned project keeps a null
		# `deleted_at` and went on blocking live work from outside every listing there is.
		# Worse than a wrong answer: the caller was told an item was blocked and shown no
		# link at all, because `links.edges` drops an end they cannot see.
		filed_in.deleted_at.is_(None),
	)


def blocked_among (
	session: sqlalchemy.orm.Session, identifiers: typing.Iterable[uuid.UUID]
) -> set[uuid.UUID]:
	"""Return which of these tasks something unfinished is blocking — item ``#425``.

	**The row-level form of :func:`unblocked`, and built from it rather than beside it.** The
	pair `claims.held_by` and `readiness.held` are the precedent, and §6.3a's warning is the
	reason: a predicate the database filters by and a reader that labels a loaded row have to
	agree, or a listing marks one set of rows and ``?ready=true`` hides another.

	**One query for a page, which is what made this affordable at all.** Readiness is a filter
	by design (`#69`), so asking it per row is `#39`'s N+1 — the recorded obstacle to marking a
	blocked item for as long as the marking was wanted. Asking it once for every id on the page
	costs a single ``EXISTS`` scan and is what :class:`subroutine.views.Vocabulary` already does
	for statuses, types and parents.

	Deliberately not narrowed by visibility, for :func:`unblocked`'s reason: whether work is
	blocked is a fact about the work rather than about the viewer, and counting only the
	blockers a caller can see would mark an item startable when it is not. What that discloses
	is bounded — that something unseen blocks an item, never what.
	"""

	return _matching(session, identifiers, lambda model: sqlalchemy.not_(unblocked(model)))


def blocking_among (
	session: sqlalchemy.orm.Session, identifiers: typing.Iterable[uuid.UUID]
) -> set[uuid.UUID]:
	"""Return which of these tasks are holding something unfinished up — item ``#569``.

	The row-level form of :func:`blocking`, built from it for :func:`blocked_among`'s reason:
	a predicate and a reader that label the same rows have to agree or a listing marks one set
	and a filter hides another.

	**Deliberately not narrowed by visibility, and that is what keeps it honest.** Marking a
	row says *that* it is holding something up and never *what*, which is the bound
	:func:`unblocked`'s excuse already draws — and counting only the work a caller can see
	would leave a row unmarked while it really is on somebody's critical path. Naming the far
	end is `subroutine show`'s job, and that goes through ``domain.links.edges``, which drops
	an end the caller cannot see. **The listing says *that*; the detail view says *what*.**

	`#856` is why the distinction is written down: an *ordering* that inherited the far end's
	importance disclosed *what*, and was refused by ``tests/test_scoping.py`` for it.
	"""

	return _matching(session, identifiers, blocking)


def _matching (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	predicate: typing.Callable[[type[typing.Any]], sqlalchemy.ColumnElement[bool]],
) -> set[uuid.UUID]:
	"""Return which of these task ids satisfy one predicate, in a single query.

	**One query for a page, which is what made marking a row affordable at all.** Readiness is
	a filter by design (`#69`), so asking it per row is `#39`'s N+1 — the recorded obstacle to
	marking a blocked item for as long as the marking was wanted. Asking it once for every id
	on the page costs a single ``EXISTS`` scan and is what
	:class:`subroutine.views.Vocabulary` already does for statuses, types and parents.
	"""

	wanted = set(identifiers)

	if not wanted:
		return set()

	model = subroutine.db.models.work.Task

	return set(
		session.scalars(
			sqlalchemy.select(model.id).where(model.id.in_(wanted), predicate(model))
		)
	)


def undeferred (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items whose defer instant has passed, or that have none.

	``now`` is passed in rather than read here so that one request resolves every relative
	comparison against a single instant — the same rule ``domain.tasks`` follows, and the
	reason a task cannot be deferred and ready in one listing.
	"""

	return sqlalchemy.or_(model.snoozed_until.is_(None), model.snoozed_until <= now)


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


def in_a_running_project (model: type[typing.Any]) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items whose project is actually running — `#983`.

	A project on hold, completed or abandoned still holds its work and still answers
	``list --project X`` and a search. What it stops doing is offering that work as something
	to start, which is the whole of what putting a project down means. OmniFocus and Things
	both answer the same question the same way, and §5.5 seeded ``on_hold`` for it on day one.

	**Read off the status *category*, never the key.** §5.5 makes a workspace's vocabulary its
	own, so ``active`` is a label an installation may rename while ``in_progress`` is one of
	the four fixed categories a client can rely on. This is :func:`unblocked`'s reasoning about
	``completed_at`` arriving at the opposite answer: there a column let the join be avoided,
	here there is none, so the join is made and the *stable* column is what it reads.

	**A correlated ``EXISTS`` in ``WHERE`` is not `#856`'s shape**, which is worth saying
	because that item is the reason to be careful. `#856` died on ``ORDER BY <subquery>``,
	which computes a sort key for every row in the table so ``LIMIT`` cannot help. In a
	``WHERE`` clause it short-circuits and both planners make it a semi-join — measured on
	`#823` at roughly double an unordered page, against thousands of times.

	**No existing instance changes behaviour when this lands**, which is what makes it safe to
	add to a predicate this many listings go through: nothing could set a project's status
	before `#983`, so every project in existence is the seeded default and this is true of all
	of them.
	"""

	project = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
	status = sqlalchemy.orm.aliased(subroutine.db.models.vocabulary.Status)

	return sqlalchemy.exists(
		sqlalchemy.select(project.id)
		.join(status, status.id == project.status_id)
		.where(project.id == model.project_id, status.category == "in_progress")
		.correlate(model)
	)


def ready (
	model: type[typing.Any], *, now: datetime.datetime, by: uuid.UUID | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that can actually be started.

	Every clause together. ``completed`` is left to the caller's own
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
		unblocked(model),
		undeferred(model, now=now),
		unclaimed(model, now=now, by=by),
		in_a_running_project(model),
	)
