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
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions

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
	"domain/authentication.py": "reads a project's `path` while issuing a credential, to place "
	"the write set inside the reach (`#413`). **Deliberately not narrowed by visibility**, and "
	"that is the point rather than an oversight: the question is the shape of the tree, not who "
	"may look at it, and `_canonical_project_scope` has always allowed a credential to name a "
	"project its issuer cannot see or one created later. Narrowing here would turn 'is this "
	"under that' into 'can you see this', refusing a legitimate credential for the wrong "
	"reason. Nothing is reported back but the key of a project the caller itself named, and "
	"`_refuse_amplification` has already bounded the caller to its own reach before it runs",
	"domain/mentions.py": "rewrites refs inside text it was already given",
	"domain/bootstrap.py": "runs before any principal exists, by definition",
	"domain/tasks.py": "single-row reads by id, each followed by an authorize() call",
	"domain/projects.py": "key-uniqueness and subtree maintenance, not caller-facing lists",
	"domain/tags.py": "reads a task's own tag rows, having been handed the task",
	"domain/events.py": "`descriptions()` reads titles and refs by id for events the caller "
	"already holds — the feed narrowed them through visible_events, a history resolved its "
	"subject — so the rows are vetted before this sees them. Same standing as views.py: it "
	"decides how a row is *named*, never which rows there are. It deliberately applies no "
	"deleted or archived filter, because a deletion is the event most worth reporting and an "
	"item in the trash still has to be nameable in the line that says it went there",
	"domain/agenda.py": "builds on the helper and adds only what the agenda means",
	"api/tasks.py": "every listing and lookup starts at readable_tasks; the direct select is "
	"the include_total count, taken over that same narrowed statement as a subquery",
	"api/projects.py": "likewise, over readable_projects",
	"views.py": "reads display columns by id for rows the caller already holds; it "
	"decides how a row is rendered, never which rows there are",
	"api/documents.py": "listings start at readable_documents and single-document lookups "
	"go through the same statement; the direct select is the include_total count and the "
	"link lookup, which is keyed to a link already resolved from a visible item",
	"domain/documents.py": "single-row reads by id, each after an authorize() call; the "
	"vocabulary lookups are workspace-scoped and hold no work",
	"domain/readiness.py": "builds *predicates* the caller applies to a statement that already "
	"started at readable_tasks — and, since `#425`, one row set: `blocked_among` answers "
	"'which of these are blocked' for a page whose ids the caller has already fetched through "
	"the helper, so it narrows a set that was narrowed, and can only ever return a subset of "
	"what it was given. **This entry said 'never a row set' until then**, which is the kind of "
	"excuse that outlives its own reason. Both paths read blocker tasks **without narrowing by "
	"visibility, deliberately**: readiness is a fact about the work, not about the viewer, and "
	"counting only the blockers a caller can see would report an item as startable when it is "
	"not. The alternative leaks less and lies, and what this discloses is bounded — that "
	"something unseen blocks an item, never what",
	"domain/links.py": "resolves each end through scoping.readable_tasks/_documents and "
	"drops an end the caller cannot see; the direct select finds link rows, which carry no "
	"content of their own",
	"clients/local.py": "every task, project and document it reaches goes through "
	"scoping.readable_tasks/_projects/_documents — including `_in_the_trash_too`, which widens "
	"to deleted rows and narrows nowhere else. The one direct select is over `Link`, which "
	"carries no content of its own and is already bounded to a subject resolved through "
	"scoping; it is here because this module names `Task` and `Document` on nearly every line, "
	"so the detector cannot tell that select from the ones that are narrowed",
	"domain/comments.py": "every caller-facing path resolves the subject through "
	"scoping.readable_tasks/_projects/_documents; the one direct `session.get` is the "
	"actor=None branch, which is the unauthenticated internal caller that has no principal to "
	"scope by — the same escape hatch every service here has, and the one "
	"test_actor_discipline guards",
}


