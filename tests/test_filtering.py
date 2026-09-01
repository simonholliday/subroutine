"""Asking a listing about a date — item `SR#815`, decision `SR#817`.

The compiler, on its own, before any route reaches it. What is checked here is the part that
would otherwise be checked by reading: which end of a day an operator means, that a literal and
an expression are both accepted, and that every refusal names the half that was wrong.

**Predicates are compared as compiled SQL with the values inlined.** Asserting on the object
would check that a comparison was built and say nothing about *what it compares against*, which
is the entire question — a boundary off by a day compiles perfectly.
"""

import datetime

import pytest

import subroutine.domain.filtering
import subroutine.domain.grouping
import subroutine.domain.ordering
import subroutine.errors

#: A fixed instant, so nothing here depends on when it runs — `SR#762`'s fixture-that-expires
#: trap, met on a same-day timestamp that passed in the morning and failed in the evening.
NOW = datetime.datetime(2026, 8, 11, 14, 0, tzinfo=datetime.UTC)

#: **A zone that is not UTC, deliberately.** In London in August the offset is +1, so every
#: boundary below lands on `23:00` of the previous day — which is exactly what a test running in
#: UTC could never tell apart from a boundary computed wrongly.
ZONE = "Europe/London"


def _sql (name: str, value: str, *, entity: str = "task") -> list[str]:
	"""Compile one filter and return its SQL, values inlined."""

	built = subroutine.domain.filtering.asked(
		[(name, value)], entity=entity, now=NOW, timezone=ZONE
	)

	return [
		str(predicate.compile(compile_kwargs={"literal_binds": True}))
		for predicate in built
	]


@pytest.mark.parametrize(
	("name", "value", "expected"),
	[
		# Inclusive lower bound: the whole of the day it names is in.
		("created_at.gte", "2026-08-04", "task.created_at >= '2026-08-03 23:00:00+00:00'"),
		# Exclusive lower bound: the day it names is out, so it starts where that day ends.
		("created_at.gt", "2026-08-04", "task.created_at > '2026-08-04 22:59:59.999999+00:00'"),
		# Exclusive upper bound: the day it names is out, so it stops where that day starts.
		("created_at.lt", "2026-08-04", "task.created_at < '2026-08-03 23:00:00+00:00'"),
		# Inclusive upper bound: the whole of the day it names is in.
		("created_at.lte", "2026-08-04", "task.created_at <= '2026-08-04 22:59:59.999999+00:00'"),
	],
)
def test_an_inclusive_operator_takes_the_whole_day_and_an_exclusive_one_leaves_it_out (
	name: str, value: str, expected: str
) -> None:
	"""**The rule that would otherwise have shipped a confidently short list.**

	A caller writing a day against a column holding an instant means a range, and which end
	depends on the comparison. Resolving every day to its midnight — the obvious
	implementation — makes `created_at.lte=2026-08-04` exclude all but the first microsecond
	of the 4th, and answer with a list that looks complete.
	"""

	assert _sql(name, value) == [expected]


def test_a_relative_expression_and_a_literal_reach_the_same_place () -> None:
	"""`yesterday` is 10 August here, and its boundary is computed the same way a literal's is.

	The first version read values through `dates.resolve`, which answers the narrower question
	of what a *keyword* means and **refused `2026-08-04` outright** — on the example this item
	was filed for. Found by driving it.
	"""

	assert _sql("created_at.gte", "yesterday") == _sql("created_at.gte", "2026-08-10")
	assert _sql("created_at.gte", "yesterday") == [
		"task.created_at >= '2026-08-09 23:00:00+00:00'"
	]


def test_a_time_is_compared_as_a_time_rather_than_as_a_day () -> None:
	"""Saying which instant you meant is respected, and no boundary is applied to it."""

	assert _sql("created_at.gte", "2026-08-04T14:00") == [
		"task.created_at >= '2026-08-04 13:00:00+00:00'"
	]


def test_a_start_is_compared_as_an_instant_now_that_it_is_one () -> None:
	"""`starts_at` was a bare `DATE` and took `eq`; `#854` made it a timestamp.

	**This test used to assert the opposite**, and it is kept pointing the other way rather
	than deleted, because the capability really did change: *what starts today* is a half-open
	range now instead of one equality. The boundary is still resolved in the caller's zone,
	which is the step `#773` is about — a day read in the wrong zone is right in winter and
	wrong in summer.
	"""

	assert _sql("starts_at.gte", "today") == [
		"task.starts_at >= '2026-08-10 23:00:00+00:00'"
	]


