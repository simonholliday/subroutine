"""A merged listing orders titles case-folded, nearer the order the browser shows - `SR#1059`.

**L-7's collation bullet of the cold review of 2026-10-17, decided in `#5097`**: the terminal's
``list`` merges a page per workspace and per kind, and re-sorted them by code point, so every capital
came before every lower-case letter - *Water the plants* before *renew the certificate* - where
PostgreSQL's collation, which the browser shows, reads them as a person would. The merge is the
client's own sort over pages already fetched, so the backend that answered changes nothing here.
"""

import types
import typing

import typer.testing

import subroutine.domain.ordering
import test_multi_user

#: The terminal's harness, bound here so pytest finds it.
home = test_multi_user.home
run = test_multi_user.run

Run = typing.Callable[..., typer.testing.Result]


def _merged (titles: typing.Sequence[str], *, descending: bool) -> list[str]:
	"""Return these titles as a merge orders them by title."""

	rows = [types.SimpleNamespace(title=title) for title in titles]

	return [
		row.title
		for row in subroutine.domain.ordering.merged(
			rows, key=lambda row: row, order=(("title", descending),)
		)
	]


def test_a_merge_orders_titles_case_folded_and_the_title_breaks_a_tie () -> None:
	"""Both directions, and two titles that fold alike in the same order every time."""

	titles = ["Water the plants", "renew the certificate", "Call the landlord", "call the landlord"]
	ascending = _merged(titles, descending=False)

	assert ascending == [
		"Call the landlord",
		"call the landlord",
		"renew the certificate",
		"Water the plants",
	], ascending
	assert _merged(titles, descending=True) == list(reversed(ascending))


def test_the_terminal_lists_a_task_page_and_a_document_page_in_one_title_order (run: Run) -> None:
	"""Tasks and a document, merged by ``list --order title``, read as a person reads them."""

	run("init", "--username", "keanu", "--workspace", "Metacortex")

	for title in ("Water the plants", "renew the certificate", "Call the landlord"):
		run("add", title)

	run("doc", "create", "Pick up the dry cleaning", "--body", "Before Friday.")

	listed = run("list", "--order", "title").output
	titles = ["Call the landlord", "Pick up the dry cleaning", "renew the certificate", "Water the plants"]

	assert all(title in listed for title in titles), listed
	assert sorted(titles, key=listed.index) == titles, listed
