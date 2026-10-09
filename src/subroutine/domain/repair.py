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
* a private project - its workspace's members holding a row on it, whom nothing hides it from.

Removing, demoting or unsharing is refused where it would leave a scope with nobody, and
deactivating somebody is named before and after with each scope's repair, which is the same
answer asked of a future in which they have gone. **Agents are not counted**, reversing `#4020`: an
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
) -> set[uuid.UUID]:
	"""Return the people who could repair ``scope``, by id - `SR#4569`, decision `#4526`.

	``leaving`` asks it of a future in which those accounts have gone, which is what a deactivation
	names before it happens. A caller taking one person out of a scope - removing, demoting or
	unsharing them - takes their id out of the answer, since that is all the act changes.
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
		candidates = _seeing(session, scope, people)

	return {
		one.id
		for one in candidates
		if subroutine.domain.accountability.can_act(session, one, leaving=leaving)
	}


def _seeing (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	people: sqlalchemy.Select[subroutine.db.models.identity.User],
) -> list[subroutine.db.models.identity.User]:
	"""Return the people who belong to a project's workspace, hold a row on it and can see it.

	**A row alone is not enough** (`#1453`, `#2626`): an account taken out of the workspace keeps
	its project rows, and privacy inherits down the tree, so a member of a private project inside
	another they are not a member of cannot see it either. ``hidden_by`` is that rule, asked here
	rather than restated; imported here, since ``domain.projects`` asks this module.
	"""

	from subroutine.domain import projects as listing

	model = subroutine.db.models.identity.User
	membership = subroutine.db.models.project.ProjectMember
	belongs = subroutine.db.models.identity.WorkspaceMember

	found = session.scalars(
		people.join(membership, membership.user_id == model.id)
		.join(
			belongs,
			sqlalchemy.and_(
				belongs.user_id == model.id, belongs.workspace_id == project.workspace_id
			),
		)
		.where(membership.project_id == project.id)
	)

	return [one for one in found if listing.hidden_by(session, project, one.id) is None]


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
