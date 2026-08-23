"""Optimistic concurrency (docs/design.md §8.9).

The scenario the whole system is built around: a person and an agent editing the same
items. Without this, the second writer silently wins and the first one's work is gone with
no error anywhere. With it, the second writer is told — and told enough to merge.

Optional by default, which these also check: a solo user adding a task must not have to
think about versions at all.
"""

import concurrent.futures
import threading
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.app
import subroutine.api.routing
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.versions
import subroutine.errors
import test_api_tasks
import test_claims


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_a_change_without_a_version_is_accepted (world: test_api_tasks.World) -> None:
	"""The check is opt-in. A solo user does not want the ceremony."""

	created = world.call("POST", "/v1/tasks", json={"title": "No ceremony"}).json()

	assert world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "Renamed"}
	).status_code == 200


def test_a_matching_version_is_accepted (world: test_api_tasks.World) -> None:
	"""The ordinary read-modify-write, done properly."""

	created = world.call("POST", "/v1/tasks", json={"title": "Careful"}).json()
	response = world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "Renamed", "expected_version": created["version"]},
	)

	assert response.status_code == 200
	assert response.json()["version"] > created["version"], "a change moves the version"


def test_a_stale_version_is_refused_with_both_numbers_and_the_current_entity (
	world: test_api_tasks.World,
) -> None:
	"""The 409 has to carry enough to merge from, or the caller can only refetch and guess.

	This is the case `subroutine edit` exists inside: read, spend ten minutes in a text
	editor, write — while an agent changed the same task.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Contested"}).json()
	stale = created["version"]

	# Somebody else gets there first.
	world.call("PATCH", f"/v1/tasks/{created['ref']}", json={"description": "Agent was here"})

	response = world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "My edit", "expected_version": stale},
	)

	assert response.status_code == 409

	body = response.json()

	assert body["code"] == "version_conflict"
	assert body["expected_version"] == stale
	assert body["current_version"] > stale

	# Named the way a ref is written, so the message can be read aloud or pasted.
	assert f"#{created['ref']}" in body["detail"]

	# §8.9: "plus the current entity, so the caller can merge rather than refetch". The
	# hint says the current one is in the response, so it had better be.
	assert body["current"]["ref"] == created["ref"]
	assert body["current"]["description"] == "Agent was here"
	assert body["current"]["version"] == body["current_version"]


def test_a_refused_change_changes_nothing (world: test_api_tasks.World) -> None:
	"""A conflict must leave the task exactly as the other writer left it."""

	created = world.call("POST", "/v1/tasks", json={"title": "Contested"}).json()
	stale = created["version"]

	world.call("PATCH", f"/v1/tasks/{created['ref']}", json={"title": "Theirs"})
	world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "Mine", "expected_version": stale},
	)

	assert world.call("GET", f"/v1/tasks/{created['ref']}").json()["title"] == "Theirs"


def test_the_header_form_works_too (world: test_api_tasks.World) -> None:
	"""``If-Match`` is what HTTP says; the body field is for clients that find it awkward."""

	created = world.call("POST", "/v1/tasks", json={"title": "Header form"}).json()

	assert world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "Renamed"},
		headers={"if-match": f'"{created["version"]}"'},
	).status_code == 200

	# And the same header, now stale.
	assert world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "Again"},
		headers={"if-match": f'"{created["version"]}"'},
	).status_code == 409


@pytest.mark.parametrize("supplied", ['"3"', "3", 'W/"3"'])
def test_the_header_is_read_in_the_forms_people_send_it (
	world: test_api_tasks.World, supplied: str
) -> None:
	"""Quoted, unquoted and weak all mean the same version here.

	An entity tag is quoted by the standard, but a caller that sent a bare number meant the
	same thing; refusing it would teach nobody anything. A weak validator claims semantic
	equivalence, which is exactly what a version *is* in this API (§8.9 calls it a
	concurrency token, not a cache validator).
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Forms"}).json()

	assert created["version"] != 3

	response = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "x"}, headers={"if-match": supplied}
	)

	assert response.status_code == 409, "the header was read, and 3 is not the version"


