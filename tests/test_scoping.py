"""What a listing is allowed to show, and the check that keeps it that way.

Two of these are regressions for defects that shipped. Both were found by running queries
against a real database with a second user, not by reading the code — and both had a
correct implementation of the same rule sitting a few modules away, which is what makes
"one helper" the fix rather than "more care".
"""

import ast
import pathlib
import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces

SOURCE = pathlib.Path(subroutine.__file__).parent

#: Modules that query the task or project tables without going through
#: :mod:`subroutine.domain.scoping`, each with the reason it is allowed to. Anything not
#: listed here must narrow through the helper — that is the whole instrument, and the
#: reasons are what make adding an entry a decision rather than a reflex (SPEC.md §7.3).
#:
#: The rule of thumb: fetching **one** row by id or ref and then authorising it is safe,
#: because the permission check is what decides whether the caller may have it. Returning
#: **many** rows is where a missing narrowing becomes a disclosure, and that is what the
#: helper is for.
REACHES_DIRECTLY: dict[str, str] = {
	"domain/scoping.py": "the helper itself",
	"domain/authorization.py": "defines the visibility predicate the helper applies",
	"domain/refs.py": "resolves one ref to one id; the caller authorises what it gets",
	"domain/mentions.py": "rewrites refs inside text it was already given",
	"domain/bootstrap.py": "runs before any principal exists, by definition",
	"domain/tasks.py": "single-row reads by id, each followed by an authorize() call",
	"domain/projects.py": "key-uniqueness and subtree maintenance, not caller-facing lists",
	"domain/tags.py": "reads a task's own tag rows, having been handed the task",
	"domain/agenda.py": "builds on the helper and adds only what the agenda means",
	"api/tasks.py": "every listing and lookup starts at readable_tasks; the direct select is "
	"the include_total count, taken over that same narrowed statement as a subquery",
	"api/projects.py": "likewise, over readable_projects",
	"api/views.py": "reads display columns by id for rows the caller already holds; it "
	"decides how a row is rendered, never which rows there are",
	"api/documents.py": "listings start at readable_documents and single-document lookups "
	"go through the same statement; the direct select is the include_total count and the "
	"link lookup, which is keyed to a link already resolved from a visible item",
	"domain/documents.py": "single-row reads by id, each after an authorize() call; the "
	"vocabulary lookups are workspace-scoped and hold no work",
	"domain/links.py": "resolves each end through scoping.readable_tasks/_documents and "
	"drops an end the caller cannot see; the direct select finds link rows, which carry no "
	"content of their own",
}


def _modules_touching_work () -> dict[str, str]:
	"""Return every module under ``src`` that both selects and names a scoped entity.

	Documents joined the list when S3-04 built them: they live in projects and inherit the
	same visibility, so a document listing that forgets to narrow leaks a private project's
	specifications exactly as a task listing leaks its work.
	"""

	found: dict[str, str] = {}

	for path in sorted(SOURCE.rglob("*.py")):
		if "migrations" in path.parts:
			continue

		text = path.read_text(encoding="utf-8")
		names = {
			node.attr
			for node in ast.walk(ast.parse(text))
			if isinstance(node, ast.Attribute)
		}

		if "select" in names and {"Task", "Project", "Document"} & names:
			found[str(path.relative_to(SOURCE))] = text

	return found


def test_no_query_reaches_tasks_or_projects_without_a_written_reason () -> None:
	"""SPEC.md §7.3's instrument: the narrowing is one function, and this is what says so.

	A listing that forgets to narrow does not fail — it returns more rows, to somebody who
	should not have them, and looks exactly like a listing that works. This makes adding
	such a query a thing somebody has to write a sentence about.
	"""

	undeclared = sorted(set(_modules_touching_work()) - set(REACHES_DIRECTLY))

	assert not undeclared, (
		f"These modules query tasks or projects directly: {', '.join(undeclared)}. Narrow "
		f"through subroutine.domain.scoping, or add them to REACHES_DIRECTLY with the "
		f"reason it is safe."
	)


def test_the_exemption_list_names_only_modules_that_exist () -> None:
	"""An exemption for a module that no longer queries anything is noise that hides signal."""

	stale = sorted(set(REACHES_DIRECTLY) - set(_modules_touching_work()))

	assert not stale, f"REACHES_DIRECTLY names modules that no longer qualify: {', '.join(stale)}."


