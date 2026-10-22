"""An account or a project out of reach is answered as one nobody has, in words that say so - `SR#5109`.

**Decided in `#5097`**, "A refusal to find an account or a project says it is out of reach, not that
it does not exist": a read outside reach answers as a name nobody has (decision `#4943`, rule 2),
which stands, and the sentence said the account or the private project did not exist - a cause the
refusal had not established. Both now answer in one sentence that is true of either.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.selection
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_accounts_within_reach


def test_an_account_out_of_reach_and_one_nobody_has_are_said_to_be_out_of_reach (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Keanu cannot reach Tank, and nobody is called *nobody-at-all*: one sentence for both."""

	here = test_accounts_within_reach.Installation(session)
	keanu = subroutine.domain.authentication.Principal(user=here.keanu)

	for name in (here.tank.username, "nobody-at-all"):
		with pytest.raises(subroutine.errors.NotFound) as refused:
			subroutine.domain.users.read(session, name, actor=keanu)

		assert refused.value.detail == f"There is no account called {name!r} that you can reach."


def test_a_private_project_out_of_reach_and_a_key_nobody_has_are_said_to_be_out_of_reach (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A member with no row on a private project, and a key no project has: one sentence for both."""

	carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{uuid.uuid4().hex[:6]}")
	keanu = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="Metacortex", owner=carrieanne
	)
	subroutine.domain.workspaces.add_member(session, workspace, keanu, role_key="member")
	subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=carrieanne.id,
	)
	session.flush()
	actor = subroutine.domain.authentication.Principal(user=keanu)

	for key in ("vault", "nowhere"):
		with pytest.raises(subroutine.errors.NotFound) as refused:
			subroutine.domain.selection.project(session, actor, workspace, key)

		assert refused.value.detail == f"There is no project {key!r} that you can reach."
