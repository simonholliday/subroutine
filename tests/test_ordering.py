"""The ordering vocabulary, and the two places it has to mean the same thing.

An ordering is applied twice by necessity: as SQL that arranges a query, and as Python that
compares rows already fetched. The query half serves a single page; the Python half serves a
*merged* listing, which is what ``subroutine list`` prints after asking one page per workspace
per kind. A disagreement between them is not a cosmetic one — it is the right rows in the
wrong order, or the wrong rows entirely, and neither announces itself.

**Applied twice, stated once, since `#569`.** The Python half used to re-derive the rule from
the fields on a view; it reads the value the query computed now, because an ordering may
consult rows other than the one it is placing and no amount of care lets a rendered view
reproduce that.

That is not a hypothetical. ``#71`` shipped a ``--order`` flag whose result was re-sorted by
``created_at`` one level further up, so the flag chose which items appeared and then discarded
the arrangement. The output looked entirely reasonable.
"""

import datetime
import types
import typing

import pytest

import subroutine.db.models.work
import subroutine.domain.ordering


class Row:
	"""The smallest thing the view readers accept: something with the attributes they name."""

	def __init__ (self, **fields: typing.Any) -> None:
		"""Store whatever the test gave, and nothing it did not."""

		for name, value in fields.items():
			setattr(self, name, value)


def _everything_a_listing_accepts () -> set[str]:
	"""Return every sort name a task listing takes, including the ones it adds per request.

	**`#878`. `TASK_FIELDS` is no longer the vocabulary**, and reading it as though it were is
	what let `relevance` ship with no view reader: a search adds it (`#823`) and every listing
	adds `deferred` (`#877`), so the static map is the endpoint's vocabulary minus the two
	fields added most recently — which is precisely the half a guard needs to see.

	Built by calling the same two functions the endpoint and the local client call, so a third
	per-request field is covered on the day it is written rather than when somebody notices.
	"""

	model = subroutine.db.models.work.Task

	return set(
		subroutine.domain.ordering.searching(
			subroutine.domain.ordering.sinking(
				subroutine.domain.ordering.TASK_FIELDS,
				model=model,
				now=datetime.datetime.now(datetime.UTC),
			),
			terms=["anything"],
			columns=[model.title, model.description],
			carried_on=model.relevance,
		)
	)


def test_every_sortable_task_field_can_be_read_off_a_view () -> None:
	"""A sort field with no reader is one a merged listing silently ignores.

	The guard that matters, rather than a restatement of the map: adding a name to the
	vocabulary gives ``GET /v1/tasks?order=`` a new key immediately, and ``subroutine list
	--order`` would accept it, ask for it, and then merge on nothing. Refusing a field is a
	fine answer; accepting it and not applying it is not.

	**Read through `_everything_a_listing_accepts` rather than off `TASK_FIELDS`** (`#878`),
	because two of the three most recent sort fields are added per request and this guard could
	not see either of them.
	"""

	missing = _everything_a_listing_accepts() - set(subroutine.domain.ordering.VIEW_READERS)

	assert not missing, (
		f"{sorted(missing)} can be sorted by and cannot be read off a rendered view, so a "
		f"merged listing would accept the name and ignore it."
	)


def test_a_document_can_be_read_by_every_field_it_may_be_sorted_by () -> None:
	"""The shorter vocabulary has to be a subset, or a shared ordering cannot merge."""

	missing = set(subroutine.domain.ordering.DOCUMENT_FIELDS) - set(
		subroutine.domain.ordering.VIEW_READERS
	)

	assert not missing


