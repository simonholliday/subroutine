"""Splitting one listing into several, so that no group can starve its neighbours.

A board asks a question an ordinary page cannot answer. ``GET /v1/tasks?limit=100`` spends
one allowance across every column in one order, so a column holding older work loses its rows
to an unrelated column's recency — and says nothing about it. Measured on this instance while
`SR#1790` was filed: *In progress* drew one row where three existed, and *Open* drew 27 where
there were 275.

**A group is given its own allowance, and says whether it used all of it.** That is the whole
mechanism. Each group is the caller's query narrowed by one value of one axis, ordered by the
same keys and capped on its own — so what one column holds cannot decide what another shows.

**This is the agenda's arrangement asked of a second axis** (:mod:`subroutine.domain.agenda`),
and `SR#1285`'s rule governs both: *a cap is a display choice and never a membership one*. The
agenda pays for that by loading every capped bucket whole and slicing in Python, because its
buckets are disjoint by subtraction — a row hidden by one cap would fall through into the next
bucket and be offered as work. **Nothing here needs that, and the reason is worth knowing before
somebody copies the agenda's machinery over.** An axis partitions on a value each row already
carries, so every row is in exactly one group by construction. There is no subtraction, nothing
to spill, and the cap can therefore go into the query where it belongs.

**An axis must be bounded, which is why this is a register rather than a free field.** Grouping
by a status category asks four questions; grouping by an assignee would ask one per member of
the workspace, which is an N+1 wearing a query parameter.

**`SR#1425` answered that rather than waiving it** (Simon, 2026-09-20). There are two kinds of
axis now. A **fixed** one declares its keys, and its groups are the vocabulary's: four
categories, asked whether or not any row carries them. A **row-keyed** one reads its keys off
the rows the caller's own query returns, so the number of groups cannot exceed what that query
found — **there is no roster query and no cap for anybody to justify**, which is what makes it
affordable without a number nobody can defend. Measured on this instance while it was built:
163 tasks carry an assignee and the whole installation has nine accounts, so the worst case
there is ten columns, against the agenda's seventeen statements.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.domain.filtering
import subroutine.domain.tasks
import subroutine.errors

#: The one axis a listing can be grouped by today.
#:
#: **Declared in :mod:`subroutine.domain.filtering` since `SR#1803`**, and re-exported here
#: because this module's own functions are what a reader looking for grouping opens first. It
#: is one string in one place: an axis is a *property of an item* with a third capability, and
#: keeping its name here while its keys were declared there is the shape the registry removes.
STATUS_CATEGORY = subroutine.domain.filtering.STATUS_CATEGORY

#: Which axes each kind of thing can be grouped by, and the keys each one has.
#:
#: **The keys come from the fixed vocabulary rather than from the rows**, and that is what makes
#: an empty group reportable. A group derived from what came back cannot distinguish *this
#: column holds nothing* from *this column was not asked about*, which is the false statement
#: `SR#718`, `SR#738` and `SR#744` were each filed about on the surface that renders them.
#:
#: **That is a statement about this register and not about grouping**, since `SR#1425`.
#: :data:`ROW_AXES` holds the other kind, whose keys *are* derived from what came back — and
#: it pays exactly the price this paragraph names: a person holding nothing in a selection has
#: no column there, so that axis cannot say *nothing for them*. It was chosen knowing so.
#:
#: **Read from the property registry since `SR#1803`.** This was the third of three lists
#: declaring what a listing may be asked, in the third module, and **no field was in all
#: three** — so *groupable* was a fact kept somewhere none of the other two could see. A kind
#: with no axis answers with an empty map, which is what it always did.
AXES: dict[str, dict[str, tuple[str, ...]]] = {
	kind: subroutine.domain.filtering.axes(kind)
	for kind in subroutine.domain.filtering.PROPERTIES
	if subroutine.domain.filtering.axes(kind)
}

#: The axes whose keys are the rows' own values — `SR#1425`.
#:
#: **A tuple of names and no keys, because there are none to declare.** What a listing may be
#: grouped by is :data:`AXES` *and* this; :func:`refuse_unknown_axis` is the one place that
#: joins them, so nothing else has to remember that there are two registers.
ROW_AXES: dict[str, tuple[str, ...]] = {
	kind: subroutine.domain.filtering.row_axes(kind)
	for kind in subroutine.domain.filtering.PROPERTIES
	if subroutine.domain.filtering.row_axes(kind)
}

#: The group holding the rows nobody is named on — `SR#1425`.
#:
#: **The one key of a row-keyed axis that is *not* read off the rows**, and that is deliberate:
#: it is known before any row is looked at, so it can be reported empty. *Nothing here is
#: unassigned* is the answer `SR#1422` §5 says a lead is actually looking for — *work in
#: progress that nobody owns* — and it is the one column of this axis that can give it.
#:
#: **Spelled as the filter grammar already spells it.** ``assignee.is=unset`` is how a caller
#: asks this question today (two reserved words, `SR#1801` §11), so a group key of ``unset``
#: is the same word in the same place rather than a second name for one idea. It also cannot
#: collide with an account: a username is compared against this only after the reserved word
#: has been taken, which :func:`narrowings_from_rows` does by construction.
UNASSIGNED = "unset"

#: How many rows one group carries when the caller does not say.
#:
#: **Smaller than a page, because there are several of them.** A board drawing four columns at
#: the ordinary page size would fetch four hundred rows to show what fits on a screen, which is
#: the cost this exists to avoid rather than to move. Simon's own range for the browser was 20
#: to 50; twenty-five sits in it and is what `SR#1790` was measured against.
DEFAULT_GROUP_SIZE = 25

#: The most any one group may be asked for.
#:
#: **Deliberately below :func:`subroutine.domain.paging.size`'s ceiling**, because the cost of
#: a grouped request is this number times the number of groups. A caller who genuinely wants a
#: thousand rows of one category is asking for an ordinary listing narrowed to it, which is
#: cheaper, pages properly, and already exists.
MAX_GROUP_SIZE = 100


def refuse_unknown_axis (asked: str, *, kind: str) -> str:
	"""Return the axis a listing was asked to group by, or refuse it by name.

	**Named rather than ignored**, which is `SR#1484`'s rule and `SR#1626`'s defect: a listing
	that quietly drops an axis it does not understand answers with the whole ungrouped page,
	and the wrong answer is a *superset*, so nothing looks broken.
	"""

	available = set(AXES.get(kind, {})) | set(ROW_AXES.get(kind, ()))

	if asked in available:
		return asked

	known = ", ".join(sorted(available))

	raise subroutine.errors.ValidationError(
		f"{asked!r} is not something this listing can be grouped by.",
		errors=[
			subroutine.errors.FieldError(
				field="group_by",
				code="invalid_field_value",
				message=f"No grouping called {asked!r}.",
				hint=f"Group by one of: {known}." if known else "This listing cannot be grouped.",
			)
		],
	)


def size (asked: int | None) -> int:
	"""Return how many rows one group may carry, refusing an impossible answer by name.

	**One arbiter, like :func:`subroutine.domain.paging.size`**, so that this endpoint and the
	local client refuse the same request identically. Two copies of that rule produced two
	different field names in the refusal once already, which is the whole reason that function
	exists to be copied from.
	"""

	if asked is None:
		return DEFAULT_GROUP_SIZE

	if asked < 1 or asked > MAX_GROUP_SIZE:
		raise subroutine.errors.ValidationError(
			f"A group can hold between 1 and {MAX_GROUP_SIZE} rows.",
			errors=[
				subroutine.errors.FieldError(
					field="group_limit",
					code="invalid_field_value",
					message=f"{asked} is outside 1 to {MAX_GROUP_SIZE}.",
					hint=(
						"Ask for fewer, or narrow the listing to one group and page it "
						"in the ordinary way."
					),
				)
			],
		)

	return asked


def keys_for (axis: str, *, kind: str) -> tuple[str, ...]:
	"""Return every key an axis *declares*, in the order a reader meets them.

	**Empty for a row-keyed axis** (`SR#1425`), which declares none — its groups are read off
	the rows by :func:`columns` and cannot be known without them. Answering with nothing
	rather than raising is what keeps :func:`unreached` free of the axis guard its own
	docstring explains it must not have: the question that function asks is which keys are
	finished categories, and an axis with no declared keys has none by construction.
	"""

	return AXES.get(kind, {}).get(axis, ())


def unreached (axis: str, *, kind: str, reaching_finished: bool) -> frozenset[str]:
	"""Return the keys of the groups a listing's completion rule never looked at — `SR#2293`.

	**A grouped request names an axis and not a value**, which is what makes this necessary.
	:func:`subroutine.domain.tasks.completion_wanted` reaches finished work when something in
	the request *asks* for it — a finished category, a finished status key, a ``completed_at``
	comparison, the trash, a bare ref — and ``group_by=status_category`` asks for all four
	categories at once, two of which are finished. So the rows for those two are excluded by
	the ordinary default, and the groups come back empty rather than absent.

	**An empty group and an unasked one are different facts**, which is the distinction this
	module exists to preserve and the one its own docstring names. Saying so on the answer is
	what stops each surface modelling the completion rule for itself: the browser carried such
	a model and could only be right about the spellings it happened to know.

	**Read through :func:`keys_for`, so the answer can only ever name groups the axis has.** A
	kind whose categories are not about finishing — a document is ``draft``/``current``/
	``superseded``/``archived`` — has none of these keys and gets an empty answer without a
	branch saying so.

	**And there is deliberately no second guard on the axis.** One reading `axis !=
	`:data:`STATUS_CATEGORY` was written here and then removed: every answer it changed was
	already empty, because the keys of any other axis cannot be a task's finished categories.
	A clause that cannot change an answer is this codebase's inert control, and the reason to
	take it out rather than leave it is that it reads as the thing doing the work.
	"""

	if reaching_finished:
		return frozenset()

	return frozenset(
		key
		for key in keys_for(axis, kind=kind)
		if key in subroutine.domain.tasks.FINISHED_CATEGORIES
	)


def columns (
	session: sqlalchemy.orm.Session,
	statement: sqlalchemy.Select[typing.Any],
	*,
	axis: str,
	kind: str,
	workspace_id: uuid.UUID,
	status_column: sqlalchemy.orm.Mapped[uuid.UUID],
) -> list[tuple[str, sqlalchemy.ColumnElement[bool]]]:
	"""Return this listing's groups in order, each with the clause that selects it — `SR#1425`.

	**One question asked of two kinds of axis**, so that :mod:`subroutine.api.grouped` does not
	have to know there are two. A fixed axis answers from the vocabulary and always returns
	every key it has; a row-keyed one answers from the caller's own rows and returns the keys
	those rows carry, plus :data:`UNASSIGNED`.

	``statement`` is the caller's whole query, which is what bounds the second kind: the keys
	cannot name anybody the caller's own filters did not already return.
	"""

	if axis in AXES.get(kind, {}):
		clauses = narrowings(
			session,
			workspace_id=workspace_id,
			axis=axis,
			kind=kind,
			status_column=status_column,
		)

		return [(key, clauses[key]) for key in keys_for(axis, kind=kind)]

	return narrowings_from_rows(session, statement, axis=axis, kind=kind)


def narrowings_from_rows (
	session: sqlalchemy.orm.Session,
	statement: sqlalchemy.Select[typing.Any],
	*,
	axis: str,
	kind: str,
) -> list[tuple[str, sqlalchemy.ColumnElement[bool]]]:
	"""Return the groups a row-keyed axis has in this listing — `SR#1425`.

	**Two statements, whatever the answer.** One reads the distinct values the caller's rows
	carry; one turns the ids among them into names. Neither grows with the workspace, which is
	the whole argument for reading the keys off the rows: a roster query would have made the
	cost a function of how many people exist rather than of how many are in this answer.

	**Nobody comes first**, because it is the exception a lead is looking for (`SR#1422` §5:
	*work in progress that nobody owns*), and the rest are in name order — **not** most-work
	first, which would reshuffle the board between one look and the next. The set of columns
	already moves as work moves; the order should not move as well.

	**An id with no account behind it is left out rather than given a column named after a
	uuid.** It cannot happen through the application, which is why this is a dropped row and
	not a refusal: a column headed by 36 hex characters is worse than a column that is absent,
	and the rows are still reachable from the ungrouped listing.
	"""

	column = subroutine.domain.filtering.PROPERTIES[kind][axis].column

	assert column is not None, f"{axis} is an axis with no column to group on"

	held: set[uuid.UUID | None] = set(
		session.scalars(statement.with_only_columns(column).order_by(None).distinct())
	)
	named = _named(session, {one for one in held if one is not None})
	found: list[tuple[str, sqlalchemy.ColumnElement[bool]]] = [
		(UNASSIGNED, column.is_(None))
	]

	found.extend(
		(name, column == identifier)
		for identifier, name in sorted(named.items(), key=lambda pair: pair[1])
	)

	return found


def _named (
	session: sqlalchemy.orm.Session, identifiers: typing.Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
	"""Return the username of each of these accounts, for the ones that exist."""

	if not identifiers:
		return {}

	model = subroutine.db.models.identity.User

	# **`.all()`, and never `dict(session.execute(...))`.** A `Result` has a `.keys()`
	# method, so `dict()` treats it as a mapping and raises `TypeError: not subscriptable` -
	# a trap this project met once by *applying* ruff's C416 to working code. C416 asks for
	# `dict()` here too; this is the spelling that satisfies it and works.
	return dict(
		session.execute(
			sqlalchemy.select(model.id, model.username).where(model.id.in_(set(identifiers)))
		)
		.all()
	)


def narrowings (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	axis: str,
	kind: str,
	status_column: sqlalchemy.orm.Mapped[uuid.UUID],
) -> dict[str, sqlalchemy.ColumnElement[bool]]:
	"""Return the clause that selects each group, keyed by the group it selects.

	**One query for every group, rather than one per group.** The obvious shape here is to ask
	:func:`subroutine.domain.tasks.statuses_in_category` once per category, which is four
	statements to answer a question about one table — `SR#39`'s N+1 at the vocabulary rather
	than at the rows.

	**A category with no status in it gets a clause that selects nothing**, never a missing
	entry. It is a real group that is really empty, and dropping it
	here would push the decision onto whichever surface renders it — where the answer has been got wrong three
	times already.
	"""

	model = subroutine.db.models.vocabulary.Status

	held: dict[str, list[uuid.UUID]] = {key: [] for key in keys_for(axis, kind=kind)}

	for identifier, category in session.execute(
		sqlalchemy.select(model.id, model.category).where(
			model.workspace_id == workspace_id, model.entity_type == kind
		)
	):
		# A status in a category this kind does not have cannot be reached by a row of this
		# kind either, so it is skipped rather than being made into a group nobody asked for.
		if category in held:
			held[category].append(identifier)

	return {
		key: status_column.in_(identifiers) if identifiers else sqlalchemy.false()
		for key, identifiers in held.items()
	}
