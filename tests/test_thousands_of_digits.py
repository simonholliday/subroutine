"""A run of thousands of digits is answered or refused, never a 500 - `SR#5150`.

M-1 of the cold review of 2026-10-17. ``int`` refuses more than 4,300 digits with an error of its
own, and three readers called it on an unbounded run before any range check: a ref, a mention and
a date expression's count. Every request below answered 500 with 5,000 nines, on both backends,
and ``show`` and ``comment`` ended in the terminal's crash report.
"""

import typing

import pytest
import sqlalchemy.orm
import typer.testing

import subroutine.domain.mentions
import subroutine.domain.refs
import test_api_tasks
import test_personal_path

#: More digits than ``int`` reads.
NINES = "9" * 5000

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

class Made (typing.NamedTuple):
	"""An installation reachable over HTTP, with a task and a document to write to."""

	world: test_api_tasks.World
	task: int
	document: int


@pytest.fixture
def made (session: sqlalchemy.orm.Session) -> Made:
	"""An installation with one task and one document in it."""

	world = test_api_tasks._world(session)
	task = world.call("POST", "/v1/tasks", json={"title": "Collect the package"}).json()["ref"]
	document = world.call("POST", "/v1/documents", json={"title": "The house rules"}).json()["ref"]

	return Made(world, task, document)


@pytest.mark.parametrize(
	("method", "path", "body"),
	[
		("POST", "/v1/tasks", {"title": "Read the report", "description": f"See #{NINES}."}),
		("PATCH", "/v1/tasks/{task}", {"description": f"See #{NINES}."}),
		("POST", "/v1/tasks/{task}/comments", {"body": f"See #{NINES}."}),
		("POST", "/v1/documents", {"title": "The report", "body": f"See #{NINES}."}),
		("PATCH", "/v1/documents/{document}", {"body": f"See #{NINES}."}),
		(
			"POST",
			"/v1/tasks",
			{"title": "Read the report", "description": f"[The report](subroutine:{NINES})"},
		),
	],
	ids=["task", "task edited", "comment", "document", "document edited", "link"],
)
def test_a_mention_of_thousands_of_digits_is_kept_as_prose (
	made: Made, method: str, path: str, body: dict[str, str]
) -> None:
	"""Written down as the writer wrote it, and indexed as a mention of nothing."""

	answered = made.world.call(
		method, path.format(task=made.task, document=made.document), json=body
	)

	assert answered.status_code in (200, 201), answered.text[:300]
	assert NINES in answered.text
	assert subroutine.domain.mentions.candidates(*body.values()) == []


@pytest.mark.parametrize("kind", ["tasks", "documents"])
def test_an_item_numbered_with_thousands_of_digits_is_not_there (made: Made, kind: str) -> None:
	"""No item has that number, which is what a lookup of one too large already says."""

	for written in (NINES, f"%23{NINES}"):
		answered = made.world.call("GET", f"/v1/{kind}/{written}")

		assert answered.status_code == 404, answered.text[:300]


def _said (answered: typing.Any, number: str) -> tuple[int, str, typing.Any]:
	"""Return an answer's status, its detail with ``number`` written as ``N``, and its rows."""

	shape = answered.json()

	return (
		answered.status_code,
		str(shape.get("detail", "")).replace(number, "N"),
		shape.get("items"),
	)


@pytest.mark.parametrize(
	("path", "name", "written"),
	[
		("/v1/tasks", "q", "{}"),
		("/v1/tasks", "q", "#{}"),
		("/v1/tasks", "q", "ref:{}"),
		("/v1/tasks", "ref.eq", "{}"),
		("/v1/documents", "ref.eq", "{}"),
		("/v1/tasks", "parent", "{}"),
		("/v1/documents", "parent.eq", "{}"),
		("/v1/tasks", "under.eq", "{}"),
	],
	ids=["search", "search for a ref", "ref term", "ref", "document ref", "parent",
		"document parent", "under"],
)
def test_a_filter_on_thousands_of_digits_is_answered_as_one_on_too_large_a_ref (
	made: Made, path: str, name: str, written: str
) -> None:
	"""Answered as a filter on a number one past the largest ref already was."""

	beyond = str(subroutine.domain.refs.MAX_REF + 1)
	long = made.world.call("GET", path, params={name: written.format(NINES)})
	short = made.world.call("GET", path, params={name: written.format(beyond)})

	assert short.status_code < 500, short.text[:300]
	assert _said(long, NINES) == _said(short, beyond), long.text[:300]