def test_a_star_match_means_any_version (world: test_api_tasks.World) -> None:
	"""``If-Match: *`` asks for something weaker than the check, not something impossible."""

	created = world.call("POST", "/v1/tasks", json={"title": "Anything"}).json()

	assert world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "x"}, headers={"if-match": "*"}
	).status_code == 200


def test_an_unreadable_header_is_refused_rather_than_ignored (
	world: test_api_tasks.World,
) -> None:
	"""Ignoring it would perform the write the caller was trying to make conditional."""

	created = world.call("POST", "/v1/tasks", json={"title": "Guarded"}).json()
	response = world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "x"},
		headers={"if-match": '"not-a-version"'},
	)

	assert response.status_code == 422
	assert response.json()["errors"][0]["field"] == "If-Match"


def test_the_two_forms_may_not_disagree (world: test_api_tasks.World) -> None:
	"""Picking one silently would be picking which of the caller's intentions to honour."""

	created = world.call("POST", "/v1/tasks", json={"title": "Both"}).json()
	response = world.call(
		"PATCH",
		f"/v1/tasks/{created['ref']}",
		json={"title": "x", "expected_version": created["version"]},
		headers={"if-match": '"999"'},
	)

	assert response.status_code == 422
	assert "If-Match" in response.json()["errors"][0]["message"]


def test_completing_and_deleting_honour_it_too (world: test_api_tasks.World) -> None:
	"""Any change that could overwrite somebody is a change worth guarding."""

	created = world.call("POST", "/v1/tasks", json={"title": "Guarded"}).json()
	stale = created["version"]

	world.call("PATCH", f"/v1/tasks/{created['ref']}", json={"description": "moved on"})

	assert world.call(
		"POST", f"/v1/tasks/{created['ref']}/complete", headers={"if-match": f'"{stale}"'}
	).status_code == 409
	assert world.call(
		"DELETE", f"/v1/tasks/{created['ref']}", headers={"if-match": f'"{stale}"'}
	).status_code == 409

	assert world.call("GET", f"/v1/tasks/{created['ref']}").json()["completed_at"] is None


def test_projects_and_documents_are_guarded_the_same_way (
	world: test_api_tasks.World,
) -> None:
	"""One rule, applied everywhere it could matter."""

	project = world.call("POST", "/v1/projects", json={"key": "web", "title": "Site"}).json()
	document = world.call("POST", "/v1/documents", json={"title": "Spec"}).json()

	world.call("PATCH", "/v1/projects/WEB", json={"title": "Website"})
	world.call("PATCH", f"/v1/documents/{document['ref']}", json={"body": "Written"})

	assert world.call(
		"PATCH", "/v1/projects/WEB", json={"title": "x", "expected_version": project["version"]}
	).status_code == 409
	assert world.call(
		"PATCH",
		f"/v1/documents/{document['ref']}",
		json={"title": "x", "expected_version": document["version"]},
	).status_code == 409


#: The prefixes whose entities carry a version, and how to address one of them.
_ENTITIES = (("/v1/tasks/", "{id_or_ref}"), ("/v1/documents/", "{id_or_ref}"),
	("/v1/projects/", "{id_or_key:path}"))

#: What to send where a route needs a body before it gets as far as the version check. A move
#: has to name a destination, because an omitted parent and an explicit ``null`` mean different
#: things (§8.3). Everything else takes none, and a route that grows a required body fails here
#: rather than quietly passing on a 422.
_BODIES: dict[str, dict[str, typing.Any]] = {
	"POST /v1/tasks/{id_or_ref}/move": {"parent": None},
	"POST /v1/documents/{id_or_ref}/move": {"parent": None},
	"POST /v1/projects/{id_or_key:path}/move": {"parent": None},
	"PATCH /v1/tasks/{id_or_ref}": {"title": "Renamed"},
	"PATCH /v1/documents/{id_or_ref}": {"title": "Renamed"},
	"PATCH /v1/projects/{id_or_key:path}": {"title": "Renamed"},
}

