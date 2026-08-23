"""What moves ``content_updated_at``, asked field by field from one named list.

The rule under test is `#1112`'s: a change of **content** is a change to what an item is and
what it asks of you; everything else is bookkeeping. It is worth a module of its own because it
is one rule applied to two entities by two services, and because the previous version of it —
`tests/test_api_tasks.py::test_a_task_reports_when_its_meaning_last_changed` — exercised one
field from each side of the line and so was green while a deadline change was silently
bookkeeping for as long as the column had existed.

**The completeness half is what makes this survive the next column.** A field the comparison can
produce and neither set names fails the build, so adding one is a classification rather than a
default; and a set naming a field the comparison cannot produce fails too, which is what makes
deleting a column delete its entry.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.tasks
import test_api_tasks

#: Stands in for the account the fixture bootstraps, whose username is generated per test.
_ME = "<the caller>"


#: One field, the ``PATCH`` that changes it, and whatever has to be true first.
#:
#: **Requests rather than service calls**, deliberately: the rule lives below the endpoint, and
#: a test calling the service directly would be blind to a field the API cannot reach — which is
#: the shape of `#1112` itself, where a specification and an implementation disagreed for the
#: life of the column and nothing drove either.
#:
#: The setup is why this is a table of pairs rather than of patches. Three of the flags refuse
#: outright when there is no date for them to describe, and clearing an assignee nobody set
#: changes nothing — so a bare patch would leave four fields silently proving that a refused
#: request moves no timestamp.
TASK_EDITS: dict[str, tuple[dict[str, typing.Any], dict[str, typing.Any]]] = {
	"title": ({}, {"title": "A different title"}),
	"description": ({}, {"description": "Some words that were not there"}),
	"type_id": ({}, {"type": "bug"}),
	"status_id": ({}, {"status": "in_progress"}),
	"due_at": ({}, {"due": "tomorrow"}),
	"due_is_all_day": ({"due": "tomorrow"}, {"due_is_all_day": False}),
	# The account's own username rather than `me`: the *listing* accepts that word and the
	# body does not, which is a real asymmetry and not this module's to settle.
	"assignee_id": ({}, {"assignee": _ME}),
	"importance": ({}, {"importance": 3}),
	"urgency": ({}, {"urgency": 4}),
	"estimate_minutes": ({}, {"estimate": "2h"}),
	"starts_at": ({}, {"starts": "tomorrow"}),
	"starts_is_all_day": ({"starts": "tomorrow"}, {"starts_is_all_day": False}),
	"snoozed_until": ({}, {"snooze": "tomorrow"}),
	"snoozed_is_all_day": ({"snooze": "tomorrow"}, {"snoozed_is_all_day": False}),
	"tags": ({}, {"tags": ["ops"]}),
	# `project_id`, `completed_at` and `timezone` have their own tests below: one needs a
	# project to move to, one is derived from the status and arrives by its own verb, and one
	# moves only as a side effect of re-dating something from another zone.
}

DOCUMENT_EDITS: dict[str, tuple[dict[str, typing.Any], dict[str, typing.Any]]] = {
	"title": ({}, {"title": "A different title"}),
	"body": ({}, {"body": "Some words that were not there"}),
	"status_id": ({}, {"status": "active"}),
	"type_id": ({}, {"type": "decision"}),
	"owner_id": ({}, {"owner_id": None}),
	"tags": ({}, {"tags": ["ops"]}),
	# `project_id` and `supersedes_id` have their own tests below: each needs a second row.
}


def _stamp (world: test_api_tasks.World, path: str) -> str:
	"""Read an item's content stamp."""

	return str(world.call("GET", path).json()["content_updated_at"])


