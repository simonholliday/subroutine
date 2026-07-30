"""Tests for the service layer: refs, events, paths and mentions.

The done-criteria for this slice live here — creating a task allocates ``#1``, writes
one event and sets a correct path, and a description citing it produces exactly one
mention row that disappears when the sentence does.

Everything runs on both backends. One test does not: concurrent ref allocation is
meaningless on SQLite, which has a single writer, and it is precisely the bug that is
invisible there by construction.
"""

import concurrent.futures
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.mentions
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors


def _founder (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.User:
	"""Create the user a workspace is founded by."""

	return subroutine.domain.users.create(
		session, username=f"founder-{uuid.uuid4().hex[:8]}"
	)


def _workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Create a fully seeded workspace with an owner."""

	return subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Test workspace",
		owner=_founder(session),
	)


def _project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	**kwargs: typing.Any,
) -> subroutine.db.models.project.Project:
	"""Create a project in a workspace."""

	kwargs.setdefault("key", f"P{uuid.uuid4().hex[:10].upper()}")
	kwargs.setdefault("title", "Test project")

	return subroutine.domain.projects.create(session, workspace_id=workspace.id, **kwargs)


def _events (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
) -> list[subroutine.db.models.activity.Event]:
	"""Return one entity's events, oldest first."""

	model = subroutine.db.models.activity.Event

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(
				model.workspace_id == workspace_id,
				model.entity_type == entity_type,
				model.entity_id == entity_id,
			)
			.order_by(model.seq)
		)
	)


