"""Odd values a caller can send, each refused by name - `SR#3933`.

**Every one answered 500 on at least one backend**, or was stored by SQLite where PostgreSQL
refused it - a row ``db copy`` cannot move. The cold review of 2026-09-28 found them by probing
both backends, and the engine fixture runs every case here on both.

**Each refusal is asserted to name what was wrong**, since a 422 that names nothing is the
vague answer these replace, one status code better.
"""

import datetime
import json
import typing

import pytest
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.api.mcp
import subroutine.db.failures
import subroutine.db.models.vocabulary
import subroutine.domain.agenda
import subroutine.domain.instances
import subroutine.errors
import subroutine.mcp.protocol
import test_api_tasks

#: A day to build agendas from, so a horizon reaching the calendar's end reaches it exactly,
#: whatever day the suite runs on.
SOME_DAY = datetime.date(2026, 9, 29)

#: Days from :data:`SOME_DAY` to the last day a date can hold.
TO_THE_LAST_DAY = (datetime.date(9999, 12, 31) - SOME_DAY).days


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, with one task to hang a verification off."""

	built = test_api_tasks._world(session)
	made = built.call("POST", "/v1/tasks", json={"title": "Water the plants"})

	assert made.status_code == 201, made.text

	return built


def _named (answered: typing.Any, field: str, status: int = 422) -> None:
	"""Assert an answer refused the request with ``status``, naming ``field``.

	**Named where it was sent**: a field of the body bare, and a parameter as ``query.<name>``
	or ``path.<name>``, which is how this API tells the two halves of a request apart.
	"""

	assert answered.status_code == status, answered.text

	named = [one["field"] for one in answered.json().get("errors") or []]

	assert {field, f"query.{field}", f"path.{field}"} & set(named), (field, answered.text)


@pytest.mark.parametrize(
	("params", "field"),
	[
		({"horizon_days": 4_000_000}, "horizon_days"),
		({"horizon_days": 2**63}, "horizon_days"),
		(
			{
				"date": SOME_DAY.isoformat(),
				"horizon_days": TO_THE_LAST_DAY,
				"timezone": "America/Los_Angeles",
			},
			"horizon_days",
		),
		({"date": "9999-12-31", "timezone": "America/Los_Angeles"}, "date"),
		({"date": "9999-12-31", "timezone": "UTC"}, "date"),
		(
			{"date": "9999-12-30", "horizon_days": 1, "timezone": "America/Los_Angeles"},
			"horizon_days",
		),
	],
	ids=[
		"four-million-days",
		"past-what-a-span-holds",
		"a-horizon-onto-the-last-day",
		"the-last-day-west-of-utc",
		"the-last-day-in-utc",
		"a-day-before-it-and-one-more",
	],
)
def test_an_agenda_reaching_past_the_calendar_is_refused_by_name (
	world: test_api_tasks.World, params: dict[str, typing.Any], field: str
) -> None:
	"""A look-ahead of four million days, and one landing on 31 December 9999 west of UTC,
	were 500s: the end of that day there is in the year 10000 in UTC.

	**The last two are the review's case through the other parameter**: an agenda for the last
	day itself, or one looking a day ahead onto it, was the same 500 however short the horizon.
	The day is held to every clock, as an item's dates are, so UTC's last day is refused too.
	"""

	_named(world.call("GET", "/v1/agenda", params=params), field)


def test_an_agenda_near_the_end_of_the_calendar_is_still_built (
	world: test_api_tasks.World,
) -> None:
	"""The positive twin: a day every clock can show is answered, with a whole-day item on it
	written in the zone furthest ahead."""

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Last call", "due": "9999-12-29", "timezone": "Pacific/Kiritimati"},
	)

	assert made.status_code == 201, made.text

	built = world.call(
		"GET",
		"/v1/agenda",
		params={"date": "9999-12-29", "horizon_days": 1, "timezone": "America/Los_Angeles"},
	)

	assert built.status_code == 200, built.text

	# **And the answer is the right one** (`SR#4030`, L-10 of the cold review of 2026-09-30): the
	# defect was a 500, and a 200 alone would pass a wrong one. The day asked, every bucket there,
	# the deadline today and nowhere else, and nothing late or past the calendar.
	body = built.json()
	buckets = {name: [row["ref"] for row in body.get(name, [])] for name in subroutine.domain.agenda.BUCKETS}
	holding = [name for name, refs in buckets.items() if made.json()["ref"] in refs]

	assert (body["date"], body["timezone"]) == ("9999-12-29", "America/Los_Angeles"), body
	assert set(subroutine.domain.agenda.BUCKETS) <= set(body), sorted(body)
	assert holding == ["today"], buckets
	assert (buckets["overdue"], buckets["upcoming"]) == ([], []), buckets


@pytest.mark.parametrize(
	"number",
	# **The first number past each end** (`SR#4030`, gap 2 of L-10 of the cold review of 2026-09-30),
	# so a bound one out either way is refused: 40,000 is far past, and let 2**15 through.
	[2**15, -(2**15) - 1, 40_000, 2**31, 2**63, -(2**63) - 1],
)
def test_a_rank_filter_past_what_the_column_holds_is_refused_by_name (
	world: test_api_tasks.World, number: int
) -> None:
	"""A rank is a ``smallint``: past it, PostgreSQL answered 500 from 40000 and both backends
	from 2**63, where SQLite answered 200 for everything short of that."""

	_named(world.call("GET", "/v1/tasks", params={"importance.gt": number}), "importance")


@pytest.mark.parametrize("number", [2**15 - 1, -(2**15)])
def test_a_rank_filter_at_the_edge_of_the_column_is_answered (
	world: test_api_tasks.World, number: int
) -> None:
	"""The positive twin: the largest number the column holds is a question with an answer, and the
	smallest (`SR#4030`, gap 2), so a bound moved in by one is caught at either end."""

	answered = world.call("GET", "/v1/tasks", params={"urgency.lte": number})

	assert answered.status_code == 200, answered.text


@pytest.mark.parametrize(("field", "seq"), [("since", 2**63), ("since", 2**64), ("before", 2**63)])
def test_a_feed_bound_past_the_largest_seq_is_refused_by_name (
	world: test_api_tasks.World, field: str, seq: int
) -> None:
	"""``seq`` is a 64-bit integer, and a cursor past it could not even be bound: a 500 on both
	backends from 2**63."""

	_named(world.call("GET", "/v1/changes", params={field: seq}), field)


def test_a_feed_resumed_from_the_largest_seq_is_answered (world: test_api_tasks.World) -> None:
	"""The positive twin: the last seq there could be names nothing yet, which is an answer."""

	answered = world.call("GET", "/v1/changes", params={"since": 2**63 - 1})

	assert answered.status_code == 200, answered.text


@pytest.mark.parametrize(
	("path", "params", "field"),
	[
		("/v1/tasks", {"tag": "\x00"}, "tag"),
		("/v1/tasks", {"type": "\x00"}, "type"),
		("/v1/tasks", {"status": "\x00"}, "status"),
		("/v1/tasks", {"project": "\x00"}, "project"),
		("/v1/tasks", {"assignee": "\x00"}, "assignee"),
		("/v1/documents", {"tag": "\x00"}, "tag"),
		("/v1/changes", {"actor": "\x00"}, "actor"),
		("/v1/users/%00", None, "username"),
	],
)
def test_a_nul_asked_about_is_refused_by_name (
	world: test_api_tasks.World,
	path: str,
	params: dict[str, str] | None,
	field: str,
) -> None:
	"""A NUL in a listing's filter, or in a path, was a 500 on PostgreSQL - which will not bind
	one - and an answer on SQLite. Refused by name on both now, before anything is read."""

	_named(world.call("GET", path, params=params), field)


def test_a_nul_in_a_search_term_is_searched_for_and_said (world: test_api_tasks.World) -> None:
	"""``q=tag:%00`` compiled to a lookup PostgreSQL would not bind: a 500 there, where SQLite
	found no tag called that. A search line refuses nothing, so the term is searched for as text
	and reported, on both backends alike."""

	answered = world.call("GET", "/v1/tasks", params={"q": "tag:\x00"})

	assert answered.status_code == 200, answered.text
	assert "not text" in " ".join(answered.json()["page"]["unread"] or []), answered.text


@pytest.mark.parametrize("field", ["assignee", "project", "type", "status"])
def test_a_nul_in_a_name_to_look_something_up_by_is_refused (
	world: test_api_tasks.World, field: str
) -> None:
	"""The same NUL in a body: a 500 on PostgreSQL, where no query could bind it.

	**Refused by name on both backends** (`SR#4287`), since each lookup now reads the name it is
	given before asking the database. It was refused without the field's name on PostgreSQL, by
	the backstop that knows a value held a NUL and not which, and SQLite said nothing was called
	that.
	"""

	answered = world.call("POST", "/v1/tasks", json={"title": "Fine", field: "a\x00b"})

	assert answered.status_code == 422, answered.text
	assert answered.json()["errors"][0]["field"] == field, answered.text


@pytest.mark.parametrize(
	("path", "body", "field"),
	[
		("/v1/tags", {"name": "ops", "description": "a\x00b"}, "description"),
		("/v1/tasks/1/verifications", {"passed": True, "summary": "a\x00b"}, "summary"),
		(
			"/v1/tasks/1/verifications",
			{"passed": True, "output_excerpt": "a\x00b"},
			"output_excerpt",
		),
	],
)
def test_a_nul_in_text_somebody_wrote_is_refused_by_name (
	world: test_api_tasks.World, path: str, body: dict[str, typing.Any], field: str
) -> None:
	"""SQLite stored these and PostgreSQL answered 500: a tag's description, and what a
	verification says of the check it records."""

	_named(world.call("POST", path, json=body), field)


def test_a_nul_in_a_tag_description_is_refused_on_an_edit_too (
	world: test_api_tasks.World,
) -> None:
	"""The second writer of the same column."""

	made = world.call("POST", "/v1/tags", json={"name": "ops"})

	assert made.status_code == 201, made.text

	_named(
		world.call("PATCH", f"/v1/tags/{made.json()['id']}", json={"description": "a\x00b"}),
		"description",
	)


def test_an_instance_name_longer_than_its_column_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""The one unbounded name of the twenty-one the review probed: 300 characters were stored by
	SQLite and a 500 on PostgreSQL."""

	_named(world.call("PATCH", "/v1/instance", json={"name": "x" * 256}), "name", status=413)