def _drove (world: test_api_tasks.World, path: str, patch: dict[str, typing.Any]) -> bool:
	"""Apply a patch and say whether it moved the content stamp."""

	before = _stamp(world, path)
	response = world.call("PATCH", path, json=patch)

	assert response.status_code == 200, f"{patch} was refused: {response.text}"

	return _stamp(world, path) != before


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP."""

	return test_api_tasks._world(session)


@pytest.mark.parametrize("field", sorted(TASK_EDITS))
def test_a_task_field_moves_the_content_stamp_exactly_when_it_is_content (
	world: test_api_tasks.World, field: str
) -> None:
	"""Every field, against the list, in both directions.

	Parametrised per field rather than looped, so a failure names the field rather than the
	first field to disagree — and so the count is visible, which is what tells a reader the
	walk ran at all.
	"""

	setup, patch = TASK_EDITS[field]
	ref = world.call("POST", "/v1/tasks", json={"title": "Subject"}).json()["ref"]
	wanted = field in subroutine.domain.events.CONTENT_FIELDS["task"]

	if setup:
		assert world.call("PATCH", f"/v1/tasks/{ref}", json=setup).status_code == 200

	patch = {
		key: world.user.username if value == _ME else value for key, value in patch.items()
	}

	assert _drove(world, f"/v1/tasks/{ref}", patch) is wanted, (
		f"{field} is classified as {'content' if wanted else 'bookkeeping'} and behaved as the other"
	)


@pytest.mark.parametrize("field", sorted(DOCUMENT_EDITS))
def test_a_document_field_moves_the_content_stamp_exactly_when_it_is_content (
	world: test_api_tasks.World, field: str
) -> None:
	"""The same question of the other entity, which had its own shorter copy of the rule."""

	setup, patch = DOCUMENT_EDITS[field]
	ref = world.call(
		"POST", "/v1/documents", json={"title": "Subject", "body": "Words"}
	).json()["ref"]
	wanted = field in subroutine.domain.events.CONTENT_FIELDS["document"]

	if setup:
		assert world.call("PATCH", f"/v1/documents/{ref}", json=setup).status_code == 200

	assert _drove(world, f"/v1/documents/{ref}", patch) is wanted, (
		f"{field} is classified as {'content' if wanted else 'bookkeeping'} and behaved as the other"
	)


def test_moving_a_task_to_another_project_is_not_a_change_of_meaning (
	world: test_api_tasks.World,
) -> None:
	"""`project_id`, which needs somewhere to move to and so cannot join the table above."""

	world.call("POST", "/v1/projects", json={"key": "elsewhere", "title": "Elsewhere"})
	ref = world.call("POST", "/v1/tasks", json={"title": "Subject"}).json()["ref"]

	assert not _drove(world, f"/v1/tasks/{ref}", {"project": "elsewhere"})


def test_completing_a_task_is_a_change_of_meaning (world: test_api_tasks.World) -> None:
	"""`completed_at` is derived, so this is the status entry arriving by its own verb.

	Worth driving separately because `complete` is a wrapper rather than a `PATCH`, and a rule
	that held for the endpoint and not for the verb would be exactly the split this module
	exists to refuse.
	"""

	ref = world.call("POST", "/v1/tasks", json={"title": "Subject"}).json()["ref"]
	before = _stamp(world, f"/v1/tasks/{ref}")
	world.call("POST", f"/v1/tasks/{ref}/complete")

	assert _stamp(world, f"/v1/tasks/{ref}") != before


def test_a_zone_with_no_date_to_interpret_changes_nothing_at_all (
	world: test_api_tasks.World,
) -> None:
	"""`timezone`, which is why it cannot have a row in the table.

	The column records *the zone the dates were authored in* (`#1014`), so it is written only
	when a date is written — and a request naming a zone and no date has authored nothing.
	That makes it unreachable on its own in either direction, which is a stronger statement
	than classifying it and is the reason its entry in `DRIVEN_ELSEWHERE` says so.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Subject"}).json()
	response = world.call(
		"PATCH", f"/v1/tasks/{made['ref']}", json={"timezone": "Australia/Sydney"}
	)

	assert response.status_code == 200
	assert response.json()["version"] == made["version"], "a zone alone wrote something"
	assert _stamp(world, f"/v1/tasks/{made['ref']}") == made["content_updated_at"]