def _modules_touching_work (root: pathlib.Path = SOURCE) -> dict[str, str]:
	"""Return every module under ``root`` that both selects and names a scoped entity.

	Documents joined the list when S3-04 built them: they live in projects and inherit the
	same visibility, so a document listing that forgets to narrow leaks a private project's
	specifications exactly as a task listing leaks its work.

	**The tree is an argument so that the guard can be shown a defect** (`#405`). The pair of
	tests below already catch a walk that reads nothing — an empty scan makes every exemption
	look stale — but they cannot say the *rule* still matches, which is the half that quietly
	stops working when somebody renames a model.
	"""

	found: dict[str, str] = {}

	for path in sorted(root.rglob("*.py")):
		if "migrations" in path.parts:
			continue

		text = path.read_text(encoding="utf-8")
		names = {
			node.attr
			for node in ast.walk(ast.parse(text))
			if isinstance(node, ast.Attribute)
		}

		if "select" in names and {"Task", "Project", "Document"} & names:
			found[str(path.relative_to(root))] = text

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
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=owner.id,
	)
	public = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="open", title="Open"
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
		session, workspace_id=world.workspace.id, key="child", title="Child", parent=world.public
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


def test_an_empty_project_scope_admits_nothing_rather_than_everything (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`#201`. The other sentinel, in the direction that failed open — and it did.

	``None`` narrows nothing; ``[]`` is a restriction naming no project, which is what
	``authorization._within_project_scope`` has always returned ``False`` for. The predicate
	built ``sqlalchemy.or_()`` with no clauses, which renders as nothing, so the ``WHERE``
	lost the restriction and the listing returned every project — the two copies of one rule
	disagreeing on one edge, in opposite directions.

	**Set on the token row rather than issued**, because ``issue_token`` refuses an empty list
	and should go on refusing it. That refusal is what makes this unreachable today and is
	exactly why the test has to reach around it: a guard resting on a validator two modules
	away is one that stops holding the moment anything else writes the column.
	"""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="scoped", project_scope=[str(world.public.id)]
	)
	token.project_scope = []
	session.flush()

	starved = subroutine.domain.authentication.Principal(user=world.owner, token=token)

	# **The predicate itself, not only the rows.** Under `filterwarnings = ["error"]` the
	# unfixed code raises on `or_()` before a single row is read, so a row assertion alone
	# would fail for the wrong reason — and would go on "passing" the day SQLAlchemy stops
	# warning about it, when the empty clause list would once again render as nothing at all
	# and quietly take the restriction out of the `WHERE`. That is the shape this guard has to
	# be written from: the property, not the symptom.
	assert str(subroutine.domain.scoping.within_project_scope(starved)) == "false"

	assert _titles(session, starved, world.workspace) == []

	# And the two copies agree about it, which is the part that was wrong.
	assert not subroutine.domain.authorization._within_project_scope(starved, world.public)


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


def _scoped_to (
	session: sqlalchemy.orm.Session,
	world: World,
	*projects: subroutine.db.models.project.Project,
) -> subroutine.domain.authentication.Principal:
	"""Return the owner, presenting a credential restricted to those projects."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=world.owner,
		title="Bounded",
		project_scope=[str(project.id) for project in projects],
	)
	session.flush()

	return subroutine.domain.authentication.Principal(user=world.owner, token=token)


