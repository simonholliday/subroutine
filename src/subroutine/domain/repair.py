"""Who could repair a scope if it were left with nobody - one predicate for every last one.

`SR#4569`, decision `#4526`, on G9 to G11 of the cold review of 2026-10-05. **Three "last one" checks
counted three different things**: the installation counted people, a workspace's administrators
counted agents too, so its founding owner could be removed while only an agent administered it;
ownership had no invariant at all; and ``unshare`` counted rows, those of people who had left
included, so the last member who could act could strand a private project.

**The people who could repair a scope are people, not agents, who can act, belong there and hold
what administers it**, and for a private project can see it:

* the installation - its superusers;
* a workspace's administration - its members whose role holds ``workspace:admin``;
* a workspace's ownership - its members whose role holds ``workspace:delete``;
* a private project - its workspace's members whom nothing hides it from: it is shared with
  them, or with an agent that answers to them (`SR#4944`).

**A scope keeps somebody who can repair it** (decision `#4943`, which states this once). An act
taken inside it - removing, demoting, unsharing, leaving, and making a project private or moving one
across a privacy boundary (`SR#5101`, decision `#5097`) - is refused where it would leave nobody.
Stopping an account is taken from above the scope, so no workspace or project blocks it: it is
named before and after with each scope's repair, which is the same answer asked of a future in
which they have gone, and an administrator of the installation repairs what it leaves. The
installation has nothing above it, so its last administrator who can act is not stopped either.
**Agents are not counted**, reversing `#4020`: an
agent's standing ends with its person's, so a scope only an agent could repair is one a single
departure strands.
"""

import dataclasses
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.accountability
import subroutine.permissions

#: The installation, which has no row of its own to name it by - the one scope named by a string.
INSTALLATION = "installation"


@dataclasses.dataclass(frozen=True)
class Administration:
	"""A workspace's administration: who may add, regrade and remove its members."""

	workspace_id: uuid.UUID


@dataclasses.dataclass(frozen=True)
class Ownership:
	"""A workspace's ownership: who may make an owner, and delete the workspace."""

	workspace_id: uuid.UUID


@dataclasses.dataclass(frozen=True)
class Afterwards:
	"""Who could see a project once an act is taken, asked before it is - `SR#5101`.

	**Computed, not rehearsed** (decision `#5097`): ``private`` is the projects the act makes
	private, ``moved`` the project it moves and ``above`` the path it moves to, outermost first and
	ending at the new parent, or empty for the top level. ``granted`` is the rows the act writes, as
	a project and an account: a new owner is shared in by the same act that makes the project private.
	"""

	private: frozenset[uuid.UUID] = frozenset()
	moved: uuid.UUID | None = None
	above: tuple[uuid.UUID, ...] = ()
	granted: frozenset[tuple[uuid.UUID, uuid.UUID]] = frozenset()

	def path (self, identifiers: list[uuid.UUID]) -> list[uuid.UUID]:
		"""Return a project's path, outermost first, as it will be once the act is taken."""

		if self.moved is None or self.moved not in identifiers:
			return identifiers

		return [*self.above, *identifiers[identifiers.index(self.moved) :]]


#: Every scope a last one is held for. A private project is named by its row.
Scope = str | Administration | Ownership | subroutine.db.models.project.Project

#: The verb a workspace's role must hold for each of its two scopes.
_VERB: dict[type, str] = {
	Administration: subroutine.permissions.WORKSPACE_ADMIN,
	Ownership: subroutine.permissions.WORKSPACE_DELETE,
}


def repairers (
	session: sqlalchemy.orm.Session,
	scope: Scope,
	*,
	leaving: typing.Collection[uuid.UUID] = (),
	unsharing: subroutine.db.models.project.ProjectMember | None = None,
	afterwards: Afterwards | None = None,
) -> set[uuid.UUID]:
	"""Return the people who could repair ``scope``, by id - `SR#4569`, decision `#4526`.

	``leaving`` asks it of a future in which those accounts have gone, which is what a deactivation
	names before it happens. A caller taking one person out of a workspace's scope - removing or
	demoting them - takes their id out of the answer, since that is all the act changes.
	**``unsharing`` asks it of a private project with one row gone** (`SR#4944`): an agent's row
	is what whoever it answers to sees by, so taking it away can change somebody else's answer.
	**``afterwards`` asks it of a private project once a change to who sees it is made** (`SR#5101`):
	made private, moved, or given a new owner.
	"""

	model = subroutine.db.models.identity.User
	people = sqlalchemy.select(model).where(
		subroutine.domain.accountability.live(model), model.is_service_account.is_(False)
	)

	if isinstance(scope, str):
		candidates = list(session.scalars(people.where(model.is_superuser.is_(True))))

	elif isinstance(scope, (Administration, Ownership)):
		member = subroutine.db.models.identity.WorkspaceMember
		role = subroutine.db.models.identity.Role
		rows = session.execute(
			people.add_columns(role.permissions)
			.join(member, member.user_id == model.id)
			.join(role, role.id == member.role_id)
			.where(member.workspace_id == scope.workspace_id)
		).all()
		candidates = [held for held, permissions in rows if _VERB[type(scope)] in (permissions or [])]

	else:
		candidates = _seeing(session, scope, people, unsharing, afterwards)

	return {
		one.id
		for one in candidates
		if subroutine.domain.accountability.can_act(session, one, leaving=leaving)
	}