def test_an_instance_named_longer_than_its_column_at_init_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The column's first writer, ``init --instance-name``, held to the same width."""

	with pytest.raises(subroutine.errors.PayloadTooLarge) as refused:
		subroutine.domain.instances.establish(session, name="x" * 256)

	assert "instance_name" in str(refused.value.errors), refused.value


@pytest.mark.parametrize(
	("method", "path", "body"),
	[
		("PATCH", "/v1/instance", {"timezone": "A" * 300}),
		("POST", "/v1/workspaces", {"slug": "acme", "title": "Acme", "timezone": "A" * 300}),
	],
)
def test_a_zone_name_longer_than_a_filename_is_refused_by_name (
	world: test_api_tasks.World, method: str, path: str, body: dict[str, str]
) -> None:
	"""Looked for as a file, and refused by the system before the zone database was asked: a
	500 on every surface that takes a zone."""

	_named(world.call(method, path, json=body), "timezone")


def test_an_estimate_in_a_digit_int_cannot_read_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""``²`` is a digit to ``str.isdigit`` and not to ``int``, so it was a 500."""

	_named(world.call("POST", "/v1/tasks", json={"title": "Fine", "estimate": "²"}), "estimate")


@pytest.mark.parametrize(
	"estimate",
	["9" * 4301 + "m", "9" * 4301, "0" * 5000 + "999999999999m"],
	ids=["nines and a unit", "nines", "zeros and then too many digits"],
)
def test_an_estimate_longer_than_a_number_can_be_read_is_refused_by_name (
	world: test_api_tasks.World, estimate: str
) -> None:
	"""`SR#4027`, L-3 (7) of the cold review of 2026-09-30: 4,301 nines answered 500.

	``int`` refuses more than 4,300 digits with an error of its own, leading zeros counted. **Refused
	by name, saying how many digits** rather than quoting them all back.
	"""

	answered = world.call("POST", "/v1/tasks", json={"title": "Fine", "estimate": estimate})

	_named(answered, "estimate")

	assert len(answered.text) < 2_000, f"the refusal quoted the digits back: {len(answered.text)}"


def test_an_estimate_padded_with_zeros_is_still_read (world: test_api_tasks.World) -> None:
	"""`SR#4027`'s other side: leading zeros are not what makes a number too long to read."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fine", "estimate": "0" * 5000 + "90m"})

	assert made.status_code == 201 and made.json()["estimate_minutes"] == 90, made.text


