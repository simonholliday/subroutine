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
the workspace, which is an N+1 wearing a query parameter. `SR#1425` wants a board grouped by
principal and will need an answer to that before it can use any of this.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.domain.filtering
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
#: **Read from the property registry since `SR#1803`.** This was the third of three lists
#: declaring what a listing may be asked, in the third module, and **no field was in all
#: three** — so *groupable* was a fact kept somewhere none of the other two could see. A kind
#: with no axis answers with an empty map, which is what it always did.
AXES: dict[str, dict[str, tuple[str, ...]]] = {
	kind: subroutine.domain.filtering.axes(kind)
	for kind in subroutine.domain.filtering.PROPERTIES
	if subroutine.domain.filtering.axes(kind)
}

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

	available = AXES.get(kind, {})

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
	"""Return every key an axis has, in the order a reader meets them."""

	return AXES[kind][axis]


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