def test_a_resent_field_that_did_not_change_is_not_a_change_of_meaning (
	world: test_api_tasks.World,
) -> None:
	"""`#1140`. The rule reads what changed, not what was named.

	A client that reads an item, edits one field and sends the whole object back names its
	title in every request — so asking "was a title given" recorded a change of meaning on
	every bookkeeping write such a client made. The single-field case cannot see it: an update
	that changes nothing returns before the rule is reached at all.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Subject"}).json()
	before = _stamp(world, f"/v1/tasks/{made['ref']}")

	response = world.call(
		"PATCH",
		f"/v1/tasks/{made['ref']}",
		json={"title": made["title"], "description": None, "importance": 2},
	)

	assert response.status_code == 200
	assert response.json()["version"] > made["version"], "nothing changed, so nothing was proved"
	assert _stamp(world, f"/v1/tasks/{made['ref']}") == before


def _comparable (session: sqlalchemy.orm.Session, world: test_api_tasks.World) -> dict[str, set[str]]:
	"""Return, per entity, every field name the change comparison can report.

	Read from the services rather than listed, so this cannot fall behind them — which is the
	whole failure mode. A task's set is `_snapshot`'s keys; a document builds its changes
	inline, so its set is read from the assignment loop's own tuple.
	"""

	task = subroutine.db.models.work.Task(title="x", workspace_id=world.workspace.id)

	return {
		"task": set(subroutine.domain.tasks._snapshot(session, task)),
		"document": set(subroutine.domain.documents.COMPARED),
	}


def test_every_field_a_change_can_name_is_classified (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""A new column is a decision, not a default.

	This is the half that survives the next field. `due_at` was specified as content in two
	places and implemented as bookkeeping, and nothing could see it because the classification
	lived in six scattered assignments with no list to be incomplete against.
	"""

	for entity, fields in _comparable(session, world).items():
		content = subroutine.domain.events.CONTENT_FIELDS[entity]
		bookkeeping = subroutine.domain.events.BOOKKEEPING_FIELDS[entity]

		assert not (content & bookkeeping), f"{entity}: a field cannot be both"
		assert fields - (content | bookkeeping) == set(), (
			f"{entity}: unclassified — add each to CONTENT_FIELDS or BOOKKEEPING_FIELDS"
		)