def test_half_a_character_is_refused_by_name (world: test_api_tasks.World) -> None:
	"""`SR#4027`, L-4 (1) of the cold review of 2026-09-30: a lone surrogate answered 500.

	``\\ud800`` is half of a character, sent as a JSON escape, and cannot be written as UTF-8. It
	passed every text check, since none asked about it. **Refused as a control character is.**
	"""

	answered = world.call(
		"POST",
		"/v1/tasks",
		content=b'{"title": "Plan \\ud800 it"}',
		headers={"content-type": "application/json"},
	)

	_named(answered, "title")

	assert "half of a character" in answered.json()["detail"], answered.text


@pytest.mark.parametrize(
	("path", "body", "field"),
	[
		("/v1/tasks", {"title": "Plan", "tags": ["İ" * 65]}, "tags"),
		("/v1/users", {"username": "İ" * 40}, "username"),
	],
	ids=["a tag", "a username"],
)
def test_a_name_too_long_once_written_in_lower_case_is_refused_by_name (
	world: test_api_tasks.World, path: str, body: dict[str, typing.Any], field: str
) -> None:
	"""`SR#4027`, L-4 (2) of the cold review of 2026-09-30: ``İ`` fits and its lower case does not.

	A name is stored as written and lower-cased for comparing, both as wide, and ``İ`` lower-cases to
	two characters: 65 in a tag and 40 in a username answered 500 on PostgreSQL and were stored by
	SQLite. **Refused by name on both.**
	"""

	_named(world.call("POST", path, json=body), field, status=413)