#: Routes at an entity's address that change something else, and so have no version of this
#: entity to be asked about. Keyed ``"METHOD path"``, like ``PUBLIC_ROUTES`` and
#: ``NOT_REFUSED``, and each entry says what it writes instead.
#:
#: **Not "these are exempt from concurrency"** — a comment and a link each have their own
#: identity, and neither changes the row it hangs off. An ``If-Match`` naming the task's
#: version would compare a number nothing here moves, which is a check that always passes:
#: worse than no check, because a caller would believe it.
NOT_A_CHANGE_TO_THE_ENTITY: dict[str, str] = {
	"POST /v1/tasks/{id_or_ref}/comments": "writes a comment, which is its own entity",
	"POST /v1/documents/{id_or_ref}/comments": "writes a comment, which is its own entity",
	"POST /v1/projects/{id_or_key:path}/comments": "writes a comment, which is its own entity",
	"POST /v1/tasks/{id_or_ref}/verifications": (
		"writes a verification, which is its own entity and is append-only (`#1121`) — a "
		"record of what was checked at a moment is not a thing to edit, so there is no version "
		"of it to be stale, and the task it is about is unchanged by one being written"
	),
	"POST /v1/tasks/{id_or_ref}/links": "writes a link, which is its own entity",
	"POST /v1/documents/{id_or_ref}/links": "writes a link, which is its own entity",
	"DELETE /v1/tasks/{id_or_ref}/links/{link_id}": "removes a link, addressed by its own id",
	"DELETE /v1/documents/{id_or_ref}/links/{link_id}": (
		"removes a link, addressed by its own id"
	),
}


def _changes_an_entity () -> list[tuple[str, str, str, str]]:
	"""Return every declared route that changes a task, a document or a project.

	As ``(name, method, path, segment)`` — the segment being what the entity's own address is
	spelled with, which differs between a ref and a key.
	"""

	found: list[tuple[str, str, str, str]] = []

	for path, methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		for prefix, segment in _ENTITIES:
			if not path.startswith(prefix):
				continue

			for method in sorted(methods - {"GET", "HEAD"}):
				found.append((f"{method} {path}", method, path, segment))

	return found


def test_every_change_to_an_entity_honours_the_version_it_was_given (
	world: test_api_tasks.World,
) -> None:
	"""Derived from the routes, because the hand-written version of this fell behind.

	``If-Match`` was read by ``PATCH``, ``DELETE``, ``complete``, ``skip`` and ``restore`` and
	silently ignored by ``claim``, ``release`` and all three ``move`` endpoints — against RFC
	9110 §13.1.1, which says an unsafe method must not be performed when the condition fails.
	Silently is the whole problem: a caller doing read-modify-write correctly was told 200 for
	a change it had asked to have refused.

	A route that changes something else — a comment, a link — says so in
	``NOT_A_CHANGE_TO_THE_ENTITY`` with what it writes instead. Anything else is asked, and a
	route added tomorrow is asked without anybody remembering to add it here.
	"""

	# Repeating, because `skip` refuses a task that is not — a real 422 that would otherwise
	# hide whether the route reads the header at all.
	filed = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Guarded", "due": "2026-09-01", "recurrence": "every 14 days"},
	)

	assert filed.status_code == 201, filed.text

	task = filed.json()
	document = world.call("POST", "/v1/documents", json={"title": "Guarded"}).json()
	project = world.call("POST", "/v1/projects", json={"key": "web", "title": "Site"}).json()

	stale = {
		"/v1/tasks/": (str(task["ref"]), task["version"]),
		"/v1/documents/": (str(document["ref"]), document["version"]),
		"/v1/projects/": ("web", project["version"]),
	}

	# Moved on, so the version each caller holds is genuinely out of date. Without this every
	# route would pass by agreeing with a version that never changed.
	world.call("PATCH", f"/v1/tasks/{task['ref']}", json={"description": "moved on"})
	world.call("PATCH", f"/v1/documents/{document['ref']}", json={"body": "moved on"})
	world.call("PATCH", "/v1/projects/web", json={"description": "moved on"})

	routes = _changes_an_entity()

	assert len(routes) >= 15, f"only {len(routes)} routes were found, so this reads almost nothing"

	ignored = []

	for name, method, path, segment in routes:
		if name in NOT_A_CHANGE_TO_THE_ENTITY:
			continue

		prefix = next(start for start, _ in _ENTITIES if path.startswith(start))
		address, version = stale[prefix]
		answered = world.call(
			method,
			path.replace("{" + segment.strip("{}") + "}", address),
			headers={"if-match": f'"{version}"'},
			json=_BODIES.get(name),
		)

		if answered.status_code != 409:
			ignored.append(f"{name} answered {answered.status_code}")

	assert not ignored, (
		"These routes were given a version that is no longer current and changed the entity "
		"anyway: " + ", ".join(sorted(ignored)) + ". Pass concurrency.expected(request) to "
		"the service, or record in NOT_A_CHANGE_TO_THE_ENTITY what the route writes instead."
	)