def test_creating_a_task_allocates_a_ref_an_event_and_a_path (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The done-criterion for S1-10."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	task = subroutine.domain.tasks.create(session, project=project, title="First thing")

	assert task.ref == 1
	assert task.path == f"/{task.id}/"
	assert task.depth == 0

	events = _events(session, workspace.id, "task", task.id)

	assert len(events) == 1
	assert events[0].action == "created"
	assert events[0].changes == {"ref": {"from": None, "to": 1}, "title": {"from": None, "to": "First thing"}}


def test_refs_are_sequential_and_shared_with_documents (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One counter per workspace, so a ref names exactly one thing (SPEC.md §6.2)."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	refs = [
		subroutine.domain.tasks.create(session, project=project, title=f"Task {index}").ref
		for index in range(5)
	]

	assert refs == [1, 2, 3, 4, 5]

	# The next allocation continues the same sequence whoever asks for it.
	assert subroutine.domain.refs.allocate(session, workspace.id) == 6


def test_allocating_refreshes_the_counter_a_caller_is_holding (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``allocate`` expires the in-memory counter, and this is what keeps that alive.

	Nothing in the application reads ``workspace.next_ref_number`` — the counter is only ever
	moved by the ``UPDATE … RETURNING`` inside ``allocate`` — so if the expiry stopped
	working, no behaviour would change and no other test would fail. It is cheap insurance
	against a stale attribute being written back, and insurance nothing observes is
	insurance somebody deletes while tidying.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	subroutine.domain.tasks.create(session, project=project, title="First")

	assert workspace.next_ref_number == 2, "the loaded workspace was refreshed, not left stale"


def test_the_counter_is_the_workspace_not_the_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Two projects share one sequence, which is what makes a bare number unambiguous.

	Under the per-project counters this replaced, both of these would have been ``1``.
	"""

	workspace = _workspace(session)
	home = _project(session, workspace, key="HOME")
	other = _project(session, workspace, key="SR")

	first = subroutine.domain.tasks.create(session, project=home, title="In one")
	second = subroutine.domain.tasks.create(session, project=other, title="In the other")

	assert (first.ref, second.ref) == (1, 2)


def test_a_ref_survives_its_task_moving_project (session: sqlalchemy.orm.Session) -> None:
	"""A ref names nothing the task can be moved out of, so a move cannot invalidate it."""

	workspace = _workspace(session)
	home = _project(session, workspace, key="HOME")
	other = _project(session, workspace, key="SR")

	task = subroutine.domain.tasks.create(session, project=home, title="Moves later")

	assert task.ref == 1

	task.project_id = other.id
	session.flush()

	sibling = subroutine.domain.tasks.create(session, project=other, title="Native")

	assert task.ref == 1, "unchanged by the move"
	assert sibling.ref == 2, "the next number, not a second 1 in this project"


def test_a_ref_is_read_with_or_without_its_sigil () -> None:
	"""``#42`` is how a ref is written; ``42`` is what a shell leaves of it."""

	assert subroutine.domain.refs.format_ref(42) == "#42"
	assert subroutine.domain.refs.parse_ref("42") == 42
	assert subroutine.domain.refs.parse_ref("#42") == 42
	assert subroutine.domain.refs.parse_ref("  #42  ") == 42
	assert subroutine.domain.refs.parse_ref("nonsense") is None
	assert subroutine.domain.refs.parse_ref("#") is None
	assert subroutine.domain.refs.parse_ref("SR-42") is None
	assert subroutine.domain.refs.parse_ref("4 2") is None


def test_a_ref_too_large_for_the_column_is_not_a_ref () -> None:
	"""Bounded in Python, because both backends refuse the query and neither does it quietly.

	Asking for ref 2147483648 raised ``NumericValueOutOfRange`` on PostgreSQL and
	``OverflowError`` on SQLite — each unhandled, each a 500 where the honest answer is that
	nothing answers to it. Python integers have no ceiling, so a bound the parser does not
	impose is one nothing imposes until a driver refuses.
	"""

	assert subroutine.domain.refs.parse_ref(str(subroutine.domain.refs.MAX_REF)) == (
		subroutine.domain.refs.MAX_REF
	)
	assert subroutine.domain.refs.parse_ref(str(subroutine.domain.refs.MAX_REF + 1)) is None
	assert subroutine.domain.refs.parse_ref("9" * 40) is None


def test_an_address_is_read_relatively_nearest_scope_first () -> None:
	"""SPEC.md §13.7's grammar: ``42``, ``acme/42``, ``work/acme/42``.

	Two components mean *workspace*, never *connection*, and that has to be a stated rule
	rather than a guess — with two names in the text there is nothing to tell them apart.
	"""

	parse = subroutine.domain.refs.parse_address

	assert parse("42") == subroutine.domain.refs.Address(ref=42)
	assert parse("#42") == subroutine.domain.refs.Address(ref=42)
	assert parse("acme/42") == subroutine.domain.refs.Address(ref=42, workspace="acme")
	assert parse("acme/#42") == subroutine.domain.refs.Address(ref=42, workspace="acme")
	assert parse("work/acme/42") == subroutine.domain.refs.Address(
		ref=42, workspace="acme", connection="work"
	)

	# Shapes that are not addresses at all.
	assert parse("") is None
	assert parse("/42") is None, "an empty component names nothing"
	assert parse("acme//42") is None
	assert parse("acme/") is None
	assert parse("a/b/c/42") is None, "there is no fourth level"
	assert parse("acme/nonsense") is None
	assert parse(f"acme/{subroutine.domain.refs.MAX_REF + 1}") is None


def test_an_address_prints_only_the_context_it_needs () -> None:
	"""The shortest form that resolves, which is what makes a listing safe to copy from."""

	assert subroutine.domain.refs.format_address(42) == "#42"
	assert subroutine.domain.refs.format_address(42, workspace="acme") == "acme/#42"


def test_a_ref_has_one_spelling_in_both_parsers () -> None:
	"""``007`` is not ref 7, in a path or in prose.

	The two patterns have to agree: ``mentions.REF_PATTERN`` leaves ``#007`` as prose — "a
	Bond film, not ref 7" — so ``parse_ref`` must not resolve it either, or the same string
	means different things depending on which one reads it. Zero is not a ref at all; the
	counter starts at one.
	"""

	assert subroutine.domain.refs.parse_ref("007") is None
	assert subroutine.domain.refs.parse_ref("0") is None
	assert subroutine.domain.refs.parse_ref("#0") is None
	assert subroutine.domain.mentions.candidates("see #007 and #0") == []


def test_a_ref_resolves_to_the_thing_it_names (session: sqlalchemy.orm.Session) -> None:
	"""What the mention index is built on.

	Against ``mentions.resolve`` rather than the ``refs.find`` this used to call, because
	that helper was deleted: it narrowed by nothing and had no callers left. Testing the
	function the application actually uses is the point of the change.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	assert subroutine.domain.mentions.resolve(session, workspace.id, [1]) == {
		1: ("task", task.id)
	}
	assert subroutine.domain.mentions.resolve(session, workspace.id, [99]) == {}


def test_a_new_workspace_arrives_complete (session: sqlalchemy.orm.Session) -> None:
	"""Vocabulary, an owner and an event, or none of it (SPEC.md §10.7 invariant 7)."""

	owner = _founder(session)
	workspace = subroutine.domain.workspaces.create(
		session, slug="Home Workspace", title="Home", owner=owner
	)

	assert workspace.slug == "home-workspace"
	assert workspace.settings[subroutine.db.seed.SEED_VERSION_KEY] == subroutine.db.seed.SEED_VERSION

	membership = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.WorkspaceMember).where(
			subroutine.db.models.identity.WorkspaceMember.workspace_id == workspace.id
		)
	).one()

	assert membership.user_id == owner.id

	role = session.get(subroutine.db.models.identity.Role, membership.role_id)

	assert role is not None
	assert role.key == "owner"

	# Creation, then the vocabulary that was written for it — one event for ~35 rows, so
	# the feed's first page is not entirely statuses and roles (SPEC.md §10.7 invariant 9).
	events = _events(session, workspace.id, "workspace", workspace.id)

	assert [event.action for event in events] == ["created", "seeded"]

	seeded = events[1].changes

	assert seeded is not None
	assert seeded["seed_version"]["to"] == subroutine.db.seed.SEED_VERSION
	assert seeded["roles"]["to"] == 5
	assert seeded["statuses"]["to"] == 14


def test_a_duplicate_workspace_slug_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The error names the field, not the constraint."""

	subroutine.domain.workspaces.create(
		session, slug="home", title="Home", owner=_founder(session)
	)

	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.workspaces.create(
			session, slug="Home", title="Home again", owner=_founder(session)
		)

	assert error.value.status == 409
	assert error.value.errors[0].field == "slug"


def test_a_duplicate_username_is_refused_by_name (session: sqlalchemy.orm.Session) -> None:
	"""Normalised comparison, so case is not a way around it."""

	subroutine.domain.users.create(session, username="Simon")

	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.users.create(session, username="simon")

	assert error.value.errors[0].field == "username"


def test_a_weak_password_is_refused_with_the_reason (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The message is for the person choosing it."""

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.users.create(session, username="someone", password="short")

	assert error.value.errors[0].field == "password"
	assert "12" in error.value.detail


def test_a_service_account_cannot_have_a_password (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent authenticates with a token that can be bounded and revoked."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.users.create(
			session, username="claude", password="a decent passphrase", is_service_account=True
		)


def test_a_password_verifies_and_rehashes_transparently (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Login is the only moment the plaintext exists, so it is when the hash is upgraded."""

	user = subroutine.domain.users.create(
		session, username="simon", password="a decent passphrase"
	)

	assert subroutine.domain.users.verify_password(session, user, "a decent passphrase")
	assert not subroutine.domain.users.verify_password(session, user, "the wrong one")


def test_creating_a_project_makes_its_owner_a_member_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Otherwise a private project is invisible to the person who created it.

	SPEC.md §7.3a grants sight of a private project to holders of a ``project_member`` row
	and to nobody else. Nothing in the application ever wrote one until this was added —
	every row in existence had been inserted by a test — so private visibility was a
	feature that could not be reached through any supported entry point. The row is written
	for public projects too, so that making one private later does not lock its owner out.
	"""

	workspace = _workspace(session)
	owner = _founder(session)
	project = _project(session, workspace, owner_id=owner.id, visibility="private")

	model = subroutine.db.models.project.ProjectMember
	membership = session.scalars(
		sqlalchemy.select(model).where(
			model.project_id == project.id, model.user_id == owner.id
		)
	).one()

	assert membership.role_id is None, "an owner keeps their workspace role, not a new one"

	principal = subroutine.domain.authentication.Principal(user=owner)

	assert subroutine.domain.authorization.is_visible(session, principal, project)


def test_a_project_template_writes_settings_and_nothing_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §6.12: templates are seed-time only and create no statuses."""

	workspace = _workspace(session)

	personal = _project(session, workspace, template="personal")
	software = _project(session, workspace, template="software")

	assert personal.settings["visible_status_keys"] == ["open", "done"]
	assert personal.settings["require_verification_to_complete"] is False
	assert software.settings["require_verification_to_complete"] is True
	assert "in_progress" in software.settings["visible_status_keys"]

	# Both projects live in one workspace with one set of statuses, which is the whole
	# reason a template writes settings rather than seeding rows.
	statuses = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status.key).where(
			subroutine.db.models.vocabulary.Status.workspace_id == workspace.id,
			subroutine.db.models.vocabulary.Status.entity_type == "task",
		)
	).all()

	assert len(statuses) == len(set(statuses)) == 6


def test_an_unknown_template_lists_the_real_ones (session: sqlalchemy.orm.Session) -> None:
	"""Errors name the valid alternatives."""

	workspace = _workspace(session)

	with pytest.raises(subroutine.errors.ValidationError) as error:
		_project(session, workspace, template="agile")

	hint = error.value.errors[0].hint

	assert hint is not None
	assert "software" in hint


def test_a_project_tree_maintains_its_paths (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §10.7 invariant 1: path and depth always agree with parent_id."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	middle = _project(session, workspace, parent=root)
	leaf = _project(session, workspace, parent=middle)

	assert root.path == f"/{root.id}/"
	assert middle.path == f"/{root.id}/{middle.id}/"
	assert leaf.path == f"/{root.id}/{middle.id}/{leaf.id}/"
	assert (root.depth, middle.depth, leaf.depth) == (0, 1, 2)


def test_moving_a_project_takes_its_subtree_with_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""And rewrites every descendant, which is the price of a materialised path."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	middle = _project(session, workspace, parent=root)
	leaf = _project(session, workspace, parent=middle)
	elsewhere = _project(session, workspace)

	rewritten = subroutine.domain.projects.move(session, middle, parent=elsewhere)

	assert rewritten == 2, "the moved project and its one descendant"

	session.refresh(middle)
	session.refresh(leaf)

	assert middle.path == f"/{elsewhere.id}/{middle.id}/"
	assert leaf.path == f"/{elsewhere.id}/{middle.id}/{leaf.id}/"
	assert (middle.depth, leaf.depth) == (1, 2)
	assert middle.parent_id == elsewhere.id

	events = _events(session, workspace.id, "project", middle.id)

	assert [event.action for event in events] == ["created", "moved"]


def test_a_project_cannot_be_moved_inside_itself (session: sqlalchemy.orm.Session) -> None:
	"""The cycle check, including the degenerate case of moving under itself."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)

	for target in (root, child):
		with pytest.raises(subroutine.errors.Conflict) as error:
			subroutine.domain.projects.move(session, root, parent=target)

		assert error.value.code == "cycle_detected"


def test_depth_is_bounded_for_the_whole_subtree (session: sqlalchemy.orm.Session) -> None:
	"""Checked against the deepest descendant, since a move brings everything with it."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)
	deep = _project(session, workspace)

	# `child` sits one below `root`; moving `root` under `deep` would put it two down.
	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.projects.move(session, root, parent=deep, max_depth=1)

	assert error.value.code == "cycle_detected"
	assert "limit is 1" in error.value.detail
	assert child.path.startswith(root.path), "the refused move must have changed nothing"


def test_moving_to_the_same_place_does_nothing (session: sqlalchemy.orm.Session) -> None:
	"""No rows rewritten and no event, so the feed records changes rather than requests."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)

	assert subroutine.domain.projects.move(session, child, parent=root) == 0
	assert [event.action for event in _events(session, workspace.id, "project", child.id)] == [
		"created"
	]


def test_subtasks_get_paths_too (session: sqlalchemy.orm.Session) -> None:
	"""The same machinery serves both trees."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	assert child.path == f"/{parent.id}/{child.id}/"
	assert child.depth == 1


def test_a_mention_appears_and_disappears_with_the_sentence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half of the S1-10 done-criterion (SPEC.md §6.15)."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	target = subroutine.domain.tasks.create(session, project=project, title="The spec")
	cited = subroutine.domain.refs.format_ref(target.ref)
	citing = subroutine.domain.tasks.create(
		session, project=project, title="Implements it", description=f"As decided in {cited}."
	)

	mentions = subroutine.domain.mentions.backlinks(
		session, workspace_id=workspace.id, target_type="task", target_id=target.id
	)

	assert len(mentions) == 1
	assert mentions[0].source_id == citing.id

	subroutine.domain.tasks.update(session, citing, description="No longer refers to anything.")

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=target.id
		)
		== []
	)


