"""A credential narrowed to some projects stays within its reach at the top level - `#4013`.

Decision `#4095`: it cannot make a top-level project, since a new one cannot already be named
in its scope, and it may move a project to the top level only where its scope names that
project itself. Refused with the narrowing's own sentence and a hint saying where it can go.
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
		assert "narrowed to" in refused.json()["detail"], refused.text
		assert "--parent" in refused.json()["hint"] and "web" in refused.json()["hint"], refused.text

		inside = narrowed.call(
			"POST",
			"/v1/projects",
			json={"key": f"launch-{number}", "title": "Launch", "parent": "web"},
		)

		assert inside.status_code == 201, (narrowing, inside.text)

	assert world.call("POST", "/v1/projects", json={"key": "launch", "title": "Launch"}).is_success


def test_a_narrowed_credential_moves_to_the_top_level_only_what_its_scope_names (
	world: World,
) -> None:
	"""``ops/x`` is reached through ``ops``, so moving it out from under ``ops`` would lose it.

	A credential whose scope names ``x`` itself still reaches it at the top level, so that move is
	within it.
	"""

	ops = _project(world, {"key": "ops", "title": "Operations"})
	inner = _project(world, {"key": "x", "title": "Rota", "parent": "ops"})

	through_ops = _narrowed(world, project_scope=[ops["id"]])
	refused = through_ops.call("POST", "/v1/projects/ops/x/move", json={"parent": None})

	assert refused.status_code == 403, refused.text
	assert "narrowed to" in refused.json()["detail"], refused.text

	naming_it = _narrowed(world, project_scope=[ops["id"], inner["id"]])
	moved = naming_it.call("POST", "/v1/projects/ops/x/move", json={"parent": None})

	assert moved.status_code == 200, moved.text
	assert naming_it.call("GET", "/v1/projects/x").status_code == 200, "still in reach"
