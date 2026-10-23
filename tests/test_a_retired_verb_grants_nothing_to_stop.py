"""A credential scoped to a retired verb is listed and stopped by its owner's others - `SR#5114`.

**A bullet of L-7 of the cold review of 2026-10-17**: ``token:admin`` was deleted, and a credential
issued before the migration keeps it in its stored scopes. Dominance compared every stored name, so
no narrowed credential of its owner's, however wide, held what it held: it was missing from their
listing and its revocation answered *"No credential here answers to ..."*. Measured over HTTP, as
the review measured it.
"""

import typing
import uuid

import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions
import test_api_tasks

#: Every verb the presenter below holds: each one a workspace has, bar deleting it.
HELD = sorted(subroutine.permissions.WORKSPACE_LEVEL - {subroutine.permissions.WORKSPACE_DELETE})


def _stored (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User, scopes: list[str]
) -> str:
	"""Issue ``user`` a credential and store ``scopes`` on it, as one issued before a verb was
	deleted keeps them, and return its prefix."""

	row, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title=f"Before {uuid.uuid4().hex[:4]}", scopes=["task:read"]
	)
	row.scopes = scopes
	session.flush()

	return row.token_prefix


def test_a_retired_verb_alone_is_listed_and_stopped (session: sqlalchemy.orm.Session) -> None:
	"""A narrowed credential holding every live verb lists and revokes one scoped to
	``token:admin`` alone, and still neither lists nor revokes one also scoped to a live verb it
	lacks."""

	world = test_api_tasks._world(session)
	keanu = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, keanu, role_key="owner")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=keanu, title="Nearly everything", scopes=HELD
	)
	session.flush()

	retired = _stored(session, keanu, ["token:admin"])
	wider = _stored(session, keanu, ["token:admin", subroutine.permissions.WORKSPACE_DELETE])
	secret = str(issued.value.get_secret_value())

	def sent (method: str, path: str) -> typing.Any:
		"""Make one request as keanu's narrowed credential."""

		return api_support.call(
			world.application, method, path, headers={"authorization": f"Bearer {secret}"}
		)

	listed = sent("GET", "/v1/tokens")

	assert listed.status_code == 200, listed.text

	prefixes = {row["prefix"] for row in listed.json()["items"]}

	assert retired in prefixes
	assert wider not in prefixes
	assert sent("DELETE", f"/v1/tokens/{wider}").status_code == 404

	stopped = sent("DELETE", f"/v1/tokens/{retired}")

	assert stopped.status_code in {200, 204}, stopped.text
