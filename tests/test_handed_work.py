"""Work is handed only to a member of the workspace who can see it - `SR#4571`.

Decision `#4509`, on S14 and E #9 of the cold review of 2026-10-05: a task in a private project was
assigned, and a document there given a maintainer, who could not see it; a project's owner was asked
only whether they belonged to the workspace; and the task domain took an assignee from no workspace
at all, since only the doors asked. **Both questions, at every way of naming one**: membership, and
sight of where the work is - sight alone took an outsider on a public project.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks

projects = subroutine.domain.projects


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""A seeded workspace with the real application in front of it."""

	return test_api_tasks._world(session)


def _person (session: sqlalchemy.orm.Session, name: str) -> subroutine.db.models.identity.User:
	"""Make a person with a name nobody else in the run has."""

	return subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")


def _as (user: subroutine.db.models.identity.User) -> subroutine.domain.authentication.Principal:
	"""Return an account as somebody acting, holding no credential that narrows it."""

	return subroutine.domain.authentication.Principal(user=user)


def _metacortex (
	session: sqlalchemy.orm.Session,
	founder: subroutine.db.models.identity.User,
	*members: subroutine.db.models.identity.User,
) -> subroutine.db.models.identity.Workspace:
	"""Make a workspace ``founder`` founded, with each other account in it as a member."""

	workspace = subroutine.domain.workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="Metacortex", owner=founder
	)

	for user in members:
		subroutine.domain.workspaces.add_member(session, workspace, user, role_key="member")

	return workspace


def _refused (field: str, refusal: pytest.ExceptionInfo[subroutine.errors.ValidationError]) -> str:
	"""Return a refusal's detail, having checked it names the field the caller sent."""

	assert [error.field for error in refusal.value.errors] == [field], refusal.value.errors

	return refusal.value.detail


def test_private_work_is_handed_only_to_somebody_who_can_see_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""S14, as measured: keanu owns the private ``vault``; erin is a member who is not shared in. A
	vault task assigned to them, a vault document they were named to maintain, and a project inside
	the vault handed to them were each taken, and they could see none of them. **Refused, each naming
	the field**; and once they are shared in, each is theirs.
	"""

	keanu = _person(session, "keanu")
	erin = _person(session, "erin")
	workspace = _metacortex(session, keanu, erin)
	vault = projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=keanu.id,
		actor=_as(keanu),
	)
	inner = projects.create(
		session, workspace_id=workspace.id, key="inner", title="Inner", parent=vault,
		owner_id=keanu.id, actor=_as(keanu),
	)
	task = subroutine.domain.tasks.create(session, project=vault, title="Count it", actor=_as(keanu))

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.tasks.update(session, task, assignee_id=erin.id, actor=_as(keanu))

	assert _refused("assignee", refusal) == (
		f"{erin.username} cannot see vault, so this task cannot be assigned to them."
	)
	assert refusal.value.errors[0].hint == (
		f"'subroutine project share vault {erin.username}' first, or choose somebody who can see it."
	)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.tasks.create(
			session, project=vault, title="Count it again", assignee_id=erin.id, actor=_as(keanu)
		)

	_refused("assignee", refusal)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.documents.create(
			session, project=vault, title="How we count", owner_id=erin.id, actor=_as(keanu)
		)

	_refused("owner_id", refusal)

	handings: tuple[typing.Callable[[], object], ...] = (
		lambda: projects.update(session, inner, owner_id=erin.id, actor=_as(keanu)),
		lambda: projects.create(
			session, workspace_id=workspace.id, key="deeper", title="Deeper", parent=vault,
			owner_id=erin.id, actor=_as(keanu),
		),
	)

	for handed in handings:
		with pytest.raises(subroutine.errors.ValidationError) as refusal:
			handed()

		_refused("owner_id", refusal)

	assert task.assignee_id is None and inner.owner_id == keanu.id

	projects.share(session, vault, erin, actor=_as(keanu))

	subroutine.domain.tasks.update(session, task, assignee_id=erin.id, actor=_as(keanu))
	subroutine.domain.documents.create(
		session, project=vault, title="How we count", owner_id=erin.id, actor=_as(keanu)
	)
	projects.update(session, inner, owner_id=erin.id, actor=_as(keanu))

	assert task.assignee_id == erin.id and inner.owner_id == erin.id


def test_handing_over_a_private_project_itself_shares_its_new_owner_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The project handed over is not what hides it from its new owner: the hand-over shares them in.
	Only a private project above it decides.
	"""

	keanu = _person(session, "keanu")
	erin = _person(session, "erin")
	workspace = _metacortex(session, keanu, erin)
	vault = projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=keanu.id,
		actor=_as(keanu),
	)

	projects.update(session, vault, owner_id=erin.id, actor=_as(keanu))

	assert vault.owner_id == erin.id


def test_a_task_is_assigned_only_to_a_member_of_its_workspace_whichever_door_names_them (
	session: sqlalchemy.orm.Session,
) -> None:
	"""E #9: the domain took an assignee from no workspace at all, since only the doors asked. **Refused
	in the domain**, on a public project, which sight alone would have let through.
	"""

	keanu = _person(session, "keanu")
	stranger = _person(session, "stranger")
	workspace = _metacortex(session, keanu)
	web = projects.create(
		session, workspace_id=workspace.id, key="web", title="Web", owner_id=keanu.id, actor=_as(keanu)
	)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.tasks.create(
			session, project=web, title="Ship it", assignee_id=stranger.id, actor=_as(keanu)
		)

	_refused("assignee", refusal)


def test_somebody_who_may_not_share_is_pointed_at_who_may (session: sqlalchemy.orm.Session) -> None:
	"""A remedy the caller can take: bob, shared into keanu's vault, may not share it, so the hint
	names its owner rather than the command bob would be refused.
	"""

	keanu = _person(session, "keanu")
	bob = _person(session, "bob")
	erin = _person(session, "erin")
	workspace = _metacortex(session, keanu, bob, erin)
	vault = projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=keanu.id,
		actor=_as(keanu),
	)
	projects.share(session, vault, bob, actor=_as(keanu))
	task = subroutine.domain.tasks.create(session, project=vault, title="Count it", actor=_as(bob))

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.tasks.update(session, task, assignee_id=erin.id, actor=_as(bob))

	assert refusal.value.errors[0].hint == (
		"Choose somebody who can see it, or ask the owner of 'vault' to share it with them."
	)


def test_the_refusal_names_the_field_over_http (world: test_api_tasks.World) -> None:
	"""The doors pass the domain's refusal on, naming what the caller sent."""

	erin = subroutine.domain.users.create(world.session, username=f"erin-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(world.session, world.workspace, erin, role_key="member")
	made = world.call(
		"POST", "/v1/projects", json={"key": "vault", "title": "Vault", "visibility": "private"}
	)

	assert made.status_code == 201, made.text

	refused = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Count it", "project": "vault", "assignee": erin.username},
	)

	assert refused.status_code == 422, refused.text
	assert refused.json()["errors"][0]["field"] == "assignee", refused.text
