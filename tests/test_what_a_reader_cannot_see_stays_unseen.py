"""A project or series its reader cannot see is unseen in an item's history, the feed and the journal - `SR#5148`.

**The review's L-7 bullet, widened to `/v1/changes` in its verification, and NEW-C-2**, both decided in
`#5097`. keanu's weekly series is filed in `aaa`, private to him, and its occurrence was moved to `web`
and handed to carrieanne, a member who cannot see `aaa`. The occurrence's history and the feed gave
carrieanne `aaa`'s id as where it was filed, the journal said it had been filed *nowhere*, and the
occurrence gave her the series' number.
"""

import datetime
import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.domain.events
import subroutine.domain.journal
import subroutine.domain.scoping
import subroutine.views
import test_series_out_of_reach

Place = test_series_out_of_reach.Place


@pytest.fixture(autouse=True)
def _no_watermark (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report an event the moment it is written, as ``test_events_scoping`` does (`#404`).

	The feed and the journal withhold the last second by design, and everything here is written and
	read in the same breath.
	"""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


def _moves (rows: list[dict[str, typing.Any]], ref: int) -> list[dict[str, typing.Any]]:
	"""Return the changes of every event on item ``ref`` that moved where it is filed."""

	return [
		row["changes"]["project_id"]
		for row in rows
		if row.get("item_ref") == ref
		and isinstance(row.get("changes"), dict)
		and isinstance(row["changes"].get("project_id"), dict)
		and row["changes"]["project_id"].get("from") is not None
	]


def _project_id (place: Place, key: str) -> str:
	"""Return a project's id as keanu reads it."""

	answer = place.ask(place.keanu, "GET", f"/v1/projects/{key}")

	assert answer.status_code == 200, answer.text

	return str(answer.json()["id"])


def test_the_history_and_the_feed_name_a_private_project_only_to_who_can_see_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""carrieanne is told the occurrence moved from something she cannot see to `web`; keanu, from `aaa`."""

	place = Place(session, private=True, count=None)
	aaa, web = _project_id(place, "aaa"), _project_id(place, "web")

	for path in (f"/v1/tasks/{place.ref}/events", "/v1/changes"):
		hers = place.ask(place.carrieanne, "GET", path)
		his = place.ask(place.keanu, "GET", path)

		assert hers.status_code == 200 and his.status_code == 200, (hers.text, his.text)
		assert _moves(hers.json()["items"], place.ref) == [{"from": {"visible": False}, "to": web}], path
		assert _moves(his.json()["items"], place.ref) == [{"from": aaa, "to": web}], path

	# **What is said is a copy**: the stored row still names `aaa`.
	stored = session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)).all()

	assert any(
		isinstance(row.changes, dict) and (row.changes.get("project_id") or {}).get("from") == aaa
		for row in stored
	), "the stored event was rewritten"


def test_the_journal_says_something_you_cannot_see_where_it_said_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Unquoted, because it is not a name; and keanu reads `aaa`'s."""

	place = Place(session, private=True, count=None)

	def filed (who: typing.Any) -> subroutine.views.Change:
		"""Return the journal's line for the move, as this person reads it."""

		answer = place.ask(who, "GET", "/v1/journal")

		assert answer.status_code == 200, answer.text

		(change,) = [
			change
			for entry in answer.json()["items"]
			if entry["item_ref"] == place.ref
			for change in entry["changed"]
			if change["field"] == "project_id" and change["before"] is not None
		]

		return subroutine.views.Change.model_validate(change)

	hers, his = filed(place.carrieanne), filed(place.keanu)

	assert hers.before == subroutine.domain.scoping.UNSEEN, hers
	assert ": something you cannot see to " in subroutine.views.change_in_words(hers)
	assert his.before not in (None, subroutine.domain.scoping.UNSEEN), his


def test_an_occurrence_numbers_its_series_only_to_who_can_see_it (session: sqlalchemy.orm.Session) -> None:
	"""carrieanne gets no number and the rule; the rule is what says it repeats, so she is still asked."""

	place = Place(session, private=True, count=None)
	hers = place.ask(place.carrieanne, "GET", f"/v1/tasks/{place.ref}")
	his = place.ask(place.keanu, "GET", f"/v1/tasks/{place.ref}")

	assert hers.status_code == 200 and his.status_code == 200, (hers.text, his.text)
	assert hers.json()["recurrence_template_ref"] is None, hers.json()
	assert hers.json()["recurrence_rule"] is not None, hers.json()
	assert subroutine.views.repeats(subroutine.views.Task.model_validate(hers.json()))
	assert his.json()["recurrence_template_ref"] == place.series.ref


def test_the_series_is_numbered_where_it_sits_with_its_occurrence (session: sqlalchemy.orm.Session) -> None:
	"""A series in `aaa` while `aaa` is open is numbered to carrieanne, as it always was."""

	place = Place(session, private=False, count=None)
	hers = place.ask(place.carrieanne, "GET", f"/v1/tasks/{place.ref}")

	assert hers.json()["recurrence_template_ref"] == place.series.ref, hers.json()


#: The three places carrieanne reads an item's history: its own events, the feed and the journal.
SURFACES = ("/v1/tasks/{ref}/events", "/v1/changes", "/v1/journal")


def _her_surfaces_hide (place: Place, ref: int, hidden: str) -> None:
	"""Assert ``hidden`` is on none of carrieanne's surfaces, and on keanu's events and feed."""

	for path in SURFACES:
		hers = place.ask(place.carrieanne, "GET", path.format(ref=ref))
		his = place.ask(place.keanu, "GET", path.format(ref=ref))

		assert hers.status_code == 200 and his.status_code == 200, (hers.text, his.text)
		assert hidden not in hers.text, f"{path} gave carrieanne {hidden}"

		# The journal names a row rather than giving its id, to keanu as to anybody.
		if path != "/v1/journal":
			assert hidden in his.text, f"{path} did not give keanu {hidden}, so this reads nothing"


def test_a_series_joined_in_a_private_project_is_unseen_after_it_moves (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5306`, EV1 of the cold review of 2026-11-11: the field a series is joined by.

	A one-off in `aaa` is given a repeat, whose event records ``recurrence_template_id`` from
	nothing to the series' id, and the occurrence then moves to `web` for carrieanne. Dropping the
	field from what an event may name passed seven event and journal files, and gave her the id.
	"""

	place = Place(session, private=True, count=None)
	made = place.ask(
		place.keanu,
		"POST",
		"/v1/tasks",
		json={"title": "Feed the cat", "project": "aaa", "due": "2031-12-01"},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	repeated = place.ask(
		place.keanu, "PATCH", f"/v1/tasks/{ref}", json={"recurrence": "every week"}
	)

	assert repeated.status_code == 200, repeated.text

	joined = repeated.json()["recurrence_template_ref"]
	series = place.ask(place.keanu, "GET", f"/v1/tasks/{joined}")
	moved = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{ref}",
		json={"project": "web", "assignee": place.carrieanne.username, "applies_to": "this_one"},
	)

	assert series.status_code == 200 and moved.status_code == 200, (series.text, moved.text)

	_her_surfaces_hide(place, ref, str(series.json()["id"]))


def test_a_parent_left_in_a_private_project_is_unseen_after_its_sub_task_moves (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5306`, EV2 of the cold review of 2026-11-11: the field a sub-task is joined by.

	A sub-task in `aaa` is detached from its parent, whose event records ``parent_task_id`` from
	the parent's id to nothing, and is then moved to `web` for carrieanne. Dropping the field from
	what an event may name passed the same seven files, and gave her the parent's id.
	"""

	place = Place(session, private=True, count=None)
	parent = place.ask(
		place.keanu, "POST", "/v1/tasks", json={"title": "Plan the move", "project": "aaa"}
	)

	assert parent.status_code == 201, parent.text

	child = place.ask(
		place.keanu,
		"POST",
		"/v1/tasks",
		json={"title": "Book the van", "project": "aaa", "parent_task_id": parent.json()["ref"]},
	)

	assert child.status_code == 201, child.text

	ref = child.json()["ref"]
	detached = place.ask(place.keanu, "POST", f"/v1/tasks/{ref}/move", json={"parent": None})
	moved = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{ref}",
		json={"project": "web", "assignee": place.carrieanne.username},
	)

	assert detached.status_code == 200 and moved.status_code == 200, (detached.text, moved.text)

	_her_surfaces_hide(place, ref, str(parent.json()["id"]))


def test_the_fields_that_name_a_row_are_the_ones_the_journal_names_by_project_or_task () -> None:
	"""One declaration, read by both: the journal's lookups for these are the events' own."""

	journal = subroutine.domain.journal
	named = {
		field: lookup
		for field, lookup in journal.NAMED_BY.items()
		if lookup in (journal.PROJECT, journal.TASK)
	}

	assert named == subroutine.domain.events.NAMES_A_ROW
