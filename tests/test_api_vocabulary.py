"""Curating a workspace's vocabulary — `#826`, docs/design.md §5.5.

Every one of these was unreachable until this landed: `Status`, `LinkType` and `Tag` rows were
written by ``db.seed`` and by nothing else, so an installation could not add, rename or remove
one on any surface. The visible cost was three published permissions gating nothing.

**The tests that matter most are the refusals**, because the shape this feature arrives in is one
where every mistake is silent: a second default status makes ``tasks.create`` file into whichever
row the database returns first, a removed status leaves a foreign key to raise later, and a
renamed key leaves a setting that names the old one quietly matching nothing.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.settings
import subroutine.domain.tasks
import subroutine.permissions
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _statuses (world: test_api_tasks.World, **params: typing.Any) -> list[dict[str, typing.Any]]:
	"""Return the status listing's rows."""

	response = world.call("GET", "/v1/statuses", params=params)

	assert response.status_code == 200, response.text

	return list(response.json()["items"])


def _keyed (rows: list[dict[str, typing.Any]], key: str) -> dict[str, typing.Any]:
	"""Return the one row with this key, failing loudly if it is not there."""

	found = [row for row in rows if row["key"] == key]

	assert found, f"no status called {key!r} in {[row['key'] for row in rows]}"

	return found[0]


def test_a_status_can_be_added_renamed_and_removed (world: test_api_tasks.World) -> None:
	"""The whole of the happy path, on the surface that had none at all."""

	made = world.call(
		"POST",
		"/v1/statuses",
		json={
			"entity_type": "task",
			"key": "in_review",
			"label": "In review",
			"category": "in_progress",
		},
	)

	assert made.status_code == 201, made.text
	assert made.json()["key"] == "in_review"
	assert made.json()["category"] == "in_progress"

	which = made.json()["id"]

	assert which, "the id is what the other three routes take, so it has to be published"

	renamed = world.call("PATCH", f"/v1/statuses/{which}", json={"key": "reviewing"})

	assert renamed.status_code == 200, renamed.text
	assert renamed.json()["key"] == "reviewing"
	assert renamed.json()["category"] == "in_progress", "renaming does not change the meaning"

	removed = world.call("DELETE", f"/v1/statuses/{which}")

	assert removed.status_code == 204, removed.text
	assert not [row for row in _statuses(world, entity_type="task") if row["key"] == "reviewing"]


def test_a_status_cannot_change_what_it_means (world: test_api_tasks.World) -> None:
	"""``category`` is settable once and is refused on a change.

	Not a nicety: it is what every client branches on, so moving a status between categories
	would change the meaning of every item already in it. `views.Status`'s own sentence — *the
	key is renameable; the category is not* — is a promise to a caller that cached one.
	"""

	which = _keyed(_statuses(world, entity_type="task"), "open")["id"]
	refused = world.call("PATCH", f"/v1/statuses/{which}", json={"category": "done"})

	assert refused.status_code == 422, refused.text
	assert "category" in refused.text


def test_a_document_category_is_refused_on_a_task_status (world: test_api_tasks.World) -> None:
	"""The two vocabularies are refused against each other by name.

	A superseded specification is not "done", which is why the sets are separate — so
	``STATUS_CATEGORIES``, the union, is the wrong thing to validate against and the refusal
	has to name the half that applies.
	"""

	refused = world.call(
		"POST",
		"/v1/statuses",
		json={
			"entity_type": "task",
			"key": "shelved",
			"label": "Shelved",
			"category": "superseded",
		},
	)

	assert refused.status_code == 422, refused.text
	assert "todo" in refused.text, "the refusal names the categories a task may have"


