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
import subroutine.db.failures
import subroutine.domain.authentication
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.vocabulary
import subroutine.domain.workspaces
import subroutine.errors
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


#: Every request R2-L17 of the cold review of 2026-10-04 found answering 500 (`SR#4428`), as
#: ``(method, path, body, field)``. ``{task}``, ``{other}``, ``{document}``, ``{workspace}`` and
#: ``{person}`` are filled in from what the test makes first, in the body as well as the path.
LOOKED_UP_TOO = [
	("POST", "/v1/tasks/{task}/links", {"target": "{other}", "link_type": HALF}, "link_type"),
	("POST", "/v1/documents/{document}/links", {"target": "{task}", "link_type": HALF}, "link_type"),
	("POST", "/v1/tags", {"name": HALF}, "name"),
	("POST", "/v1/workspaces/{workspace}/members", {"username": "{person}", "role": HALF}, "role"),
	("PATCH", "/v1/workspaces/{workspace}/members/{person}", {"role": HALF}, "role"),
	("POST", "/v1/tokens", {"title": "For the deploy", "username": HALF}, "username"),
	("POST", "/v1/tokens", {"title": "For the deploy", "service_account": HALF}, "service_account"),
	("POST", "/v1/tokens", {"title": "For the deploy", "scopes": [HALF]}, "scopes"),
]


@pytest.mark.parametrize(("method", "path", "body", "field"), LOOKED_UP_TOO)
def test_half_a_character_in_a_link_type_tag_role_account_or_scope_is_refused_by_name (
	method: str,
	path: str,
	body: dict[str, typing.Any],
	field: str,
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4428`: each answered 500, where it is a 422 naming the field now."""

	world = test_api_tasks._world(session)
	task = world.call("POST", "/v1/tasks", json={"title": "Collect the package"}).json()
	other = world.call("POST", "/v1/tasks", json={"title": "Ring the dentist"}).json()
	document = world.call("POST", "/v1/documents", json={"title": "The rules"}).json()
	person = subroutine.domain.users.create(session, username="keanu")

	if "members/{person}" in path:
		subroutine.domain.workspaces.add_member(session, world.workspace, person, role_key="member")

	session.flush()
	filled = {
		"{task}": str(task["ref"]),
		"{other}": str(other["ref"]),
		"{document}": str(document["ref"]),
		"{workspace}": world.workspace.slug,
		"{person}": person.username,
	}
	sent = json.dumps(body)

	for name, value in filled.items():
		path = path.replace(name, value)
		sent = sent.replace(f'"{name}"', json.dumps(value))

	answer = api_support.call(
		world.application,
		method,
		path,
		content=sent,
		headers={"content-type": "application/json", "authorization": f"Bearer {world.secret}"},
	)

	# Quoted as ASCII, as every message here is: a report holding half a character cannot be sent
	# between test workers, and stops the run rather than failing the test.
	assert answer.status_code == 422, f"{answer.status_code}: {answer.text[:300]!a}"
	assert answer.json()["errors"][0]["field"] == field, ascii(answer.text)


def test_half_a_character_a_driver_cannot_encode_is_refused_rather_than_a_fault (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4428`: the backstop, for a name no check stands in front of, on every surface.

	Both drivers raise the codec's own error, unwrapped, which none of the three surfaces
	recognised: a 500 over HTTP, a codec error to an agent and a crash report in the terminal.
	"""

	failed = UnicodeEncodeError("utf-8", "a\ud800b", 1, 2, "surrogates not allowed")
	refused = subroutine.db.failures.unreadable(failed)

	assert refused is not None and refused.status == 422, ascii(refused)
	assert "U+D800" in refused.detail, ascii(refused.detail)

	other = UnicodeEncodeError("ascii", "caf\u00e9", 3, 4, "ordinal not in range(128)")

	assert subroutine.db.failures.unreadable(other) is None, "an encoding not about a surrogate"


def test_half_a_character_no_check_stands_in_front_of_is_refused_on_every_surface (
	session: sqlalchemy.orm.Session,
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#4428`: where no check stands, the driver's own refusal is a refusal, not a fault.

	With the checks in front of each lookup taken away, as for a name nothing checks yet: over
	HTTP it was a 500, and in the terminal a crash report.
	"""

	monkeypatch.setattr(subroutine.domain.text, "readable", lambda value, **_: value)
	monkeypatch.setattr(subroutine.domain.text, "whole", lambda value, **_: value)
	world = test_api_tasks._world(session)
	answer = api_support.call(
		world.application,
		"POST",
		"/v1/tags",
		content=json.dumps({"name": HALF}),
		headers={"content-type": "application/json", "authorization": f"Bearer {world.secret}"},
	)

	assert answer.status_code == 422, f"{answer.status_code}: {answer.text[:300]!a}"
	assert "U+D800" in answer.json()["detail"], ascii(answer.text)

	run("init")
	run("add", "Collect the package from reception")
	result = run("search", "caf\udcff", expect=1)

	assert "half of a character" in result.output, ascii((result.output, result.exception))


def test_a_refusal_quoting_half_a_character_can_still_be_sent (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#4428`: a problem document is written in ASCII, so one quoting half a character is sent.

	Written as UTF-8 it could not be encoded, and a refusal became a 500. **And a scope is quoted
	with its escape**, so that refusal never holds one in the first place.
	"""

	def refusing (*_: typing.Any, **__: typing.Any) -> typing.NoReturn:
		"""Refuse as a check that quoted what it was sent would."""

		raise subroutine.errors.ValidationError(f"There is no tag called {HALF} here.")

	monkeypatch.setattr(subroutine.domain.vocabulary, "create_tag", refusing)
	world = test_api_tasks._world(session)
	answer = world.call("POST", "/v1/tags", json={"name": "ops"})

	assert answer.status_code == 422, f"{answer.status_code}: {answer.text[:300]!a}"
	assert answer.content.isascii(), answer.content[:300]
	assert answer.json()["detail"] == f"There is no tag called {HALF} here.", ascii(answer.text)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.authentication.issue_token(
			session, user=world.user, title="For the deploy", scopes=[HALF]
		)

	assert HALF not in refused.value.detail, ascii(refused.value.detail)
	assert all(HALF not in one.message for one in refused.value.errors), ascii(refused.value)


def test_an_invalid_byte_in_an_argument_is_refused_rather_than_crashing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Python hands an invalid byte in an argument on as a lone surrogate, so a status got one.

	**And a search and a link type** (`SR#4428`), which ended in a crash report.
	"""

	run("init")
	run("add", "Collect the package from reception")
	run("add", "Ring the dentist")

	for arguments in (
		("update", "1", "--status", "\udcff"),
		("search", "caf\udcff"),
		("link", "1", "\udcff", "2"),
	):
		result = run(*arguments, expect=1)

		assert not isinstance(result.exception, UnicodeError), ascii((arguments, result.exception))
		assert "Something went wrong" not in result.output, ascii((arguments, result.output))
