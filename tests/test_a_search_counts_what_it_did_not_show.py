"""What a search left out is counted past what it showed, and a full page as a floor - `SR#5300`.

M-12 of the cold review of 2026-11-11, with NEW-E-1 and L-2's bullet of its verification, decided
in `#5285`. **A search counted the finished work it had just listed**, wherever it reached finished
work for a reason other than a status in its line; **a count that filled its page was printed as
exact**, the deferred count's since it was written; and **a retired document, which a search
leaves out as it leaves finished work out, was not counted at all**.
"""

import pathlib
import typing

import pytest
import typer.testing

import subroutine.domain.documents
import subroutine.domain.grammar
import subroutine.domain.tasks
import test_personal_path

#: The terminal's harness, bound here so pytest finds it.
home = test_personal_path.home
run = test_personal_path.run

Run = typing.Callable[..., typer.testing.Result]


def _flat (output: str) -> str:
	"""Return a command's output as one line, so a wrapped sentence reads whole."""

	return " ".join(output.split())


def _watering (run: Run) -> None:
	"""Make an instance with one open match for ``water`` and two finished ones."""

	run("init")
	run("add", "Water the plants")
	run("add", "Water the garden")
	run("add", "Water the lawn")
	run("done", "2")
	run("done", "3")


def test_a_search_for_a_finished_items_number_counts_nothing (run: Run) -> None:
	"""``search 2`` lists the finished #2, and said one finished thing also matched: itself."""

	_watering(run)

	said = _flat(run("search", "2").output)

	assert "Water the garden" in said, said
	assert "finished thing" not in said, said


@pytest.mark.parametrize(
	"asked",
	[
		pytest.param(("water", "--filter", "status_category.eq=done"), id="a category filter"),
		pytest.param(("water", "--filter", "status.eq=done"), id="a status filter"),
		pytest.param(("water completed_at>=2000-01-01",), id="completed_at in the line"),
		pytest.param(
			("water", "--filter", "completed_at.gte=2000-01-01"), id="a completion filter"
		),
		pytest.param(("water touched_at>=2000-01-01",), id="touched_at in the line"),
	],
)
def test_a_search_that_lists_finished_work_counts_none_of_it (
	run: Run, asked: tuple[str, ...]
) -> None:
	"""Each reaches finished work for a reason other than a status in the line, and lists both."""

	_watering(run)

	said = _flat(run("search", *asked).output)

	assert "Water the garden" in said and "Water the lawn" in said, said
	assert "finished thing" not in said, said


def test_a_finished_count_that_fills_its_page_is_a_floor (run: Run) -> None:
	"""Two finished matches counted at ``--limit 1`` were said to be one."""

	_watering(run)

	said = _flat(run("search", "water", "--limit", "1").output)

	assert "At least 1 finished thing also matches. Run this to see them:" in said, said
	assert subroutine.domain.grammar.FINISHED in said, said


def test_a_deferred_count_that_fills_its_page_is_a_floor (run: Run) -> None:
	"""NEW-E-1: two deferred tasks at ``--limit 1`` were said to be one, where `N+` was promised."""

	run("init")
	run("add", "Call the landlord")
	run("add", "Water the plants defer 2099-01-01")
	run("add", "Renew the certificate defer 2099-01-01")

	said = _flat(run("list", "--limit", "1").output)

	assert "At least 1 thing deferred until later. Run this to include them:" in said, said


def test_an_events_count_that_fills_its_page_is_a_floor (run: Run) -> None:
	"""Two events left out of a list at ``--limit 1`` were said to be one."""

	run("init")
	run("add", "Call the landlord")
	run("add", "Payday on 2026-10-01", "--type", "event")
	run("add", "Rent day on 2026-10-02", "--type", "event")

	said = _flat(run("list", "--limit", "1").output)

	assert "At least 1 event not listed. 'subroutine list --events' to include them." in said, said


def test_a_search_says_the_retired_document_it_left_out (run: Run, home: pathlib.Path) -> None:
	"""Only a superseded decision matched: *Nothing matches* and an offer of ``--deferred``."""

	run("init")
	test_personal_path._a_document(
		home, title="The watering rota", body="Mondays and Thursdays.", status_key="superseded"
	)

	said = _flat(run("search", "watering").output)

	assert "Nothing matches 'watering'." in said, said
	assert (
		"1 retired document also matches. Run this to see it: subroutine search watering "
		f"{subroutine.domain.grammar.RETIRED}"
	) in said, said
	assert "--deferred" not in said, said

	found = _flat(run("search", "watering", subroutine.domain.grammar.RETIRED).output)

	assert "The watering rota" in found and "retired document" not in found, found


def test_a_search_for_a_retired_documents_number_counts_nothing (
	run: Run, home: pathlib.Path
) -> None:
	"""A line that is a retired document's number lists it, and is not told it also matched."""

	run("init")
	ref = test_personal_path._a_document(
		home, title="The watering rota", body="Mondays and Thursdays.", status_key="superseded"
	)

	said = _flat(run("search", str(ref)).output)

	assert "The watering rota" in said, said
	assert "retired document" not in said, said


def test_a_retired_count_that_fills_its_page_is_a_floor (run: Run, home: pathlib.Path) -> None:
	"""Two retired matches counted at ``--limit 1`` are said as at least one."""

	run("init")
	run("add", "Water the plants")

	for title in ("The watering rota", "The old watering rota"):
		test_personal_path._a_document(
			home, title=title, body="Mondays and Thursdays.", status_key="archived"
		)

	said = _flat(run("search", "watering", "--limit", "1").output)

	assert "At least 1 retired document also matches. Run this to see them:" in said, said


def test_finished_work_and_a_retired_document_are_one_line_whose_term_finds_both (
	run: Run, home: pathlib.Path
) -> None:
	"""One line for both kinds, and the term it names brings both back in one search."""

	_watering(run)
	test_personal_path._a_document(
		home, title="Water rota", body="Mondays and Thursdays.", status_key="archived"
	)

	said = _flat(run("search", "water").output)

	assert "Water the plants" in said, said
	assert (
		"2 finished things and 1 retired document also match. Run this to see them: "
		f"subroutine search water {subroutine.domain.grammar.FINISHED_OR_RETIRED}"
	) in said, said

	found = _flat(run("search", "water", subroutine.domain.grammar.FINISHED_OR_RETIRED).output)

	assert all(title in found for title in ("Water the garden", "Water the lawn", "Water rota"))
	assert "also match" not in found, found


def test_the_retired_terms_name_the_retired_categories () -> None:
	""":data:`grammar.RETIRED` is a document's retired categories, and the joint term is both."""

	field, _colon, values = subroutine.domain.grammar.RETIRED.partition(":")

	assert field == "status_category"
	assert frozenset(values.split(",")) == subroutine.domain.documents.RETIRED_CATEGORIES

	field, _colon, values = subroutine.domain.grammar.FINISHED_OR_RETIRED.partition(":")

	assert field == "status_category"
	assert frozenset(values.split(",")) == (
		subroutine.domain.tasks.FINISHED_CATEGORIES | subroutine.domain.documents.RETIRED_CATEGORIES
	)
	assert subroutine.domain.grammar.names_a_status(subroutine.domain.grammar.FINISHED_OR_RETIRED)