def test_a_tag_renamed_too_long_once_written_in_lower_case_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""The second door to the same column: renaming a tag."""

	made = world.call("POST", "/v1/tags", json={"name": "ops"})

	assert made.status_code == 201, made.text

	_named(
		world.call("PATCH", f"/v1/tags/{made.json()['id']}", json={"name": "İ" * 65}),
		"name",
		status=413,
	)


def test_an_estimate_in_another_script_s_decimal_digits_is_still_read (
	world: test_api_tasks.World,
) -> None:
	"""The positive twin, and the line the fix draws: a decimal digit in any script reads as the
	number it is, as it did, where a superscript or a circled one does not."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fine", "estimate": "٣"})

	assert made.status_code == 201, made.text
	assert made.json()["estimate_minutes"] == 3


@pytest.mark.parametrize("position", [2**31, -(2**31) - 1, 2**40, -(2**40)])
def test_a_status_position_past_its_column_is_refused_by_name (
	world: test_api_tasks.World, position: int
) -> None:
	"""SQLite stored 2**40 and PostgreSQL, whose ``integer`` stops at 2**31 - 1, answered 500."""

	_named(
		world.call(
			"POST",
			"/v1/statuses",
			json={
				"entity_type": "task",
				"key": "far",
				"label": "Far",
				"category": "todo",
				"position": position,
			},
		),
		"position",
	)


@pytest.mark.parametrize("position", [2**31, -(2**31) - 1, 2**40, -(2**40)])
def test_a_status_moved_past_its_column_is_refused_by_name (
	world: test_api_tasks.World, position: int
) -> None:
	"""The edit, the second writer of the same column."""

	made = world.call(
		"POST",
		"/v1/statuses",
		json={"entity_type": "task", "key": "near", "label": "Near", "category": "todo"},
	)

	assert made.status_code == 201, made.text

	_named(
		world.call("PATCH", f"/v1/statuses/{made.json()['id']}", json={"position": position}),
		"position",
	)


@pytest.mark.parametrize("position", [2**31 - 1, -(2**31)])
def test_a_status_position_at_the_edge_of_its_column_is_kept (
	world: test_api_tasks.World, position: int
) -> None:
	"""`SR#4030`, gap 1 of L-10 of the cold review of 2026-09-30: only 2**40 was ever asked.

	So a range one past the column's last value let 2**31 through, to PostgreSQL's 500. With the
	first number past each end refused above, **the last one inside each end is kept**, on the
	create and on the edit.
	"""

	made = world.call(
		"POST",
		"/v1/statuses",
		json={
			"entity_type": "task",
			"key": "edge",
			"label": "Edge",
			"category": "todo",
			"position": position,
		},
	)

	def stored () -> int:
		"""Return the position the status holds, read from its row, since no view reports one."""

		world.session.expire_all()

		return world.session.scalars(
			sqlalchemy.select(subroutine.db.models.vocabulary.Status.position).where(
				subroutine.db.models.vocabulary.Status.workspace_id == world.workspace.id,
				subroutine.db.models.vocabulary.Status.key == "edge",
			)
		).one()

	assert made.status_code == 201, made.text
	assert stored() == position

	moved = world.call("PATCH", f"/v1/statuses/{made.json()['id']}", json={"position": -position - 1})

	assert moved.status_code == 200, moved.text
	assert stored() == -position - 1