def test_a_credential_reaching_one_project_files_there_rather_than_the_inbox (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`#369`. The Inbox default sent every bounded agent's first write into a refusal.

	§1.4's whole shape is that ``subroutine add "something"`` works without knowing projects
	exist, and it does that by defaulting to the workspace's Inbox — which is *outside* the
	reach of a credential scoped to a project. Measured over HTTPS before this: ``403 Not
	permitted``, on the primary capture path, for exactly the credentials `#216` exists to
	create.

	The rule is unchanged and only its application widens: **the default is the only place the
	caller could have meant.** For a credential that reaches one project, that is the project.
	"""

	bounded = _scoped_to(session, world, world.public)
	filed = subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert filed.id == world.public.id

	# And the unrestricted caller still gets the Inbox, which is the behaviour §1.4 needs and
	# the reason this could not simply be changed for everybody.
	owner = subroutine.domain.authentication.Principal(user=world.owner)
	inbox = subroutine.domain.bootstrap.inbox_for(session, world.workspace)

	assert inbox is not None
	assert (
		subroutine.domain.selection.project(session, owner, world.workspace, None).id
		== inbox.id
	)


def test_a_credential_reaching_the_inbox_still_files_there (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""A scope that names the Inbox means the Inbox, and the narrower rule must not override it."""

	inbox = subroutine.domain.bootstrap.inbox_for(session, world.workspace)

	assert inbox is not None

	bounded = _scoped_to(session, world, inbox, world.public)
	filed = subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert filed.id == inbox.id, "an explicitly reachable Inbox wins over the one-project rule"


def test_a_credential_with_one_writable_project_files_there (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Item ``#416``. `#369` again, on the list `#371` added and this had not been taught.

	The ``collaborator`` shape — reach two, write one — is what decision `#370` was taken for,
	and its first ``subroutine add "something"`` was refused as *"could be filed under any of
	several projects"*, with exactly one of them legal and the answer sitting unread in the
	credential's own write set.

	The rule is the one this function has always applied and nothing more: **the default is the
	only place the caller could have meant.** What changed is which list answers "could".

	**Falsified against the original code**: narrow by ``actor.project_scope`` again and this
	raises. Its neighbours all pass either way — they use credentials with no write set, where
	the reach *is* the answer, which is why the case survived `#371` shipping.
	"""

	bounded = _reaching_writing(
		session, world, reach=[world.public, world.private], writes=[world.public]
	)
	filed = subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert filed.id == world.public.id

	# The captured line is the path anybody actually uses, and `#374` is what happens when only
	# one of the two is taught something.
	created, _capture = subroutine.domain.tasks.create_from_text(
		session, workspace=world.workspace, text="filed by a collaborator", actor=bounded
	)

	assert created.project_id == world.public.id


def test_an_inbox_it_can_reach_but_not_write_in_is_not_the_default (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The same omission one line up — the original defect with an extra step (``#416``).

	``test_a_credential_reaching_the_inbox_still_files_there`` is right and stays right; this
	is the case it does not cover. A reach naming the Inbox and a write set that does not meant
	the task was filed there and *then* refused on the write — a `403` one layer later than
	`#369`'s, which is the shape that reads as a bug in the program rather than in the
	credential.
	"""

	inbox = subroutine.domain.bootstrap.inbox_for(session, world.workspace)

	assert inbox is not None

	bounded = _reaching_writing(
		session, world, reach=[inbox, world.public], writes=[world.public]
	)
	filed = subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert filed.id == world.public.id, "the Inbox is reachable and unwritable, so not a default"

	# And it is genuinely writable there, which is the half that makes this an answer rather
	# than a different refusal.
	subroutine.domain.authorization.authorize(
		session,
		bounded,
		subroutine.permissions.TASK_WRITE,
		workspace_id=world.workspace.id,
		project=filed,
	)


def test_a_credential_that_writes_nowhere_here_is_refused_in_those_terms (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Which of the two restrictions bit, because they have different remedies (``#416``).

	"Cannot reach any project in this workspace" is the wrong sentence for a credential that
	reads this workspace perfectly well and writes in another — it sends the reader to widen
	the wrong list.
	"""

	elsewhere = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="otherwhere",
		title="Otherwhere",
		actor=subroutine.domain.authentication.Principal(user=world.owner),
	)
	session.flush()

	bounded = _reaching_writing(
		session, world, reach=[world.public, elsewhere], writes=[elsewhere]
	)

	# Its write set is real, so the refusal is not about having none — it is about this
	# workspace. Archive the one place it can write and there is nowhere left.
	elsewhere.archived_at = subroutine.db.types.utcnow()
	elsewhere.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert "write in" in str(refused.value)


def test_a_credential_reaching_two_projects_is_asked_which (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Ambiguity is a refusal, never a guess — the rule `selection.workspace` already applies.

	A task filed somewhere its author did not look is found days later, if at all, and a
	credential reaching two projects has said nothing about which one it meant.
	"""

	bounded = _scoped_to(session, world, world.public, world.private)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert refused.value.errors[0].field == "project"
	assert "open" in str(refused.value.errors[0].hint)
	assert "secret" in str(refused.value.errors[0].hint), "and it names them both"


def test_the_reach_is_what_was_scoped_to_not_everything_underneath (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""A scope of ``SR`` reaches ``SR/WEB`` too, and that must not make it look ambiguous.

	Written because the obvious implementation — "the projects this credential can read" —
	returns the whole subtree, so a credential deliberately pointed at one project would be
	refused for naming none. The one-project case is the case this exists for.
	"""

	child = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="opensub",
		title="Underneath",
		parent=world.public,
	)
	session.flush()

	bounded = _scoped_to(session, world, world.public)

	assert [
		found.key
		for found in session.scalars(
			subroutine.domain.scoping.readable_projects(
				bounded, workspace_ids=[world.workspace.id]
			)
		)
	] != [world.public.key], "the subtree really is reachable, so the guard is not vacuous"

	filed = subroutine.domain.selection.project(session, bounded, world.workspace, None)

	assert filed.id == world.public.id
	assert child.id != world.public.id


def _reaching_writing (
	session: sqlalchemy.orm.Session,
	world: World,
	*,
	reach: typing.Sequence[subroutine.db.models.project.Project],
	writes: typing.Sequence[subroutine.db.models.project.Project],
) -> subroutine.domain.authentication.Principal:
	"""Return the owner, presenting a credential that reads wider than it writes."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=world.owner,
		title="Reads two, writes one",
		project_scope=[str(project.id) for project in reach],
		project_write_scope=[str(project.id) for project in writes],
	)
	session.flush()

	return subroutine.domain.authentication.Principal(user=world.owner, token=token)


def test_a_credential_can_read_a_project_it_cannot_write_to (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`#371`, and the arrangement decision `#370` exists for.

	An agent working on one project inside a related tree needs to read its neighbours for
	context and write only its own. Before this, one list gated reads and writes together, so
	the choice was between an agent that could see nothing else and one that could change
	everything.
	"""

	bounded = _reaching_writing(
		session, world, reach=[world.public, world.private], writes=[world.public]
	)

	# It reads both — the whole point, and the half that a single project_scope already did.
	assert _titles(session, bounded, world.workspace) == [
		"Acquire the rival company",
		"Ordinary work",
	]

	# It writes in its own, and is refused in the other, *by name*: the refusal says the
	# credential can read here and writes elsewhere, because that is the fact that decides
	# what the caller does next.
	subroutine.domain.authorization.authorize(
		session,
		bounded,
		subroutine.permissions.TASK_WRITE,
		workspace_id=world.workspace.id,
		project=world.public,
	)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authorization.authorize(
			session,
			bounded,
			subroutine.permissions.TASK_WRITE,
			workspace_id=world.workspace.id,
			project=world.private,
		)

	assert "may only write in another" in str(refused.value)


def test_a_write_set_narrows_only_the_verbs_that_land_in_a_project (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Reading is governed by the reach alone, and the two controls must not overlap.

	Written because the tempting implementation — "narrow anything that is not a read" — would
	have caught `tag:write` and `status:write`, which curate the *workspace's* vocabulary and
	have no project to be inside.
	"""

	bounded = _reaching_writing(
		session, world, reach=[world.public, world.private], writes=[world.public]
	)

	for permitted in (
		subroutine.permissions.TASK_READ,
		subroutine.permissions.COMMENT_READ,
		subroutine.permissions.PROJECT_READ,
	):
		subroutine.domain.authorization.authorize(
			session,
			bounded,
			permitted,
			workspace_id=world.workspace.id,
			project=world.private,
		)

	assert subroutine.permissions.TAG_WRITE not in (
		subroutine.permissions.WRITES_INSIDE_A_PROJECT
	), "curating a workspace's vocabulary is not a write inside a project"


def test_a_write_set_reaches_the_subtree_under_it (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""A write set of ``SR`` that refused ``SR/WEB`` would be useless past one level.

	The same rule the reach already follows, and they share one implementation so that
	"reaches" and "may write in" cannot come to mean different things about one tree.
	"""

	child = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="underneath",
		title="Underneath",
		parent=world.public,
	)
	session.flush()

	bounded = _reaching_writing(
		session, world, reach=[world.public], writes=[world.public]
	)

	subroutine.domain.authorization.authorize(
		session,
		bounded,
		subroutine.permissions.TASK_WRITE,
		workspace_id=world.workspace.id,
		project=child,
	)


def test_a_write_set_outside_the_reach_is_refused_at_issue (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""A permission the credential could never exercise, reported as though it could.

	That is the 'specified, documented and inert' family with a security label on it, so the
	two lists are checked against each other where they are written rather than left to
	disagree quietly.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.authentication.issue_token(
			session,
			user=world.owner,
			title="Writes where it cannot read",
			project_scope=[str(world.public.id)],
			project_write_scope=[str(world.private.id)],
		)

	assert refused.value.errors[0].field == "project_write_scope"

	# **By its key** (`#413`, `#203`). Somebody typed a key; reading it back as a UUID sends
	# them to look up what they just wrote, in the one message they meet while still holding
	# the command they meant to type.
	assert world.private.key in str(refused.value.errors[0].message)
	assert str(world.private.id) not in str(refused.value.errors[0].message)


def test_a_write_set_may_name_a_project_under_the_reach (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The shape `#370` exists for, refused for a year of afternoons — item ``#413``.

	*"Read the related tree, write only my own project"* is a parent and a child on any real
	tree, and this was the flat set subset that turned it down — with the sentence *"a project
	it cannot read"*, about a project the credential reads and writes perfectly well.

	**Falsified against the original code**, not against a mutation of the fix: restore
	``set(project_write_scope) - set(project_scope)`` in
	``authentication._refuse_a_write_set_outside_the_reach`` and this raises. The test beside
	it, ``test_a_write_set_reaches_the_subtree_under_it``, stayed green throughout — it issues
	its credential with the reach and the write set naming the *same* project, so it exercises
	the enforcement's view of the tree and never the issuer's. The guard for the rule, sitting
	green beside the defect in it.
	"""

	child = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="beneath",
		title="Beneath",
		parent=world.public,
	)
	session.flush()

	token, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=world.owner,
		title="Reads the tree, writes one part of it",
		project_scope=[str(world.public.id)],
		project_write_scope=[str(child.id)],
	)
	session.flush()

	assert token.project_write_scope == [str(child.id)]

	bounded = subroutine.domain.authentication.Principal(user=world.owner, token=token)

	# And it behaves as the pair says: writes in the child, refused in the parent it reads.
	subroutine.domain.authorization.authorize(
		session,
		bounded,
		subroutine.permissions.TASK_WRITE,
		workspace_id=world.workspace.id,
		project=child,
	)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.authorization.authorize(
			session,
			bounded,
			subroutine.permissions.TASK_WRITE,
			workspace_id=world.workspace.id,
			project=world.public,
		)


def test_a_write_set_naming_a_project_nobody_can_place_is_still_refused (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Unknown means unplaceable, never unwelcome — the conservative half of ``#413``.

	The ids are still not validated for *existence*: a credential may name a project its issuer
	cannot see, or one created later. What changed is that a project with a row can be placed in
	the tree. One without a row cannot, so it is covered only by being named outright — which is
	exactly the behaviour that was there before, kept rather than widened.
	"""

	absent = uuid.uuid4()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.authentication.issue_token(
			session,
			user=world.owner,
			title="Writes somewhere nobody can find",
			project_scope=[str(world.public.id)],
			project_write_scope=[str(absent)],
		)

	# Named by its id, because there is no key to name it by — which is the honest answer.
	assert str(absent) in str(refused.value.errors[0].message)

	# Named outright in the reach, it is accepted, unplaceable or not.
	allowed, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=world.owner,
		title="Writes where it says it reaches",
		project_scope=[str(world.public.id), str(absent)],
		project_write_scope=[str(absent)],
	)

	assert allowed.project_write_scope == [str(absent)]


def test_a_credential_cannot_issue_one_that_writes_more_widely (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The fifth way to amplify, refused beside the other four.

	Both directions matter and only one is obvious: a credential with a write set may not hand
	out a wider one, *and* a credential restricted only by its reach may not hand out a write
	set beyond that reach — because for it, the reach is what bounds writing.
	"""

	narrow = _reaching_writing(
		session, world, reach=[world.public, world.private], writes=[world.public]
	)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.authentication.issue_token(
			session,
			user=world.owner,
			title="Wider",
			project_scope=[str(world.public.id), str(world.private.id)],
			project_write_scope=[str(world.private.id)],
			actor=narrow,
		)

	assert refused.value.errors[0].field == "project_write_scope"

	# A subset is fine, which is what makes this a narrowing rule rather than a freeze.
	allowed, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=world.owner,
		title="Narrower",
		project_scope=[str(world.public.id)],
		project_write_scope=[str(world.public.id)],
		actor=narrow,
	)

	assert allowed.project_write_scope == [str(world.public.id)]


def test_a_captured_line_files_where_the_credential_can_reach (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`#374`. `#369` fixed one of two copies of the Inbox default, and I called it fixed.

	`domain.tasks.create_from_text` reached for the Inbox itself rather than asking
	`selection`, so the *captured line* path — the one `subroutine add` and `subroutine_add`
	both use, which is to say the one anybody actually uses — still walked into a project the
	credential cannot reach and was refused.

	Found end-to-end against the deployed instance, minutes after it came back up, and not by
	the suite: two copies of a rule that agreed until one of them was taught something.
	"""

	bounded = _scoped_to(session, world, world.public)
	created, _capture = subroutine.domain.tasks.create_from_text(
		session, workspace=world.workspace, text="filed by a bounded agent", actor=bounded
	)

	assert created.project_id == world.public.id


def test_a_captured_key_still_wins_over_the_default (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The ordering that had to survive the fix.

	A `+KEY` in the line is the author saying where it goes, and it beat the Inbox before —
	so it must beat whatever replaced the Inbox, or `#374`'s fix would have quietly taken
	something away while giving something else.
	"""

	bounded = _scoped_to(session, world, world.public, world.private)
	created, _capture = subroutine.domain.tasks.create_from_text(
		session,
		workspace=world.workspace,
		text=f"filed deliberately +{world.private.key}",
		actor=bounded,
	)

	assert created.project_id == world.private.id, "the key the author typed still decides"


def test_the_scoping_guard_catches_a_query_it_has_not_been_told_about (
	tmp_path: pathlib.Path,
) -> None:
	"""Item ``#405``. The rule, shown a module that reaches the tables directly.

	**The pair above already covers a walk that reads nothing** — an empty scan makes every
	entry in ``REACHES_DIRECTLY`` look stale, and the second test says so. What neither can
	say is whether the *match* still works: it looks for a ``select`` beside a ``Task``,
	``Project`` or ``Document`` attribute, and a model renamed or a query written another way
	would leave it reporting a clean tree for ever.
	"""

	(tmp_path / "leaky.py").write_text(
		"import sqlalchemy\n"
		"import subroutine.db.models.work\n"
		"def everything (session):\n"
		"\treturn session.scalars(\n"
		"\t\tsqlalchemy.select(subroutine.db.models.work.Task)\n"
		"\t).all()\n",
		encoding="utf-8",
	)

	assert set(_modules_touching_work(tmp_path)) == {"leaky.py"}


def test_the_scoping_guard_leaves_alone_a_module_that_queries_nothing (
	tmp_path: pathlib.Path,
) -> None:
	"""And the other half: a matcher that flagged every file would pass the test above.

	Without this, "reports the offender" and "reports everything" are the same green — and the
	second, combined with a broken walk, is a guard that has stopped measuring anything while
	looking exactly like one that works.
	"""

	(tmp_path / "quiet.py").write_text(
		"import subroutine.domain.scoping\n"
		"def readable (session, actor):\n"
		"\treturn subroutine.domain.scoping.readable_tasks(session, actor)\n",
		encoding="utf-8",
	)

	assert _modules_touching_work(tmp_path) == {}


def test_a_read_narrowed_credential_cannot_read_past_its_scope (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`#930`, from `#927` H-2. A read verb gated nothing, so narrowing one did nothing.

	**Measured before the fix**: a token issued ``--scope task:delete`` answered 200 for
	``/v1/tasks``, ``/v1/documents``, ``/v1/agenda`` and ``/v1/changes`` while ``/v1/me``
	reported the single permission it held. Every read-narrowed credential was wider than it
	was issued.

	**Both directions**, because a refusal that fires for everybody is not enforcement: the
	credential holding the verb reads normally, and the one holding a different verb is
	refused by name.
	"""

	deleting, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="delete only", scopes=["task:delete"]
	)
	reading, _also = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="read only", scopes=["task:read"]
	)
	session.flush()

	narrowed = subroutine.domain.authentication.Principal(user=world.owner, token=deleting)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as refused:
		subroutine.domain.scoping.readable_tasks(narrowed, workspace_ids=[world.workspace.id])

	# Named, because the operator's remedy is to reissue with the verb it lacks.
	assert "task:read" in str(refused.value)

	permitted = subroutine.domain.authentication.Principal(user=world.owner, token=reading)

	assert _titles(session, permitted, world.workspace)


def test_a_credential_with_no_scopes_is_narrowed_by_none_of_this (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The sentinel, which is the half of this rule that fails open when it is got wrong.

	An empty scope list means *no narrowing*, never *no permission* — §7.3, and the
	shape `#201` found in the project-scope predicate where the two readings disagreed and the
	query side admitted everything. Every credential on this instance is in this state, so a
	check that read the list falsily would refuse every read there is.
	"""

	unscoped, _issued = subroutine.domain.authentication.issue_token(
		session, user=world.owner, title="as wide as its owner"
	)
	session.flush()

	wide = subroutine.domain.authentication.Principal(user=world.owner, token=unscoped)

	assert _titles(session, wide, world.workspace)


def test_the_read_scope_can_be_waived_in_exactly_one_place () -> None:
	"""`#930`. An opt-out with one caller is a decision; with three it is a hole.

	``readable_projects`` takes ``enforce_read_scope=False`` for one reason: ``keys_for``
	resolves ids that came out of the caller's *own* token, so naming them discloses nothing
	they do not already hold, and enforcing there would stop a narrowed credential running
	``whoami`` at all.

	**Found by the suite rather than by reasoning.** The check went in without the exception
	and took five tests with it, all of them about a credential describing itself — which is
	the case the enforcement is least entitled to refuse.
	"""

	waivers = [
		f"{path.relative_to(SOURCE)}:{number}"
		for path in sorted(SOURCE.rglob("*.py"))
		for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
		if "enforce_read_scope=False" in line
	]

	assert len(waivers) == 1, (
		f"the read-scope waiver is used {len(waivers)} times: {waivers}. Each one is a "
		f"listing that is not narrowed by the caller's own scopes — add the reason to this "
		f"test, or do not waive it."
	)
	assert waivers[0].startswith("domain/projects.py"), waivers
