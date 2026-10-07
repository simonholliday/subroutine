"""A credential narrowed to some projects acts on nothing beyond them at the top level - `#4013`.

Decision `#4095`: it cannot make a top-level project, since a new one cannot already be named in
its scope. **The top level is the workspace's place** (`SR#4558`, Q15 of the cold review of
2026-10-05, decision `#4527`), which such a credential does not hold, so it moves no project there
either - `#4095` allowed one its scope named - and decides no project's visibility, which is the
workspace's to see. Refused with the one sentence every such act meets, and a hint saying where it
can go.
"""

import typing

import subroutine.domain.authentication
import test_api_tasks

World = test_api_tasks.World
world = test_api_tasks.world


def _project (world: World, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Make a project over HTTP and return it, failing loudly if it was refused."""

	made = world.call("POST", "/v1/projects", json=body)

	assert made.status_code == 201, made.text

	return typing.cast(dict[str, typing.Any], made.json())


def _narrowed (world: World, **narrowing: typing.Any) -> World:
	"""Return the same person with a credential narrowed as asked."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title="Narrowed", **narrowing
	)
	world.session.flush()

	return world._replace(secret=issued.value.get_secret_value())


def test_a_narrowed_credential_makes_no_project_at_the_top_level (world: World) -> None:
	"""It could not have read, changed or deleted it, while the whole workspace saw it.

	Both narrowings: reaching only ``web``, and reaching everything while changing only ``web``.
	Inside ``web`` it makes one as before.
	"""

	web = _project(world, {"key": "web", "title": "Website rebuild"})

	for number, narrowing in enumerate(
		({"project_scope": [web["id"]]}, {"project_write_scope": [web["id"]]})
	):
		narrowed = _narrowed(world, **narrowing)
		refused = narrowed.call("POST", "/v1/projects", json={"key": "launch", "title": "Launch"})

		assert refused.status_code == 403, (narrowing, refused.text)
		assert "beyond the projects" in refused.json()["detail"], refused.text
		assert "--parent" in refused.json()["hint"] and "web" in refused.json()["hint"], refused.text

		inside = narrowed.call(
			"POST",
			"/v1/projects",
			json={"key": f"launch-{number}", "title": "Launch", "parent": "web"},
		)

		assert inside.status_code == 201, (narrowing, inside.text)

	assert world.call("POST", "/v1/projects", json={"key": "launch", "title": "Launch"}).is_success


def test_a_narrowed_credential_moves_no_project_to_the_top_level (world: World) -> None:
	"""The top level is the workspace's, whatever the scope names (`SR#4558`, Q15).

	``ops/x`` is reached through ``ops``, so moving it out from under ``ops`` would lose it - and a
	credential whose scope names ``x`` itself, which `#4095` let move it, is refused too: two
	readings of the top level were one too many. Moving it inside its projects is untouched.
	"""

	ops = _project(world, {"key": "ops", "title": "Operations"})
	inner = _project(world, {"key": "x", "title": "Rota", "parent": "ops"})
	rota = _project(world, {"key": "rota", "title": "Rotas", "parent": "ops"})

	for scope in ([ops["id"]], [ops["id"], inner["id"]]):
		narrowed = _narrowed(world, project_scope=scope)
		refused = narrowed.call("POST", "/v1/projects/ops/x/move", json={"parent": None})

		assert refused.status_code == 403, (scope, refused.text)
		assert "beyond the projects" in refused.json()["detail"], refused.text

		# **Told how to move it** (`SR#4323`): it was told to use ``--parent``, which a move does
		# not take.
		assert "'subroutine project move ops/x --under <key>'" in refused.json()["hint"], refused.text
		assert "--parent" not in refused.json()["hint"], refused.text

	inside = _narrowed(world, project_scope=[ops["id"]]).call(
		"POST", "/v1/projects/ops/x/move", json={"parent": rota["id"]}
	)

	assert inside.status_code == 200, inside.text


def test_a_narrowed_credential_decides_no_projects_visibility (world: World) -> None:
	"""`SR#4558`, C I-3 of the cold review of 2026-10-05: who sees a project is the workspace's.

	A credential narrowed to ``web`` made ``web`` public, putting it in front of the whole
	workspace, while it was refused sharing a view for that reason (`#3151`). Both directions are
	the workspace's place, and both narrowings are refused; the same person's unnarrowed credential
	is not, and the credential still changes anything else about the project.
	"""

	web = _project(world, {"key": "web", "title": "Website rebuild", "visibility": "private"})

	for narrowing in ({"project_scope": [web["id"]]}, {"project_write_scope": [web["id"]]}):
		narrowed = _narrowed(world, **narrowing)
		published = narrowed.call("PATCH", "/v1/projects/web", json={"visibility": "public"})

		assert published.status_code == 403, (narrowing, published.text)
		assert "beyond the projects" in published.json()["detail"], published.text

		renamed = narrowed.call("PATCH", "/v1/projects/web", json={"title": "Website"})

		assert renamed.status_code == 200, (narrowing, renamed.text)

	assert world.call("GET", "/v1/projects/web").json()["visibility"] == "private"
	assert world.call("PATCH", "/v1/projects/web", json={"visibility": "public"}).status_code == 200

	hidden = _narrowed(world, project_scope=[web["id"]]).call(
		"PATCH", "/v1/projects/web", json={"visibility": "private"}
	)

	assert hidden.status_code == 403, hidden.text
