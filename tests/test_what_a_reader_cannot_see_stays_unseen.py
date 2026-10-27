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


def test_the_fields_that_name_a_row_are_the_ones_the_journal_names_by_project_or_task () -> None:
	"""One declaration, read by both: the journal's lookups for these are the events' own."""

	journal = subroutine.domain.journal
	named = {
		field: lookup
		for field, lookup in journal.NAMED_BY.items()
		if lookup in (journal.PROJECT, journal.TASK)
	}

	assert named == subroutine.domain.events.NAMES_A_ROW