@pytest.mark.parametrize("path", ["/v1/tasks", "/v1/projects", "/v1/documents", "/v1/tags", "/v1/users"])
@pytest.mark.parametrize("cursor", ["é.x", "x.é"])
def test_a_cursor_that_is_not_ascii_is_refused_as_unusable (
	world: test_api_tasks.World, path: str, cursor: str
) -> None:
	"""A cursor this instance issued is ASCII, and one that was not reached a 500 through either
	half: the body is signed as ASCII, and the signature is compared as it."""

	_named(world.call("GET", path, params={"cursor": cursor}), "cursor")


def test_a_captured_tag_that_cannot_be_made_is_left_in_the_title (
	world: test_api_tasks.World,
) -> None:
	"""``Ship it #ops,web`` was refused whole, where capture leaves a token it cannot use where
	it was written."""

	made = world.call("POST", "/v1/tasks", json={"text": "Ship it #ops,web"})

	assert made.status_code == 201, made.text
	assert made.json()["title"] == "Ship it #ops,web"
	assert made.json()["tags"] == []


@pytest.mark.parametrize(
	"arguments",
	[{"filter": {"tag.eq": "\x00"}}, {"assignee": "\x00"}, {"project": "\x00"}],
	ids=["a-filter", "an-assignee", "a-project"],
)
def test_an_agent_s_nul_is_not_answered_as_a_missing_instance (
	world: test_api_tasks.World, arguments: dict[str, typing.Any]
) -> None:
	"""The same NUL from an agent's tool: on PostgreSQL the database's refusal was answered
	*could not be read*, under advice to check the database was reachable - a cause nobody had
	established, about an instance that was answering.

	**Both of the catch-all's sentences are asserted absent**, since which one it gives turns on
	the configuration: here, whose settings name a database file nobody made, it said *no
	Subroutine instance has been set up here yet*.
	"""

	answered = world.call(
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": "subroutine_list", "arguments": arguments},
			}
		),
		headers={"content-type": "application/json"},
	)
	said = answered.json()["result"]["content"][0]["text"]

	assert answered.json()["result"]["isError"], said
	assert "set up" not in said, said
	assert "could not be read" not in said, said

	# **One answer on both backends since `SR#4023`**, the API's own, where PostgreSQL's was the
	# driver's refusal translated and SQLite's a lookup that found nothing.
	assert "control character" in said, said


class _Refused(Exception):
	"""Stand in for the driver's own exception, which the translation does not read."""


def _refused_as_data (bound: dict[str, typing.Any]) -> sqlalchemy.exc.DataError:
	"""Return what SQLAlchemy raises when the database refuses a statement bound with ``bound``.

	Real-shaped, statement and all, since what the fallback must not do is show them.
	"""

	return sqlalchemy.exc.DataError(
		'SELECT "user".id FROM "user" WHERE "user".username = %(username)s',
		bound,
		_Refused("PostgreSQL text fields cannot contain NUL (0x00) bytes"),
	)


def test_an_agent_is_told_a_nul_was_sent_rather_than_shown_the_sql () -> None:
	"""The protocol's own fallback, for a failure reaching it without a client that translated
	it first: a refusal of what was sent, with the statement and its values kept out."""

	answer = subroutine.mcp.protocol._explained(_refused_as_data({"username": "a\x00b"}))

	assert "NUL" in answer, answer
	assert "SELECT" not in answer, answer
	assert "username" not in answer, answer


def test_a_data_error_with_no_nul_bound_is_not_called_one () -> None:
	"""**Keyed on the values bound, not on the kind of error.** A number out of a column's range
	is a data error too, and telling its caller about a NUL they never sent would be a refusal
	asserting a cause nobody established."""

	assert subroutine.db.failures.unreadable(_refused_as_data({"username": "keanu"})) is None
	assert subroutine.db.failures.unreadable(
		_refused_as_data({"username": ["keanu", ("carrie-anne", "a\x00b")]})
	) is not None
