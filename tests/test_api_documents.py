"""Documents and links over HTTP.

The last test in this file is the point of the slice: write a specification into the
system, derive tasks from it, and read the relationship back from both ends. That is what
"the roadmap moves into the system" means in practice, and everything else in slice 3 is
machinery for it.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_a_document_is_written_and_read_back (world: test_api_tasks.World) -> None:
	"""The default type is a note, which is the least presumptuous thing to assume."""

	response = world.call(
		"POST", "/v1/documents", json={"title": "How deploys work", "body": "Step one…"}
	)

	assert response.status_code == 201

	body = response.json()

	assert body["title"] == "How deploys work"
	assert body["type"] == "note"
	assert body["status_category"] == "draft"
	assert body["owner_id"] == str(world.user.id)

	assert world.call("GET", f"/v1/documents/{body['ref']}").json()["body"] == "Step one…"


def test_a_document_has_no_deadline_and_cannot_be_given_one (
	world: test_api_tasks.World,
) -> None:
	"""SPEC.md §6.14: a specification is never "done" and nobody is working on it.

	"The spec must be signed off by Friday" is a *task* that documents the spec. Keeping
	dates off the document is what stops every scheduling query needing an entity filter.
	"""

	created = world.call("POST", "/v1/documents", json={"title": "Spec"}).json()

	assert "due_at" not in created
	assert "assignee_id" not in created

	refused = world.call("PATCH", f"/v1/documents/{created['ref']}", json={"due": "tomorrow"})

	assert refused.status_code == 422
	assert refused.json()["code"] == "unknown_field"


def test_documents_and_tasks_share_one_ref_space (world: test_api_tasks.World) -> None:
	"""So ``#42`` is unambiguous whichever it names (SPEC.md §5.6)."""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()
	document = world.call("POST", "/v1/documents", json={"title": "A document"}).json()

	assert task["ref"] != document["ref"]

	# And each endpoint declines the other's ref rather than pretending it has no such row.
	assert world.call("GET", f"/v1/documents/{task['ref']}").status_code == 404
	assert world.call("GET", f"/v1/tasks/{document['ref']}").status_code == 404


def test_superseding_a_document_retires_the_one_it_replaces (
	world: test_api_tasks.World,
) -> None:
	"""The two are one fact: a superseded document still reading as active is a trap."""

	first = world.call(
		"POST", "/v1/documents", json={"title": "Deploy process v1", "status": "active"}
	).json()

	assert first["status_category"] == "current"

	second = world.call(
		"POST", "/v1/documents", json={"title": "Deploy process v2", "supersedes": first["ref"]}
	).json()

	assert second["supersedes_id"] == first["id"]

	retired = world.call("GET", f"/v1/documents/{first['ref']}").json()

	assert retired["status_category"] == "superseded"


def test_a_document_cannot_supersede_itself (world: test_api_tasks.World) -> None:
	"""A one-element cycle is still a cycle."""

	created = world.call("POST", "/v1/documents", json={"title": "Spec"}).json()
	response = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"supersedes": created["ref"]}
	)

	assert response.status_code == 409
	assert response.json()["code"] == "cycle_detected"


def test_an_omitted_field_is_untouched_and_a_null_one_is_cleared (
	world: test_api_tasks.World,
) -> None:
	"""§8.3 again, because it has to hold on every entity or it holds on none."""

	created = world.call(
		"POST", "/v1/documents", json={"title": "Spec", "body": "Original"}
	).json()

	renamed = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"title": "Specification"}
	).json()

	assert renamed["body"] == "Original", "an omitted field must be left alone"

	cleared = world.call("PATCH", f"/v1/documents/{created['ref']}", json={"body": None}).json()

	assert cleared["body"] is None
	assert cleared["title"] == "Specification"


def test_a_document_is_soft_deleted (world: test_api_tasks.World) -> None:
	"""Recoverable, like everything else (SPEC.md §6.9)."""

	created = world.call("POST", "/v1/documents", json={"title": "Throw away"}).json()
	deleted = world.call("DELETE", f"/v1/documents/{created['ref']}")

	assert deleted.status_code == 200
	assert deleted.json()["deleted_at"] is not None
	assert world.call("GET", "/v1/documents").json()["items"] == []


def test_documents_in_a_private_project_are_hidden_like_its_tasks (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A specification is exactly as private as the work derived from it.

	It would be an odd kind of privacy if the plan were readable and only the tasks were not.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST", "/v1/projects", json={"key": "secret", "title": "Secret", "visibility": "private"}
	)
	hidden = world.call(
		"POST", "/v1/documents", json={"title": "The plan", "project": "secret"}
	).json()

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert nosy.call("GET", "/v1/documents").json()["items"] == []
	assert nosy.call("GET", f"/v1/documents/{hidden['ref']}").status_code == 404


