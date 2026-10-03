"""Half a character in a name that is looked up is refused by name - `SR#4287`.

M4 of the cold review of 2026-10-03. Titles and tags were refused by name since `6a036f4`; a
status, a type, an assignee, a project and an ``@mention`` in a captured line reached the database
as they were, and the driver, which cannot encode a lone surrogate, answered 500 on both backends.
The terminal met the same through an argument holding an invalid byte, which Python hands on as a
lone surrogate.
"""

import json
import typing

import pytest
import sqlalchemy.orm
import typer.testing

import api_support
import test_api_tasks
import test_personal_path

#: Half a character: the first of a pair with no second.
HALF = "\ud800"

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

#: Every request that looks a name up, as ``(method, path, body)``, with ``{task}`` and
#: ``{document}`` filled in from the items the test makes first.
LOOKED_UP = [
	("POST", "/v1/tasks", {"title": "Ring the dentist", "status": HALF}),
	("POST", "/v1/tasks", {"title": "Ring the dentist", "type": HALF}),
	("POST", "/v1/tasks", {"title": "Ring the dentist", "assignee": HALF}),
	("POST", "/v1/tasks", {"title": "Ring the dentist", "project": HALF}),
	("PATCH", "/v1/tasks/{task}", {"status": HALF}),
	("PATCH", "/v1/tasks/{task}", {"type": HALF}),
	("PATCH", "/v1/tasks/{task}", {"assignee": HALF}),
	("PATCH", "/v1/tasks/{task}", {"project": HALF}),
	("POST", "/v1/tasks", {"text": f"Ring the dentist @{HALF}"}),
	("POST", "/v1/tasks", {"text": f"Ring the dentist +{HALF}"}),
	("POST", "/v1/tasks", {"text": "Ring the dentist", "project": HALF}),
	("POST", "/v1/tasks", {"text": "Ring the dentist", "status": HALF}),
	("POST", "/v1/tasks", {"text": "Ring the dentist", "type": HALF}),
	("POST", "/v1/documents", {"title": "The handbook", "status": HALF}),
	("POST", "/v1/documents", {"title": "The handbook", "type": HALF}),
	("POST", "/v1/documents", {"title": "The handbook", "project": HALF}),
	("PATCH", "/v1/documents/{document}", {"status": HALF}),
	("PATCH", "/v1/documents/{document}", {"type": HALF}),
	("PATCH", "/v1/documents/{document}", {"project": HALF}),
	("POST", "/v1/projects", {"key": "web", "title": "Website", "status": HALF}),
]


@pytest.mark.parametrize(("method", "path", "body"), LOOKED_UP)
def test_half_a_character_in_a_looked_up_name_is_refused_by_name (
	method: str, path: str, body: dict[str, str], session: sqlalchemy.orm.Session
) -> None:
	"""Refused with a 422 that names a field, where it was a 500 from the driver."""

	world = test_api_tasks._world(session)
	task = world.call("POST", "/v1/tasks", json={"title": "Collect the package"}).json()
	document = world.call("POST", "/v1/documents", json={"title": "The rules"}).json()
	answer = api_support.call(
		world.application,
		method,
		path.format(task=task["ref"], document=document["ref"]),
		# Written out, because a client encoding the body itself would refuse half a character.
		content=json.dumps(body),
		headers={"content-type": "application/json", "authorization": f"Bearer {world.secret}"},
	)

	assert answer.status_code == 422, f"{answer.status_code}: {answer.text[:300]}"
	assert answer.json()["errors"][0]["field"], answer.text


def test_an_invalid_byte_in_an_argument_is_refused_rather_than_crashing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Python hands an invalid byte in an argument on as a lone surrogate, so a status got one."""

	run("init")
	run("add", "Collect the package from reception")

	result = run("update", "1", "--status", "\udcff", expect=1)

	assert not isinstance(result.exception, UnicodeError), result.exception
	assert "Something went wrong" not in result.output, result.output