def test_the_same_ref_twice_is_one_mention (session: sqlalchemy.orm.Session) -> None:
	"""Repeated references collapse to one edge."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="Cited")
	cited = subroutine.domain.refs.format_ref(target.ref)

	subroutine.domain.tasks.create(
		session,
		project=project,
		title=f"See {cited}",
		description=f"{cited} again, and {cited} once more.",
	)

	assert (
		len(
			subroutine.domain.mentions.backlinks(
				session, workspace_id=workspace.id, target_type="task", target_id=target.id
			)
		)
		== 1
	)


def test_numbers_that_are_not_references_stay_prose (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The cost of a bare-integer ref is that prose is full of integers.

	``#`` is what separates a reference from a quantity, and this is the list of things
	that carry a ``#`` or a number without meaning one. The hex colour is the case worth
	keeping: ``#42FF00`` starts with exactly the characters a reference does.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	# Something for #1 to resolve to, so this tests the pattern and not an empty database.
	subroutine.domain.tasks.create(session, project=project, title="Exists")

	prose = "1 test passing, on line 1, about 1% — brand #1FF000, and ##1, and issue#1."

	task = subroutine.domain.tasks.create(
		session, project=project, title="Reading", description=prose
	)

	mentions = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Mention).where(
				subroutine.db.models.work.Mention.source_id == task.id
			)
		)
	)

	assert mentions == []
	assert task.description == prose, "the text is never altered"


def test_a_task_does_not_mention_itself (session: sqlalchemy.orm.Session) -> None:
	"""Quoting your own ref records nothing."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="First")
	itself = subroutine.domain.refs.format_ref(task.ref)

	subroutine.domain.tasks.update(session, task, description=f"This is {itself}.")

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=task.id
		)
		== []
	)