def test_a_merged_page_is_ordered_by_the_rank_and_not_by_the_reported_score () -> None:
	"""§6.3a's ordering, applied to a merged page as well as to a query.

	``views.Task.priority_score`` is ``importance * urgency`` and null unless both are set;
	``views.Task.rank`` is where the ordering the listing asked for put this row. Reading the
	first here would put a part-ranked item back below an unranked one — the exact defect the
	bands were added to fix, reintroduced one layer up and only in the merged case, where no
	test was looking.

	The rows below are shaped like what a server sorting by ``-priority_score`` sends back, so
	the two fields disagree in exactly the way that makes reading the wrong one visible.
	"""

	read = subroutine.domain.ordering.VIEW_READERS["priority_score"]

	ranked = read(Row(rank=216, importance=4, urgency=4, priority_score=16))
	part = read(Row(rank=105, importance=5, urgency=None, priority_score=None))
	unranked = read(Row(rank=None, importance=None, urgency=None, priority_score=None))

	assert ranked > part
	assert unranked is None

	# The part-ranked item's own reported field is null, which is precisely why sorting on it
	# would drop it in among the unranked.
	assert part is not None

	# And the ranked one's two fields differ, so reading the reported score would be visible
	# here as well as in the ordering — 16 is what a person assessed, 216 is where it sat.
	assert ranked == 216


def test_a_document_has_no_priority_and_says_so_rather_than_raising () -> None:
	"""A merged page holds both kinds, and the task-only readers meet documents constantly."""

	read = subroutine.domain.ordering.VIEW_READERS["priority_score"]

	assert read(Row(ref=4, title="A spec")) is None
	assert subroutine.domain.ordering.VIEW_READERS["due_at"](Row(ref=4)) is None


@pytest.mark.parametrize("descending", [True, False])
def test_nulls_sort_last_in_both_directions (descending: bool) -> None:
	"""SPEC.md §10.3's rule, which the merge has to keep as much as the query does.

	Nulls last *in both directions* is what makes "a document sorts last in a list ranked by
	priority" true whichever way the list runs — the same answer §6.3a gives an unranked
	task, which is why the merge needs no separate rule for documents.
	"""

	# `rank` as a server sorting by this would report it: the banded value, and null for a row
	# with neither axis — which is the case the rule is about.
	rows = [
		Row(ref=1, rank=201, importance=1, urgency=1),
		Row(ref=2, rank=None, importance=None, urgency=None),
		Row(ref=3, rank=225, importance=5, urgency=5),
	]

	found = subroutine.domain.ordering.merged(
		rows, key=lambda row: row, order=(("priority_score", descending),)
	)

	assert found[-1].ref == 2
	assert found[0].ref == (3 if descending else 1)


def test_each_field_carries_its_own_direction () -> None:
	""""Newest first, then title ascending" is the ordering people actually ask for.

	Applied one field at a time from the last to the first on a stable sort, which is what
	lets the directions differ — a single composite key cannot express this without
	inverting values by type.
	"""

	moment = datetime.datetime(2026, 7, 31, tzinfo=datetime.UTC)
	earlier = moment - datetime.timedelta(days=1)

	rows = [
		Row(ref=1, created_at=moment, title="b"),
		Row(ref=2, created_at=earlier, title="a"),
		Row(ref=3, created_at=moment, title="a"),
	]

	found = subroutine.domain.ordering.merged(
		rows, key=lambda row: row, order=(("created_at", True), ("title", False))
	)

	assert [row.ref for row in found] == [3, 1, 2]


def test_two_rows_with_no_value_at_all_compare_rather_than_raise () -> None:
	"""Nulls are indistinguishable, and comparing two of them must not reach their values.

	The null flag leads every sort key for this reason. Without the placeholder behind it,
	two unranked rows would compare ``None`` against ``None`` and raise ``TypeError`` — on
	the commonest page there is, a personal to-do list where nothing is ranked.
	"""

	rows = [Row(ref=1, importance=None, urgency=None), Row(ref=2, importance=None, urgency=None)]

	found = subroutine.domain.ordering.merged(
		rows, key=lambda row: row, order=(("priority_score", True), ("ref", True))
	)

	assert [row.ref for row in found] == [2, 1]


