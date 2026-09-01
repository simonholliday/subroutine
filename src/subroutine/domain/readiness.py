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
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.errors

#: The link-type category that holds work up — decision `#1157`, and the narrowest of the three
#: questions a category answers. Owned here rather than in :mod:`subroutine.domain.links` because
#: this is the rule that decides what ``--ready`` hides, and `#1156` is what happens when it and
#: the ring refusal are two literals that merely happen to agree: ``links.SEQUENCING`` is built
#: from this name, so the two cannot part company.
GATING = "gating"

#: The task-type category that says something *happens to you* — decision `#1235`, Simon's
#: *out of our control, never due or overdue; it just happens*. A birthday, a booked fortnight,
#: a street closed by the council, a code freeze.
#:
#: **Read here rather than beside each caller** for :data:`GATING`'s reason: ``--ready`` hides
#: these, :func:`passed` decides when one stops holding work up, and the agenda gives them a
#: section — three rules about one category, and `#1156` is what it costs when a set of rules
#: written in terms of one vocabulary keep their own copies of it.
#:
#: **The category and never the key**, so a workspace adding ``holiday`` or ``freeze`` under it
#: through `#1129` inherits all three without a release.
OCCASION = "occasion"


def is_occasion (model: type[typing.Any]) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that happen rather than get done — decision `#1235`.

	**A correlated ``EXISTS`` rather than a join**, which is :func:`in_a_running_project`'s
	shape and its reasoning: in a ``WHERE`` clause both planners make it a semi-join and it
	short-circuits, where `#856`'s ``ORDER BY <subquery>`` computes a key for every row in the
	table. ``task.type_id`` is NOT NULL, so this never has to reason about a row with no type.
	"""

	kind = sqlalchemy.orm.aliased(subroutine.db.models.vocabulary.ItemType)

	return sqlalchemy.exists(
		sqlalchemy.select(kind.id)
		.where(kind.id == model.type_id, kind.category == OCCASION)
		.correlate(model)
	)


def passed (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching occasions that have gone by — decision `#1235`, §3.

	**Derived, never written.** ``completed_at`` stays null and no scheduler exists to be
	trusted: `#915` chose to compute occurrences rather than materialise them precisely because
	an in-process timer that only fires when the program happens to be up is worse than none,
	and the same argument settles this. A computed answer cannot be stale.

	Three shapes, because *when is it over* is a different question for each:

	* **a span** — ``ends_at`` is the answer and needs nothing else, whether it is a fortnight
	  off or a code freeze that lifts at nine on Monday;
	* **a whole day with no end** — a birthday. Over once a whole day has gone by since it
	  began, which is what ``now - 1 day`` says without asking the database to do date
	  arithmetic in two dialects. An all-day start is stored at the first instant of its day
	  (§6.5), so comparing that instant against ``now`` directly would call somebody's birthday
	  passed at one minute past midnight *on* their birthday;
	* **an instant with no end** — over when it happens, which is all an instant can mean.

	**The known slop is one hour, twice a year.** A local day is 23 or 25 hours long across a
	daylight-saving boundary and this subtracts 24. **No portable SQL converts an instant using
	a zone taken from the row** — measured rather than recalled, SQLite answers
	``datetime(t, 'Europe/London')`` with NULL, silently — and
	:func:`subroutine.domain.schedule.is_overdue` is where a per-row zone is honoured, in
	Python, on a loaded row.

	**The sentence that used to close this paragraph was the one that hid `#1296`.** It read
	*"Nothing here decides what a person sees: the agenda buckets an occasion by overlapping the
	day being shown, which is exact."* That was true of the case it was written for and was
	never re-asked: the overlap compared a whole-day row's stored instant against the reader's
	day, so an event moved between sections — and sometimes into none — depending on who was
	looking. The agenda enumerates the zones actually present and compares a date against a
	date now, which is exact.

	**The coarseness here is defensible; the reason first given for it was not** (`#1332`).
	That sentence closed *"this function stays coarse because nothing it answers is drawn"*,
	and three things it answers are drawn: ``passed_total`` is printed on the terminal, in the
	browser and to an agent; ``later_total`` is its complement, so the same hour decides which
	of two counts a row is in; and :func:`over` reads this to decide whether a blocker still
	gates, which decides what ``--ready`` returns and what lands in *waiting on somebody else*
	— rows, on a page. The true version is that the slop is **an hour, twice a year, inside the
	day this compares**, which is a claim about the size of the error rather than about who
	reads it, and cannot rot the way the other one did. Making a claim of exactly the kind that
	cost this item, in the paragraph rewritten to record it, is the thing to notice.
	"""

	a_day_ago = now - datetime.timedelta(days=1)

	return sqlalchemy.and_(
		is_occasion(model),
		model.starts_at.is_not(None),
		sqlalchemy.or_(
			sqlalchemy.and_(model.ends_at.is_not(None), model.ends_at < now),
			sqlalchemy.and_(
				model.ends_at.is_(None),
				model.starts_is_all_day,
				model.starts_at <= a_day_ago,
			),
			sqlalchemy.and_(
				model.ends_at.is_(None),
				sqlalchemy.not_(model.starts_is_all_day),
				model.starts_at < now,
			),
		),
	)