def test_the_explicit_link_form_is_recognised (session: sqlalchemy.orm.Session) -> None:
	"""``[label](subroutine:1)`` means the same as ``#1``.

	This form carries no sigil, so it is found by its own pattern rather than by the one
	that reads prose — which is exactly why it needs its own test.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="The spec")

	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Implements",
		description=f"Implements [the spec](subroutine:{target.ref}).",
	)

	assert (
		len(
			subroutine.domain.mentions.backlinks(
				session, workspace_id=workspace.id, target_type="task", target_id=target.id
			)
		)
		== 1
	)


def test_a_cross_workspace_link_is_not_resolved_locally (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``subroutine:acme/1`` names a workspace this index does not cover."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="Local number one")

	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Elsewhere",
		description=f"See [theirs](subroutine:acme/{target.ref}).",
	)

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=target.id
		)
		== []
	)


def test_an_update_that_changes_nothing_writes_no_event (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The change feed records changes, not requests."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Steady")

	version = task.version
	subroutine.domain.tasks.update(session, task, title="Steady")

	assert task.version == version
	assert [event.action for event in _events(session, workspace.id, "task", task.id)] == [
		"created"
	]


def test_an_update_records_only_what_moved (session: sqlalchemy.orm.Session) -> None:
	"""And bumps the version once."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Before")

	subroutine.domain.tasks.update(session, task, title="After", importance=3)

	events = _events(session, workspace.id, "task", task.id)

	assert [event.action for event in events] == ["created", "updated"]
	assert events[1].changes == {
		"title": {"from": "Before", "to": "After"},
		"importance": {"from": None, "to": 3},
	}
	assert task.version == 2