def test_a_document_is_never_deferred_and_a_task_needs_a_clock () -> None:
	"""`SR#877`. The two halves of :func:`sinking`, and the second is a guard on the first.

	A kind with no start date takes the constant band, so a merged listing can be asked for
	one order and keep both collections (`SR#782`). A kind that *can* be deferred needs the
	instant the band is judged against — and omitting it is the mistake worth being loud
	about, because the quiet version is a task listing that sorts everything as startable and
	looks exactly like one where nothing is deferred.
	"""

	documents = subroutine.domain.ordering.sinking(
		subroutine.domain.ordering.DOCUMENT_FIELDS
	)
	entry = documents[subroutine.domain.ordering.DEFERRED]

	assert isinstance(entry, subroutine.domain.ordering.Derived)
	assert entry.carried_on is None, "a constant needs nothing attached to the row"
	assert entry.read(object()) == subroutine.domain.ordering.STARTABLE_BAND

	with pytest.raises(ValueError):
		subroutine.domain.ordering.sinking(
			subroutine.domain.ordering.TASK_FIELDS,
			model=subroutine.db.models.work.Task,
		)


@pytest.mark.parametrize(
	("start", "expected"),
	[
		(None, subroutine.domain.ordering.STARTABLE_BAND),
		(datetime.timedelta(days=-1), subroutine.domain.ordering.STARTABLE_BAND),
		(datetime.timedelta(days=1), subroutine.domain.ordering.DEFERRED_BAND),
	],
	ids=["no-start-date", "start-has-passed", "start-is-ahead"],
)
def test_the_view_side_band_agrees_with_the_predicate_the_query_uses (
	start: datetime.timedelta | None, expected: int
) -> None:
	"""`SR#877`. `put_off` is a copy of `readiness.undeferred`, so it has to be the same rule.

	**A start date that has passed is not a deferral.** That is the half a simpler reading
	gets wrong — `snoozed_until is not None` would sink a task months after the day it was waiting
	for, and the mark beside it would say nothing, so the position and the phrase would
	disagree about one row.
	"""

	class Row:
		"""The two fields a rendered row contributes to this decision."""

		snoozed_until = (
			None if start is None else datetime.datetime.now(datetime.UTC) + start
		)

	assert subroutine.domain.ordering.put_off(Row()) == expected


def test_an_unestimated_task_sorts_last_whichever_way_the_estimate_runs () -> None:
	"""`#319`'s open design question, answered by measuring rather than by banding.

	That item expected this to need §6.3a's treatment — explicit bands, because most of the
	backlog has no estimate. Two measurements changed it. **105 of 163 open tasks carry one**
	(64%, not "most have none"), and `NULLS LAST` is already applied in **both** directions by
	`ordering.terms`, which is exactly the arrangement bands would have been built to produce.

	The reason no band is needed is that the two fields are different shapes. `priority_score`
	bands because a part-ranked item has a real value on a *different scale* — one axis runs 1
	to 5 where the product runs 1 to 25 — so one column would sort "critically important,
	urgency unjudged" below "judged trivial". An estimate has one scale and one absence.

	**Both directions, because one of them passing proves nothing.** Ascending means shortest
	first and an unestimated task is not known to be short; descending means longest first and
	it is not known to be long. Last is honest either way, and a test of one direction would be
	satisfied by an ordering that put nulls first in the other.
	"""

	for expression in ("estimate_minutes", "-estimate_minutes"):
		terms = subroutine.domain.ordering.clauses(
			expression,
			allowed=subroutine.domain.ordering.TASK_FIELDS,
			default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
			tiebreak=subroutine.db.models.work.Task.id,
		)

		rendered = str(terms[0].compile(compile_kwargs={"literal_binds": True}))

		assert "NULLS LAST" in rendered.upper(), f"{expression} put the unestimated first"


def test_the_estimate_can_be_sorted_on_by_a_merged_listing () -> None:
	"""A sort field the CLI silently ignores is worse than one it refuses.

	This file already fails if `TASK_FIELDS` grows a name `VIEW_READERS` has not got — this
	names the one `#319` added, so the failure has a case beside it rather than only a rule.
	"""

	assert "estimate_minutes" in subroutine.domain.ordering.VIEW_READERS

	reader = subroutine.domain.ordering.VIEW_READERS["estimate_minutes"]

	assert reader(types.SimpleNamespace(estimate_minutes=90)) == 90
	# A document has no estimate, and a merged page holds both kinds.
	assert reader(types.SimpleNamespace()) is None