def over (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items that are finished *or* have simply gone by.

	**One predicate because there are now two ways to be over** — decision `#1235` §3 says so
	in terms. Work is over when somebody completed or cancelled it; an occasion is over when its
	end is behind you and nobody did anything at all. Every rule asking *is this finished* has to
	ask both, and asking it as two literals that happen to agree is `#1156`.

	``completed_at`` rather than the status vocabulary, for :func:`unblocked`'s reason: §10.7's
	invariant 5 makes that column non-null exactly when the category is ``done`` or
	``cancelled``, so it answers without joining a table an installation may rename rows in.
	"""

	return sqlalchemy.or_(model.completed_at.is_not(None), passed(model, now=now))


def _nothing_blocks_it (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items nothing unfinished is blocking *directly*.

	:func:`unblocked` is this plus the parent axis. Split out because the ancestor test has to
	ask this question of a *different* row, and a rule that called the whole of ``unblocked``
	would call itself.

	**Splitting here rather than making the ancestor test recursive is what keeps it one
	query.** Every ancestor on a path is examined, so a row under a chain is caught by the
	blocked ancestor itself rather than by walking up through the ones between — which is the
	same answer and needs no recursion.
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
			*_live_blocks_edge(link, kind, blocker, filed_in, now=now),
		)
		.correlate(model)
	)


def under_a_blocked_ancestor (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching work filed beneath something that cannot start — `#1610`.

	**Simon's decision of 2026-08-31: blocking inherits down the parent axis.** Until then
	readiness read ``blocks`` edges and never walked ``parent_task_id`` in either direction, so
	a sub-task of a blocked milestone was offered as ready while the milestone itself was
	correctly absent from the same listing — and the offered row printed ``^6``, naming the very
	parent whose state it was ignoring.

	**The direction of the failure is what decided it.** Inheriting hides work that may be
	genuinely startable, and that row is *delayed* — it stays in ``list``, on the board and in
	``show``. Not inheriting hands an agent work from a milestone whose foundations do not
	exist, on the one query it makes with no other context, and says nothing. A delay against
	wasted work is not a close trade.

	**Written as *does any blocked task's path prefix mine*, not as *walk my ancestors*.** The
	indexed direction is descendants (:func:`subroutine.domain.hierarchy.subtree`); ancestors
	*from* a row is a leading-wildcard match that no index can serve. Phrased this way the scan
	is over tasks that have live blockers — forty of five hundred and twenty-one open items on
	this instance — rather than over the table, and it is bounded by that set rather than by how
	deep anybody nests.

	``LIKE 'prefix%'`` rather than a range, for the reason :func:`hierarchy.subtree` records in
	full: PostgreSQL's ``en_GB.UTF-8`` collation does not sort byte-wise, so a half-open range
	silently omits descendants while looking correct on SQLite. A path is ``/id/id/`` with a
	separator on both ends, so a prefix match cannot run one id into another.

	**A finished or deleted ancestor holds nothing**, which is :func:`_live_blocks_edge`'s rule
	arriving on the other axis: finished work is neither held up nor holding anything up.

	**A row with nothing above it is answered without asking** — `#1827`. Its path is
	``/<own id>/``, and this needs a *distinct* row whose path is a prefix of that; for a
	one-segment path the only prefix that is itself a path is the row's own. So the scan is
	dead work for every root, and on this instance's own proportions that is four rows in five.

	``parent_task_id`` rather than ``depth`` or the path's own shape, though all three measure
	the same: it is the column the other two are derived *from*, so where they disagree the
	derived one is the wrong one. The direction of that failure is the safe one — a row with a
	stale path and a parent set still runs the scan below and still finds nothing, where a row
	with a correct multi-segment path and a null parent cannot arise from any write here.

	**Measured on SQLite at 2,000 tasks: 116 ms to 16 ms, identical over all 1,400 rows.** On
	PostgreSQL it is worth nothing at all, and that is the half worth knowing rather than the
	speed-up: `#1800` tried this exact clause there, found it *"moved the estimate not at all"*,
	and recorded it as a dead end — because PostgreSQL had already made the inner half a hashed
	subplan run once. SQLite hashes none of it and evaluates the pair per row, which
	``EXPLAIN QUERY PLAN`` says as ``SCAN task`` inside ``SCAN task``. **A shape measured on one
	backend and declined has only been declined on that backend.**
	"""

	ancestor = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)

	return sqlalchemy.exists(
		sqlalchemy.select(ancestor.id)
		.where(
			# **Inside the `EXISTS`, never beside it** — `#1827`, and it cost a tenfold
			# regression on PostgreSQL to find out. `unblocked` negates this whole predicate,
			# so an `AND` written outside becomes an `OR` by De Morgan and both branches are
			# costed for every row: `ready` went from 23 ms to 1,168 ms with the identical
			# clause one bracket further out. That is `#1800`'s own finding — *a negation is
			# not free* — arriving on the fix for the thing it was written about.
			model.parent_task_id.is_not(None),
			ancestor.id != model.id,
			ancestor.workspace_id == model.workspace_id,
			ancestor.deleted_at.is_(None),
			sqlalchemy.not_(over(ancestor, now=now)),
			# Paths carry no `%` or `_` — they are hex and separators — so there is nothing to
			# escape, which is the argument `hierarchy.subtree` makes for its own `autoescape`.
			model.path.like(ancestor.path.concat("%")),
			sqlalchemy.not_(_nothing_blocks_it(ancestor, now=now)),
		)
		.correlate(model)
	)


