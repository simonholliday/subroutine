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
import subroutine.domain.accountability
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
	writes = (subroutine.domain.authorization.refusal(
		session, principal, subroutine.permissions.TASK_WRITE, workspace_id=workspace.id
	) is None)

	assert listed == writes, f"the agent lists the workspace: {listed}, and may write in it: {writes}"

	return listed


@pytest.mark.parametrize("through", [False, True], ids=["directly", "through another agent"])
def test_the_roster_says_which_agents_are_stopped_by_the_rule_that_stops_them (
	session: sqlalchemy.orm.Session, through: bool
) -> None:
	"""`SR#5062`: an agent stopped here read as any other member, so nothing said why it stopped.

	Found by the upgrade rehearsal of 2026-10-13: an agent whose person had been taken out of a
	workspace under 0.10.0 went on working there, stopped after the upgrade, and the member listing,
	``doctor`` and the agent's own refusal said nothing of why. **Asked of the rule itself**: the
	roster's answer for every member must be :func:`accountability.standing`'s, out and back.
	"""

	workspace, person, _secret = _seated(session, through=through)

	def agreed () -> set[str]:
		"""Return who the roster says is stopped, having checked it against the rule for each."""

		rows = subroutine.domain.workspaces.members(session, workspace)
		accounts = [account for _row, account, _role in rows]
		answerable = subroutine.domain.accountability.answerable_for_many(
			session, [account.id for account in accounts]
		)
		outside = subroutine.domain.accountability.outside_in(
			session, workspace.id, accounts, answerable
		)

		for account in accounts:
			ruled = subroutine.domain.accountability.standing(
				session, account, workspace_id=workspace.id
			)

			assert (account.id in outside) == (ruled == subroutine.domain.accountability.OUTSIDE), (
				f"{account.username}: the roster and the rule disagree"
			)

		return {account.username for account in accounts if account.id in outside}

	assert agreed() == set(), "nobody is stopped while the person is a member"

	subroutine.domain.workspaces.remove_member(session, workspace, person)
	session.flush()

	stopped = agreed()

	assert len(stopped) == 1 and next(iter(stopped)).startswith("agent-"), stopped

	subroutine.domain.workspaces.add_member(session, workspace, person, role_key="member")
	session.flush()

	assert agreed() == set(), "and it starts again with nothing re-issued"


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
