"""``scripts/readme_counts.py``, which measures the README's *It runs on itself* - item ``#3607``.

**The README publishes whatever this prints**, so what is worth testing is each definition: which
commits count, what a citation is, how the figures are worded, and that a listing is counted
whole or not at all. The instance half runs against a stand-in, because a test must never reach
a real one.
"""

import collections
import datetime
import importlib.util
import json
import os
import pathlib
import subprocess
import types
import typing

import pytest

import subroutine.clients.base


@pytest.fixture(scope="module")
def counts () -> types.ModuleType:
	"""Load ``scripts/readme_counts.py`` by path, the way the other scripts' tests do."""

	spec = importlib.util.spec_from_file_location(
		"readme_counts",
		pathlib.Path(__file__).resolve().parent.parent / "scripts" / "readme_counts.py",
	)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


@pytest.mark.parametrize(
	("message", "cites"),
	[
		("Refresh the figures\n\n- SR#3607, Simon's request.", True),
		# How the commits of 2026-07-30 wrote a ref, before the prefix.
		("- #42 closed", True),
		("Release 0.9.7\n\nSee CHANGELOG.md for what 0.9.7 contains.", False),
		("issue#1 is somebody else's", False),
		("The colour #42FF00", False),
		("A tag, #3d-printing", False),
		("##1 and #007", False),
	],
)
def test_a_citation_is_either_spelling_of_a_ref (
	counts: types.ModuleType, message: str, cites: bool
) -> None:
	"""``SR#42`` or a bare ``#42``, and nothing else that happens to hold a hash and digits."""

	assert bool(counts.CITES.search(message)) is cites


def test_only_the_commits_from_the_day_items_began_are_counted (
	counts: types.ModuleType, tmp_path: pathlib.Path
) -> None:
	"""The README's *since its third day*: the two days before item ``#1`` had nothing to cite.

	**Dated by the author's day as recorded**, so the commit of the 29th stays out, citing or not,
	and the uncited one of the 30th stays in.
	"""

	_git(tmp_path, "init", "-q")
	_git(tmp_path, "config", "user.email", "keanu@example.com")
	_git(tmp_path, "config", "user.name", "Keanu")

	for day, message in (
		("2026-07-29", "Before any item existed, naming #1 anyway"),
		("2026-07-30", "- #1 the first day, bare"),
		("2026-07-30", "The first day, citing nothing"),
		("2026-09-25", "Later\n\n- SR#3607"),
	):
		_git(tmp_path, "commit", "-q", "--allow-empty", "-m", message, day=day)

	assert counts.commits(tmp_path) == counts.Commits(citing=2, total=3)


def test_the_figures_are_worded_as_the_readme_words_them (counts: types.ModuleType) -> None:
	"""Most documents first, the last joined with *and*, and a single one not made plural."""

	held = counts.Holdings(
		who="keanu (a person)",
		open_items=1_419,
		projects=17,
		documents=collections.Counter({"Finding": 238, "Decision": 129, "Dead end": 26, "Note": 1}),
	)

	assert counts.written(counts.Commits(citing=1_191, total=1_225), held) == [
		"1,191 of the 1,225 commits since then",
		"1,419 open items across 17 projects",
		"394 written-up documents - 238 findings, 129 decisions, 26 dead ends and 1 note",
	]


@pytest.mark.parametrize(
	("day", "written"),
	[
		(1, "1st"),
		(2, "2nd"),
		(3, "3rd"),
		(4, "4th"),
		(11, "11th"),
		(12, "12th"),
		(13, "13th"),
		(21, "21st"),
		(22, "22nd"),
		(23, "23rd"),
		(31, "31st"),
	],
)
def test_a_day_is_written_as_the_readme_writes_it (
	counts: types.ModuleType, day: int, written: str
) -> None:
	"""*At the time of writing (21st September 2026)*, and the teens are all *th*."""

	assert counts.dated(datetime.date(2026, 10, day)) == f"{written} October 2026"


def test_a_workspace_is_counted_across_every_page (counts: types.ModuleType) -> None:
	"""Open items across two pages, the projects they are in, and documents by their label."""

	instance = _Instance(tasks_total=3)
	held = counts.holdings(typing.cast(subroutine.clients.base.Client, instance), "projects")

	assert held == counts.Holdings(
		who="keanu (a person)",
		open_items=3,
		projects=2,
		documents=collections.Counter({"Finding": 2, "Dead end": 1}),
	)
	assert {"cursor": "next"}.items() <= instance.asked[-2].items(), instance.asked


def test_a_listing_whose_pages_fall_short_of_its_total_is_refused (
	counts: types.ModuleType,
) -> None:
	"""**A smaller number reads exactly like a real one**, so a page not followed is a refusal."""

	instance = _Instance(tasks_total=4)

	with pytest.raises(SystemExit, match="said it holds 4 and gave 3"):
		counts.holdings(typing.cast(subroutine.clients.base.Client, instance), "projects")


class _Instance:
	"""Stands in for a client: tasks over two pages, documents on one, and a person asking."""

	def __init__ (self, *, tasks_total: int) -> None:
		"""Say how many tasks the listing will claim to hold, whatever its pages carry."""

		self.tasks_total = tasks_total
		self.asked: list[dict[str, str]] = []

	def me (self) -> types.SimpleNamespace:
		"""Answer as a person."""

		return types.SimpleNamespace(
			user=types.SimpleNamespace(username="keanu", is_service_account=False)
		)

	def call_api (
		self,
		*,
		method: str,
		path: str,
		body: typing.Any | None = None,
		query: dict[str, str] | None = None,
	) -> subroutine.clients.base.Answered:
		"""Serve one page of whichever listing was asked for."""

		query = query or {}
		self.asked.append(query)

		if path == "/v1/documents":
			labels = ("Finding", "Finding", "Dead end")

			return _page([{"ref": 10 + n, "type_label": one} for n, one in enumerate(labels)], 3)

		if "cursor" not in query:
			rows = [{"ref": 1, "project_key": "subsample"}, {"ref": 2, "project_key": "subnet"}]

			return _page(rows, self.tasks_total, following="next")

		return _page([{"ref": 3, "project_key": "subsample"}], self.tasks_total)


def _page (
	rows: list[dict[str, typing.Any]], total: int, *, following: str | None = None
) -> subroutine.clients.base.Answered:
	"""Return a listing page as an instance would send one."""

	page = {"limit": 200, "next_cursor": following, "has_more": following is not None, "total": total}

	return subroutine.clients.base.Answered(200, json.dumps({"items": rows, "page": page}))


def _git (root: pathlib.Path, *arguments: str, day: str | None = None) -> None:
	"""Run git in a repository of the test's own, dated ``day`` where one is given."""

	dates = {} if day is None else {
		"GIT_AUTHOR_DATE": f"{day}T12:00:00+00:00",
		"GIT_COMMITTER_DATE": f"{day}T12:00:00+00:00",
	}

	subprocess.run(["git", *arguments], cwd=root, env={**os.environ, **dates}, check=True)