def test_nothing_is_excused_that_no_longer_exists () -> None:
	"""An excuse outliving its route is a decision nobody can find to reverse."""

	declared = {name for name, _method, _path, _segment in _changes_an_entity()}
	gone = sorted(set(NOT_A_CHANGE_TO_THE_ENTITY) - declared)

	assert not gone, f"These excused routes no longer exist: {', '.join(gone)}"


def test_two_writers_holding_the_same_version_do_not_both_win (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""`#927`'s H-12 — the check answered the wrong question until the ``UPDATE`` carried it.

	Every test above sends one request at a time, so each one reads a version that has already
	settled. That is the case ``versions.require`` handles: the caller read 7, the entity is at
	9, refused before any work is done. **It could not see the case where both writers read the
	same number**, because it compares against the row as loaded in *this* transaction and
	nothing serialised the two.

	Reproduced before the fix, on both backends: two titles sent, both accepted, one silently
	gone — and the row left at **version 2 rather than 3**, so a client that read 1 and now sees
	2 concludes exactly one change happened. The mechanism built to report a lost update was
	concealing it.

	**Both backends, unlike `#354`'s claim test, which skips on SQLite.** That one is about two
	writers writing at once, which SQLite genuinely serialises. This one needs only two
	overlapping *read-then-write* cycles, and serialising the writes does not prevent the
	second from overwriting the first — which is why the skip would have been wrong here and is
	the distinction worth keeping.

	Real connections rather than the shared-transaction fixture, for that fixture's own stated
	reason: it exists to stop tests seeing each other's transactions, and this test is entirely
	about what two transactions do to one row.
	"""

	factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)
	written: list[uuid.UUID] = []

	try:
		with factory() as setup:
			workspace, project, owner = test_claims._place(setup)
			task = test_claims._task(setup, project, "Both will try to rename this")

			setup.commit()
			task_id, workspace_id = task.id, workspace.id
			written = [owner.user.id]

		both_read = threading.Barrier(2)

		def rename (title: str) -> str | None:
			"""Read the task, wait for the other reader, then write claiming version 1."""

			with factory() as writer:
				row = writer.get(subroutine.db.models.work.Task, task_id)

				assert row is not None and row.version == 1

				# Both read, then both write. Without this one would finish before the other
				# started, and the test would pass against the defect it was written for.
				both_read.wait(timeout=30)

				try:
					subroutine.domain.versions.require(row, 1, noun="This task")

					row.title = title
					row.version += 1
					writer.commit()

					return title

				except (subroutine.errors.Conflict, subroutine.domain.versions.RACED):
					writer.rollback()

					return None

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			results = [
				future.result()
				for future in [pool.submit(rename, name) for name in ("First", "Second")]
			]

		winners = [one for one in results if one is not None]

		assert len(winners) == 1, "both writers were told their change landed"

		with factory() as check:
			row = check.get(subroutine.db.models.work.Task, task_id)

			assert row is not None
			assert row.title == winners[0], "the surviving title is the one that was accepted"

			# **The number matters as much as the title.** Landing at 2 with two writes is what
			# made the loss invisible: a reader who held 1 sees 2 and concludes one change.
			assert row.version == 2, "one change was applied, so the version moved once"

	finally:
		# This test commits, so it owns everything it wrote — `test_concurrent_ref_allocation`
		# is the recorded case of a test that cleaned up its workspace and not its accounts,
		# and failed ten unrelated PostgreSQL tests only in a full run.
		with factory() as tidy:
			tidy.execute(
				sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
					subroutine.db.models.identity.Workspace.id == workspace_id
				)
			)

			for user_id in written:
				tidy.execute(
					sqlalchemy.delete(subroutine.db.models.identity.User).where(
						subroutine.db.models.identity.User.id == user_id
					)
				)

			tidy.commit()