def test_absent_and_null_mean_different_things (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §8.3: leaving a field out keeps it; passing null clears it."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(
		session, project=project, title="Has one", description="Something."
	)

	subroutine.domain.tasks.update(session, task, title="Renamed")

	assert task.description == "Something."

	subroutine.domain.tasks.update(session, task, description=None)

	assert task.description is None


def test_an_unknown_status_lists_the_real_ones (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §8.8's worked example, as an actual error."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.tasks.create(
			session, project=project, title="Bad status", status_key="in-progress"
		)

	assert error.value.code == "invalid_status"
	assert error.value.errors[0].field == "status"

	hint = error.value.errors[0].hint

	assert hint is not None
	assert "in_progress" in hint


def test_events_carry_their_actor (session: sqlalchemy.orm.Session) -> None:
	"""Attribution is what makes the trail worth keeping."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	user = subroutine.domain.users.create(session, username="agent-owner")

	# A member, because the service now enforces the permission check. Before it did, this
	# test passed with an actor who belonged to no workspace at all — which is what made
	# the missing enforcement invisible.
	subroutine.domain.workspaces.add_member(session, workspace, user, role_key="member")

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="Agent"
	)
	principal = subroutine.domain.authentication.Principal(user=user, token=token)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Done by an agent", actor=principal
	)

	event = _events(session, workspace.id, "task", task.id)[0]

	assert event.actor_user_id == user.id
	assert event.actor_token_id == token.id
	assert task.created_by == user.id


def test_event_changes_survive_json (session: sqlalchemy.orm.Session) -> None:
	"""UUIDs and datetimes serialise nowhere by default, and appear in changes constantly."""

	moment = subroutine.db.types.utcnow()
	identifier = subroutine.db.types.new_uuid()

	converted = subroutine.domain.events.jsonable(
		{"when": moment, "who": identifier, "tags": {"a", "b"}}
	)

	assert converted["when"] == moment.isoformat()
	assert converted["who"] == str(identifier)
	assert sorted(converted["tags"]) == ["a", "b"]


def test_seq_orders_events_across_entities (session: sqlalchemy.orm.Session) -> None:
	"""The change cursor has to increase, or syncing has nothing to page on."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	for index in range(3):
		subroutine.domain.tasks.create(session, project=project, title=f"Task {index}")

	sequences = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.activity.Event.seq)
			.where(subroutine.db.models.activity.Event.workspace_id == workspace.id)
			.order_by(subroutine.db.models.activity.Event.seq)
		)
	)

	assert sequences == sorted(sequences)
	assert len(sequences) == len(set(sequences))


