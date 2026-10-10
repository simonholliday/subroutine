"""Who manages what is owned, and who decides who sees work - `SR#4570` and `SR#4572`.

Decisions `#4509`, `#4525` and `#4516`, on R5 of the cold review of 2026-10-05. **None of the decided
audience behaviours was tested** (NEW-O-3 of its verification): the review's mutant, seven behaviour
changes, passed every test. Each is pinned here as it was measured, with its control.

* **Who can see existing work is its steward's to change**, in both directions: a member shared into
  somebody else's private project made it public, moved its sub-project out, shared it onward and
  evicted an administrator; a contributor moved a task assigned to somebody else, and a document
  somebody else maintains, into a private project, and everybody else lost sight of both (S5).
* **The steward's stand-in**: a departed owner's private project was pinned by their row, and nobody
  who could see it could take it on (C section 2.4).
* **One rule for every owned thing**: an administrator could forget a shared view and not unshare it,
  delete a comment and not say so, and a person could manage nothing their own agent owned (C sections
  2.2 and 2.3).

The last tests hold the rule in one place: nothing outside ``domain/stewardship.py`` compares an
actor's own account with anything, unless written down here with the reason it decides no ownership.
"""

import ast
import pathlib
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.calendars
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.domain.saved
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"

projects = subroutine.domain.projects
workspaces = subroutine.domain.workspaces


def _person (session: sqlalchemy.orm.Session, name: str) -> subroutine.db.models.identity.User:
	"""Make a person with a name nobody else in the run has."""

	return subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")