def test_nothing_is_classified_that_a_change_can_no_longer_name (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""The other direction, so deleting a column deletes its entry.

	Every register in this repository is asked what makes its entries go away; a classification
	that outlives its column reads as a considered decision about a field nobody has.
	"""

	for entity, fields in _comparable(session, world).items():
		named = (
			subroutine.domain.events.CONTENT_FIELDS[entity]
			| subroutine.domain.events.BOOKKEEPING_FIELDS[entity]
		)

		assert named - fields == set(), f"{entity}: classified but never compared"


def test_filing_a_document_elsewhere_is_not_a_change_of_meaning (
	world: test_api_tasks.World,
) -> None:
	"""`project_id`, which needs somewhere to file it."""

	world.call("POST", "/v1/projects", json={"key": "elsewhere", "title": "Elsewhere"})
	ref = world.call(
		"POST", "/v1/documents", json={"title": "Subject", "body": "Words"}
	).json()["ref"]

	assert not _drove(world, f"/v1/documents/{ref}", {"project": "elsewhere"})


def test_superseding_is_a_change_of_meaning_for_the_document_that_stops_being_current (
	world: test_api_tasks.World,
) -> None:
	"""`supersedes_id`, and the asymmetry is the point.

	Saying *this replaces that* is a relationship, and the successor's own words are unchanged
	— so its content stamp holds. What moves is the **predecessor's**, because its status
	becomes `superseded` and a decision that has stopped being in force means something
	different to anybody reading it.
	"""

	old = world.call(
		"POST", "/v1/documents", json={"title": "First", "body": "Words", "type": "decision"}
	).json()
	replacement = world.call(
		"POST", "/v1/documents", json={"title": "Second", "body": "Better", "type": "decision"}
	).json()
	was = _stamp(world, f"/v1/documents/{old['ref']}")

	assert not _drove(
		world, f"/v1/documents/{replacement['ref']}", {"supersedes": old["ref"]}
	)
	assert _stamp(world, f"/v1/documents/{old['ref']}") != was


def test_every_field_a_document_declares_as_compared_can_really_be_produced (
	world: test_api_tasks.World,
) -> None:
	"""`documents.COMPARED` is a declaration, so this is what stops it drifting.

	A task's comparable set is read off `_snapshot`, which cannot be wrong about itself; a
	document's is written out by hand, and a written list of what a function does is the thing
	this project keeps finding stale. So every name in it is driven and the emitted event is
	asked whether it carries that name — which makes an entry for a field nobody can change
	fail, and leaves only the opposite direction relying on the two being edited together.
	"""

	world.call("POST", "/v1/projects", json={"key": "elsewhere", "title": "Elsewhere"})
	predecessor = world.call(
		"POST", "/v1/documents", json={"title": "First", "body": "Words", "type": "decision"}
	).json()
	subject = world.call(
		"POST", "/v1/documents", json={"title": "Subject", "body": "Words"}
	).json()
	seen: set[str] = set()

	for patch in (
		*(patch for _setup, patch in DOCUMENT_EDITS.values()),
		{"project": "elsewhere"},
		{"supersedes": predecessor["ref"]},
	):
		assert world.call(
			"PATCH", f"/v1/documents/{subject['ref']}", json=patch
		).status_code == 200, patch

	# **Both documents**, because superseding writes to the one being retired as well — which
	# is the path `COMPARED` is most likely to fall behind, since it is the one that does not
	# go through `update`.
	for ref in (subject["ref"], predecessor["ref"]):
		history = world.call("GET", f"/v1/documents/{ref}/events").json()

		# `updated` only: a `created` event carries the row it wrote, `ref` included, and this
		# is a question about what an *edit* can report.
		for event in history["items"]:
			if event["action"] == "updated":
				seen.update(event.get("changes") or {})

	assert seen == set(subroutine.domain.documents.COMPARED), (
		"COMPARED names a field no request can produce, or a request produced one it omits"
	)


#: Fields with a test of their own rather than a row in the tables above, and why.
#:
#: Each needs something a one-field patch cannot express — a second row to move to, a verb
#: rather than a `PATCH`, or a side effect of another field. Written out so that a field
#: arriving here silently, because nobody wrote a case for it, fails instead.
DRIVEN_ELSEWHERE: dict[str, dict[str, str]] = {
	"task": {
		"project_id": "needs a project to move to",
		"completed_at": "derived from the status, and reached by its own verb",
		"timezone": "unreachable alone — written only when a date is, which carries the answer",
	},
	"document": {
		"project_id": "needs a project to file it under",
		"supersedes_id": "needs a second document to replace",
		"superseded_by": "written on the *other* document, by the path that retires it",
	},
}


def test_the_tables_above_cover_every_field_this_module_can_drive (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""And the *tests* are asked the same question, so a new field gets a case as well.

	Without this the two registers stay complete while the behavioural half quietly samples
	fewer and fewer of them — a guard whose fixture stops being representative, which is how
	the test this module replaces came to prove nothing about deadlines.
	"""

	tables = {"task": set(TASK_EDITS), "document": set(DOCUMENT_EDITS)}

	for entity, fields in _comparable(session, world).items():
		assert tables[entity] | set(DRIVEN_ELSEWHERE[entity]) == fields, (
			f"{entity}: add a case to the table, or an entry to DRIVEN_ELSEWHERE saying why not"
		)
