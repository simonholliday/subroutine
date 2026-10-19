"""A credential that may file tasks and not read them files one over HTTP - `SR#5118`.

M-2 of the cold review of 2026-10-17 (`#5096`). **The write-only capture credential is intended**
(decision `#4527`: a write verb without its read is not refused). Every terminal command over HTTP
opens with ``identity()``, which is ``GET /v1/meta``, and that counted each tag's uses over the
tasks the credential could read - refusing one that could read none, so it could file nothing.
"""

import uuid

import sqlalchemy.orm

import api_support
import subroutine.clients.http
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.bootstrap


def _capture_only (session: sqlalchemy.orm.Session) -> tuple[str, str, str]:
	"""Return a workspace's short name, its owner's whole credential, and one that may only file tasks.

	A task carrying a tag is filed first with the owner's whole authority, so the vocabulary has a
	tag in use for the narrow credential not to see.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"keanu-{uuid.uuid4().hex[:6]}", instance_name="MetaCortex"
	)
	_row, everything = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Everything"
	)
	_row, filing = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Filing only", scopes=["task:write"]
	)
	session.flush()

	with _over_http(session, everything.value.get_secret_value()) as client:
		client.capture(text="Water the plants #garden", workspace=setup.workspace.slug)

	return setup.workspace.slug, everything.value.get_secret_value(), filing.value.get_secret_value()


def _over_http (session: sqlalchemy.orm.Session, token: str) -> subroutine.clients.http.Client:
	"""Return the real HTTP client, driving the real application against this database."""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://example.com"),
		token=token,
		transport=api_support.SyncTransport(api_support.build_app(api_support.factory_for(session))),
		base_url=api_support.BASE_URL,
	)


def test_a_capture_credential_reads_the_vocabulary_and_sees_no_tag_in_use (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``GET /v1/meta`` answers it, and the tag a task it cannot read carries is not among its tags."""

	slug, everything, filing = _capture_only(session)
	application = api_support.build_app(api_support.factory_for(session))

	def tags (secret: str) -> dict[str, int]:
		"""Return the tags /v1/meta lists to one credential, with their uses, failing unless it answers."""

		answer = api_support.call(
			application,
			"GET",
			f"/v1/meta?workspace_id={slug}",
			headers={"Authorization": f"Bearer {secret}"},
		)

		assert answer.status_code == 200, answer.text

		return {one["name"]: one["usage"] for one in answer.json()["tags"]["items"]}

	assert tags(everything).get("garden") == 1, "the control: the owner's whole credential counts it"
	assert "garden" not in tags(filing)


def test_a_capture_credential_files_a_task_as_the_terminal_does (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Through the HTTP client, as every terminal command opens, and the task it filed is there."""

	slug, _everything, filing = _capture_only(session)

	with _over_http(session, filing) as client:
		identity = client.identity()
		captured = client.capture(text="Feed the cat", workspace=slug)

	assert slug in [one.slug for one in identity.workspaces], identity
	assert captured.task.title == "Feed the cat", captured
