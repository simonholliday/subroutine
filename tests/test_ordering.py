"""The ordering vocabulary, and the two places it has to mean the same thing.

An ordering here exists twice by necessity: as SQL that arranges a query, and as Python that
compares rows already fetched. The query half serves a single page; the Python half serves a
*merged* listing, which is what ``subroutine list`` prints after asking one page per workspace
per kind. A disagreement between them is not a cosmetic one — it is the right rows in the
wrong order, or the wrong rows entirely, and neither announces itself.

That is not a hypothetical. ``#71`` shipped a ``--order`` flag whose result was re-sorted by
``created_at`` one level further up, so the flag chose which items appeared and then discarded
the arrangement. The output looked entirely reasonable.
"""

import datetime
import typing

import pytest

import subroutine.domain.ordering


class Row:
	"""The smallest thing the view readers accept: something with the attributes they name."""

	def __init__ (self, **fields: typing.Any) -> None:
		"""Store whatever the test gave, and nothing it did not."""

		for name, value in fields.items():
			setattr(self, name, value)


def test_every_sortable_task_field_can_be_read_off_a_view () -> None:
	"""A sort field with no reader is one a merged listing silently ignores.

	The guard that matters, rather than a restatement of the map: adding a name to
	``TASK_FIELDS`` gives ``GET /v1/tasks?order=`` a new key immediately, and
	``subroutine list --order`` would accept it, ask for it, and then merge on nothing.
	Refusing a field is a fine answer; accepting it and not applying it is not.
	"""

	missing = set(subroutine.domain.ordering.TASK_FIELDS) - set(
		subroutine.domain.ordering.VIEW_READERS
	)

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


def test_priority_score_orders_by_the_bands_not_by_the_reported_field () -> None:
	"""§6.3a's three bands, applied to a merged page as well as to a query.

	``views.Task.priority_score`` is ``importance * urgency`` and null unless both are set;
	an *ordering* by that name is the banded rule. Reading the view's field here would put a
	part-ranked item back below an unranked one — the exact defect the bands were added to
	fix, reintroduced one layer up and only in the merged case, where no test was looking.
	"""

	read = subroutine.domain.ordering.VIEW_READERS["priority_score"]

	ranked = read(Row(importance=4, urgency=4, priority_score=16))
	part = read(Row(importance=5, urgency=None, priority_score=None))
	unranked = read(Row(importance=None, urgency=None, priority_score=None))

	assert ranked > part
	assert unranked is None

	# The part-ranked item's own reported field is null, which is precisely why sorting on it
	# would drop it in among the unranked.
	assert part is not None


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

	rows = [
		Row(ref=1, importance=1, urgency=1),
		Row(ref=2, importance=None, urgency=None),
		Row(ref=3, importance=5, urgency=5),
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