def _agent (
	session: sqlalchemy.orm.Session, name: str, person: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.User:
	"""Make an agent that answers to ``person``."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=person.id,
	)


def _as (user: subroutine.db.models.identity.User) -> subroutine.domain.authentication.Principal:
	"""Return an account as somebody acting, holding no credential that narrows it."""

	return subroutine.domain.authentication.Principal(user=user)


def _metacortex (
	session: sqlalchemy.orm.Session,
	founder: subroutine.db.models.identity.User,
	**members: tuple[subroutine.db.models.identity.User, str],
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
	owner: subroutine.db.models.identity.User | None,
	*,
	visibility: str = "private",
	parent: subroutine.db.models.project.Project | None = None,
) -> subroutine.db.models.project.Project:
	"""Make a project ``owner`` owns, as the domain makes one, their row written."""

	return projects.create(
		session,
		workspace_id=workspace.id,
		key=key,
		title=key.title(),
		visibility=visibility,
		owner_id=None if owner is None else owner.id,
		parent=parent,
	)


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


def test_a_member_shared_in_no_longer_decides_who_sees_somebody_elses_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""S5, as measured: keanu, shared into alice's private ``hr``, published it, moved its sub-project
	out, shared it onward and evicted an administrator. **Each is refused, by one sentence**, and
	removing himself stays his. The controls: alice and trinity, an administrator who can see it.
	"""

	alice = _person(session, "alice")
	keanu = _person(session, "keanu")
	trinity = _person(session, "trinity")
	bob = _person(session, "bob")
	workspace = _metacortex(
		session, alice, keanu=(keanu, "member"), trinity=(trinity, "admin"), bob=(bob, "member")
	)
	hr = _project(session, workspace, "hr", alice)
	pay = _project(session, workspace, "pay", alice, visibility="public", parent=hr)

	for one in (keanu, trinity):
		projects.share(session, hr, one, actor=_as(alice))

	refusals = (
		(lambda: projects.update(session, hr, visibility="public", actor=_as(keanu)), hr, "make it public"),
		(
			lambda: projects.move(session, pay, parent=None, actor=_as(keanu)),
			pay,
			"move it out from under a private project",
		),
		(lambda: projects.share(session, hr, bob, actor=_as(keanu)), hr, "share it with somebody"),
		(
			lambda: projects.unshare(session, hr, trinity, actor=_as(keanu)),
			hr,
			"remove somebody else from it",
		),
	)

	for refused, whose, act in refusals:
		with pytest.raises(subroutine.errors.Forbidden) as caught:
			refused()

		assert caught.value.detail == (
			f"Only the owner of '{whose.key}', or somebody who administers this workspace, may {act}."
		)

	assert hr.visibility == "private" and pay.parent_id == hr.id
	assert not _sees(session, bob, hr) and _sees(session, trinity, hr)

	projects.update(session, hr, visibility="public", actor=_as(trinity))
	projects.update(session, hr, visibility="private", actor=_as(alice))
	projects.share(session, hr, bob, actor=_as(alice))
	projects.unshare(session, hr, keanu, actor=_as(keanu))

	assert _sees(session, bob, hr) and not _sees(session, keanu, hr)


def test_an_administrator_stewards_only_what_they_can_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A private project stays hidden from an administrator who is not in it: they are told it is
	not there, as anybody would be, rather than allowed to decide who sees it.
	"""

	alice = _person(session, "alice")
	trinity = _person(session, "trinity")
	workspace = _metacortex(session, alice, trinity=(trinity, "admin"))
	hr = _project(session, workspace, "hr", alice)

	with pytest.raises(subroutine.errors.NotFound):
		projects.update(session, hr, visibility="public", actor=_as(trinity))

	assert hr.visibility == "private"


def test_whoever_an_owner_answers_to_stewards_its_project_and_never_the_reverse (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `#4516`: neo stands in for his agent as the owner of what it owns. **The direction never
	reverses**: the agent gains nothing over neo's own project, though it can see it.
	"""

	neo = _person(session, "neo")
	bot = _agent(session, "bot", neo)
	morpheus = _person(session, "morpheus")
	workspace = _metacortex(session, neo, bot=(bot, "member"), morpheus=(morpheus, "member"))
	lab = _project(session, workspace, "lab", bot)
	home = _project(session, workspace, "home", neo)

	projects.share(session, lab, neo, actor=_as(bot))
	projects.share(session, home, bot, actor=_as(neo))

	projects.share(session, lab, morpheus, actor=_as(neo))
	projects.update(session, lab, visibility="public", actor=_as(neo))

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		projects.update(session, home, visibility="public", actor=_as(bot))

	assert refused.value.hint == f"Ask {neo.username}, or somebody who administers this workspace."
	assert lab.visibility == "public" and home.visibility == "private"


def test_when_the_owner_can_no_longer_act_anybody_who_can_see_it_stands_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""C section 2.4, as measured: keanu owns ``kp`` and shares bob in, then leaves. His row pinned it,
	bob could not take it on, and no administrator could see it, so it was stranded. **Bob stands in**:
	he removes keanu's row, takes it on and lets trinity in. Before keanu left, each was refused.
	"""

	thomas = _person(session, "thomas")
	keanu = _person(session, "keanu")
	bob = _person(session, "bob")
	trinity = _person(session, "trinity")
	workspace = _metacortex(
		session, thomas, keanu=(keanu, "member"), bob=(bob, "member"), trinity=(trinity, "admin")
	)
	kp = _project(session, workspace, "kp", keanu)

	projects.share(session, kp, bob, actor=_as(keanu))

	with pytest.raises(subroutine.errors.Forbidden):
		projects.update(session, kp, owner_id=bob.id, actor=_as(bob))

	workspaces.remove_member(session, workspace, keanu)

	projects.unshare(session, kp, keanu, actor=_as(bob))
	projects.update(session, kp, owner_id=bob.id, actor=_as(bob))
	projects.share(session, kp, trinity, actor=_as(bob))

	assert kp.owner_id == bob.id and _sees(session, trinity, kp)


def test_a_project_nobody_owns_is_stewarded_by_anybody_who_can_see_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An owner nobody names cannot act either, so the stand-in applies: a project with no owner is
	not one only an administrator who happens to see it may ever decide about.
	"""

	thomas = _person(session, "thomas")
	bob = _person(session, "bob")
	workspace = _metacortex(session, thomas, bob=(bob, "member"))
	nobodys = _project(session, workspace, "nobodys", None, visibility="public")

	projects.update(session, nobodys, visibility="private", actor=_as(bob))

	assert nobodys.visibility == "private"


def test_a_move_within_one_private_tree_crosses_nothing (session: sqlalchemy.orm.Session) -> None:
	"""NEW-O-1: keanu, shared into alice's private ``tree``, was refused moving ``left`` under
	``right``, though nobody's sight changes. **Allowed**; moving it out of the tree is still refused.
	"""

	alice = _person(session, "alice")
	keanu = _person(session, "keanu")
	workspace = _metacortex(session, alice, keanu=(keanu, "member"))
	tree = _project(session, workspace, "tree", alice)
	left = _project(session, workspace, "left", alice, visibility="public", parent=tree)
	right = _project(session, workspace, "right", alice, visibility="public", parent=tree)

	projects.share(session, tree, keanu, actor=_as(alice))
	projects.move(session, left, parent=right, actor=_as(keanu))

	assert left.parent_id == right.id

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		projects.move(session, left, parent=None, actor=_as(keanu))

	assert refused.value.detail == (
		f"Only the owner of '{left.key}', or somebody who administers this workspace, may move it "
		"out from under a private project."
	)


def test_work_crosses_a_privacy_boundary_only_by_the_steward_of_where_it_leaves (
	session: sqlalchemy.orm.Session,
) -> None:
	"""S5's items, as measured: charlie, a contributor shared into keanu's private ``vault``, moved a
	task assigned to erin and a document erin maintains out of dave's public ``web`` into it, and erin,
	the administrator and dave all lost sight of both. **Refused**, for both kinds and both directions;
	a move between two public projects is not; and dave, web's steward, moves them.
	"""

	dave = _person(session, "dave")
	erin = _person(session, "erin")
	charlie = _person(session, "charlie")
	keanu = _person(session, "keanu")
	workspace = _metacortex(
		session, dave, erin=(erin, "member"), charlie=(charlie, "contributor"), keanu=(keanu, "member")
	)
	web = _project(session, workspace, "web", dave, visibility="public")
	docs = _project(session, workspace, "docs", dave, visibility="public")
	vault = _project(session, workspace, "vault", keanu)

	for one in (charlie, dave):
		projects.share(session, vault, one, actor=_as(keanu))

	task = subroutine.domain.tasks.create(
		session, project=web, title="Fix the sign-up form", assignee_id=erin.id, actor=_as(dave)
	)
	document = subroutine.domain.documents.create(
		session, project=web, title="How we ship", owner_id=erin.id, actor=_as(dave)
	)
	hidden = subroutine.domain.tasks.create(session, project=vault, title="Audit", actor=_as(keanu))

	for refused, where in (
		(lambda: subroutine.domain.tasks.update(session, task, project=vault, actor=_as(charlie)), "web"),
		(
			lambda: subroutine.domain.documents.update(
				session, document, project=vault, actor=_as(charlie)
			),
			"web",
		),
		(
			lambda: subroutine.domain.tasks.update(session, hidden, project=web, actor=_as(charlie)),
			"vault",
		),
	):
		with pytest.raises(subroutine.errors.Forbidden) as caught:
			refused()

		assert caught.value.detail.startswith(f"Only the owner of '{where}"), caught.value.detail

	assert task.project_id == web.id and document.project_id == web.id and hidden.project_id == vault.id

	subroutine.domain.tasks.update(session, task, project=docs, actor=_as(charlie))
	subroutine.domain.tasks.update(session, task, project=vault, actor=_as(dave))

	assert task.project_id == vault.id


def test_filing_ones_own_capture_into_ones_own_private_project_asks_the_inboxs_steward (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The cost decision `#4509` accepted: the Inbox belongs to whoever founded the workspace, so erin's
	own capture is moved into their own private project by its steward. **They file it there
	directly**.
	"""

	thomas = _person(session, "thomas")
	erin = _person(session, "erin")
	workspace = _metacortex(session, thomas, erin=(erin, "member"))
	inbox = session.scalars(
		sqlalchemy.select(subroutine.db.models.project.Project).where(
			subroutine.db.models.project.Project.workspace_id == workspace.id,
			subroutine.db.models.project.Project.is_inbox.is_(True),
		)
	).one()
	mine = _project(session, workspace, "mine", erin)
	captured = subroutine.domain.tasks.create(session, project=inbox, title="Call mum", actor=_as(erin))

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.tasks.update(session, captured, project=mine, actor=_as(erin))

	filed = subroutine.domain.tasks.create(session, project=mine, title="Call mum", actor=_as(erin))

	assert filed.project_id == mine.id


def test_a_share_records_who_granted_it (session: sqlalchemy.orm.Session) -> None:
	"""C section 5: nothing wrote ``created_by`` on a membership row, so reporting who let somebody in
	would have reported nobody on every row. **Written by both writers**: sharing, and owning.
	"""

	alice = _person(session, "alice")
	keanu = _person(session, "keanu")
	workspace = _metacortex(session, alice, keanu=(keanu, "member"))
	hr = projects.create(
		session,
		workspace_id=workspace.id,
		key="hr",
		title="Hr",
		visibility="private",
		owner_id=alice.id,
		actor=_as(alice),
	)

	projects.share(session, hr, keanu, actor=_as(alice))

	granted = {
		row.user_id: row.created_by
		for row, _holder in projects.members(session, hr, actor=_as(alice))
	}

	assert granted == {alice.id: alice.id, keanu.id: alice.id}


def _view (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	owner: subroutine.db.models.identity.User,
	title: str,
	*,
	shared: bool,
) -> subroutine.db.models.saved.SavedView:
	"""Save a view as its owner would."""

	return subroutine.domain.saved.create(
		session,
		workspace_id=workspace.id,
		title=title,
		arrangement="list",
		q="is:open",
		shared=shared,
		actor=_as(owner),
	)


def test_a_view_is_managed_by_its_owner_their_answerer_or_an_administrator_and_rewritten_by_its_author (
	session: sqlalchemy.orm.Session,
) -> None:
	"""C section 2.2, as measured: an administrator could forget a shared view and not stop sharing it,
	and an owner demoted to contributor could forget their own and not stop sharing it. **Both may now**;
	neither the administrator nor neo, who answers for his agent's view, may change what one says.
	"""

	thomas = _person(session, "thomas")
	erin = _person(session, "erin")
	trinity = _person(session, "trinity")
	neo = _person(session, "neo")
	bot = _agent(session, "bot", neo)
	workspace = _metacortex(
		session,
		thomas,
		erin=(erin, "member"),
		trinity=(trinity, "admin"),
		neo=(neo, "member"),
		bot=(bot, "member"),
	)
	team = _view(session, workspace, erin, "Team queue", shared=True)
	bots = _view(session, workspace, bot, "Bot queue", shared=False)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.saved.update(
			session, team, title="Renamed", given={"title"}, actor=_as(trinity)
		)

	subroutine.domain.saved.update(session, team, shared=False, given={"shared"}, actor=_as(trinity))
	assert not team.shared

	subroutine.domain.saved.update(session, team, shared=True, given={"shared"}, actor=_as(erin))
	workspaces.set_member_role(session, workspace, erin, role_key="contributor")
	subroutine.domain.saved.update(session, team, shared=False, given={"shared"}, actor=_as(erin))
	assert not team.shared

	seen = session.scalars(
		subroutine.domain.saved.readable(session, _as(neo), workspace_id=workspace.id)
	).all()
	assert bots in seen and team not in seen

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.saved.update(session, bots, title="Mine now", given={"title"}, actor=_as(neo))

	subroutine.domain.saved.delete(session, bots, actor=_as(neo))


def test_a_comment_is_removed_by_its_author_their_answerer_or_an_administrator_and_said_so (
	session: sqlalchemy.orm.Session,
) -> None:
	"""C sections 2.3 and 2.6: neo could do nothing with his agent's comment, and a member refused
	deleting somebody else's was told only that it needs ``workspace:admin``, hiding that its author
	could. **Neo removes it, and the refusal names who may**; nobody but the author rewrites one.
	"""

	thomas = _person(session, "thomas")
	neo = _person(session, "neo")
	bot = _agent(session, "bot", neo)
	morpheus = _person(session, "morpheus")
	trinity = _person(session, "trinity")
	workspace = _metacortex(
		session,
		thomas,
		neo=(neo, "member"),
		bot=(bot, "member"),
		morpheus=(morpheus, "member"),
		trinity=(trinity, "admin"),
	)
	web = _project(session, workspace, "web", thomas, visibility="public")
	task = subroutine.domain.tasks.create(session, project=web, title="Ship it", actor=_as(thomas))
	said = subroutine.domain.comments.create(
		session, entity_type="task", entity_id=task.id, body="Done on staging.", actor=_as(bot)
	)
	again = subroutine.domain.comments.create(
		session, entity_type="task", entity_id=task.id, body="And live.", actor=_as(bot)
	)

	for editor in (neo, trinity):
		with pytest.raises(subroutine.errors.Forbidden):
			subroutine.domain.comments.update(session, said, body="Not done.", actor=_as(editor))

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.comments.delete(session, said, actor=_as(morpheus))

	assert refused.value.detail == (
		"Only the author of this comment, whoever that agent answers to, or somebody who administers "
		"this workspace, may delete it."
	)

	subroutine.domain.comments.delete(session, said, actor=_as(neo))
	subroutine.domain.comments.delete(session, again, actor=_as(trinity))

	assert said.deleted_at is not None and again.deleted_at is not None


def test_a_feed_is_given_a_new_address_only_by_its_holder_whoever_found_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""C section 2.6: resetting a feed trusted the lookup that found it, so a caller resolving one
	another way would have handed somebody else's feed a new address reading with their sight. **Asked
	inside the act**, of a superuser too, who may stop the feed but not take it on.
	"""

	alice = _person(session, "alice")
	root = subroutine.domain.users.create(
		session, username=f"root-{uuid.uuid4().hex[:6]}", is_superuser=True
	)
	workspace = _metacortex(session, alice, root=(root, "admin"))
	feed, _minted = subroutine.domain.calendars.issue(
		session, _as(alice), title="Alice's", workspace=workspace.slug, enabled=True
	)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.calendars.reset(session, feed, actor=_as(root), enabled=True)

	assert refused.value.detail == "Only the person a calendar feed belongs to may give it a new address."

	subroutine.domain.calendars.revoke(session, feed, actor=_as(root))

	assert feed.revoked_at is not None


#: Every function outside ``domain/stewardship.py`` that compares an actor's own account with
#: something, and why that decides no ownership. **Deleting an entry is what closes it**, and a new one
#: is a decision somebody takes: whose a thing is is :mod:`subroutine.domain.stewardship`'s question.
NOT_OWNERSHIP: dict[str, str] = {
	"domain/authorization.py:visible_projects": (
		"Which project rows are the caller's own memberships: sight of a private project, §7.3a."
	),
	"domain/authorization.py:reaches": "Which workspaces the caller is a member of: reach, `#4674`.",
	"domain/authorization.py:_reached": "The caller's own role in a workspace, `#4674`.",
	"domain/claims.py:claim": (
		"Whether the caller already holds a lease: a claim is a fact apart from ownership, `#726`."
	),
	"domain/claims.py:renewed": "Whether the caller holds the lease it is renewing, `#726`.",
	"domain/local.py:workspace_for": "The workspaces the account at the terminal belongs to, `#3091`.",
	"domain/projects.py:unreachable": (
		"Which workspaces the caller is inside, to say whether joining comes first, `#2626`."
	),
	"domain/users.py:set_timezone": (
		"An account's own setting is an identity fact, not an owned thing: nobody else sets it."
	),
	"domain/workspaces.py:on_instance": "The workspaces the caller belongs to, `#2626`.",
}

#: The module that answers whose a thing is, and so the one that may ask.
STEWARDSHIP = "domain/stewardship.py"

_ACTORS = frozenset({"actor", "principal"})


def _actor_account (node: ast.AST) -> bool:
	"""Report whether an expression is ``actor.user.id`` or ``principal.user.id``."""

	return (
		isinstance(node, ast.Attribute)
		and node.attr == "id"
		and isinstance(node.value, ast.Attribute)
		and node.value.attr == "user"
		and isinstance(node.value.value, ast.Name)
		and node.value.value.id in _ACTORS
	)


def _comparing (root: pathlib.Path) -> dict[str, list[int]]:
	"""Return every function comparing an actor's own account with something, with its lines.

	**A comparison, or a column's ``in_`` holding one**, which is the two shapes an ownership test has
	taken here: ``row.owner_id == actor.user.id`` and ``model.owner_id.in_(...)``. Takes the tree as
	an argument, so a synthetic offender goes through this function rather than a copy of its rule
	(`#405`). **Only the stewardship module is skipped**, being the answer.
	"""

	found: dict[str, list[int]] = {}

	for path in sorted(root.rglob("*.py")):
		where = path.relative_to(root).as_posix()

		if where == STEWARDSHIP:
			continue

		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for function in ast.walk(tree):
			if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
				continue

			for node in ast.walk(function):
				compared = isinstance(node, ast.Compare) and any(
					_actor_account(one) for one in (node.left, *node.comparators)
				)
				contained = (
					isinstance(node, ast.Call)
					and isinstance(node.func, ast.Attribute)
					and node.func.attr == "in_"
					and any(_actor_account(inner) for arg in node.args for inner in ast.walk(arg))
				)

				if isinstance(node, ast.Compare | ast.Call) and (compared or contained):
					found.setdefault(f"{where}:{function.name}", []).append(node.lineno)

	return found


def test_nothing_but_the_stewardship_module_decides_whose_a_thing_is () -> None:
	"""Decision `#4525`'s test: nothing outside it compares an actor with an owner."""

	unexplained = sorted(set(_comparing(SOURCE)) - set(NOT_OWNERSHIP))

	assert not unexplained, (
		f"{unexplained} compare the caller's own account with something. Whose a thing is, and who "
		"may manage it, is subroutine.domain.stewardship's to say: ask it, or say in NOT_OWNERSHIP "
		"why this decides no ownership."
	)


def test_the_list_names_only_functions_that_still_compare () -> None:
	"""An entry outliving its comparison would excuse the next one written there."""

	gone = sorted(set(NOT_OWNERSHIP) - set(_comparing(SOURCE)))

	assert not gone, f"{gone} no longer compare the caller's own account. Delete them."


def test_the_scan_finds_the_owner_test_it_was_written_for (tmp_path: pathlib.Path) -> None:
	"""**At the site the defect lived** (`#405`): the project owner's test, as ``projects`` wrote it."""

	domain = tmp_path / "domain"
	domain.mkdir()
	(domain / "projects.py").write_text(
		"def _refuse_deciding_sight (session, actor, project):\n"
		"\tif actor is None or actor.user.id == project.owner_id:\n"
		"\t\treturn\n"
	)
	(domain / "saved.py").write_text(
		"def readable (session, principal, model):\n"
		"\treturn model.owner_id.in_([principal.user.id])\n"
	)
	(domain / "stewardship.py").write_text(
		"def holds (actor, owner_id):\n\treturn owner_id == actor.user.id\n"
	)

	assert sorted(_comparing(tmp_path)) == [
		"domain/projects.py:_refuse_deciding_sight",
		"domain/saved.py:readable",
	]


def test_the_scan_reads_the_source_it_guards () -> None:
	"""A walk reading nothing would pass the first test and fail only the second, so say it here."""

	assert len(_comparing(SOURCE)) >= len(NOT_OWNERSHIP) > 0