def a_container (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching an item with sub-tasks that are not finished — `#1353`.

	**Simon's decision of 2026-08-27**, in his own words: *a task with sub-tasks is done when
	those sub-tasks are done, therefore you cannot start the parent.* `#1290`'s reviewer laid
	five milestones out as parent tasks and all five appeared in ``--ready`` beside the one leaf
	that could actually be started.

	**Not *has children*, and that is the whole subtlety.** A parent whose children are all done
	is precisely the row somebody should be looking at — `#84`'s *3/3 beside an open parent is
	the question being put to a person*. So the rule is *has **unfinished** children*, and the
	row it deliberately leaves in ``--ready`` is the one `#1615` is about surfacing.

	**Deliberately not folded into :func:`unblocked`**, where its sibling `#1610` does belong.
	``unblocked`` is what :func:`blocked_among` marks rows from, and a container is not
	*blocked* — nothing is holding it up, it is simply not the row you start. Marking it
	*Blocked* would say something false on three surfaces to save one clause here.

	**And it is not auto-completion.** `#84` refuses that with two reasons that still hold: it
	credits whoever closed the last child with a decision they did not take, and it cannot
	reverse when a child is added later. A parent is *unstartable*, never *done*.
	"""

	child = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)

	return sqlalchemy.exists(
		sqlalchemy.select(child.id)
		.where(
			child.parent_task_id == model.id,
			child.deleted_at.is_(None),
			sqlalchemy.not_(over(child, now=now)),
		)
		.correlate(model)
	)


def every_sub_task_is_done (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching an unfinished parent whose sub-tasks are all over — `#1615`.

	**The row :func:`a_container` deliberately leaves behind.** That predicate is *has
	unfinished children*, so a parent whose children are all done stays in ``--ready`` on
	purpose — `#84`'s *3/3 beside an open parent is the question being put to a person*. This is
	the other half: nothing anywhere was *putting* the question.

	**The failure it exists to catch is silent and delayed.** Somebody finishes a milestone,
	moves on, and the next milestone never becomes ready — because `#84` refuses auto-completion
	for two reasons that still hold, and the person best placed to notice has already left.

	**Three clauses and each is load-bearing.** It must have children, or every leaf in the
	instance qualifies vacuously. None of them may be unfinished, which is
	:func:`a_container` negated. And the parent itself must not be over, because once somebody
	has taken the decision there is no question left to put.
	"""

	child = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)

	return sqlalchemy.and_(
		sqlalchemy.not_(over(model, now=now)),
		sqlalchemy.exists(
			sqlalchemy.select(child.id)
			.where(child.parent_task_id == model.id, child.deleted_at.is_(None))
			.correlate(model)
		),
		sqlalchemy.not_(a_container(model, now=now)),
	)


