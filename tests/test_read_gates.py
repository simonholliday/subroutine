"""Each read verb gates its own kind of row wherever it appears - `#4553`, decision `#4511`.

S3 and S10 of the cold review of 2026-10-05. The permission matrix holds the feed, an item's
history, both journals, the export and ``/v1/me`` for text planted under each verb; these are the
answers it cannot see a plant in: search matching on what a comment said, and the workspace's record
and listing, which no client reads.
"""

import typing

import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.permissions
import test_api_tasks

World = test_api_tasks.World

P = subroutine.permissions


def _scoped (world: World, *scopes: str) -> World:
	"""Return the same installation through a credential narrowed to these verbs."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title=" and ".join(scopes), scopes=list(scopes)
	)
	world.session.flush()

	return world._replace(secret=issued.value.get_secret_value())


def test_search_matches_what_a_comment_said_only_for_a_credential_that_reads_comments (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A credential refused ``GET .../comments`` was matched on a comment's words (S3)."""

	world = test_api_tasks._world(session)
	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"}).json()
	said = world.call(
		"POST", f"/v1/tasks/{made['ref']}/comments", json={"body": "The zanzibarquokka key expired."}
	)

	assert said.status_code == 201, said.text

	def found (through: World) -> list[typing.Any]:
		"""Return the refs a search for the comment's word answers."""

		answer = through.call("GET", "/v1/tasks?q=zanzibarquokka")

		assert answer.status_code == 200, answer.text

		return [item["ref"] for item in answer.json()["items"]]

	assert found(_scoped(world, P.TASK_READ)) == [], "matched on a comment it may not read"
	assert found(_scoped(world, P.TASK_READ, P.COMMENT_READ)) == [made["ref"]]


def test_the_workspace_record_and_listing_take_workspace_read (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Its members and settings refused a credential that read its record and listing (S10)."""

	world = test_api_tasks._world(session)
	record = f"/v1/workspaces/{world.workspace.slug}"
	without = _scoped(world, P.TASK_READ)
	holding = _scoped(world, P.TASK_READ, P.WORKSPACE_READ)

	assert without.call("GET", record).status_code == 403
	assert without.call("GET", "/v1/workspaces").status_code == 403
	assert holding.call("GET", record).status_code == 200
	assert [one["slug"] for one in holding.call("GET", "/v1/workspaces").json()["items"]] == [
		world.workspace.slug
	]