def _seeing (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	people: sqlalchemy.Select[subroutine.db.models.identity.User],
	unsharing: subroutine.db.models.project.ProjectMember | None = None,
	afterwards: Afterwards | None = None,
) -> list[subroutine.db.models.identity.User]:
	"""Return the people who belong to a project's workspace and can see it.

	**A row alone is not enough** (`#1453`, `#2626`): an account taken out of the workspace keeps
	its project rows, and privacy inherits down the tree, so a member of a private project inside
	another they are not a member of cannot see it either. ``hidden_by`` is that rule, asked here
	rather than restated; imported here, since ``domain.projects`` asks this module.

	**Nor is a row of their own needed** (`SR#4944`, decision `#4943`): sight follows the chain
	upward, so whoever an agent answers to sees what is shared with it. So ``hidden_by`` is asked
	of everybody a row could give sight to: whoever holds one, and every account above an agent
	that does.
	"""

	from subroutine.domain import projects as listing

	model = subroutine.db.models.identity.User
	membership = subroutine.db.models.project.ProjectMember
	belongs = subroutine.db.models.identity.WorkspaceMember

	holders = list(
		session.scalars(
			sqlalchemy.select(model)
			.join(membership, membership.user_id == model.id)
			.where(membership.project_id == project.id)
		)
	)

	# **And whoever the act shares in** (`SR#5101`): a new owner's row is written by the same act.
	if afterwards is not None:
		for identifier, account in afterwards.granted:
			granted = session.get(model, account) if identifier == project.id else None

			if granted is not None:
				holders.append(granted)

	given: set[uuid.UUID] = set()

	for holder in holders:
		given.update(_above(session, holder))

	found = session.scalars(
		people.join(
			belongs,
			sqlalchemy.and_(
				belongs.user_id == model.id, belongs.workspace_id == project.workspace_id
			),
		).where(model.id.in_(given))
	)

	return [
		one
		for one in found
		if listing.hidden_by(session, project, one.id, without=unsharing, afterwards=afterwards)
		is None
	]


def _above (
	session: sqlalchemy.orm.Session, account: subroutine.db.models.identity.User
) -> set[uuid.UUID]:
	"""Return an account and every account it answers to, however its chain is written.

	**Wider than the chain a door would walk, never narrower**: this only says who to ask
	``hidden_by`` about, which is where the rule is, so a chain that loops or runs long is followed
	as far as it goes and nobody is refused for it.
	"""

	model = subroutine.db.models.identity.User
	met: set[uuid.UUID] = set()
	current: subroutine.db.models.identity.User | None = account

	while current is not None and current.id not in met:
		met.add(current.id)

		if not current.is_service_account or current.responsible_user_id is None:
			break

		if len(met) > subroutine.domain.accountability.MAX_DEPTH:
			break

		current = session.get(model, current.responsible_user_id)

	return met


def departing (
	session: sqlalchemy.orm.Session, leaving: subroutine.db.models.identity.User
) -> set[uuid.UUID]:
	"""Return who stops when ``leaving`` goes: they and every agent answering to them, that can act.

	**That can act now**, so a scope somebody already gone held a part in is not blamed on their
	departure a second time: it was left so already, which a listing without ``leaving`` says.
	"""

	return {
		one.id
		for one in (leaving, *subroutine.domain.accountability.agents_answering_to(session, leaving))
		if subroutine.domain.accountability.can_act(session, one)
	}


def takes_part (
	session: sqlalchemy.orm.Session,
	scope: Administration | Ownership | subroutine.db.models.project.Project,
	accounts: typing.Collection[uuid.UUID],
) -> bool:
	"""Report whether any of these accounts, agents included, holds a part in ``scope`` now.

	**What a departure is named for** (`SR#4569`): a workspace only an agent administers has no
	person who could repair it already, so asking only *nobody afterwards* would stop naming it when
	the agent's person leaves - the regression the verification measured (G9 of the cold review of
	2026-10-05). It is named where the leaver, or an agent answering to them, administers it or
	sees it - or owns it - and nobody could repair it afterwards.
	"""

	if not accounts:
		return False

	if isinstance(scope, (Administration, Ownership)):
		member = subroutine.db.models.identity.WorkspaceMember
		role = subroutine.db.models.identity.Role

		return any(
			_VERB[type(scope)] in (permissions or [])
			for permissions in session.scalars(
				sqlalchemy.select(role.permissions)
				.join(member, member.role_id == role.id)
				.where(member.workspace_id == scope.workspace_id, member.user_id.in_(accounts))
			)
		)

	membership = subroutine.db.models.project.ProjectMember

	return (
		session.scalars(
			sqlalchemy.select(membership.id).where(
				membership.project_id == scope.id, membership.user_id.in_(accounts)
			)
		).first()
		is not None
	)
