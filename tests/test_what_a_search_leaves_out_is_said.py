"""What a list or a search leaves out is counted as it was asked, and said - `SR#5145`.

**Two bullets of L-7 of the cold review of 2026-10-17**: ``list --to-act-on`` counted deferred work
assigned to other people, which ``--deferred`` beside it then did not show, and its hint dropped
the narrowing; and ``search`` left finished matches out in silence and refused an unquoted phrase.
Leaving finished work out of a search is decided (`#3549`); the silence was the defect.
"""

import typing

import typer.testing

import subroutine.domain.grammar
import subroutine.domain.tasks
import test_multi_user

#: The terminal's harness, bound here so pytest finds it.
home = test_multi_user.home
run = test_multi_user.run

Run = typing.Callable[..., typer.testing.Result]


def _flat (output: str) -> str:
	"""Return a command's output as one line, so a wrapped sentence reads whole."""

	return " ".join(output.split())


def _watering (run: Run) -> None:
	"""Make an instance with one open and one finished match for ``water``."""

	run("init", "--username", "keanu", "--workspace", "Metacortex")
	run("add", "Water the plants")
	run("add", "Water the garden")
	run("done", "2")


def test_a_search_says_what_finished_work_it_left_out (run: Run) -> None:
	"""The open match is listed, the finished one counted, and the command it names finds it."""

	_watering(run)

	said = _flat(run("search", "water").output)

	assert "Water the plants" in said and "Water the garden" not in said, said
	assert (
		"1 finished thing also matches. Run this to see it: "
		"subroutine search water status_category:done,cancelled"
	) in said, said

	again = _flat(run("search", "water", "status_category:done,cancelled").output)

	assert "Water the garden" in again, again
	assert "finished thing" not in again, "a search for finished work counted finished work"


def test_a_search_with_only_finished_matches_says_so_rather_than_nothing (run: Run) -> None:
	"""Nothing open matched, so it says what did, and does not offer to add work."""

	_watering(run)

	said = _flat(run("search", "garden").output)

	assert "1 finished thing also matches" in said, said
	assert "subroutine add" not in said, said


def test_a_search_naming_a_status_counts_nothing_finished (run: Run) -> None:
	"""A line naming a status chose what it reaches, so nothing is counted as left out."""

	_watering(run)

	said = _flat(run("search", "water status:open").output)

	assert "Water the plants" in said, said
	assert "finished thing" not in said, said


def test_an_unquoted_phrase_is_one_search (run: Run) -> None:
	"""``search water plants`` finds what ``search "water plants"`` finds, rather than refusing."""

	_watering(run)

	unquoted = _flat(run("search", "water", "plants").output)
	quoted = _flat(run("search", "water plants").output)

	assert "Water the plants" in unquoted, unquoted
	assert unquoted == quoted


def test_the_deferred_count_is_of_what_the_list_would_show (run: Run) -> None:
	"""Under ``--to-act-on`` deferred work assigned to somebody else is not counted, and the hint
	names the command as asked."""

	run("init", "--username", "keanu", "--workspace", "Metacortex")
	run("user", "create", "carrie", "--name", "Carrie-Anne Moss", "--workspace", "metacortex")
	run("add", "Water the plants defer 2099-01-01")
	run("add", "Renew the certificate @carrie defer 2099-01-01")
	run("add", "Call the landlord")

	said = _flat(run("list", "--to-act-on").output)

	assert (
		"1 thing deferred until later. Run this to include it: "
		"subroutine list --to-act-on --deferred"
	) in said, said

	widened = _flat(run("list", "--to-act-on", "--deferred").output)

	assert "Water the plants" in widened and "Renew the certificate" not in widened, widened


def test_explain_searching_says_finished_work_is_left_out (run: Run) -> None:
	"""The topic says what the line says, in the same term."""

	said = _flat(run("explain", "searching").output)

	assert "A search leaves finished work out" in said, said
	assert f"'{subroutine.domain.grammar.FINISHED}' finds them" in said, said


def test_the_finished_term_names_the_finished_categories () -> None:
	""":data:`grammar.FINISHED` is the categories a task finishes in, as the grammar reads it."""

	field, _colon, values = subroutine.domain.grammar.FINISHED.partition(":")

	assert field == "status_category"
	assert frozenset(values.split(",")) == subroutine.domain.tasks.FINISHED_CATEGORIES
	assert subroutine.domain.grammar.names_a_status(subroutine.domain.grammar.FINISHED)


def test_a_status_named_in_a_line_is_recognised () -> None:
	"""Either field, any operator, beside words; words alone name no status."""

	assert subroutine.domain.grammar.names_a_status("water status:open")
	assert subroutine.domain.grammar.names_a_status("status_category!=done deploy")
	assert not subroutine.domain.grammar.names_a_status("water the plants")
	assert not subroutine.domain.grammar.names_a_status(None)