def test_concurrent_ref_allocation_never_duplicates (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> None:
	"""The bug SQLite cannot express, because it has one writer.

	Runs on PostgreSQL only, with real connections rather than the shared-transaction
	fixture: the whole question is what two transactions do to one row at once, and the
	fixture exists to stop tests seeing each other's transactions.
	"""

	if engine.dialect.name != "postgresql":
		pytest.skip("SQLite serialises writers, so there is no contention to test")

	workers = 4
	each = 10

	setup_engine = subroutine.db.session.create_engine(postgres_url)
	factory = sqlalchemy.orm.sessionmaker(bind=setup_engine, expire_on_commit=False)

	try:
		with factory() as setup:
			# The founder is made here rather than inside `_workspace`, so that the cleanup
			# below can name it. This test is the only one that *commits* to the shared
			# PostgreSQL database, so anything it leaves behind is there for the rest of the
			# run — see the cleanup for what that cost once.
			founder = subroutine.domain.users.create(
				setup, username=f"founder-{uuid.uuid4().hex[:8]}"
			)
			workspace = subroutine.domain.workspaces.create(
				setup,
				slug=f"ws-{uuid.uuid4().hex[:8]}",
				title="Test workspace",
				owner=founder,
			)
			_project(setup, workspace, key="RACE")
			setup.commit()
			workspace_id = workspace.id
			founder_id = founder.id

		def allocate_many () -> list[int]:
			"""Claim refs from an independent connection."""

			with factory() as worker:
				numbers = [
					subroutine.domain.refs.allocate(worker, workspace_id) for _ in range(each)
				]
				worker.commit()

				return numbers

		with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
			results = [future.result() for future in [pool.submit(allocate_many) for _ in range(workers)]]

		allocated = sorted(number for batch in results for number in batch)

		assert allocated == sorted(set(allocated)), "the same ref was handed out twice"
		assert allocated == list(range(1, workers * each + 1))

	finally:
		# **In the ``finally``, and everything this test created — not just the workspace.**
		# Two separate lessons in one block. The user was left behind until 2026-07-30, which was
		# invisible for as long as nothing asserted that the database held exactly one account;
		# local mode does exactly that (§12.1a), so the first test to open a local client against
		# PostgreSQL failed with "this database has more than one account" — a correct test
		# broken by one that had passed for weeks. And the cleanup then sat *after* the
		# assertions, so the one outcome it most needed to survive — this test failing — was the
		# one where it did not run. A test that commits to the shared database owns the whole of
		# what it wrote, on every path.
		with factory() as cleanup:
			cleanup.execute(
				sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
					subroutine.db.models.identity.Workspace.id == workspace_id
				)
			)
			cleanup.execute(
				sqlalchemy.delete(subroutine.db.models.identity.User).where(
					subroutine.db.models.identity.User.id == founder_id
				)
			)
			cleanup.commit()

		setup_engine.dispose()


