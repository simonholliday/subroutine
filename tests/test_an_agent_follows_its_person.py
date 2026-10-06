"""An agent acts in a workspace only while the person at the top of its chain is a member of it.

`SR#4546`, decision `#4518`, S11 of the cold review of 2026-10-05: after a person was taken out of a
workspace, the agent answering to them kept ``task:write`` there, still listed the workspace and
still read its rows, measured on both databases. **Reach is where it is decided**, not only the
role: checked where a role is looked up alone, the agent was refused writes and read everything.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions


def _seated (
	session: sqlalchemy.orm.Session, *, through: bool = False
) -> tuple[subroutine.db.models.identity.Workspace, subroutine.db.models.identity.User, str]:
	"""Return a workspace, the person an agent in it answers to, and the agent's secret.

	``through`` puts a second agent between them, which is not a member of the workspace: only the
	person at the top of the chain has to be.
	"""

	owner = subroutine.domain.users.create(session, username=f"owner-{uuid.uuid4().hex[:8]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace", owner=owner
	)
	person = subroutine.domain.users.create(session, username=f"person-{uuid.uuid4().hex[:8]}")
	above = person

	if through:
		above = subroutine.domain.users.create(
			session,
			username=f"between-{uuid.uuid4().hex[:8]}",
			is_service_account=True,
			responsible_user_id=person.id,
		)

	agent = subroutine.domain.users.create(
		session,
		username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		responsible_user_id=above.id,
	)

	for user in (person, agent):
		subroutine.domain.workspaces.add_member(session, workspace, user, role_key="member")

	_row, issued = subroutine.domain.authentication.issue_token(session, user=agent, title="Agent")
	session.flush()

	return workspace, person, issued.value.get_secret_value()


def _reaches (
	session: sqlalchemy.orm.Session, secret: str, workspace: subroutine.db.models.identity.Workspace
) -> bool:
	"""Say whether the agent lists the workspace, and check its role there agrees."""

	principal = subroutine.domain.authentication.authenticate(session, secret)
	listed = workspace in subroutine.domain.workspaces.readable(session, principal)
	writes = subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_WRITE, workspace_id=workspace.id
	)

	assert listed == writes, f"the agent lists the workspace: {listed}, and may write in it: {writes}"

	return listed


@pytest.mark.parametrize("through", [False, True], ids=["directly", "through another agent"])
def test_an_agent_stops_where_its_person_was_taken_out_and_works_again_on_their_return (
	session: sqlalchemy.orm.Session, through: bool
) -> None:
	"""Neither listed nor written to while its person is out, and both again once they are back.

	**Computed on every request**, so adding the person back restores the agent with nothing
	re-issued; and it still authenticates in between, because its standing on the installation is
	unchanged.
	"""

	workspace, person, secret = _seated(session, through=through)

	assert _reaches(session, secret, workspace)

	subroutine.domain.workspaces.remove_member(session, workspace, person)
	session.flush()

	assert not _reaches(session, secret, workspace)

	subroutine.domain.workspaces.add_member(session, workspace, person, role_key="member")
	session.flush()

	assert _reaches(session, secret, workspace)
