"""Sight follows an agent's chain of accountability upward, and never down - `SR#4944`.

Decision `#4943`, rule 1, which answers `#4940`. **Measured before it**: a workspace owner's agent
made a private project, and the owner could neither see it nor share themselves into it, though a
person stands in as owner for their agent (decision `#4516`). Two rules met there that nothing had
asked of one arrangement, so every test of each passed.

**An account sees a private project that is shared with it, or with any agent that answers to
it**, directly or through other agents. The rule is written twice, as it was - the predicate every
listing narrows by, and ``projects.hidden_by`` for one account - and both ask
``accountability.sees_as`` whose rows count. The first tests hold that walk to the one
``agents_answering_to`` makes; the rest hold everything that asks *who can see this* to the rule.
"""

import datetime
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.projects
import subroutine.domain.repair
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions

accountability = subroutine.domain.accountability
projects = subroutine.domain.projects
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User
Project = subroutine.db.models.project.Project


def _person (session: sqlalchemy.orm.Session, name: str) -> User:
	"""Make a person with a name nobody else in the run has."""

	return subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")


def _agent (session: sqlalchemy.orm.Session, name: str, above: User) -> User:
	"""Make an agent that answers to ``above``, a person or another agent."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=above.id,
	)


def _as (user: User) -> subroutine.domain.authentication.Principal:
	"""Return an account as somebody acting, holding no credential that narrows it."""

	return subroutine.domain.authentication.Principal(user=user)


def _metacortex (
	session: sqlalchemy.orm.Session, founder: User, **members: tuple[User, str]
) -> subroutine.db.models.identity.Workspace:
	"""Make a workspace ``founder`` founded, with each other account in it holding a role."""

	workspace = workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="Metacortex", owner=founder
	)

	for user, role in members.values():
		workspaces.add_member(session, workspace, user, role_key=role)

	return workspace


def _project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	key: str,
	owner: User,
	*,
	visibility: str = "private",
	parent: Project | None = None,
) -> Project:
	"""Make a project ``owner`` owns, as the domain makes one, their row written."""

	return projects.create(
		session,
		workspace_id=workspace.id,
		key=key,
		title=key.title(),
		visibility=visibility,
		owner_id=owner.id,
		parent=parent,
	)


def _sees (session: sqlalchemy.orm.Session, user: User, project: Project) -> bool:
	"""Report whether somebody reaches a project through the statement every listing uses."""

	rows = session.scalars(
		subroutine.domain.scoping.readable_projects(
			session, _as(user), workspace_ids=[project.workspace_id]
		)
	)

	return project.id in {row.id for row in rows}


def _seen_as (session: sqlalchemy.orm.Session, user: User) -> set[uuid.UUID]:
	"""Return whose rows an account sees by, as the SQL walk answers."""

	return set(session.scalars(accountability.sees_as(user.id)))


def _by_hand (session: sqlalchemy.orm.Session, name: str, above: User) -> User:
	"""Write an agent's row directly, for an arrangement the way in refuses to make."""

	username = f"{name}-{uuid.uuid4().hex[:6]}"
	row = User(
		username=username,
		username_normalized=username,
		is_service_account=True,
		responsible_user_id=above.id,
	)
	session.add(row)
	session.flush()

	return row