def test_a_refused_update_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""A caller holds a live session it may still commit, so a half-applied update ships."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Original")

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session, task, title="Changed", status_key="no-such-status"
		)

	assert task.title == "Original", "the title was assigned before the status was validated"
	assert task.version == 1
	assert [event.action for event in _events(session, workspace.id, "task", task.id)] == [
		"created"
	]


def test_an_update_holds_a_title_to_the_same_rule_as_a_create (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A task whose title has been blanked is not a task anybody can find again."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.tasks.update(session, task, title="   ")

	assert error.value.errors[0].field == "title"
	assert task.title == "Findable"


def test_over_length_text_is_refused_the_same_way_on_both_backends (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SQLite does not enforce VARCHAR lengths and PostgreSQL does; neither should decide."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	with pytest.raises(subroutine.errors.PayloadTooLarge) as too_long:
		subroutine.domain.tasks.create(session, project=project, title="x" * 513)

	assert too_long.value.status == 413
	assert too_long.value.errors[0].field == "title"

	with pytest.raises(subroutine.errors.PayloadTooLarge):
		subroutine.domain.users.create(session, username="u" * 65)

	with pytest.raises(subroutine.errors.PayloadTooLarge):
		subroutine.domain.workspaces.create(
			session, slug="s" * 65, title="Fine", owner=_founder(session)
		)


def test_completing_a_task_records_when (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §10.7 invariant 5: completed_at is set exactly when the category is final."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Finish me")

	# Read through a local each time: asserting on the attribute directly narrows its type
	# for the rest of the function, and mypy cannot see that the next update changes it.
	created = task.completed_at

	assert created is None

	subroutine.domain.tasks.update(session, task, status_key="done")
	finished = task.completed_at

	assert finished is not None

	subroutine.domain.tasks.update(session, task, status_key="open")
	reopened = task.completed_at

	assert reopened is None, "reopening must clear it, or the invariant is one-way"

	subroutine.domain.tasks.update(session, task, status_key="cancelled")
	cancelled = task.completed_at

	assert cancelled is not None, "cancelled is a finished category too"


def test_moving_a_project_moves_every_etag_it_changed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`version` is the ETag (SPEC.md §8.9), and a move rewrites descendants' paths."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)
	grandchild = _project(session, workspace, parent=child)
	elsewhere = _project(session, workspace)

	versions = {p.id: p.version for p in (child, grandchild)}

	subroutine.domain.projects.move(session, child, parent=elsewhere)

	for project in (child, grandchild):
		session.refresh(project)

		assert project.version > versions[project.id], (
			f"{project.key}'s path changed but its ETag did not"
		)


def test_a_project_key_can_never_look_like_a_ref (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A key starts with a letter, so a numeric path segment is always a ref (SPEC.md §6.2).

	This mattered less when a ref was ``SR-42``: the two were told apart by shape. Now that
	a ref is a bare integer, ``/v1/projects/123`` and ``/v1/tasks/123`` would be ambiguous
	the moment a project could be keyed ``123`` — so the rule that was cosmetic is now the
	thing keeping two address spaces apart.
	"""

	workspace = _workspace(session)

	for refused in ("3D", "CAFÉ", "123", "!!"):
		with pytest.raises(subroutine.errors.ValidationError) as error:
			subroutine.domain.projects.create(
				session, workspace_id=workspace.id, key=refused, title="No"
			)

		assert error.value.errors[0].field == "key"

	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="web2", title="Yes"
	)
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	assert project.key == "WEB2"
	assert subroutine.domain.refs.parse_ref(project.key) is None, "a key is never a ref"
	assert subroutine.domain.refs.parse_ref(str(task.ref)) == task.ref


def test_a_deleted_workspace_releases_its_short_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Every other identifier frees on soft delete; this one used to be the exception."""

	first = subroutine.domain.workspaces.create(
		session, slug="reusable", title="First", owner=_founder(session)
	)
	first.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	second = subroutine.domain.workspaces.create(
		session, slug="reusable", title="Second", owner=_founder(session)
	)

	assert second.slug == "reusable"
	assert second.id != first.id