def test_a_link_reads_the_right_way_round_from_each_end (
	world: test_api_tasks.World,
) -> None:
	"""One stored row, two readings. The link type carries the inverse label (SPEC.md §5.7)."""

	blocker = world.call("POST", "/v1/tasks", json={"title": "Do this first"}).json()
	blocked = world.call("POST", "/v1/tasks", json={"title": "Then this"}).json()

	created = world.call(
		"POST",
		f"/v1/tasks/{blocker['ref']}/links",
		json={"target": blocked["ref"], "link_type": "blocks"},
	)

	assert created.status_code == 201
	assert created.json()["label"] == "Blocks"
	assert created.json()["direction"] == "outgoing"

	from_other_end = world.call("GET", f"/v1/tasks/{blocked['ref']}/links").json()["items"]

	assert len(from_other_end) == 1
	assert from_other_end[0]["label"] == "Blocked by"
	assert from_other_end[0]["direction"] == "incoming"
	assert from_other_end[0]["other"]["ref"] == blocker["ref"]


def test_a_symmetric_link_reads_the_same_from_both_ends (
	world: test_api_tasks.World,
) -> None:
	"""``relates_to`` has no inverse, so it must not be given one."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "relates_to"}
	)

	assert (
		world.call("GET", f"/v1/tasks/{one['ref']}/links").json()["items"][0]["label"]
		== "Relates to"
	)
	assert (
		world.call("GET", f"/v1/tasks/{two['ref']}/links").json()["items"][0]["label"]
		== "Relates to"
	)


def test_linking_twice_is_not_an_error (world: test_api_tasks.World) -> None:
	"""A client retrying a request it is unsure landed should not be punished for it."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()
	body = {"target": two["ref"], "link_type": "blocks"}

	first = world.call("POST", f"/v1/tasks/{one['ref']}/links", json=body)
	again = world.call("POST", f"/v1/tasks/{one['ref']}/links", json=body)

	assert first.json()["id"] == again.json()["id"]
	assert len(world.call("GET", f"/v1/tasks/{one['ref']}/links").json()["items"]) == 1


def test_nothing_can_be_linked_to_itself (world: test_api_tasks.World) -> None:
	"""The database refuses it too; this refuses it in words."""

	task = world.call("POST", "/v1/tasks", json={"title": "Alone"}).json()
	response = world.call(
		"POST", f"/v1/tasks/{task['ref']}/links", json={"target": task["ref"], "link_type": "blocks"}
	)

	assert response.status_code == 422


def test_an_unknown_link_type_names_the_ones_that_exist (
	world: test_api_tasks.World,
) -> None:
	"""Link types are workspace data, so the valid set is read rather than assumed."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	response = world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "supersedes"}
	)

	assert response.status_code == 422
	assert "derives_from" in response.json()["errors"][0]["hint"]


def test_a_link_can_be_withdrawn (world: test_api_tasks.World) -> None:
	"""And withdrawing it leaves the items alone."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	link = world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "blocks"}
	).json()

	removed = world.call("DELETE", f"/v1/tasks/{one['ref']}/links/{link['id']}")

	assert removed.status_code == 204
	assert world.call("GET", f"/v1/tasks/{one['ref']}/links").json()["items"] == []
	assert world.call("GET", f"/v1/tasks/{two['ref']}").status_code == 200