def test_whose_rows_an_account_sees_by_is_itself_and_every_agent_beneath_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Downward only, through other agents, a stopped one included and a deleted one not.

	Each answer written out, so the two walks agreeing in the next test cannot be two walks that
	both find nothing.
	"""

	keanu = _person(session, "keanu")
	claude = _agent(session, "claude", keanu)
	scout = _agent(session, "scout", claude)
	carrieanne = _person(session, "carrieanne")
	stopped = _agent(session, "stopped", carrieanne)
	laurence = _person(session, "laurence")
	gone = _agent(session, "gone", laurence)
	beneath_gone = _agent(session, "orphan", gone)
	hugo = _person(session, "hugo")

	stopped.is_active = False
	gone.deleted_at = datetime.datetime.now(datetime.UTC)
	# By hand: a person answers for themselves, whatever the column happens to say.
	hugo.responsible_user_id = keanu.id
	session.flush()

	assert _seen_as(session, keanu) == {keanu.id, claude.id, scout.id}, "a person was walked as an agent"
	assert _seen_as(session, claude) == {claude.id, scout.id}, "an agent saw through who it answers to"
	assert _seen_as(session, scout) == {scout.id}
	assert _seen_as(session, carrieanne) == {carrieanne.id, stopped.id}, "a stopped agent's rows went"
	assert _seen_as(session, laurence) == {laurence.id}, "a deleted agent, or what is beneath it, counted"
	assert _seen_as(session, beneath_gone) == {beneath_gone.id}


def test_the_two_walks_beneath_an_account_agree (session: sqlalchemy.orm.Session) -> None:
	"""``sees_as`` in SQL and ``agents_answering_to`` in Python are one walk, on every arrangement.

	The ordinary ones, and the two a database somebody edited can hold: agents that answer to each
	other in a circle, and a chain longer than anything the way in allows. Both walks stop at
	``MAX_DEPTH`` levels, so neither runs for ever and neither sees further than the other.
	"""

	keanu = _person(session, "keanu")
	claude = _agent(session, "claude", keanu)
	scout = _agent(session, "scout", claude)
	second = _agent(session, "second", keanu)
	carrieanne = _person(session, "carrieanne")
	stopped = _agent(session, "stopped", carrieanne)
	laurence = _person(session, "laurence")
	gone = _agent(session, "gone", laurence)
	beneath_gone = _agent(session, "orphan", gone)
	hugo = _person(session, "hugo")
	one = _agent(session, "one", hugo)
	other = _agent(session, "other", hugo)
	marcus = _person(session, "marcus")
	deep = [marcus]

	for level in range(accountability.MAX_DEPTH + 3):
		deep.append(_by_hand(session, f"deep{level}", deep[-1]))

	stopped.is_active = False
	gone.deleted_at = datetime.datetime.now(datetime.UTC)
	one.responsible_user_id = other.id
	other.responsible_user_id = one.id
	session.flush()

	everybody = [
		keanu, claude, scout, second, carrieanne, stopped, laurence, gone, beneath_gone, hugo, one,
		other, *deep,
	]

	for account in everybody:
		walked = {account.id} | accountability.answering_to(session, account)

		assert _seen_as(session, account) == walked, account.username

	assert _seen_as(session, one) == {one.id, other.id}, "a circle was not walked, or not left"
	assert len(_seen_as(session, marcus)) == 1 + accountability.MAX_DEPTH, "the walk has no floor"


def test_whoever_an_agent_answers_to_sees_the_private_project_it_made (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#4940`, as measured: keanu's agent made a private project that keanu could not see or join.

	He sees it, and the work in it, and may share himself in, as its owner's stand-in. The controls
	are everybody the rule does not name: an administrator of the workspace and another member see
	nothing, whatever their role.
	"""

	keanu = _person(session, "keanu")
	carrieanne = _person(session, "carrieanne")
	hugo = _person(session, "hugo")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(
		session,
		keanu,
		carrieanne=(carrieanne, "admin"),
		hugo=(hugo, "member"),
		claude=(claude, "member"),
	)
	lab = _project(session, workspace, "lab", claude)
	task = subroutine.domain.tasks.create(
		session, project=lab, title="Trace the call", actor=_as(claude)
	)

	assert _sees(session, claude, lab)
	assert _sees(session, keanu, lab), "the person an agent answers to cannot see what it made"
	assert not _sees(session, carrieanne, lab), "an administrator saw a project not shared with them"
	assert not _sees(session, hugo, lab)

	for user, expected in ((keanu, True), (claude, True), (carrieanne, False), (hugo, False)):
		found = {
			row.id
			for row in session.scalars(
				subroutine.domain.scoping.readable_tasks(
					session, _as(user), workspace_ids=[workspace.id]
				)
			)
		}

		assert (task.id in found) is expected, user.username

	assert subroutine.domain.authorization.refusal(
		session, _as(keanu), subroutine.permissions.TASK_WRITE, workspace_id=workspace.id, project=lab
	) is None

	projects.share(session, lab, keanu, actor=_as(keanu))

	shared = {holder.id for _row, holder in projects.members(session, lab, actor=_as(keanu))}

	assert shared == {claude.id, keanu.id}, "sharing himself in wrote no row of his own"

	with pytest.raises(subroutine.errors.ValidationError, match="can already see"):
		projects.share(session, lab, keanu, actor=_as(keanu))


def test_sight_never_follows_the_chain_downward (session: sqlalchemy.orm.Session) -> None:
	"""An agent sees nothing through whoever it answers to, nor through an agent above it."""

	keanu = _person(session, "keanu")
	claude = _agent(session, "claude", keanu)
	scout = _agent(session, "scout", claude)
	workspace = _metacortex(session, keanu, claude=(claude, "member"), scout=(scout, "member"))
	vault = _project(session, workspace, "vault", keanu)
	lab = _project(session, workspace, "lab", claude)
	den = _project(session, workspace, "den", scout)

	assert not _sees(session, claude, vault), "an agent saw what is shared with its person"
	assert not _sees(session, scout, vault)
	assert not _sees(session, scout, lab), "an agent saw what is shared with the agent above it"

	blocking = projects.hidden_by(session, vault, claude.id)

	assert blocking is not None and blocking.id == vault.id

	assert _sees(session, claude, den), "an agent cannot see what its own agent made"
	assert _sees(session, keanu, den), "sight stopped one level up the chain"
	assert _sees(session, keanu, lab)
	assert subroutine.domain.repair.repairers(session, den) == {keanu.id}, (
		"the person two levels above a project's only holder is not counted as able to repair it"
	)