def test_only_one_status_is_ever_the_default (world: test_api_tasks.World) -> None:
	"""docs/design.md §10.7 invariant 6, which nothing enforced — `#826`.

	**Two defaults is not a cosmetic mess.** `tasks.create` asks for *the* default and would
	get whichever row the database returned first, so the same call files two tasks into
	different statuses on two days and nothing reports it.

	Falsified by removing `_only_one_default`'s update: this then finds two.
	"""

	before = [row for row in _statuses(world, entity_type="task") if row["is_default"]]

	assert len(before) == 1, "the seeded workspace starts with exactly one"

	made = world.call(
		"POST",
		"/v1/statuses",
		json={
			"entity_type": "task",
			"key": "triage",
			"label": "Triage",
			"category": "todo",
			"is_default": True,
		},
	)

	assert made.status_code == 201, made.text

	after = [row for row in _statuses(world, entity_type="task") if row["is_default"]]

	assert [row["key"] for row in after] == ["triage"], "the old default was cleared"

	# And the other direction: promoting an existing one demotes the new one.
	promoted = world.call(
		"PATCH", f"/v1/statuses/{_keyed(after, 'triage')['id']}", json={"is_default": False}
	)

	assert promoted.status_code == 422, "something has to be the default"


def test_a_default_status_is_not_removable (world: test_api_tasks.World) -> None:
	"""Removing it would leave a new task with nowhere to go."""

	which = _keyed(_statuses(world, entity_type="task"), "open")["id"]
	refused = world.call("DELETE", f"/v1/statuses/{which}")

	assert refused.status_code == 422, refused.text
	assert "default" in refused.text


def test_a_status_something_is_in_is_not_removable (world: test_api_tasks.World) -> None:
	"""§5.5's *fails if in use*, and it says how many are in the way.

	**The database already refuses this** — the foreign keys are ``ondelete="RESTRICT"`` — so
	this is not the safety. It is the difference between a sentence naming what to move and an
	``IntegrityError`` reaching a caller as a 500 about a constraint.
	"""

	made = world.call(
		"POST",
		"/v1/statuses",
		json={
			"entity_type": "task",
			"key": "parked",
			"label": "Parked",
			"category": "todo",
		},
	)
	which = made.json()["id"]

	filed = world.call("POST", "/v1/tasks", json={"title": "Something"})

	assert filed.status_code == 201, filed.text

	moved = world.call(
		"PATCH", f"/v1/tasks/{filed.json()['ref']}", json={"status": "parked"}
	)

	assert moved.status_code == 200, moved.text

	refused = world.call("DELETE", f"/v1/statuses/{which}")

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "in_use"
	assert "1 tasks" in refused.text, "the refusal counts them rather than saying 'in use'"


def test_renaming_a_status_carries_it_into_the_settings_that_name_it (
	world: test_api_tasks.World,
) -> None:
	"""`hidden_statuses` stores **keys**, so a rename that left them behind would go quiet.

	Not fail — *go quiet*, which is the worst of the three outcomes: the list still looks
	configured and stops hiding anything. Falsified by removing `_rename_in_settings`.
	"""

	held = subroutine.domain.settings.HIDDEN_STATUSES.key
	configured = world.call(
		"PATCH",
		f"/v1/workspaces/{world.workspace.slug}",
		json={"settings": {held: ["blocked"]}},
	)

	assert configured.status_code == 200, configured.text

	which = _keyed(_statuses(world, entity_type="task"), "blocked")["id"]
	renamed = world.call("PATCH", f"/v1/statuses/{which}", json={"key": "stuck"})

	assert renamed.status_code == 200, renamed.text

	world.session.expire(world.workspace)

	assert world.workspace.settings[held] == ["stuck"], (
		"the setting names the key, so a rename has to move it"
	)


def test_removing_a_status_drops_it_from_the_settings_that_name_it (
	world: test_api_tasks.World,
) -> None:
	"""The other half of the same rule, and the one a rename test alone would miss."""

	held = subroutine.domain.settings.HIDDEN_STATUSES.key
	made = world.call(
		"POST",
		"/v1/statuses",
		json={"entity_type": "task", "key": "later", "label": "Later", "category": "todo"},
	)
	world.call(
		"PATCH",
		f"/v1/workspaces/{world.workspace.slug}",
		json={"settings": {held: ["later"]}},
	)

	assert world.call("DELETE", f"/v1/statuses/{made.json()['id']}").status_code == 204

	world.session.expire(world.workspace)

	assert world.workspace.settings[held] == []


