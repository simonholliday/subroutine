"""A credential narrowed to some projects makes no crossing move - `SR#5102`.

M-5 of the cold review of 2026-10-17, decided in `#5097`: alice's credential, narrowed to ``hidden``
and ``open``, could not make ``hidden`` public, and moved ``hidden/secret`` out of it, ``open/shown``
into it, and a task and a document from ``open`` into it - each changing who can see the work as
surely. **Each is refused, as changing a project's visibility is**, and made by her unnarrowed
credential; a move that crosses no boundary stays the narrowed one's.
"""

import typing

import sqlalchemy.orm

import test_api_tasks
import test_document_binds

_as = test_document_binds._as
_credential = test_document_binds._credential

#: The sentence every act beyond a narrowed credential's places is refused with.
BEYOND = "acts beyond the projects the token you used may change."


def _made (world: test_api_tasks.World, path: str, **body: typing.Any) -> str:
	"""Make something as the world's own account, and return its id."""

	made = world.call("POST", path, json=body)

	assert made.status_code == 201, made.text

	return str(made.json()["id"])


def test_a_narrowed_credential_moves_nothing_across_a_privacy_boundary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The four measured moves, refused on the field that took them across, then made unnarrowed."""

	world = test_api_tasks._world(session)
	hidden = _made(world, "/v1/projects", key="hidden", title="Hidden", visibility="private")
	opened = _made(world, "/v1/projects", key="open", title="Open")
	spare = _made(world, "/v1/projects", key="spare", title="Spare")
	_made(world, "/v1/projects", key="secret", title="Secret", parent=hidden)
	_made(world, "/v1/projects", key="shown", title="Shown", parent=opened)
	task = _made(world, "/v1/tasks", title="Rotate the disks", project="open")
	document = _made(world, "/v1/documents", title="How we rotate", project="open", type="note")
	places = [hidden, opened, spare]
	narrowed = _credential(world, world.user, project_scope=places, project_write_scope=places)

	moves = (
		("POST", "/v1/projects/hidden/secret/move", {"parent": "open"}, "parent"),
		("POST", "/v1/projects/open/shown/move", {"parent": "hidden"}, "parent"),
		("PATCH", f"/v1/tasks/{task}", {"project": "hidden"}, "project"),
		("PATCH", f"/v1/documents/{document}", {"project": "hidden"}, "project"),
	)

	for method, path, body, field in moves:
		refused = _as(world, narrowed, method, path, json=body)

		assert refused.status_code == 403, refused.text
		assert BEYOND in refused.json()["detail"], refused.text
		assert refused.json()["errors"][0]["field"] == field, refused.text
		assert "credential reaches the whole workspace" in refused.text, refused.text

	# **Nothing moved**, and the same moves are the unnarrowed credential's.
	assert world.call("GET", "/v1/projects/open/shown").status_code == 200
	assert world.call("GET", f"/v1/tasks/{task}").json()["project_key"] == "open"

	for method, path, body, _field in moves:
		made = world.call(method, path, json=body)

		assert made.status_code == 200, made.text


def test_a_narrowed_credential_still_moves_work_that_crosses_no_boundary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The control: between two public projects it reaches, nobody's sight changes, so it is allowed."""

	world = test_api_tasks._world(session)
	opened = _made(world, "/v1/projects", key="open", title="Open")
	spare = _made(world, "/v1/projects", key="spare", title="Spare")
	_made(world, "/v1/projects", key="shown", title="Shown", parent=opened)
	task = _made(world, "/v1/tasks", title="Rotate the disks", project="open")
	narrowed = _credential(world, world.user, project_scope=[opened, spare], project_write_scope=[opened, spare])

	moved = _as(world, narrowed, "POST", "/v1/projects/open/shown/move", json={"parent": "spare"})

	assert moved.status_code == 200, moved.text
	assert _as(world, narrowed, "PATCH", f"/v1/tasks/{task}", json={"project": "spare"}).status_code == 200