def test_equality_on_a_timestamp_is_refused_rather_than_answered_emptily () -> None:
	"""**Simon's decision, 2026-08-11, and the only option of three with no invisible failure.**

	A timestamp is stored to the microsecond, so `eq` against one almost never matches. Both
	ways of being helpful are worse: comparing exactly returns nothing and reads as an empty
	backlog, and widening to the whole day makes `eq` mean two different things depending on
	how the value was written — `schedule.interpret` infers "a whole day" from the input's
	shape, so the literal `2026-08-04` is one and the keyword `yesterday` is not.
	"""

	for operator in ("eq", "ne"):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_sql(f"created_at.{operator}", "yesterday")

		assert "cannot be filtered" in str(refused.value)
		assert "created_at.gte" in (refused.value.errors[0].hint or ""), (
			"the refusal did not say what to write instead"
		)

	# **And it is a property of the kind, not a ban on the operator** — which is what says the
	# rule is about microseconds rather than about equality. `estimate_minutes` is the
	# counterexample since `#854`: it was `starts_at`, until that stopped being a day column
	# and there was no date field left that `eq` means anything for.
	assert _sql("estimate_minutes.eq", "2h")


def test_the_two_halves_of_a_name_are_refused_separately () -> None:
	"""A misspelled field and a misspelled operator are different mistakes.

	One message covering both would tell whoever made either of them to check the wrong half.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as unknown_field:
		_sql("creatd_at.gte", "yesterday")

	assert "is not a field" in str(unknown_field.value)
	assert "created_at" in (unknown_field.value.errors[0].hint or "")

	with pytest.raises(subroutine.errors.ValidationError) as unknown_operator:
		_sql("created_at.after", "yesterday")

	assert "is not an operator" in str(unknown_operator.value)


def test_a_value_that_says_nothing_is_refused_by_name () -> None:
	"""And the refusal points at where the grammar is published, rather than describing it."""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_sql("created_at.gte", "sometime next Thursday-ish")

	assert "created_at" in str(refused.value) or "created_at" in str(
		refused.value.errors[0].field
	)


def test_several_filters_are_all_applied () -> None:
	"""Simon's fourth question is two bounds, so one of them being dropped is the failure."""

	built = subroutine.domain.filtering.asked(
		[("created_at.gte", "2026-08-02"), ("created_at.lt", "today")],
		entity="task",
		now=NOW,
		timezone=ZONE,
	)

	assert len(built) == 2


def test_a_flat_parameter_is_left_for_the_other_guard () -> None:
	"""`api/query.refuse_unknown` owns names with no separator, and this owns the rest.

	Neither holds a list of the other's, which is what keeps them from disagreeing. A name
	without a separator reaching here would be silently ignored by *both*, so this is the
	seam worth pinning.
	"""

	assert subroutine.domain.filtering.asked(
		[("limit", "50"), ("order", "-created_at")], entity="task", now=NOW, timezone=ZONE
	) == []


def test_every_entity_with_a_listing_has_a_registry () -> None:
	"""What makes an entry go away, asked of the registry itself.

	Documents are here for §6.2's reason: one ref counter names either kind, so *what was
	created yesterday* answered for tasks alone would be wrong about half of what a number
	can mean.
	"""

	for entity in ("task", "document", "project"):
		assert subroutine.domain.filtering.FILTERS[entity], f"{entity} filters on nothing"
		assert "created_at" in subroutine.domain.filtering.FILTERS[entity]