def unblocked (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items nothing unfinished is holding up.

	A ``blocks`` link runs source → target, so an item's blockers are the *sources* of links
	pointing at it. Only tasks can block: a document has no state that could finish, so a
	``derives_from`` to a specification would otherwise block every task in the project
	forever.

	**Finished is read off ``completed_at``, not off the status vocabulary.** §10.7's
	invariant 5 makes that column non-null exactly when the category is ``done`` or
	``cancelled``, so it answers the same question without joining a table an installation is
	free to rename rows in.

	**``now`` is here because there are two ways to be over** (decision `#1235` §3). A code
	freeze holds a deploy shut until it lifts and then stops, with nobody marking anything done
	— so what counts as a live blocker is a question about the clock, and :func:`over` is the
	one predicate that answers it.

	**Two axes since `#1610`, and this is the one place that joins them.** A row is held up by
	a live ``blocks`` edge pointing at it, *or* by one pointing at anything it is filed
	beneath. Both are *blocked* in the sense a reader means, which is why they are one
	predicate: :func:`blocked_among` is built from this, so a listing marks exactly the rows
	``?ready=true`` hides. §6.3a's warning is what that rule exists for — a predicate the
	database filters by and a reader that labels a loaded row must agree, or a listing marks
	one set and the filter hides another.

	**Its sibling `#1353` is deliberately elsewhere**: a parent with unfinished children is not
	*blocked*, it is simply not the row you start, and :func:`a_container` says so in
	:func:`ready` instead.
	"""

	return sqlalchemy.and_(
		_nothing_blocks_it(model, now=now),
		sqlalchemy.not_(under_a_blocked_ancestor(model, now=now)),
	)


def blocking (
	model: type[typing.Any], *, now: datetime.datetime
) -> sqlalchemy.ColumnElement[bool]:
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
			*_live_blocks_edge(link, kind, held, filed_in, now=now),
		)
		.correlate(model)
	)


def _live_blocks_edge (
	link: type[typing.Any],
	kind: type[typing.Any],
	other: typing.Any,
	filed_in: typing.Any,
	*,
	now: datetime.datetime,
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
		#
		# **And an occasion that has gone by is over without anybody saying so** (decision
		# `#1235` §3). A code freeze is exactly the thing somebody blocks a deploy on, and
		# nothing will ever set its `completed_at` — the product goes out of its way not to
		# suggest it — so reading that column alone would leave the deploy blocked for ever.
		sqlalchemy.not_(over(other, now=now)),
		other.deleted_at.is_(None),
		# **And the project it is filed in is still there.** `projects.delete` does not touch
		# its tasks — "every listing joins the project and excludes deleted ones, so they
		# leave the visible world with it" — so a task in a binned project keeps a null
		# `deleted_at` and went on blocking live work from outside every listing there is.
		# Worse than a wrong answer: the caller was told an item was blocked and shown no
		# link at all, because `links.edges` drops an end they cannot see.
		filed_in.deleted_at.is_(None),
	)


def blocked_by_somebody_else (
	model: type[typing.Any], *, now: datetime.datetime, user_id: uuid.UUID
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching work held up by an item that is somebody else's.

	**The narrow reading, and it is Simon's** (decision `#1267` §3a). *Blocked by anything*
	floods a solo instance, which is most instances — and a solo instance's blockers are its own
	work, which is `#96`'s original argument and still holds there. What this asks is narrower:
	is there a live blocker whose assignee is a person, and is that person somebody other than
	the one asking.

	**`#96` is amended by this rather than overturned.** Its rule was *blocked is tracked;
	waiting is a defer with a reason*, and its reason was that a ``blocks`` link resolves
	itself. **That sentence is a claim about a single worker.** When the blocker is somebody
	else's row it resolves when *they* act, and nothing tells you it has been sitting there.

	**An unassigned blocker does not count**, deliberately: nobody is holding it, so there is
	nobody to chase, and the honest thing to say about it is that it is unclaimed work — which
	is what ``--ready`` already says one axis along. That rule is one comparison and a test
	rather than two comparisons; the comment beside it says why.

	:func:`unblocked`'s edges exactly, read the same direction, with one more join. Deliberately
	not narrowed by visibility, for that function's reason: whether work is blocked is a fact
	about the work rather than about the viewer. What that discloses is bounded and is the same
	bound as before — that something unseen holds this up, never what, and never whom. Naming
	the holder is a decision of its own.
	"""

	link = subroutine.db.models.work.Link
	blocker = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)
	filed_in = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
	kind = subroutine.db.models.vocabulary.LinkType

	return sqlalchemy.exists(
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
			# **One clause, and an unassigned blocker falls out of it on its own.** `NULL !=
			# x` is unknown and a `WHERE` drops it, so a second `IS NOT NULL` beside this
			# grants nothing — measured, by deleting it and watching every test still pass,
			# which is `#303`'s shape and the reason it is not here. What holds the rule is
			# the test, on both backends, because *unassigned does not count* is a decision
			# rather than an accident of three-valued logic.
			blocker.assignee_id != user_id,
			*_live_blocks_edge(link, kind, blocker, filed_in, now=now),
		)
		.correlate(model)
	)