def test_a_link_to_something_invisible_is_not_reported (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A link is only as visible as the thing at the other end of it.

	Reporting "there is a link to something you may not see" would disclose exactly what
	§7.3a's existence rule protects.
	"""

	world = test_api_tasks._world(session)

	public = world.call("POST", "/v1/tasks", json={"title": "Public work"}).json()
	world.call(
		"POST", "/v1/projects", json={"key": "secret", "title": "Secret", "visibility": "private"}
	)
	secret = world.call(
		"POST", "/v1/tasks", json={"title": "Secret work", "project": "secret"}
	).json()

	world.call(
		"POST",
		f"/v1/tasks/{public['ref']}/links",
		json={"target": secret["ref"], "link_type": "relates_to"},
	)

	assert len(world.call("GET", f"/v1/tasks/{public['ref']}/links").json()["items"]) == 1

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert nosy.call("GET", f"/v1/tasks/{public['ref']}/links").json()["items"] == []


def test_a_specification_can_be_written_and_the_work_derived_from_it (
	world: test_api_tasks.World,
) -> None:
	"""**The capability slice 3 exists for**, end to end.

	Write the plan in as a document, derive a task per item with ``derives_from``, then read
	the relationship back from both ends — from the spec, everything derived from it; from a
	task, the spec it came from. This is what "the roadmap moves into the system" means, and
	the next planning conversation happening against the API rather than a markdown file
	depends on exactly this working.
	"""

	world.call("POST", "/v1/projects", json={"key": "SR", "title": "Subroutine"})

	spec = world.call(
		"POST",
		"/v1/documents",
		json={
			"title": "Slice 4 plan",
			"body": "1. Meta endpoint\n2. Response shaping\n3. Connections",
			"project": "SR",
			"type": "spec",
			"status": "active",
		},
	).json()

	derived = []

	for title in ("Meta endpoint", "Response shaping", "Connections"):
		task = world.call("POST", "/v1/tasks", json={"title": title, "project": "SR"}).json()
		linked = world.call(
			"POST",
			f"/v1/tasks/{task['ref']}/links",
			json={"target": spec["ref"], "target_type": "document", "link_type": "derives_from"},
		)

		assert linked.status_code == 201
		assert linked.json()["label"] == "Derives from"

		derived.append(task["ref"])

	# From the specification: everything that came out of it.
	from_spec = world.call("GET", f"/v1/documents/{spec['ref']}/links").json()["items"]

	assert {link["other"]["ref"] for link in from_spec} == set(derived)
	assert {link["direction"] for link in from_spec} == {"incoming"}
	assert {link["label"] for link in from_spec} == {"Derived into"}

	# From a task: the specification it came from.
	from_task = world.call("GET", f"/v1/tasks/{derived[0]}/links").json()["items"]

	assert from_task[0]["other"]["ref"] == spec["ref"]
	assert from_task[0]["other"]["entity_type"] == "document"
	assert from_task[0]["label"] == "Derives from"


def test_a_document_can_be_tagged (world: test_api_tasks.World) -> None:
	"""`#819`, and Simon's question that found it: *documents can't be tagged, can they?*

	They could not. `document_tag` has existed since the initial migration and was written and
	read by nothing — the second signature defect of this codebase, and the largest instance of
	it, because the guard written to notice exactly that carried an excuse for the table naming
	a field that did not exist (`#820`).
	"""

	created = world.call(
		"POST",
		"/v1/documents",
		json={"title": "How the thing works", "body": ".", "tags": ["design", "api"]},
	)

	assert created.status_code == 201, created.text
	assert created.json()["tags"] == ["api", "design"], "alphabetical, like a task's"

	# And read back on the listing, which loads them per page rather than per row.
	listed = world.call("GET", "/v1/documents")

	assert listed.status_code == 200, listed.text
	assert listed.json()["items"][0]["tags"] == ["api", "design"]


def test_a_document_and_a_task_share_one_tag (world: test_api_tasks.World) -> None:
	"""**Simon's decision, 2026-08-12: one tag namespace.**

	A tag is scoped to a workspace rather than to a kind, which is what both association tables
	already assumed by referencing `tag.id`. So `#health` on a document and on a task are the
	same row, unlike a status or an item type — §5.5 keeps those per kind because *done* means
	nothing about a specification.

	Checked through `/v1/meta`'s tag list, which counts usage across the workspace: one tag
	used twice rather than two tags used once.
	"""

	world.call("POST", "/v1/tasks", json={"title": "Fix it", "tags": ["health"]})
	world.call(
		"POST", "/v1/documents", json={"title": "Why", "body": ".", "tags": ["health"]}
	)

	published = world.call("GET", "/v1/meta").json()["tags"]
	health = [tag for tag in published["items"] if tag["name"] == "health"]

	assert len(health) == 1, f"the two kinds made two tags: {published['items']}"


def test_a_documents_tags_are_replaced_rather_than_merged (
	world: test_api_tasks.World,
) -> None:
	"""§8.3, and the same answer a task gives — a `PATCH` assigns, it does not merge.

	A `tags` that merged would be the only field on this endpoint a caller could not use to
	*remove* anything, and clearing is how a mistyped tag is taken off.
	"""

	created = world.call(
		"POST",
		"/v1/documents",
		json={"title": "A conclusion", "body": ".", "tags": ["draft", "api"]},
	).json()

	revised = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"tags": ["api"]}
	)

	assert revised.status_code == 200, revised.text
	assert revised.json()["tags"] == ["api"]

	cleared = world.call("PATCH", f"/v1/documents/{created['ref']}", json={"tags": []})

	assert cleared.status_code == 200, cleared.text
	assert cleared.json()["tags"] == []

	# Omitting the field leaves them alone, which is the other half of §8.3 and the half a
	# test of "it replaces" cannot see on its own.
	world.call("PATCH", f"/v1/documents/{created['ref']}", json={"tags": ["kept"]})
	untouched = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"title": "Renamed"}
	)

	assert untouched.json()["tags"] == ["kept"]


def test_a_tag_of_only_digits_is_refused_on_a_document_too (
	world: test_api_tasks.World,
) -> None:
	"""§6.2's rule reaches this by construction, and that is why it goes through `ensure`.

	A name of only digits is a *reference*, not a tag — `#12` means item 12. The rule lives in
	`tags.ensure`, which every tag passes through however it arrived, so a second entry point
	inherits it rather than needing its own copy.
	"""

	refused = world.call(
		"POST", "/v1/documents", json={"title": "x", "body": ".", "tags": ["404"]}
	)

	assert refused.status_code == 422, refused.text
	assert "404" in refused.text


def test_a_search_for_a_number_finds_the_document_with_that_ref (
	world: test_api_tasks.World,
) -> None:
	"""**`#867`, and the reason the document half is not an afterthought.**

	One ref counter serves tasks *and* documents (§6.2), so half the numbers on this instance
	name a document — `#4` is a specification. A ref lookup that reached only tasks would
	answer "no such thing" about items sitting in the reader's own listing, which is exactly
	the defect `#535` and `#700` found in two other lookups.
	"""

	made = world.call(
		"POST", "/v1/documents", json={"title": "Nothing alike", "body": "Unrelated prose."}
	).json()

	found = world.call("GET", f"/v1/documents?q={made['ref']}&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_search_finds_a_document_by_the_words_in_a_comment_on_it (
	world: test_api_tasks.World,
) -> None:
	"""`#83` reaches documents too, and this is where it pays most.

	A document is a conclusion (§5.10) and its comments are where it was argued over — so the
	half-remembered sentence somebody searches for is often in the argument rather than in the
	conclusion it produced.
	"""

	made = world.call(
		"POST", "/v1/documents", json={"title": "A settled thing", "body": "The conclusion."}
	).json()
	world.call(
		"POST",
		f"/v1/documents/{made['ref']}/comments",
		json={"body": "Reopened because the fenestration argument was never answered."},
	)

	found = world.call("GET", "/v1/documents?q=fenestration&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_document_answers_the_deferral_ordering_rather_than_refusing_it (
	world: test_api_tasks.World,
) -> None:
	"""`SR#877`. §6.14 says a document is not scheduled, and the answer is *no* rather than 422.

	**The obstacle this closes is the one that would have been silent.** A merged list holds
	tasks *and* documents (§6.2 gives them one ref counter so a reader can treat them as one
	thing), and both halves have to be asked for the same order — a name only tasks accept
	drops the documents entirely (`SR#782`), so *deferred last* would have quietly turned every
	list in the browser into a list of tasks. Half the numbers a reader can type would name
	something that was no longer on the page.

	So the field exists here, and the value is the constant first band. The ordering is then
	decided entirely by the keys under it, which is what the second assertion says: the two
	orders are the same page.

	**A bare `0` in `ORDER BY` is a column position to PostgreSQL**, so the constant is a bind
	parameter — measured on both backends before it was written rather than reasoned about.
	"""

	for title in ("Apple", "Banana", "Carrot"):
		made = world.call("POST", "/v1/documents", json={"title": title, "body": "Prose."})
		assert made.status_code == 201, made.text

	sunk = world.call("GET", "/v1/documents?order=deferred,title")

	assert sunk.status_code == 200, sunk.text
	assert [item["title"] for item in sunk.json()["items"]] == ["Apple", "Banana", "Carrot"]

	plain = world.call("GET", "/v1/documents?order=title")

	assert [item["ref"] for item in sunk.json()["items"]] == [
		item["ref"] for item in plain.json()["items"]
	], "a constant leading key changed the arrangement, so it is not constant"


def test_a_document_listing_publishes_the_deferral_ordering (
	world: test_api_tasks.World,
) -> None:
	"""`SR#877`. A sort field a listing accepts and never mentions is one nobody can discover.

	§9.4 says an agent learns what is available from `/v1/meta` rather than by being refused,
	and `relevance` is published on exactly that argument. This is the same claim for a name
	*both* item listings carry — and a project listing does not, because a project is not
	scheduled and has nothing to defer.
	"""

	published = world.call("GET", "/v1/meta").json()["listings"]

	for entity in ("task", "document"):
		assert "deferred" in published[entity]["sortable"], (
			f"a {entity} listing sorts by deferral and does not say so"
		)

	assert "deferred" not in published["project"]["sortable"], (
		"a project has nothing to defer, so offering the order would be an empty promise"
	)


@pytest.fixture
def ranked (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with the indexed backend asked for, skipping where it cannot exist.

	The twin of `test_api_tasks.ranked` and written out rather than reached into: a fixture is
	not a plain function once pytest has decorated it, and unwrapping one is a claim about that
	library's internals. `#871` is why the skip is right rather than a failure — the native
	backend is PostgreSQL-only by decision, so on SQLite there is nothing to test.
	"""

	if session.get_bind().dialect.name != "postgresql":
		pytest.skip("relevance needs a backend that can rank")

	return test_api_tasks._world(session, instance={"search_backend": "native"})


def test_a_document_search_is_ranked_by_the_backend_this_instance_was_built_with (
	ranked: test_api_tasks.World,
) -> None:
	"""**`SR#883`, and the coverage gap is worth more than the one-word fix.**

	`search.chosen()` falls back to `config.load_settings()`, which re-reads the environment.
	The documents listing did not pass its own `settings`, so an application built with
	`search_backend` injected answered **tasks with one backend and documents with the other** —
	and injection is the only mechanism the suite has for this. `test_transport_equivalence`
	sets the *environment variable*, which both paths read, so it masked the divergence rather
	than catching it, and it is tasks-only besides.

	So `ix_document_search`, the document ranking over title and body, `views.Document.relevance`
	and merged order parity had **no coverage over HTTP at all** until this.

	**Stemming is what proves which backend answered.** `paginate` finds *pagination* only under
	`native`; a substring search cannot, so this cannot pass by accident on the `like` path.
	"""

	made = ranked.call(
		"POST", "/v1/documents",
		json={"title": "Pagination", "body": "How the cursor resumes."},
	)
	assert made.status_code == 201, made.text

	found = ranked.call("GET", "/v1/documents?q=paginate")

	assert found.status_code == 200, found.text
	assert [item["title"] for item in found.json()["items"]] == ["Pagination"], (
		"a stemmed match reached the documents listing, so the native backend answered it"
	)

	assert found.json()["items"][0]["relevance"] is not None, (
		"a ranked document carries its ranking, which is what a merged list orders on"
	)


def test_a_document_listing_ranks_by_relevance_without_being_asked (
	ranked: test_api_tasks.World,
) -> None:
	"""The other half of `SR#883`: the default order, not just the field.

	A search defaults to its own ranking wherever a backend can compute one (`SR#823`). If the
	documents listing resolved a different backend from the one the instance was built with,
	this is the assertion that fails — the rows come back in creation order and say so.
	"""

	# **The best match is written *first*, and that is what makes this falsifiable.** Written
	# last, it comes out on top under `-created_at` as well, so the ranked answer and the
	# unranked one are the same list and the assertion cannot fail. Found by mutating.
	for title, body in (
		("Cursor cursor cursor", "cursor cursor cursor cursor"),
		("A note about cursors", "One mention of the cursor."),
	):
		ranked.call("POST", "/v1/documents", json={"title": title, "body": body})

	items = ranked.call("GET", "/v1/documents?q=cursor").json()["items"]

	assert [item["title"] for item in items] == ["Cursor cursor cursor", "A note about cursors"], (
		f"a ranked search must put the best match first — got {[i['title'] for i in items]}"
	)


def test_a_document_can_be_nested_over_http (world: test_api_tasks.World) -> None:
	"""`#44`'s worse half, which had no endpoint at all.

	``parent_id`` was reported by this view and accepted nowhere, so a section of a
	specification could be read as belonging to it and could never be made to.
	"""

	whole = world.call(
		"POST", "/v1/documents", json={"title": "The specification", "body": "."}
	).json()
	part = world.call(
		"POST", "/v1/documents", json={"title": "A section", "body": "."}
	).json()

	assert part["parent_id"] is None

	nested = world.call(
		"POST", f"/v1/documents/{part['ref']}/move", json={"parent": str(whole["ref"])}
	)

	assert nested.status_code == 200, nested.text
	assert nested.json()["parent_id"] == whole["id"]

	loose = world.call("POST", f"/v1/documents/{part['ref']}/move", json={"parent": None})

	assert loose.status_code == 200, loose.text
	assert loose.json()["parent_id"] is None