def test_a_move_under_thousands_of_digits_is_refused_as_one_under_too_large_a_ref (
	made: Made,
) -> None:
	"""Refused as a move under a number one past the largest ref already was."""

	beyond = str(subroutine.domain.refs.MAX_REF + 1)
	path = f"/v1/tasks/{made.task}/move"
	long = made.world.call("POST", path, json={"parent": NINES})
	short = made.world.call("POST", path, json={"parent": beyond})

	assert 400 <= short.status_code < 500, short.text[:300]
	assert _said(long, NINES) == _said(short, beyond), long.text[:300]


@pytest.mark.parametrize(
	("method", "path", "where", "written"),
	[
		("POST", "/v1/tasks", "body", "now+{}d"),
		("POST", "/v1/tasks", "body", "today+{}d"),
		("PATCH", "/v1/tasks/{task}", "body", "today-{}w"),
		("GET", "/v1/tasks", "due_at.lt", "now+{}d"),
		("GET", "/v1/tasks", "created_at.gt", "now-{}d"),
		("GET", "/v1/agenda", "date", "today+{}d"),
	],
	ids=["now", "today", "edited", "due filter", "created filter", "agenda"],
)
def test_a_date_counted_in_thousands_of_digits_is_refused_as_one_off_the_calendar (
	made: Made, method: str, path: str, where: str, written: str
) -> None:
	"""Refused by name, as a count landing past the calendar's end already was.

	**Compared with seven digits**, a count short enough to be read and then found off the
	calendar, so the refusal is held to the one that path gives rather than to itself.
	"""

	def answer (number: str) -> typing.Any:
		"""Send the expression with ``number`` as its count."""

		value = written.format(number)

		if where != "body":
			return made.world.call(method, path, params={where: value})

		return made.world.call(
			method,
			path.format(task=made.task),
			json={"due": value} | ({"title": "Renew the passport"} if method == "POST" else {}),
		)

	seven = "9" * 7
	long, short = answer(NINES), answer(seven)

	assert short.status_code == 422, short.text[:300]
	assert _said(long, NINES) == _said(short, seven), long.text[:300]
	assert [
		(one["field"], one["message"].replace(NINES, "N")) for one in long.json()["errors"]
	] == [(one["field"], one["message"].replace(seven, "N")) for one in short.json()["errors"]]


def test_a_count_padded_with_zeros_is_still_read (made: Made) -> None:
	"""Leading zeros are not what makes a count too long, as they are not for an estimate."""

	padded = made.world.call(
		"POST", "/v1/tasks", json={"title": "Water the plants", "due": f"today+{'0' * 5000}1d"}
	)
	plain = made.world.call("POST", "/v1/tasks", json={"title": "Feed the cat", "due": "today+1d"})

	assert padded.status_code == 201, padded.text[:300]
	assert padded.json()["due_at"] == plain.json()["due_at"]


def test_a_count_of_ten_digits_can_still_land_on_the_calendar (made: Made) -> None:
	"""The bound is the whole calendar counted in minutes, so a ten-digit count is still read."""

	answered = made.world.call(
		"POST", "/v1/tasks", json={"title": "Open the time capsule", "due": "now+4000000000m"}
	)

	assert answered.status_code == 201, answered.text[:300]
	assert int(answered.json()["due_at"][:4]) > 9000, answered.json()["due_at"]


def test_the_longest_ref_is_still_read () -> None:
	"""The bound is the largest ref's own length, so the largest ref is still one."""

	longest = str(subroutine.domain.refs.MAX_REF)

	assert subroutine.domain.refs.parse_ref(longest) == subroutine.domain.refs.MAX_REF
	assert subroutine.domain.mentions.candidates(f"See #{longest}.") == [
		subroutine.domain.refs.MAX_REF
	]
	assert subroutine.domain.refs.parse_ref("1" + longest) is None


def test_the_terminal_refuses_thousands_of_digits_rather_than_crashing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``show`` says there is no such item, and ``comment`` keeps the number as prose."""

	run("init")
	run("add", "Collect the package from reception")

	shown = run("show", NINES, expect=1)

	assert "Something went wrong" not in shown.output, shown.output[:300]
	assert not isinstance(shown.exception, ValueError), repr(shown.exception)[:300]

	run("comment", "1", f"see #{NINES}")

	assert NINES in run("show", "1").output
