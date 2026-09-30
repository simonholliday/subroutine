"""Events are left out where a listing asks, and back wherever the request names them - `SR#3704`.

Decision `SR#3807`: an event - a birthday, a payday, a thing that happens to you (`SR#1235`) - is
not work, and a list holding a year of them is one nobody reads. ``events=exclude`` leaves them out,
``only`` lists just them so a listing can count what it left out, and ``include``, the default,
changes nothing for a caller reading the API directly.

**A request naming them brings them back**, by the rule that already decides it for finished work
and for retired documents: an event type or ``type_category``, the trash, or when something was
touched. **And a search finds them**, by its words or an item's number.
"""

import typing

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.vocabulary
import test_api_tasks


class Listed(typing.NamedTuple):
	"""An instance holding one event and one piece of work, and the number of each."""

	world: test_api_tasks.World
	event: int
	work: int


@pytest.fixture
def listed (session: sqlalchemy.orm.Session) -> Listed:
	"""Make a payday and a piece of work beside it."""

	world = test_api_tasks._world(session)
	work = world.call("POST", "/v1/tasks", json={"title": "Fix the footer"})
	event = world.call(
		"POST", "/v1/tasks", json={"title": "Payday", "type": "event", "starts": "2026-10-01"}
	)

	assert work.status_code == 201 and event.status_code == 201, (work.text, event.text)

	return Listed(world=world, event=event.json()["ref"], work=work.json()["ref"])


def _refs (listed: Listed, **params: typing.Any) -> list[int]:
	"""Return the numbers ``GET /v1/tasks`` lists for these parameters, of the two made here."""

	answered = listed.world.call("GET", "/v1/tasks", params=params)

	assert answered.status_code == 200, answered.text

	return sorted(
		row["ref"] for row in answered.json()["items"] if row["ref"] in (listed.event, listed.work)
	)


def test_events_are_listed_unless_a_request_leaves_them_out (listed: Listed) -> None:
	"""``include`` by default, so nothing reading the API changes; ``exclude`` and ``only`` split."""

	assert _refs(listed) == sorted([listed.event, listed.work])
	assert _refs(listed, events="include") == sorted([listed.event, listed.work])
	assert _refs(listed, events="exclude") == [listed.work]
	assert _refs(listed, events="only") == [listed.event]


def test_an_unknown_way_to_treat_events_is_refused_by_name (listed: Listed) -> None:
	"""A typo is refused naming the field and the three words, rather than read as the default."""

	answered = listed.world.call("GET", "/v1/tasks", params={"events": "sometimes"})

	assert answered.status_code == 422, answered.text
	# **Named as the part of the request it was**, as every parameter refused over HTTP is.
	assert [one["field"] for one in answered.json()["errors"]] == ["query.events"], answered.text
	assert "include, exclude, only" in answered.text, answered.text


@pytest.mark.parametrize(
	"naming",
	[
		{"type": "event"},
		{"type.eq": "event"},
		{"type_category.eq": "occasion"},
		{"q": "type_category:occasion"},
		{"touched_at.gte": "today"},
	],
	ids=["the type", "the type, dotted", "the category", "the category, in a search", "activity"],
)
def test_a_request_naming_events_brings_them_back (
	listed: Listed, naming: dict[str, str]
) -> None:
	"""`SR#3807`: *exclude* gives way wherever the request names events, and nothing is refused."""

	assert listed.event in _refs(listed, events="exclude", **naming)


@pytest.mark.parametrize("looking", ["payday", "number"])
def test_a_search_finds_events_where_a_list_leaves_them_out (listed: Listed, looking: str) -> None:
	"""`SR#3807`: *search still finds them*, a search being for something specific - by its words,
	or by its number, which is a lookup rather than a filter (`SR#873`)."""

	words = "payday" if looking == "payday" else str(listed.event)

	assert listed.event in _refs(listed, events="exclude", q=words)


def test_the_trash_holds_events_too (listed: Listed) -> None:
	"""Asking what was deleted is not asking about the kind of thing (`SR#900`)."""

	deleted = listed.world.call("DELETE", f"/v1/tasks/{listed.event}")

	assert deleted.status_code in (200, 204), deleted.text
	assert _refs(listed, events="exclude", deleted="true") == [listed.event]


def test_a_request_about_something_else_leaves_them_out (listed: Listed) -> None:
	"""Naming a status category says nothing about events, so *exclude* still leaves them out."""

	assert _refs(listed, events="exclude", **{"status_category.eq": "todo"}) == [listed.work]


def test_type_category_narrows_by_the_kind_of_type (listed: Listed) -> None:
	"""`type_category` is a filter beside ``status_category``, with the product's own words."""

	assert _refs(listed, **{"type_category.eq": "occasion"}) == [listed.event]
	assert _refs(listed, **{"type_category.ne": "occasion"}) == [listed.work]
	assert _refs(listed, **{"type_category.in": "work,occasion"}) == sorted(
		[listed.event, listed.work]
	)


def test_an_unknown_type_category_is_refused_listing_the_words (listed: Listed) -> None:
	"""The set is the product's, so the refusal can list all of it."""

	answered = listed.world.call("GET", "/v1/tasks", params={"type_category.eq": "birthday"})

	assert answered.status_code == 422, answered.text
	assert "work, defect, question, occasion, target" in answered.text, answered.text


def test_a_workspaces_own_kind_of_event_is_left_out_and_named_with_them (
	listed: Listed,
) -> None:
	"""`SR#1236` chose the category over the key, so a workspace's holiday is an event too."""

	listed.world.session.add(
		subroutine.db.models.vocabulary.ItemType(
			workspace_id=listed.world.workspace.id,
			entity_type="task",
			key="holiday",
			label="Holiday",
			category="occasion",
			position=99,
		)
	)
	listed.world.session.flush()

	made = listed.world.call(
		"POST", "/v1/tasks", json={"title": "Dawlish", "type": "holiday", "starts": "2026-10-12"}
	)

	assert made.status_code == 201, made.text

	holiday = made.json()["ref"]
	left = listed.world.call("GET", "/v1/tasks", params={"events": "exclude"}).json()["items"]
	named = listed.world.call(
		"GET", "/v1/tasks", params={"events": "exclude", "type_category.eq": "occasion"}
	).json()["items"]

	assert holiday not in [row["ref"] for row in left], left
	assert holiday in [row["ref"] for row in named], named


def test_both_transports_leave_out_and_bring_back_the_same_events (listed: Listed) -> None:
	"""The rule is asked of the domain by both, so the terminal and the API cannot disagree."""

	world = listed.world
	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(world.session),
		token=world.secret,
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://work.example.com"),
		token=world.secret,
		transport=api_support.SyncTransport(world.application),
		base_url=api_support.BASE_URL,
	)
	asked: list[dict[str, typing.Any]] = [
		{"events": "exclude"},
		{"events": "only"},
		{"events": "exclude", "type": "event"},
		{"events": "exclude", "filters": [("type_category.eq", "occasion")]},
		{"filters": [("type_category.ne", "occasion")]},
	]

	with local, remote:
		for one in asked:
			answers = [
				sorted(
					row.ref
					for row in client.tasks(limit=50, **one)
					if row.ref in (listed.event, listed.work)
				)
				for client in (local, remote)
			]

			assert answers[0] == answers[1], (one, answers)