class World(typing.NamedTuple):
	"""A workspace with a private project and an ordinary one."""

	workspace: subroutine.db.models.identity.Workspace
	owner: subroutine.db.models.identity.User
	outsider: subroutine.db.models.identity.User
	private: subroutine.db.models.project.Project
	public: subroutine.db.models.project.Project


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""Build a workspace whose two members can see different things."""

	owner = subroutine.domain.users.create(session, username=f"owner-{uuid.uuid4().hex[:8]}")
	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=owner
	)
	subroutine.domain.workspaces.add_member(session, workspace, outsider, role_key="member")

	private = subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key="SECRET",
		title="Secret",
		visibility="private",
		owner_id=owner.id,
	)
	public = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="OPEN", title="Open"
	)

	subroutine.domain.tasks.create(session, project=private, title="Acquire the rival company")
	subroutine.domain.tasks.create(session, project=public, title="Ordinary work")
	session.flush()

	return World(
		workspace=workspace, owner=owner, outsider=outsider, private=private, public=public
	)


def _titles (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
) -> list[str]:
	"""Return the titles a principal's task listing shows."""

	return sorted(
		task.title
		for task in session.scalars(
			subroutine.domain.scoping.readable_tasks(principal, workspace_ids=[workspace.id])
		)
	)


def test_a_listing_hides_a_private_project_from_a_non_member (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The defect ``subroutine ls`` shipped with.

	It filtered by workspace and never joined the project, so every member of the workspace
	could read the titles of work in a private project they had no part in.
	"""

	outsider = subroutine.domain.authentication.Principal(user=world.outsider)

	assert _titles(session, outsider, world.workspace) == ["Ordinary work"]


def test_the_owner_of_a_private_project_still_sees_it (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The narrowing has to be a narrowing, not a wall."""

	owner = subroutine.domain.authentication.Principal(user=world.owner)

	assert _titles(session, owner, world.workspace) == [
		"Acquire the rival company",
		"Ordinary work",
	]


def test_a_superuser_is_narrowed_by_privacy_too (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Roles are bypassed for a superuser; visibility is not (SPEC.md §7.3).

	A privacy control a role can override is not a privacy control.
	"""

	world.outsider.is_superuser = True
	session.flush()

	elevated = subroutine.domain.authentication.Principal(user=world.outsider)

	assert _titles(session, elevated, world.workspace) == ["Ordinary work"]


def test_a_project_scoped_token_narrows_the_listing (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The second defect: the scope refused writes elsewhere and then listed everything.

	SPEC.md §7.3 calls ``project_scope`` a restriction on *which rows*, and a listing is
	exactly the thing that decides which rows.
	"""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="scoped", project_scope=[str(world.public.id)]
	)
	session.flush()

	scoped = subroutine.domain.authentication.Principal(user=world.owner, token=token)

	assert _titles(session, scoped, world.workspace) == ["Ordinary work"]


def test_a_project_scope_carries_the_subtree_into_the_listing (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Scoping to a project includes what is underneath it.

	Restricting an agent to a project and then hiding its sub-projects would make the
	restriction useless below one level — the same reading the per-project check takes.
	"""

	child = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="CHILD", title="Child", parent=world.public
	)
	subroutine.domain.tasks.create(session, project=child, title="Work underneath")
	session.flush()

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="scoped", project_scope=[str(world.public.id)]
	)
	session.flush()

	scoped = subroutine.domain.authentication.Principal(user=world.owner, token=token)

	assert _titles(session, scoped, world.workspace) == ["Ordinary work", "Work underneath"]


def test_a_null_project_scope_narrows_nothing (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The sentinel, in the direction that fails open if read backwards (SPEC.md §7.3)."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="unscoped"
	)
	session.flush()

	unscoped = subroutine.domain.authentication.Principal(user=world.owner, token=token)

	assert token.project_scope is None
	assert _titles(session, unscoped, world.workspace) == [
		"Acquire the rival company",
		"Ordinary work",
	]


def test_the_agenda_honours_a_project_scoped_token (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The agenda now narrows through the same helper as everything else."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="scoped", project_scope=[str(world.public.id)]
	)
	session.flush()

	scoped = subroutine.domain.authentication.Principal(user=world.owner, token=token)
	agenda = subroutine.domain.agenda.build(
		session,
		principal=scoped,
		workspace_ids=[world.workspace.id],
		now=subroutine.db.types.utcnow(),
		timezone="UTC",
	)

	assert [task.title for task in agenda.unscheduled] == ["Ordinary work"]


def test_an_empty_workspace_list_returns_nothing_rather_than_everything (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""``IN ()`` means none, and the helper never lets it come to mean all.

	A listing that spans every workspace when handed an empty list is one refactor away
	from spanning every workspace belonging to everybody.
	"""

	owner = subroutine.domain.authentication.Principal(user=world.owner)
	found = session.scalars(
		subroutine.domain.scoping.readable_tasks(owner, workspace_ids=[])
	).all()

	assert list(found) == []
