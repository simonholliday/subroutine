"""Measure the figures the README's *It runs on itself* gives - item ``#3607``.

**Run it before every tag** (Simon, 2026-09-25): the section is dated, and a release is when
somebody reads it fresh. The figures of 21 September could not be reproduced by any rule, so
each one is defined here, once, and measured the same way every time:

- **commits since the third day** - 2026-07-30, when item ``#1`` was made - and how many of
  them cite an item;
- **open items**: the workspace's unfinished tasks, deferred ones included and deleted ones not,
  which is ``/v1/tasks``'s own default, and **the projects they are spread across**;
- **written-up documents**: every one not deleted, by type.

The instance is asked through the connection a bare ``subroutine`` would use, **as whoever runs
this**, and the output says whose view it counted: a private project somebody cannot see is not
in their count. It prints the figures worded as the README words them, and changes nothing.

    python scripts/readme_counts.py [--workspace projects] [--connection name]
"""

import argparse
import collections
import dataclasses
import datetime
import json
import pathlib
import re
import subprocess
import sys
import typing

import subroutine.clients.base
import subroutine.clients.opening
import subroutine.config
import subroutine.connections

#: The repository, resolved from this file, because the script is run from wherever somebody is
#: standing.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The README's *its third day*: item ``#1`` was made on 2026-07-30, and the two days of commits
#: before it had nothing to cite.
ITEMS_BEGAN = "2026-07-30"

#: A citation in a commit message: ``SR#42``, or a bare ``#42`` as the commits of 2026-07-30 wrote
#: one before the prefix (§6.15). **Wider than the ``commit-msg`` hook on purpose**: the hook says
#: how a new message must spell a ref, and this counts what the history cites. The lookarounds are
#: ``mentions.REF_PATTERN``'s, so ``issue#1``, ``#42FF00`` and ``#3d-printing`` are not refs here.
CITES = re.compile(r"(?<![\w#])(?:SR)?#[1-9][0-9]*(?!\w)")

#: As large a page as an instance serves, so a count takes as few requests as it can.
PAGE = "200"


@dataclasses.dataclass(frozen=True)
class Commits:
	"""The commits since items began, and how many of them cite one."""

	citing: int
	total: int


@dataclasses.dataclass(frozen=True)
class Holdings:
	"""What one workspace holds, as one credential sees it."""

	#: Whose view this is, as the output names it.
	who: str

	open_items: int

	#: How many projects the open items are spread across - the README's *across N projects*.
	projects: int

	#: Documents by their type's label, as the instance names the type.
	documents: collections.Counter[str]


def commits (root: pathlib.Path = ROOT) -> Commits:
	"""Count the commits on ``HEAD`` since :data:`ITEMS_BEGAN`, and those citing an item.

	**The day is the author's, as it was recorded**, so a commit made late on 29 July stays on
	the 29th wherever this runs. ``root`` is an argument so a test can hand it a repository of
	its own.
	"""

	written = subprocess.run(
		["git", "log", "--format=%ad%x09%B%x00", "--date=short", "HEAD"],
		cwd=root,
		capture_output=True,
		text=True,
		check=True,
	).stdout
	since = [
		message
		for day, _, message in (record.strip().partition("\t") for record in written.split("\x00"))
		if day >= ITEMS_BEGAN
	]

	return Commits(citing=sum(1 for message in since if CITES.search(message)), total=len(since))


def holdings (client: subroutine.clients.base.Client, workspace: str) -> Holdings:
	"""Count one workspace's open items, the projects they are in, and its documents by type."""

	caller = client.me().user
	unfinished = _every(client, "/v1/tasks", workspace=workspace, fields="ref,project_key")
	documents = _every(client, "/v1/documents", workspace=workspace, fields="ref,type_label")

	return Holdings(
		who=f"{caller.username} ({'an agent' if caller.is_service_account else 'a person'})",
		open_items=len(unfinished),
		projects=len({row["project_key"] for row in unfinished}),
		documents=collections.Counter(str(row["type_label"]) for row in documents),
	)


def written (counted: Commits, held: Holdings) -> list[str]:
	"""Return the figures worded as the README words them, to paste.

	Each kind of document is named by its label, made plural by adding an *s*, which is right
	for every type this instance has; a person reads the line before it is pasted.
	"""

	kinds = [
		f"{count:,} {label.lower()}{'' if count == 1 else 's'}"
		for label, count in sorted(held.documents.items(), key=lambda pair: (-pair[1], pair[0]))
	]
	listed = kinds[0] if len(kinds) == 1 else f"{', '.join(kinds[:-1])} and {kinds[-1]}"

	return [
		f"{counted.citing:,} of the {counted.total:,} commits since then",
		f"{held.open_items:,} open items across {held.projects:,} projects",
		f"{sum(held.documents.values()):,} written-up documents - {listed}",
	]


def dated (day: datetime.date) -> str:
	"""Write a day as the README's *At the time of writing* does: *25th September 2026*."""

	suffix = "th" if 10 <= day.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")

	return f"{day.day}{suffix} {day:%B %Y}"


def main (argv: list[str] | None = None) -> int:
	"""Measure every figure and print it, saying when, where and as whom."""

	parsed = _arguments(argv)
	counted = commits()
	settings = subroutine.config.load_settings()
	roster = subroutine.connections.roster(settings)
	name = parsed.connection or roster.default
	connection = next((one for one in roster if one.name == name), None)

	if connection is None:
		print(f"There is no connection called {name!r}: {', '.join(roster.names)}.", file=sys.stderr)

		return 1

	with subroutine.clients.opening.for_connection(connection, roster, settings) as client:
		held = holdings(client, parsed.workspace)

	print(
		f"Measured on {dated(datetime.date.today())}, as {held.who} on {connection.name}, "
		f"in the {parsed.workspace} workspace:"
	)

	for line in written(counted, held):
		print(f"  {line}")

	print("README.md gives the day in its 'At the time of writing (...)'.")

	return 0


def _every (
	client: subroutine.clients.base.Client, path: str, *, workspace: str, fields: str
) -> list[dict[str, typing.Any]]:
	"""Return every row of one listing, page by page.

	**Checked against the listing's own total**, so a page this failed to follow is a refusal
	rather than a smaller number that reads exactly like a real one.
	"""

	rows: list[dict[str, typing.Any]] = []
	query = {"workspace_id": workspace, "fields": fields, "limit": PAGE, "include_total": "true"}
	total: int | None = None

	while True:
		answered = client.call_api(method="GET", path=path, query=query)

		if answered.status != 200:
			raise SystemExit(f"{path} answered {answered.status}: {answered.text}")

		page = json.loads(answered.text)
		rows.extend(page["items"])
		total = page["page"]["total"] if total is None else total

		if not page["page"]["has_more"]:
			break

		query = {**query, "cursor": page["page"]["next_cursor"]}

	if total is not None and total != len(rows):
		raise SystemExit(f"{path} said it holds {total} and gave {len(rows)} across its pages.")

	return rows


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Read the command line."""

	parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
	parser.add_argument(
		"--workspace", default="projects", help="the workspace this tracker is kept in"
	)
	parser.add_argument(
		"--connection", default=None, help="a connection other than this machine's default"
	)

	return parser.parse_args(argv)


if __name__ == "__main__":
	sys.exit(main())