def test_a_property_that_can_be_asked_and_not_ordered_says_why () -> None:
	"""**What makes an asymmetry a decision rather than an accident** — `SR#1803`, design
	`SR#1801` §1.

	Filterable, orderable and groupable were declared three times, in three modules, and **no
	field was in all three**. Nine disagreed: five were sortable and unaskable — including
	``importance`` and ``urgency``, so a reader could sort the whole backlog by urgency and not
	ask for the urgent ones — and four were askable and unsortable. Not one carried a reason
	anywhere, because there was nowhere for a reason about *two* lists to live.

	One declaration removes the *silent* disagreement; this is what removes the silent
	agreement-by-omission. A property that can be asked about and not ordered by, or the
	reverse, has to say which it is: an argument (``priority_score`` is a banded expression with
	no value to compare) or a gap with an item against it (``importance`` is `SR#1804`'s). Both
	are worth writing; neither is worth inferring.

	**Filterable against orderable, and not against groupable.** Grouping asks one query per
	group, so an axis must be *bounded* — almost nothing is one, and demanding a reason for
	every property that is not an axis would be a sentence on every entry, which is
	§12.2a's column that says the same thing on every row.
	"""

	silent = {}
	asymmetric = 0

	for entity, properties in subroutine.domain.filtering.PROPERTIES.items():
		for name, held in properties.items():
			if (held.kind is not None) == held.orderable:
				continue

			asymmetric += 1

			if not held.because:
				silent[f"{entity}.{name}"] = (
					"filterable, not orderable" if held.kind else "orderable, not filterable"
				)

	# **The floor**, and it is not a formality: a registry where every property happened to be
	# symmetric would make every line above vacuous, and this test would go on passing while
	# guarding nothing.
	assert asymmetric >= 9, (
		f"only {asymmetric} properties disagree about what can be done with them, where nine "
		f"did when this was written — either the registry shrank or this is measuring nothing"
	)

	assert not silent, (
		"a property can be asked about and not ordered by, or the reverse, and says nothing "
		"about why: "
		+ ", ".join(f"{name} ({how})" for name, how in sorted(silent.items()))
		+ " — give it a `because`, naming the item if it is a gap rather than a decision"
	)


def test_the_three_capabilities_come_from_one_declaration () -> None:
	"""The lists cannot disagree, because they are derived — `SR#1803`.

	**Asserted against the modules that publish them**, not against the registry: deriving
	``ordering.TASK_FIELDS`` from :func:`subroutine.domain.filtering.orderable` and then
	comparing the two would be comparing a value with itself. What this holds is that the
	*consumers* really do read the registry, so a fourth list declared beside one of them fails
	here rather than being discovered nine fields later.

	``priority_score`` is the one name that is declared here and built there, which is the
	layering `SR#1803` set: the registry says a property is orderable, and the module that can
	build a banded ``CASE`` builds it.
	"""

	built_elsewhere = {"priority_score"}

	for entity, published in (
		("task", subroutine.domain.ordering.TASK_FIELDS),
		("document", subroutine.domain.ordering.DOCUMENT_FIELDS),
		("project", subroutine.domain.ordering.PROJECT_FIELDS),
	):
		declared = set(subroutine.domain.filtering.orderable(entity))

		assert declared, f"{entity} orders by nothing, so this is comparing two empty sets"
		assert set(published) - built_elsewhere == declared, (
			f"{entity}'s sort fields and the registry disagree — "
			f"{sorted(set(published) - built_elsewhere ^ declared)} is in one and not the other"
		)

	for kind, axes in subroutine.domain.grouping.AXES.items():
		assert axes == subroutine.domain.filtering.axes(kind), (
			f"{kind}'s axes and the registry disagree, so grouping is a fourth list again"
		)


def test_the_published_names_are_the_product_of_the_two_tables () -> None:
	"""`names` is what a caller lists; it must not be able to drift from what `asked` accepts."""

	published = subroutine.domain.filtering.names("task")

	for name, field in subroutine.domain.filtering.TASK_FILTERS.items():
		for operator in field.operators:
			assert f"{name}.{operator}" in published, (
				f"{name}.{operator} is accepted and not published"
			)

	# **And the other direction, which is the one that was wrong.** The first version built
	# this list from every operator rather than from each field's own, so `created_at.eq` was
	# published and refused — a contract advertising something the code declines.
	for combination in published:
		name, _, operator = combination.partition(".")

		assert operator in subroutine.domain.filtering.TASK_FILTERS[name].operators, (
			f"{combination} is published and refused"
		)

	# **The field's own set and not its kind's, since `SR#1804`** — and this test is what said
	# so. A kind describes a *sort of value*, so its operators are true wherever that sort is;
	# whether a field can be *unset* is a fact about the **column**, and `created_at.is=set` is
	# every row while `created_at.is=unset` is none. Read from the kind, the two directions
	# above disagree in exactly that gap.
	narrowed = {
		name
		for name, field in subroutine.domain.filtering.TASK_FILTERS.items()
		if field.operators != field.kind.operators
	}

	assert narrowed, (
		"no task field narrows its kind's operators, so the two loops above are asking one "
		"question twice and the column half is measured by nothing"
	)

	for name in narrowed:
		assert f"{name}.{subroutine.domain.filtering.IS}" not in published, (
			f"{name} cannot be unset and {name}.is is published anyway — a filter that can "
			f"only ever answer all or nothing"
		)
