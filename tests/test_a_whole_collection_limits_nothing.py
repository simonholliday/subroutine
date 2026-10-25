"""Three values the API published wrongly, each said rightly - `SR#5143`.

**Three bullets of L-7 of the cold review of 2026-10-17**: a link's far end always said
``workspace: null``; twelve collections returned whole reported how many rows came back as
``page.limit``, which `SR#1569` made null for a collection nothing limited; and the app's manifest
and service worker refused a query the rest of its files take.
"""

import pathlib

import test_api_tasks

#: The source tree the scan below reads.
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"

#: What a whole collection wrote where its limit goes: a row count wearing a limit's name.
COUNTED = "limit=len("

#: The test's own fixture, bound here so pytest finds it.
world = test_api_tasks.world
World = test_api_tasks.World


def _counting (root: pathlib.Path) -> list[str]:
	"""Return each module under ``root`` that reports a row count as a page's limit."""

	return sorted(
		str(path.relative_to(root))
		for path in root.rglob("*.py")
		if COUNTED in path.read_text(encoding="utf-8")
	)


def test_nothing_reports_a_row_count_as_a_limit () -> None:
	"""No module writes ``limit=len(``, so a thirteenth whole collection cannot do it again."""

	assert _counting(SOURCE) == []


def test_the_scan_finds_a_row_count (tmp_path: pathlib.Path) -> None:
	"""The scan above, fed an offender through its own entry point."""

	(tmp_path / "somewhere.py").write_text(
		"page = Page(limit=len(rows), has_more=False)\n", encoding="utf-8"
	)

	assert _counting(tmp_path) == ["somewhere.py"]


def test_every_whole_collection_says_nothing_limited_it (world: World) -> None:
	"""All twelve answer ``page.limit: null``: an item's five link listings, for a task and a
	document, a task's verifications, both kinds of members and the four instance listings."""

	task = world.call("POST", "/v1/tasks", json={"title": "Pick up the dry cleaning"}).json()
	document = world.call(
		"POST", "/v1/documents", json={"title": "The site plan", "body": "."}
	).json()

	listings = ("links", "tree", "governing", "proposed-links", "backlinks")
	paths = [
		*(f"/v1/tasks/{task['ref']}/{listing}" for listing in listings),
		*(f"/v1/documents/{document['ref']}/{listing}" for listing in listings),
		f"/v1/tasks/{task['ref']}/verifications",
		"/v1/projects/inbox/members",
		f"/v1/workspaces/{world.workspace.slug}/members",
		"/v1/instance/workspaces",
		"/v1/instance/unreachable-projects",
		"/v1/instance/unadministered-workspaces",
		"/v1/instance/unowned-workspaces",
	]

	for path in paths:
		answered = world.call("GET", path)

		assert answered.status_code == 200, (path, answered.text)
		assert answered.json()["page"]["limit"] is None, path


def test_a_links_far_end_names_its_workspace (world: World) -> None:
	"""The far end of a link says the workspace its own row says, rather than null."""

	one = world.call("POST", "/v1/tasks", json={"title": "Pick up the dry cleaning"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Call the landlord"}).json()

	made = world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "relates_to"}
	)

	assert made.status_code == 201, made.text

	listed = world.call("GET", f"/v1/tasks/{one['ref']}/links").json()["items"]
	ends = [row["other"] for row in listed]

	assert [end["workspace"] for end in ends] == [two["workspace"]], ends
	assert two["workspace"] == world.workspace.slug


def test_the_manifest_and_the_worker_take_a_query (world: World) -> None:
	"""``?v=2`` on either is answered, as on every other file of the app's."""

	for path in ("/app/manifest.webmanifest", "/app/sw.js", "/app/app.js"):
		answered = world.call("GET", f"{path}?v=2")

		assert answered.status_code == 200, (path, answered.text)