def test_renaming_a_link_type_leaves_readiness_and_the_labels_agreeing (
	world: test_api_tasks.World,
) -> None:
	"""`SR#1156`, and the reason `SR#1157` exists. Driven, because reading found none of it.

	Measured on the served instance before the fix, with `#12 blocks #13`::

	    subroutine list --ready                      #12 only, marked `blocker`
	    PATCH /v1/link-types/{id} {"key":"holds_up"} 200
	    subroutine list --ready                      #12 AND #13
	    subroutine show scratch/#13                  Links / Blocked by #12

	**The item page said blocked and the ready list said startable, about one link.** The label
	survived because ``inverse_title`` is a column on the row; the behaviour did not, because six
	rules were written in terms of the literal string ``blocks``.

	**Both halves are asserted, and the second is the one that would have caught it.** A test
	that only checked the label passes against the defect — the labels were never the problem.
	"""

	blocker = world.call("POST", "/v1/tasks", json={"title": "Do this first"}).json()
	blocked = world.call("POST", "/v1/tasks", json={"title": "Then this"}).json()

	joined = world.call(
		"POST",
		f"/v1/tasks/{blocker['ref']}/links",
		json={"link_type": "blocks", "target": blocked["ref"]},
	)

	assert joined.status_code == 201, joined.text

	def startable () -> set[int]:
		"""Return the refs `?ready=true` offers, which is the half that used to break."""

		found = world.call("GET", "/v1/tasks?ready=true&limit=50")

		assert found.status_code == 200, found.text

		return {row["ref"] for row in found.json()["items"]}

	assert startable() == {blocker["ref"]}, "the probe never held anything up"

	kinds = world.call("GET", "/v1/link-types").json()["items"]
	which = next(one["id"] for one in kinds if one["key"] == "blocks")

	renamed = world.call("PATCH", f"/v1/link-types/{which}", json={"key": "holds_up"})

	assert renamed.status_code == 200, renamed.text

	assert startable() == {blocker["ref"]}, (
		"renaming the key put blocked work back on the startable list, which is `SR#1156`"
	)

	links = world.call("GET", f"/v1/tasks/{blocked['ref']}/links").json()["items"]

	assert [one["label"] for one in links] == ["Blocked by"], "the wording is not the key"
	assert [one["link_category"] for one in links] == ["gating"], (
		"what the relation is has to survive being renamed, since that is the whole fix"
	)


