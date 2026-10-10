"""Every owner's default is the domain's, and a document's owner is its maintainer - `SR#4573`.

Decision `#4523`, on Q9, F D2, F D5 and C section 2.5 of the cold review of 2026-10-05. *The creator
owns what they create* was written in four doors and nowhere in the domain, whose default was nobody;
making a private project for somebody else shared them in and not its maker, who lost sight of what
they had just made; and *owner* meant three things, of which a document's is attribution with no
authority, as a task's assignee is.
"""

import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views
import test_api_journal
import test_api_tasks

projects = subroutine.domain.projects


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


def _sees (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether somebody reaches a project through the statement every listing uses."""

	rows = session.scalars(
		subroutine.domain.scoping.readable_projects(
			session, _as(user), workspace_ids=[project.workspace_id]
		)
	)

	return project.id in {row.id for row in rows}


def test_whoever_makes_a_project_or_writes_a_document_owns_it_whichever_door (
	session: sqlalchemy.orm.Session,
) -> None:
	"""C section 2.5: the domain's default was nobody, so a caller that forgot made a project only an
	administrator could hide, and a document nobody maintains. **The domain sets it**, for a person and
	for an agent, who writes as itself.
	"""

	keanu = _person(session, "keanu")
	bot = subroutine.domain.users.create(
		session,
		username=f"bot-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=keanu.id,
	)
	workspace = _metacortex(session, keanu, bot)
	web = projects.create(session, workspace_id=workspace.id, key="web", title="Web", actor=_as(keanu))
	decision = subroutine.domain.documents.create(
		session, project=web, title="We ship on Thursdays", actor=_as(bot)
	)

	assert web.owner_id == keanu.id and _sees(session, keanu, web)
	assert decision.owner_id == bot.id


def test_making_a_private_project_for_somebody_else_keeps_its_maker_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""D2b, as measured: keanu made a private ``vault`` naming alice its owner, and could no longer see
	it. **Making it and handing it over**: he owns it first and keeps his share, then hands it to
	alice, who is shared in as any new owner is - and the record says both.
	"""

	keanu = _person(session, "keanu")
	alice = _person(session, "alice")
	workspace = _metacortex(session, keanu, alice)
	vault = projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=alice.id,
		actor=_as(keanu),
	)

	assert vault.owner_id == alice.id
	assert _sees(session, keanu, vault) and _sees(session, alice, vault)

	recorded = session.scalars(
		sqlalchemy.select(subroutine.db.models.activity.Event.action)
		.where(
			subroutine.db.models.activity.Event.entity_type == "project",
			subroutine.db.models.activity.Event.entity_id == vault.id,
		)
		.order_by(subroutine.db.models.activity.Event.seq)
	).all()

	assert list(recorded) == ["created", "updated"]


def test_a_project_is_made_for_nobody_who_could_not_see_it_and_then_is_not_made_at_all (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The new owner is asked about before anything is made, so a refusal leaves nothing behind."""

	keanu = _person(session, "keanu")
	erin = _person(session, "erin")
	workspace = _metacortex(session, keanu, erin)
	vault = projects.create(
		session,
		workspace_id=workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		actor=_as(keanu),
	)

	with pytest.raises(subroutine.errors.ValidationError):
		projects.create(
			session,
			workspace_id=workspace.id,
			key="deeper",
			title="Deeper",
			parent=vault,
			owner_id=erin.id,
			actor=_as(keanu),
		)

	made = session.scalars(
		sqlalchemy.select(subroutine.db.models.project.Project.key).where(
			subroutine.db.models.project.Project.workspace_id == workspace.id
		)
	).all()

	assert "deeper" not in made


def test_a_documents_owner_is_called_its_maintainer_and_a_projects_its_owner () -> None:
	"""Decision `#4523`: one meaning per word. The column keeps its name on the wire."""

	assert subroutine.views.field_in_words("owner_id", entity_type="document") == "maintainer"
	assert subroutine.views.field_in_words("owner_id", entity_type="project") == "owner"
	assert subroutine.views.fields_in_words({"owner_id": {}}, entity_type="document") == ["maintainer"]


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_the_journal_calls_a_documents_owner_its_maintainer_and_a_projects_its_owner (
	world: test_api_tasks.World,
) -> None:
	"""What a person reads about a change, through the journal every surface renders."""

	erin = subroutine.domain.users.create(world.session, username=f"erin-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(world.session, world.workspace, erin, role_key="member")
	document = world.call("POST", "/v1/documents", json={"title": "How we ship"}).json()
	project = world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"}).json()

	for path in (f"/v1/documents/{document['ref']}", "/v1/projects/web"):
		changed = world.call("PATCH", path, json={"owner_id": str(erin.id)})

		assert changed.status_code == 200, changed.text

	test_api_journal._settled(world.session)

	said = {
		(entry["entity_type"], change["said"])
		for entry in world.call("GET", "/v1/journal", params={"limit": 200}).json()["items"]
		for change in entry.get("changed") or []
		if change["field"] == "owner_id"
	}

	assert said == {("document", "maintainer"), ("project", "owner")}, said
	assert project["owner_id"] == str(world.user.id)