def test_a_stopped_agent_still_gives_whoever_it_answers_to_sight (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Stopping an agent does not hide what it was shared into from the person who stands in for it."""

	keanu = _person(session, "keanu")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, keanu, claude=(claude, "member"))
	lab = _project(session, workspace, "lab", claude)

	claude.is_active = False
	session.flush()

	assert _sees(session, keanu, lab)
	assert not _sees(session, claude, lab), "a stopped agent still reads"


def test_an_agent_handed_on_takes_the_sight_it_gave_with_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Sight is asked of the chain as it stands, so it moves when the agent does.

	What the decision's other answer could not do: sharing a project with the person at the top
	when an agent makes one leaves the old person seeing it after a hand-over, and the new one not.
	"""

	keanu = _person(session, "keanu")
	carrieanne = _person(session, "carrieanne")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(
		session, keanu, carrieanne=(carrieanne, "member"), claude=(claude, "member")
	)
	lab = _project(session, workspace, "lab", claude)

	assert _sees(session, keanu, lab) and not _sees(session, carrieanne, lab)

	claude.responsible_user_id = carrieanne.id
	session.flush()

	assert _sees(session, carrieanne, lab), "the new person does not see what their agent holds"
	assert not _sees(session, keanu, lab), "the old person still sees through an agent that left them"


def test_privacy_still_inherits_for_whoever_sees_through_an_agent (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A row on a project inside a private one grants nothing until the one above is shared too."""

	keanu = _person(session, "keanu")
	hugo = _person(session, "hugo")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, hugo, keanu=(keanu, "member"), claude=(claude, "member"))
	ops = _project(session, workspace, "ops", hugo)
	inner = _project(session, workspace, "inner", hugo, parent=ops)

	# By hand, as only a hand could: sharing refuses a row the project above would make worthless.
	session.add(
		subroutine.db.models.project.ProjectMember(
			workspace_id=workspace.id, project_id=inner.id, user_id=claude.id
		)
	)
	session.flush()

	blocking = projects.hidden_by(session, inner, keanu.id)

	assert blocking is not None and blocking.id == ops.id
	assert not _sees(session, keanu, inner) and not _sees(session, claude, inner)

	projects.share(session, ops, claude, actor=_as(hugo))

	assert _sees(session, keanu, ops) and _sees(session, keanu, inner)
	assert projects.hidden_by(session, inner, keanu.id) is None


def test_the_two_forms_of_the_sight_rule_agree_along_a_chain (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The predicate and ``hidden_by`` answer alike for every account and project of an arrangement."""

	keanu = _person(session, "keanu")
	hugo = _person(session, "hugo")
	carrieanne = _person(session, "carrieanne")
	claude = _agent(session, "claude", keanu)
	scout = _agent(session, "scout", claude)
	workspace = _metacortex(
		session,
		hugo,
		keanu=(keanu, "member"),
		carrieanne=(carrieanne, "admin"),
		claude=(claude, "member"),
		scout=(scout, "member"),
	)
	ops = _project(session, workspace, "ops", hugo)
	inner = _project(session, workspace, "inner", hugo, visibility="public", parent=ops)
	lab = _project(session, workspace, "lab", claude)
	den = _project(session, workspace, "den", scout)
	web = _project(session, workspace, "web", hugo, visibility="public")
	vault = _project(session, workspace, "vault", keanu)

	projects.share(session, ops, scout, actor=_as(hugo))

	seen = 0

	for account in (keanu, hugo, carrieanne, claude, scout):
		for project in (ops, inner, lab, den, web, vault):
			listed = _sees(session, account, project)
			seen += listed

			assert listed is (projects.hidden_by(session, project, account.id) is None), (
				account.username,
				project.key,
			)

	# hugo: ops, inner, web. keanu: all six. claude: ops, inner, lab, den, web. scout: ops, inner,
	# den, web. carrieanne: web.
	assert seen == 3 + 6 + 5 + 4 + 1, "the arrangement is not the one the comparison was written for"


def test_unsharing_an_agent_is_refused_where_only_its_person_sees_and_only_through_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Taking an agent's row away can be what leaves a project with nobody, and is refused as such.

	hugo's private project is shared with keanu's agent, and hugo has been stopped: keanu is the one
	person who can see it, through the agent. **The control**: once keanu holds a row of his own, the
	agent's goes.
	"""

	keanu = _person(session, "keanu")
	hugo = _person(session, "hugo")
	laurence = _person(session, "laurence")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(
		session, laurence, hugo=(hugo, "member"), keanu=(keanu, "member"), claude=(claude, "member")
	)
	ops = _project(session, workspace, "ops", hugo)

	projects.share(session, ops, claude, actor=_as(hugo))
	hugo.is_active = False
	session.flush()

	assert subroutine.domain.repair.repairers(session, ops) == {keanu.id}

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		projects.unshare(session, ops, claude, actor=_as(keanu))

	said = str(refused.value)

	assert keanu.username in said and claude.username in said, said
	assert "only through their agent" in said, said
	assert _sees(session, keanu, ops), "the refusal took the row anyway"

	projects.share(session, ops, keanu, actor=_as(keanu))
	projects.unshare(session, ops, claude, actor=_as(keanu))

	assert _sees(session, keanu, ops)
	assert subroutine.domain.repair.repairers(session, ops) == {keanu.id}


def test_somebody_who_also_sees_through_an_agent_may_give_up_their_own_row (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Removing yourself strands nothing while an agent of yours is still shared in."""

	keanu = _person(session, "keanu")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, keanu, claude=(claude, "member"))
	lab = _project(session, workspace, "lab", claude)

	projects.share(session, lab, keanu, actor=_as(keanu))
	projects.unshare(session, lab, keanu, actor=_as(keanu))

	assert _sees(session, keanu, lab)
	assert subroutine.domain.repair.repairers(session, lab) == {keanu.id}


def test_a_project_only_an_agent_holds_is_reached_by_its_person_and_named_when_they_leave (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Nobody needs rescuing into a project its owner's person can see, until that person goes.

	**And through a stopped agent too**: the person holds no row and nothing of theirs that can act
	does, so their departure is named for what they could repair, not only for what they hold.
	"""

	keanu = _person(session, "keanu")
	laurence = _person(session, "laurence")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, laurence, keanu=(keanu, "member"), claude=(claude, "member"))
	lab = _project(session, workspace, "lab", claude)

	def stranded (leaving: User | None = None) -> set[uuid.UUID]:
		"""Return the projects nobody could reach, now or once ``leaving`` has gone."""

		return {row.project.id for row in projects.unreachable(session, leaving=leaving)}

	assert lab.id not in stranded(), "a project its owner's person can see was called unreachable"
	assert lab.id in stranded(keanu)
	assert lab.id not in stranded(laurence)

	claude.is_active = False
	session.flush()

	assert lab.id not in stranded()
	assert lab.id in stranded(keanu), "leaving was not named for a project seen through a stopped agent"


def test_taking_somebody_out_of_a_workspace_counts_what_they_see_through_an_agent (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The last person to see a project is not taken out of its workspace, whoever holds the row.

	**Taking the agent out strands nothing**: its row stays, as a person's does, and whoever it
	answers to goes on seeing through it.
	"""

	keanu = _person(session, "keanu")
	laurence = _person(session, "laurence")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, laurence, keanu=(keanu, "member"), claude=(claude, "member"))
	lab = _project(session, workspace, "lab", claude)

	with pytest.raises(subroutine.errors.ValidationError, match="only person who can see lab"):
		workspaces.remove_member(session, workspace, keanu, actor=_as(laurence))

	workspaces.remove_member(session, workspace, claude, actor=_as(laurence))

	assert _sees(session, keanu, lab)
	assert subroutine.domain.repair.repairers(session, lab) == {keanu.id}


def test_work_is_handed_to_somebody_who_sees_it_through_an_agent (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Seeing through an agent is seeing: its person can be given the work, and nobody else can."""

	keanu = _person(session, "keanu")
	hugo = _person(session, "hugo")
	claude = _agent(session, "claude", keanu)
	workspace = _metacortex(session, keanu, hugo=(hugo, "member"), claude=(claude, "member"))
	lab = _project(session, workspace, "lab", claude)

	task = subroutine.domain.tasks.create(
		session, project=lab, title="Follow the white rabbit", assignee_id=keanu.id, actor=_as(claude)
	)

	assert task.assignee_id == keanu.id

	with pytest.raises(subroutine.errors.ValidationError, match="cannot see lab"):
		subroutine.domain.tasks.create(
			session, project=lab, title="Take the red pill", assignee_id=hugo.id, actor=_as(claude)
		)