def blocked_among (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	now: datetime.datetime,
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

	**Asked as the page minus what is unblocked, rather than as the negation** — `#1800`, and
	the difference is 818 ms against 18 ms on 1,497 tasks. :func:`unblocked` is an ``AND``, so
	a planner applies the cheap direct-blocker anti-join first and reaches the ancestor
	subplan only for what survives; ``NOT`` of it is an ``OR`` by De Morgan, where both
	branches have to be costed for every row. That estimated a page of a hundred at **1.5
	million** against real work of ~20 ms — and a cost estimate is what PostgreSQL decides to
	JIT-compile on, so it spent **780 ms generating 135 functions** for a query that then ran
	in 30. Measured: ``jit=off`` takes the old form to 30 ms, which is the whole of the
	evidence that nothing was ever wrong with the plan.

	**``EXCEPT`` rather than subtracting in Python**, which is the same speed and not the same
	answer: an identifier naming no task is in neither set, so subtracting from what was asked
	would report it blocked. One statement either way, so no cost guard moves.

	**This is why the predicate itself is untouched.** The rule a listing filters by and the
	rule that labels a loaded row are still the one expression — §6.3a — and this asks it in
	the direction the planner can cost. Rewriting :func:`under_a_blocked_ancestor` was tried
	first and is a dead end: hoisting its inner half into a subquery, into a CTE, and guarding
	it with ``parent_task_id IS NOT NULL`` each returned the identical answer and moved the
	estimate not at all, because the inner half was **already hashed** and evaluated once.
	"""

	wanted = set(identifiers)

	if not wanted:
		return set()

	model = subroutine.db.models.work.Task
	here = sqlalchemy.select(model.id).where(model.id.in_(wanted))

	return set(session.scalars(here.except_(here.where(unblocked(model, now=now)))))


def finished_underneath_among (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	now: datetime.datetime,
) -> set[uuid.UUID]:
	"""Return which of these tasks have sub-tasks and no unfinished one — `#1615`.

	:func:`blocked_among`'s shape and for its reason: one ``EXISTS`` scan for a whole page
	rather than a question per row, which is `#39`'s N+1 and the recorded obstacle to marking
	anything derived on a listing at all.

	**Not narrowed by visibility**, exactly as its two neighbours are not. Whether a parent's
	own sub-tasks are finished is a fact about that work rather than about the reader, and
	counting only the children somebody can see would say the question is ready to be answered
	when it is not.
	"""

	return _matching(
		session, identifiers, lambda model: every_sub_task_is_done(model, now=now)
	)


def blocking_among (
	session: sqlalchemy.orm.Session,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	now: datetime.datetime,
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

	return _matching(session, identifiers, lambda model: blocking(model, now=now))


def blockers_among (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	identifiers: typing.Iterable[uuid.UUID],
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	now: datetime.datetime,
) -> dict[uuid.UUID, tuple[subroutine.db.models.work.Task, ...]]:
	"""Return what is holding each of these tasks up, keyed by the task it holds up — `#1287`.

	**The one function here that *is* narrowed by visibility, and that is the whole decision.**
	:func:`blocked_among` and :func:`blocking_among` are deliberately not, because whether work
	is blocked is a fact about the work rather than about the viewer, and marking a row from a
	blocker the caller cannot see is honest — it says *that*, never *what*. Naming the far end
	says *what*, so it takes the rule that governs naming a far end: ``domain.links`` drops an
	end the caller may not see, and this drops the same ones, from
	:func:`subroutine.domain.scoping.readable_tasks` rather than from a second copy.

	**So a row here can be held up by more than this reports**, and nothing says by how many.
	That is deliberate: a count of what was withheld is more than *something unseen holds this
	up*, which is the bound :func:`blocked_among`'s docstring already draws and the most this
	may disclose. The consequence a reader should know is that chasing everybody named does not
	guarantee the row moves — which is true of an unassigned blocker too, and is why this
	reports **every** live blocker rather than only the ones
	:func:`blocked_by_somebody_else` counts.

	**One statement whatever the page**, which is why this is not
	:func:`subroutine.domain.links.edges` in the agenda: that function answers the general
	question — every link, both ends, either entity type — in three. Here the far end is known
	to be a task, the relation is known to be gating, and the near end is on the page, so the
	same answer is one join off the readable set. `#39`'s N+1 is what this shape exists to
	avoid and `#1295` is the guard that would see it.

	The liveness rule is :func:`_live_blocks_edge`'s, read in the same direction
	:func:`_nothing_blocks_it` reads it, so what this names is exactly what holds a row up by
	an edge of its own, less whatever visibility withheld.

	**It is the direct edges and never the ancestor rule** (`#1610`), which is worth knowing
	before reading an empty answer as a contradiction: a row can be marked ``blocked`` because
	something above it cannot start, and there is no edge on *that* row to name. Nothing here
	reaches such a row today — the agenda's section is built from
	:func:`blocked_by_somebody_else`, which is direct edges too — and the remedy if anything
	ever does is to read the parent, which is what the count beside a readiness listing already
	says.
	"""

	wanted = set(identifiers)

	if not wanted:
		return {}

	blocker = subroutine.db.models.work.Task
	filed_in = subroutine.db.models.project.Project
	link = subroutine.db.models.work.Link
	kind = subroutine.db.models.vocabulary.LinkType

	# **Deleted, archived and template rows are readable here for `links._visible`'s reason**,
	# and are then judged by `_live_blocks_edge` rather than by the listing defaults: which
	# rows a caller may *see* and which edges *count* are two questions, and answering the
	# second with the first would leave a row marked blocked by an archived item and reported
	# as blocked by nothing.
	statement = (
		subroutine.domain.scoping.readable_tasks(
			principal,
			workspace_ids=workspace_ids,
			include_deleted=True,
			include_archived=True,
			include_templates=True,
		)
		.add_columns(link.target_id)
		.join(
			link,
			sqlalchemy.and_(link.source_type == "task", link.source_id == blocker.id),
		)
		.join(kind, kind.id == link.link_type_id)
		.where(
			link.target_type == "task",
			link.target_id.in_(wanted),
			*_live_blocks_edge(link, kind, blocker, filed_in, now=now),
		)
		.order_by(blocker.ref)
	)

	# **Every id asked about gets an entry**, so a caller can tell *nothing live and visible
	# holds this up* from *this row was never asked about*. Both are empty otherwise, and on
	# the one surface that calls this the first means the ends were withheld — see
	# :func:`subroutine.views._holding_up`, which is where that distinction is published.
	holding: dict[uuid.UUID, list[subroutine.db.models.work.Task]] = {one: [] for one in wanted}

	for row, held_up in session.execute(statement):
		holding[held_up].append(row)

	return {one: tuple(rows) for one, rows in holding.items()}


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
	model: type[typing.Any],
	*,
	now: datetime.datetime | sqlalchemy.ColumnElement[datetime.datetime],
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching items whose defer instant has passed, or that have none.

	``now`` is passed in rather than read here so that one request resolves every relative
	comparison against a single instant — the same rule ``domain.tasks`` follows, and the
	reason a task cannot be deferred and ready in one listing.

	**It may be an expression rather than a value, and the agenda passes one** (`#1296`). A
	whole-day defer is stored at the first instant of *its own* local day (§6.5), so a single
	boundary answers *has this come round* differently for two people an hour apart — measured,
	a defer to tomorrow was visible to one reader and hidden from another. Taking a per-row
	boundary here is what lets that be fixed without a second copy of this rule: ``?ready=``
	goes on asking *can I start this now*, which is honestly an instant.
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


def yours_to_answer (
	model: type[typing.Any], *, now: datetime.datetime, user_id: uuid.UUID
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching work somebody has been named for — `#1774`.

	**Assigned to you, or held by you.** :func:`yours_to_act_on` is this *plus* work assigned
	to nobody, and is written in terms of this below, so the two clauses they share exist in
	one place rather than in two that agree until somebody moves one (`#508`).

	**The difference between them is whether a heading names a person.** An agenda asks *what
	could I pick up*, so an unowned row belongs on everybody's — that is decision `#1267` §1
	and it stands. *Waiting on you* asks something else: `BUCKETS` says so in its own comment,
	that *every other bucket is work the reader could pick up; this one is work they are
	holding up*. A row nobody has been given is not work anybody in particular is holding up.

	**What makes it unanswerable rather than merely unassigned is `#96`.** There is no fifth
	status category, so ``needs_input`` carries no principal at all: it says a question is
	parked and cannot say whose. Reading that silence as *everybody's* put two of Simon's
	decisions on a new colleague's agenda within an hour of the first shared instance, under a
	heading addressed to him.

	**Held by you is kept, and that clause is why this is not ``assigned_to_me``.** Somebody
	holding a live lease on a parked question is exactly the person acting on it, so dropping
	them would recreate the failure :func:`yours_to_act_on`'s third clause exists to prevent —
	work vanishing from the agenda of the one person who has started it. A calendar feed's
	``audience`` really is strictly-assigned and stays that way (decision `#1267` §2).

	**An unowned question is not hidden by this, it is relabelled.** The buckets are disjoint
	by subtraction, so a row this declines falls through to whichever bucket its dates put it
	in, and `#1383` marks ``needs_input`` on every surface — so it still says what it is
	wherever it lands. `#1116`'s argument for ranking this bucket above *Overdue* was that
	*you owe an answer* beats *this is late*, which presumes the reader owes it.
	"""

	return sqlalchemy.or_(
		model.assignee_id == user_id,
		sqlalchemy.and_(held(model, now=now), model.claimed_by_id == user_id),
	)


def yours_to_act_on (
	model: type[typing.Any], *, now: datetime.datetime, user_id: uuid.UUID
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching work one person's agenda is about — decision `#1267` §1.

	Simon's rule, taken 2026-08-26 before a second human was added: **assigned to you, or
	assigned to nobody, or held by you**. Named from the decision's own sentence — *a row
	belongs on your agenda when it is yours to act on* — so the code and the decision cannot
	drift into two vocabularies for one rule.

	**The third clause is not optional and is the reason this is three clauses rather than
	two.** The agenda's first bucket is *In progress* (`#1243`), so a rule keyed on the
	assignee alone makes something you have already started vanish from your own agenda
	because it carries somebody else's name — the one state where an agenda is most obviously
	wrong. :func:`unclaimed` is the same argument one axis along: *your own claim does not
	hide your own work*.

	**The two axes stay separate** (`#726`): a claim and a status are two different facts and
	neither is derived from the other. This joins them with ``or``; it does not conflate them.

	**This is not ``assigned_to_me``, and the difference is deliberate** (decision `#1267`
	§2). A calendar feed's ``audience`` already carries that word for ``assignee_id ==
	owner_id`` — strictly assigned, and *not* including the unassigned pool. The two answer
	different questions: a calendar asks *what am I on the hook for*, where two hundred
	unassigned backlog items are not that; an agenda asks *what could I pick up*, where they
	are exactly what it is for. One word covering both is this codebase's signature defect
	arriving in vocabulary rather than in code, so they keep two.

	**An expired lease does not count**, because :func:`held` is what *holding* means here and
	nothing else in this module reads a claim any other way.

	``user_id`` is required rather than defaulted, for :func:`ready`'s reason: a caller that
	forgot it would silently hand somebody a different person's agenda.

	**One bucket does not take the middle clause, and the exception is recorded here rather
	than only where it is taken** (`#1774`). :func:`yours_to_answer` is the other two, and
	*Waiting on you* reads it: a carve-out written at its own site alone reads to the next
	person as a dead rule, and whoever next changes this rule needs to meet its exception.
	"""

	return sqlalchemy.or_(
		yours_to_answer(model, now=now, user_id=user_id),
		model.assignee_id.is_(None),
	)


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
		unblocked(model, now=now),
		_startable_apart_from_blocking(model, now=now, by=by),
	)


def _startable_apart_from_blocking (
	model: type[typing.Any], *, now: datetime.datetime, by: uuid.UUID | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return every reason a row is not startable **except** something holding it up.

	Split out of :func:`ready` for :func:`held_under_a_blocked_ancestor`, which asks *what
	would have been offered but for the parent axis* and therefore needs all of these and none
	of the blocking. Two readers, one list — writing the clauses out twice is how a count and
	the listing it describes come to disagree about their own subject.
	"""

	return sqlalchemy.and_(
		undeferred(model, now=now),
		unclaimed(model, now=now, by=by),
		in_a_running_project(model),
		# **A container is not work anybody can start** (`#1353`, Simon 2026-08-27). Its
		# sibling `#1610` lives in `unblocked`, because a row under a blocked ancestor
		# really is blocked; this one is not, so it is a clause here and `blocked_among` does
		# not mark it. Two halves of one decision, in the two places each is true.
		sqlalchemy.not_(a_container(model, now=now)),
		# **An event is not work anybody can be offered** (decision `#1235` §4). It happens
		# whether or not you act, so ranking it against the backlog and handing it back as
		# *what to start next* is offering the wrong thing — measured on a disposable instance,
		# where a birthday five months past was returned by `--ready` with the tip
		# `subroutine done 2` beside it.
		#
		# **The category, not the dates.** An ordinary task may carry a start and an end and is
		# still work; what makes something an occasion is what it *is*.
		sqlalchemy.not_(is_occasion(model)),
	)


def held_under_a_blocked_ancestor (
	model: type[typing.Any], *, now: datetime.datetime, by: uuid.UUID | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching what ``ready`` would offer but for the parent axis.

	**The other half of Simon's decision on `#1610`**: blocking inherits down the parent axis,
	*and the listing says how many rows that held back*. `#1265`'s precedent, where the same
	trade was taken for the agenda — an empty page and a filtered page read identically, and
	the second is the ordinary state of somebody's first morning on a real plan.

	**The ancestor half only, deliberately.** A container absent from ``--ready`` needs no
	explanation: it was never work, and `#84`'s model says so. What is worth a number is work
	that exists, is otherwise startable, and is waiting on something filed above it — the case
	a reader would otherwise have to reconstruct from the whole listing.

	**Nothing here is a second copy of the rule.** It is ``_nothing_blocks_it`` and the ancestor
	test — the two halves :func:`unblocked` composes — with the *opposite* answer to the second,
	over the same clause list :func:`ready` uses. So a row can be in this count or in the
	listing and never in both, by construction rather than by agreement.
	"""

	return sqlalchemy.and_(
		_nothing_blocks_it(model, now=now),
		under_a_blocked_ancestor(model, now=now),
		_startable_apart_from_blocking(model, now=now, by=by),
	)