def test_a_link_category_outside_the_vocabulary_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""A CHECK constraint is not input validation (`SR#1157`, and this project's own rule).

	Left to the database it arrives as a driver error naming no field on PostgreSQL, and on
	SQLite it might not fire at all. The refusal has to name the field and list the alternatives,
	because the caller is somebody deciding what their own relation *means*.
	"""

	refused = world.call(
		"POST",
		"/v1/link-types",
		json={
			"key": "mitigates",
			"title": "Mitigates",
			"inverse_title": "Mitigated by",
			"category": "sequencing",
		},
	)

	assert refused.status_code == 422, refused.text

	problem = refused.json()

	assert [one["field"] for one in problem["errors"]] == ["category"]
	assert "gating" in problem["errors"][0]["message"], problem


def test_a_link_type_can_be_added_and_is_not_removable_while_it_joins_something (
	world: test_api_tasks.World,
) -> None:
	"""Create and remove together, because an asymmetry is the trap `#704` is.

	``PATCH`` and ``DELETE`` on a link type are **not** in §5.5's table — only ``GET/POST`` is.
	They are built anyway: a row somebody can add and never be rid of is the shape this arc is
	closing, and finding that out costs them a row they cannot reach.

	**The key is a workspace's own and used to be ``supersedes``**, which `SR#1688` seeded — so
	this stopped testing *a workspace adds one* and started testing *creating a duplicate*.
	"""

	made = world.call(
		"POST",
		"/v1/link-types",
		json={
			"key": "mitigates",
			"title": "Mitigates",
			"inverse_title": "Mitigated by",
			"category": "governing",
		},
	)

	assert made.status_code == 201, made.text

	which = made.json()["id"]
	first = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	second = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()
	joined = world.call(
		"POST",
		f"/v1/tasks/{first['ref']}/links",
		json={"link_type": "mitigates", "target": second["ref"]},
	)

	assert joined.status_code == 201, joined.text

	refused = world.call("DELETE", f"/v1/link-types/{which}")

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "in_use"
	assert "1 links" in refused.text


def test_a_tag_says_what_it_means_here (world: test_api_tasks.World) -> None:
	"""`#905`'s question, answered by `#826`'s surface.

	A tag vocabulary that cannot say what its tags mean drifts — two people use ``#ops`` for
	different things and nothing can tell them apart. It is also the only place a *workspace*
	can write down a convention, because a tag is created by being used and never declared.
	"""

	made = world.call(
		"POST", "/v1/tags", json={"name": "ops", "description": "Anything that pages somebody."}
	)

	assert made.status_code == 201, made.text
	assert made.json()["description"] == "Anything that pages somebody."

	listed = world.call("GET", "/v1/tags").json()["items"]

	assert [row["name"] for row in listed] == ["ops"]

	cleared = world.call(
		"PATCH", f"/v1/tags/{made.json()['id']}", json={"description": None}
	)

	assert cleared.status_code == 200, cleared.text
	assert cleared.json()["description"] is None, "a workspace can take back what it wrote"


def test_declaring_a_tag_that_exists_does_not_replace_what_somebody_wrote (
	world: test_api_tasks.World,
) -> None:
	"""`#1169`. A ``201 Created`` for something not created, and a description overwritten.

	``create_tag`` delegated to ``tags.ensure``, which returns the row it *found* when the
	normalised name already exists, and then assigned the description over it. So a caller who
	thought they were declaring a new tag silently replaced what a colleague had written about
	an existing one, was told **201 Created**, and got back a name they had not sent.

	Both siblings on the same router refuse this and nothing documented why a tag differed.
	"""

	first = world.call(
		"POST", "/v1/tags", json={"name": "ops", "description": "Anything that pages somebody."}
	)

	assert first.status_code == 201, first.text

	# **A different spelling of the same tag**, which is what made this hard to see: the caller
	# is not obviously declaring something that exists.
	again = world.call("POST", "/v1/tags", json={"name": "OPS", "description": "Ours now."})

	assert again.status_code == 409, again.text
	assert "already has a tag called 'ops'" in again.text
	assert "PATCH" in again.text, "the refusal has to say where to go instead"

	listed = world.call("GET", "/v1/tags").json()["items"]

	assert [row["name"] for row in listed] == ["ops"]
	assert listed[0]["description"] == "Anything that pages somebody.", (
		"and what the first caller wrote is still there"
	)


def test_a_tag_cannot_become_a_number_by_either_door (world: test_api_tasks.World) -> None:
	"""`#1167`. §6.15 keeps a tag and a reference apart by insisting one is all digits.

	**Both doors, because only one of them was shut.** Creating a tag went through
	``tags.ensure`` and was refused; renaming one went through ``update_tag``, which called
	``text.require``, ``text.fit`` and ``tags.normalize`` directly and never the rule — so a
	``422`` on ``POST`` and a ``200`` on ``PATCH`` for the same name.

	The harm is on items that already carry it: the terminal renders a tag as ``#name``, so
	renaming ``ops`` to ``123`` makes every task tagged ``ops`` print ``#123`` — which is how
	this product spells *task 123* everywhere else.
	"""

	refused = world.call("POST", "/v1/tags", json={"name": "123"})

	assert refused.status_code == 422, refused.text
	assert "cannot be used as a tag" in refused.text

	made = world.call("POST", "/v1/tags", json={"name": "ops"})

	assert made.status_code == 201, made.text

	renamed = world.call("PATCH", f"/v1/tags/{made.json()['id']}", json={"name": "123"})

	assert renamed.status_code == 422, renamed.text
	assert "cannot be used as a tag" in renamed.text

	# And the tag it was is untouched — a refused rename must not half-apply.
	assert [row["name"] for row in world.call("GET", "/v1/tags").json()["items"]] == ["ops"]

	# A name that merely *contains* digits is fine, which is the line §6.15 actually draws.
	fine = world.call("PATCH", f"/v1/tags/{made.json()['id']}", json={"name": "2fa"})

	assert fine.status_code == 200, fine.text
	assert fine.json()["name"] == "2fa"


def test_removing_a_tag_takes_it_off_what_it_was_on (world: test_api_tasks.World) -> None:
	"""**Deliberately not an in-use refusal**, unlike a status — §5.5's table says so.

	Removing a label *means* taking it off the things it is on. Refusing until somebody had
	untagged every item by hand would make the command useless exactly when it is wanted.
	"""

	filed = world.call("POST", "/v1/tasks", json={"text": "Something #chore"})

	assert filed.status_code == 201, filed.text
	assert filed.json()["tags"] == ["chore"]

	which = [row for row in world.call("GET", "/v1/tags").json()["items"] if row["name"] == "chore"]

	assert which, "capture created it"
	assert world.call("DELETE", f"/v1/tags/{which[0]['id']}").status_code == 204

	after = world.call("GET", f"/v1/tasks/{filed.json()['ref']}")

	assert after.json()["tags"] == []


def test_a_name_cannot_be_cleared_where_a_description_can (world: test_api_tasks.World) -> None:
	"""An explicit ``null`` is refused rather than ignored, on a field that cannot be cleared.

	The alternative — a plain ``is not None`` test, which is what this codebase does elsewhere
	for fields that were never nullable — drops it silently and tells the caller their change
	was applied.
	"""

	made = world.call("POST", "/v1/tags", json={"name": "release"})
	refused = world.call("PATCH", f"/v1/tags/{made.json()['id']}", json={"name": None})

	assert refused.status_code == 422, refused.text
	assert "cannot be cleared" in refused.text


@pytest.mark.parametrize(
	("method", "path", "body"),
	[
		("POST", "/v1/statuses",
			{"entity_type": "task", "key": "x", "label": "X", "category": "todo"}),
		(
			"POST",
			"/v1/link-types",
			{"key": "x", "title": "X", "inverse_title": "Y", "category": "describing"},
		),
		("POST", "/v1/tags", {"name": "x"}),
	],
)
def test_a_credential_without_the_verb_is_refused (
	session: sqlalchemy.orm.Session, method: str, path: str, body: dict[str, typing.Any]
) -> None:
	"""The whole point of `#826`: three permissions that gated nothing now gate this.

	Driven per verb rather than once, because they are three separate checks in three separate
	services and a single test would prove one of them.
	"""

	narrow = test_api_tasks._world(session, scopes=[subroutine.permissions.TASK_WRITE])
	refused = narrow.call(method, path, json=body)

	assert refused.status_code == 403, refused.text


def test_a_vocabulary_row_in_another_workspace_is_not_found (
	world: test_api_tasks.World,
) -> None:
	"""A 404 rather than a 403, the same choice §7.3a makes about a private project.

	Saying "forbidden" would confirm the id names something. Driven with an id that names
	nothing, which is the same answer a caller gets for one they cannot reach — and that
	sameness is the property being asserted.
	"""

	missing = world.call("PATCH", f"/v1/statuses/{uuid.uuid4()}", json={"label": "Nope"})

	assert missing.status_code == 404, missing.text


def test_a_listing_is_enveloped_like_every_other (world: test_api_tasks.World) -> None:
	"""§5.7's rule, applied before somebody has to fix it again.

	The link listing was the one bare array in this API and `#8.4` fixed it, because a caller
	cannot tell a complete set from a truncated one. ``has_more`` is always false here and that
	is a statement rather than a shrug: a vocabulary is bounded by how many somebody wrote.
	"""

	for path in ("/v1/statuses", "/v1/link-types", "/v1/tags"):
		answered = world.call("GET", path).json()

		assert "items" in answered and "page" in answered, path
		assert answered["page"]["has_more"] is False, path
		assert answered["page"]["total"] == len(answered["items"]), path
