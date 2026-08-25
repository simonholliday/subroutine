"""Tests for the service layer: refs, events, paths and mentions.

The done-criteria for this slice live here — creating a task allocates ``#1``, writes
one event and sets a correct path, and a description citing it produces exactly one
mention row that disappears when the sentence does.

Everything runs on both backends. One test does not: concurrent ref allocation is
meaningless on SQLite, which has a single writer, and it is precisely the bug that is
invisible there by construction.
"""

import concurrent.futures
import datetime
import inspect
import typing
import unittest.mock
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.event
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types
import subroutine.directory
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.links
import subroutine.domain.mentions
import subroutine.domain.patch
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views


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


def _reader (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
) -> subroutine.domain.authentication.Principal:
	"""Return the workspace's owner, as the principal a read is narrowed against.

	``mentions.backlinks`` takes one since `#144`, which is when it gained a caller: §6.15 says
	a mention from a project the reader cannot see is omitted entirely, and a read path that
	looked finished without that narrowing is how the agenda came to ignore ``project_scope``.
	"""

	member = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.WorkspaceMember).where(
			subroutine.db.models.identity.WorkspaceMember.workspace_id == workspace.id
		)
	).first()

	assert member is not None, "the workspace has no members, so nothing can read it"

	owner = session.get(subroutine.db.models.identity.User, member.user_id)

	assert owner is not None

	return subroutine.domain.authentication.Principal(user=owner)


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
	"""One counter per workspace, so a ref names exactly one thing (docs/design.md §6.2)."""

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
	home = _project(session, workspace, key="home")
	other = _project(session, workspace, key="SR")

	first = subroutine.domain.tasks.create(session, project=home, title="In one")
	second = subroutine.domain.tasks.create(session, project=other, title="In the other")

	assert (first.ref, second.ref) == (1, 2)


def test_a_ref_survives_its_task_moving_project (session: sqlalchemy.orm.Session) -> None:
	"""A ref names nothing the task can be moved out of, so a move cannot invalidate it."""

	workspace = _workspace(session)
	home = _project(session, workspace, key="home")
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
	"""docs/design.md §13.7's grammar: ``42``, ``acme/42``, ``work/acme/42``.

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


def test_a_number_too_large_for_a_ref_stays_prose (session: sqlalchemy.orm.Session) -> None:
	"""`#978`: it reached an ``INTEGER`` column, and the whole write was refused.

	``mentions.candidates`` shared ``refs.parse_ref``'s grammar and not its bound, so a number
	written after the sigil became a candidate and was compared against ``task.ref``. It
	reached the writer as *"could not be read: integer out of range"*, sending them to check
	``database_url`` for a number in their own prose.

	**Both backends fail, neither the same way, and that is why the case carries two numbers.**
	Just past :data:`~subroutine.domain.refs.MAX_REF`, PostgreSQL raises ``DataError`` and
	SQLite quietly matches nothing; past 64 bits, SQLite raises ``OverflowError`` as well.
	Measured while falsifying — `#978` recorded this as reachable only on PostgreSQL, and a
	laptop meets it too, with a longer number.

	**Driven through ``synchronize`` rather than asserted on the pattern**, because the pattern
	was never the half that failed: the query was. The write runs *first* deliberately — a
	``candidates`` assertion above it short-circuits, so the failure would read as a list
	mismatch for a defect whose symptom is a refused write.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	source = subroutine.domain.tasks.create(session, project=project, title="Writing about it")
	target = subroutine.domain.tasks.create(session, project=project, title="Findable")

	beyond = subroutine.domain.refs.MAX_REF + 1
	body = f"Compare #{beyond} with #{target.ref}, and #99999999999999999999 as well."

	written = subroutine.domain.mentions.synchronize(
		session,
		workspace_id=workspace.id,
		source_type="task",
		source_id=source.id,
		texts=[body],
	)

	assert written == 1, "the one number that names something is the one that is indexed"

	assert subroutine.domain.mentions.candidates(body) == [target.ref]


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
	"""Vocabulary, an owner and an event, or none of it (docs/design.md §10.7 invariant 7)."""

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
	# the feed's first page is not entirely statuses and roles (docs/design.md §10.7 invariant 9).
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

	docs/design.md §7.3a grants sight of a private project to holders of a ``project_member`` row
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


#: Settings a template may write, because each only *describes* how a project is meant to be
#: used — the test is that none of them can change what the program refuses.
#:
#: **Empty since `#1028`**, and deliberately kept rather than deleted with its last entry.
#: ``visible_status_keys`` was the only one and was read by nothing anywhere in ``src/``: the
#: ninth instance of the declared-and-read-by-nothing family, sitting in the settings map for
#: months while three templates seeded it. `#1029` is the item that would give a template
#: something to write again — the statuses a project's board shows — and this is what will make
#: adding it a decision rather than a habit.
DESCRIBES: frozenset[str] = frozenset()


def test_a_project_template_writes_settings_and_nothing_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""docs/design.md §6.12: templates are seed-time only and create no statuses."""

	workspace = _workspace(session)

	personal = _project(session, workspace, template="personal")
	software = _project(session, workspace, template="software")

	# **A template writes nothing at all today** (`#1028`), and that is asserted rather than
	# left implicit: `project.template` is still accepted, still validated and still refuses an
	# unknown name, so a reader meeting the column has no way to tell *writes nothing yet* from
	# *writes something this test forgot to check*.
	assert personal.settings == {}, "a template wrote a setting nothing declares"
	assert software.settings == {}, "a template wrote a setting nothing declares"

	# **A template may describe; it may not gate** (`#133`). Neither key here is read by
	# anything yet, and that is fine for one of them and was not for the other: a *descriptive*
	# setting is a statement about how this project is meant to be used, and the worst a
	# premature one can do is be ignored. A *gating* one changes what the program refuses, so
	# writing it before the gate exists stores a claim the program does not keep — and turns
	# building the feature into a behaviour change on every project ever made from that
	# template, arriving with a release about something else.
	#
	# `require_verification_to_complete: True` was the second kind and is gone. This list is
	# what makes adding a third a decision rather than a habit.
	for template, written in subroutine.domain.projects.TEMPLATES.items():
		for name in written:
			assert name in DESCRIBES, (
				f"the {template!r} template writes {name!r}. If it only describes how the "
				f"project is meant to be used, add it to DESCRIBES; if it changes what the "
				f"program refuses, it belongs with the feature that enforces it, not here."
			)

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
	"""docs/design.md §10.7 invariant 1: path and depth always agree with parent_id."""

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


def test_one_key_belongs_under_any_number_of_parents (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `#957`: a key is unique among its siblings, so ``dist`` is not spent.

	The whole point of the change. ``substation-dist`` was a real row on this instance and is
	what keying for workspace-uniqueness cost — a name carrying its parent because the address
	could not.
	"""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")
	websites = _project(session, workspace, key="websites")

	under_substation = _project(session, workspace, key="dist", parent=substation)
	under_websites = _project(session, workspace, key="dist", parent=websites)

	session.flush()

	assert under_substation.id != under_websites.id
	assert (
		subroutine.domain.projects.path_of(session, under_substation) == "substation/dist"
	)
	assert subroutine.domain.projects.path_of(session, under_websites) == "websites/dist"


def test_two_siblings_cannot_share_a_key (session: sqlalchemy.orm.Session) -> None:
	"""Which is where the address would stop being unique."""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")

	_project(session, workspace, key="dist", parent=substation)

	with pytest.raises(subroutine.errors.Conflict) as raised:
		_project(session, workspace, key="dist", parent=substation)

	assert raised.value.code == "duplicate_key"
	assert "in that project" in str(raised.value), "it says where, not just that"


def test_two_roots_cannot_share_a_key (session: sqlalchemy.orm.Session) -> None:
	"""**The half a single three-column index would have let through.**

	``parent_id`` is nullable, and NULLs compare as distinct in a unique index on both
	backends — the same rule that makes the deleted-row half of this constraint partial. So
	``(workspace_id, parent_id, key)`` alone guarantees nothing about the top level, which is
	where most projects here live. It is two partial indexes for that reason.
	"""

	workspace = _workspace(session)

	_project(session, workspace, key="dist")

	with pytest.raises(subroutine.errors.Conflict) as raised:
		_project(session, workspace, key="dist")

	assert raised.value.code == "duplicate_key"
	assert "at the top level" in str(raised.value)


def test_the_database_refuses_two_roots_sharing_a_key_too (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The service check is the message; the index is the guarantee.

	Written by inserting past the service, because that is the only way to ask whether the
	constraint is really there — a test driving :func:`create` twice proves the ``if``
	statement and nothing about the schema, and the ``if`` statement is not what a second
	writer races.
	"""

	workspace = _workspace(session)
	first = _project(session, workspace, key="dist")

	session.flush()

	model = subroutine.db.models.project.Project

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		session.execute(
			sqlalchemy.insert(model).values(
				id=subroutine.db.types.new_uuid(),
				workspace_id=workspace.id,
				parent_id=None,
				key="dist",
				title="A second one",
				status_id=first.status_id,
				path="/",
				depth=0,
				position=0,
				is_inbox=False,
				template="blank",
				settings={},
				meta={},
				version=1,
				visibility="public",
			)
		)


def test_a_move_onto_a_taken_key_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""A move could not collide before `#957`, and can now.

	A key was unique across the workspace, so a destination could never already be holding
	one. Among siblings it can, and two rows at one address is the state this whole change
	exists to make impossible.
	"""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")
	websites = _project(session, workspace, key="websites")

	_project(session, workspace, key="dist", parent=substation)
	travelling = _project(session, workspace, key="dist", parent=websites)

	with pytest.raises(subroutine.errors.Conflict) as raised:
		subroutine.domain.projects.move(session, travelling, parent=substation)

	assert raised.value.code == "duplicate_key"
	assert raised.value.errors[0].field == "parent", (
		"the caller named a destination, not a key, so that is the field to correct"
	)


def test_a_move_within_the_same_parent_is_not_a_collision (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Moving something where it already is must not refuse against itself."""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")
	staying = _project(session, workspace, key="dist", parent=substation)

	assert subroutine.domain.projects.move(session, staying, parent=substation) == 0


def test_a_cousin_holding_the_key_does_not_block_a_restore (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#957` narrowed this check, and leaving it wide would have been a refusal with no way out.

	Restoring already refuses when the key was reused, because the uniqueness index ignores
	deleted rows. Asked workspace-wide it would now refuse against a project the constraint no
	longer minds sharing with — and the advice it gives, rename that one, would be asking
	somebody to give up a name for nothing.
	"""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")
	websites = _project(session, workspace, key="websites")
	deleted = _project(session, workspace, key="dist", parent=substation)

	subroutine.domain.projects.delete(session, deleted)
	_project(session, workspace, key="dist", parent=websites)
	session.flush()

	restored = subroutine.domain.projects.restore(session, deleted)

	assert restored.deleted_at is None


def test_a_sibling_holding_the_key_still_blocks_a_restore (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other side of the narrowing, so it is not simply weaker."""

	workspace = _workspace(session)
	substation = _project(session, workspace, key="substation")
	deleted = _project(session, workspace, key="dist", parent=substation)

	subroutine.domain.projects.delete(session, deleted)
	_project(session, workspace, key="dist", parent=substation)
	session.flush()

	with pytest.raises(subroutine.errors.Conflict) as raised:
		subroutine.domain.projects.restore(session, deleted)

	assert raised.value.code == "duplicate_key"


def test_an_address_is_not_a_key (session: sqlalchemy.orm.Session) -> None:
	"""Typing the address when creating one is coherent and cannot be acted on.

	The generic shape refusal would say a key must begin with a letter, which is true of what
	they wrote and no help at all.
	"""

	workspace = _workspace(session)

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		_project(session, workspace, key="substation/dist")

	assert "address rather than a key" in raised.value.errors[0].message
	assert "'dist'" in str(raised.value.errors[0].hint), "and it names the key it would be"


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
	"""The other half of the S1-10 done-criterion (docs/design.md §6.15)."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	target = subroutine.domain.tasks.create(session, project=project, title="The spec")
	cited = subroutine.domain.refs.format_ref(target.ref)
	citing = subroutine.domain.tasks.create(
		session, project=project, title="Implements it", description=f"As decided in {cited}."
	)

	mentions = subroutine.domain.mentions.backlinks(
		session,
		principal=_reader(session, workspace),
		workspace_id=workspace.id, target_type="task", target_id=target.id
	)

	assert len(mentions) == 1
	assert mentions[0].ref == citing.ref

	subroutine.domain.tasks.update(session, citing, description="No longer refers to anything.")

	assert (
		subroutine.domain.mentions.backlinks(
			session,
			principal=_reader(session, workspace),
			workspace_id=workspace.id, target_type="task", target_id=target.id
		)
		== []
	)


def test_a_mention_is_not_a_link_and_nothing_should_say_it_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""What makes the corrected sentence true — `SR#1184`.

	Four surfaces said *"a '#42' in the body becomes a link on item 42"*: both MCP tool
	descriptions, the ``explain`` prose a person reads, and the skill. It does not. It becomes
	an indexed **mention**, and where the cited item governs, a *proposal* somebody confirms —
	which is a better feature than the one being promised, and was going unused because an agent
	reading the description believed the link already existed (`SR#1183`).

	**The prose is not what this guards.** A scan for a sentence is a spelling check, and the
	next person will write the claim differently. This pins the behaviour the sentence describes,
	so anybody who makes a citation auto-link has to come past it — and the failure names the
	four places that would then be right for the first time.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	target = subroutine.domain.tasks.create(session, project=project, title="The decision")
	cited = subroutine.domain.refs.format_ref(target.ref)
	citing = subroutine.domain.tasks.create(
		session, project=project, title="Implements it", description=f"As decided in {cited}."
	)

	# **Both halves, because the absence alone is what a broken write also looks like.** A
	# citation that recorded nothing at all would satisfy "no link was made" perfectly.
	mentions = subroutine.domain.mentions.backlinks(
		session,
		principal=_reader(session, workspace),
		workspace_id=workspace.id, target_type="task", target_id=target.id
	)

	assert [one.ref for one in mentions] == [citing.ref], (
		"the citation was not indexed at all, so this test proves nothing about links"
	)

	joined = subroutine.domain.links.around(
		session,
		_reader(session, workspace),
		workspace_id=workspace.id,
		entity_type="task",
		identifier=citing.id,
	)

	assert joined == [], (
		"citing an item created a typed link. That is a better product than the one this "
		"tests for — but four surfaces describe a mention, and they are now wrong: "
		"mcp/tools.py (twice), cli/topics.py, and the skill in both plugins."
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
				session,
				principal=_reader(session, workspace),
				workspace_id=workspace.id, target_type="task", target_id=target.id
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
			session,
			principal=_reader(session, workspace),
			workspace_id=workspace.id, target_type="task", target_id=task.id
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
				session,
				principal=_reader(session, workspace),
				workspace_id=workspace.id, target_type="task", target_id=target.id
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
			session,
			principal=_reader(session, workspace),
			workspace_id=workspace.id, target_type="task", target_id=target.id
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
	"""docs/design.md §8.3: leaving a field out keeps it; passing null clears it."""

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
	"""docs/design.md §8.8's worked example, as an actual error."""

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
			_project(setup, workspace, key="race")
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
	"""docs/design.md §10.7 invariant 5: completed_at is set exactly when the category is final."""

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


def test_finishing_something_twice_does_not_move_when_it_finished (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#723`. It records when the task became finished, and finishing it again is not a
	second time.

	**Measured before it was fixed**, on a throwaway instance: completing an already-complete
	task moved the stamp by 51 seconds. `POST /v1/tasks/{ref}/complete` on finished work is a
	200 that silently edits history, an ordinary retry does it, and so did the *Complete*
	button that sat on every card in the board's *Done* column (`#724`).

	**The reasoning was written out one function below, about `deleted_at`** — *"deleting twice
	is not an error and does not move the timestamp"* — and only one of the two columns had it.

	Asserted as *unchanged* rather than as *not null*, which is the difference between this and
	the test above it: `assert task.completed_at is not None` passes against the defect.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Finish me")

	subroutine.domain.tasks.complete(session, task)
	first = task.completed_at

	assert first is not None

	# **The clock is moved rather than waited on.** `utcnow()` at second resolution would make
	# a real pause necessary and the test slow *and* flaky; patching the source the assignment
	# reads makes the difference unmissable if it ever fires again.
	later = first + datetime.timedelta(hours=1)

	# Every read goes through a local, for the reason the test above states: asserting on the
	# attribute narrows its type for the rest of the function, and mypy cannot see that the next
	# update changes it. Written the other way first, and mypy called the last assertion
	# unreachable — which is the same trap, one test later.
	with unittest.mock.patch.object(subroutine.db.types, "utcnow", lambda: later):
		subroutine.domain.tasks.complete(session, task)
		again = task.completed_at

		assert again == first, (
			f"completing it a second time moved the record from {first} to {again}"
		)

		# The same through `update`, because `complete` is a thin wrapper over it and a caller
		# setting the status directly must not get the other behaviour.
		subroutine.domain.tasks.update(session, task, status_key="done")
		restated = task.completed_at

		assert restated == first, "setting the finished status again moved it"

		# **Cancelled to done keeps the instant**: both are finished, and the work stopped when
		# it stopped. A column that moved here would be reporting when the status last changed,
		# which is `updated_at`.
		subroutine.domain.tasks.update(session, task, status_key="cancelled")
		switched = task.completed_at

		assert switched == first, "changing which kind of finished moved it"

		# And leaving a finished category still clears it, so the invariant stays two-way.
		subroutine.domain.tasks.update(session, task, status_key="open")
		reopened = task.completed_at

		assert reopened is None, "reopening must still clear it"

		subroutine.domain.tasks.update(session, task, status_key="done")
		refinished = task.completed_at

		assert refinished == later, (
			"finishing it again after reopening is a new completion and must be stamped anew"
		)


def test_moving_a_project_moves_every_etag_it_changed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`version` is the ETag (docs/design.md §8.9), and a move rewrites descendants' paths."""

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


def test_a_subtask_carried_into_another_project_says_so_in_its_own_history (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#200`. The parts moved, their ETags moved, and their histories said nothing.

	§10.7's invariant 9 is that every mutation emits an event. Moving a parent rewrote
	``project_id`` on every descendant and bumped each version with no event anywhere, so a
	client holding a subtask's ETag met a 409 whose cause was not in that subtask's history —
	which read ``created`` and nothing else.

	The event goes on the **child**, not as a count on the parent's. A number on another item's
	event is not an answer to "what happened to this one", and the history somebody opens is
	the one belonging to the thing they were looking at.
	"""

	workspace = _workspace(session)
	home = _project(session, workspace, key="home")
	elsewhere = _project(session, workspace, key="SR")

	parent = subroutine.domain.tasks.create(session, project=home, title="Carries them")
	child = subroutine.domain.tasks.create(
		session, project=home, title="Carried", parent=parent
	)
	grandchild = subroutine.domain.tasks.create(
		session, project=home, title="Also carried", parent=child
	)

	before = {carried.id: carried.version for carried in (child, grandchild)}

	subroutine.domain.tasks.update(session, parent, project=elsewhere)

	for carried in (child, grandchild):
		assert carried.project_id == elsewhere.id

		# The version moving is what makes the silence a defect rather than an omission: a
		# caller is refused on the strength of a change nothing accounts for.
		assert carried.version > before[carried.id]

		events = _events(session, workspace.id, "task", carried.id)
		actions = [event.action for event in events]

		assert actions == ["created", "moved"], f"{carried.title} has no account of the move"

		changes = events[-1].changes or {}

		assert changes["project_id"]["to"] == str(elsewhere.id)
		assert changes["moved_with"]["to"] == parent.ref, "and which move carried it"


def test_a_project_key_can_never_look_like_a_ref (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A key starts with a letter, so a numeric path segment is always a ref (docs/design.md §6.2).

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

	assert project.key == "web2"
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


@pytest.mark.parametrize("entity", ["task", "document", "project"])
def test_a_soft_delete_moves_the_version (
	session: sqlalchemy.orm.Session, entity: str
) -> None:
	"""§8.9: a version that stands still across a delete makes the guard silently useless.

	Read an item at v3, somebody trashes it, and ``expected_version: 3`` still passes — so a
	caller edits a deleted item believing nothing had happened. ``projects.delete`` bumped and
	``tasks.delete``/``documents.delete`` did not, which is precisely what kept the gap
	invisible: anyone checking "does delete bump?" could land on the one that did.

	Parameterised over all three because the defect was *inconsistency*, and a test naming one
	entity would have been the same mistake in test form.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"v-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	inbox = subroutine.domain.bootstrap.inbox_for(session, setup.workspace)
	assert inbox is not None

	if entity == "task":
		row: typing.Any = subroutine.domain.tasks.create(
			session, project=inbox, title="Doomed", actor=None
		)
		remove: typing.Callable[..., typing.Any] = subroutine.domain.tasks.delete

	elif entity == "document":
		row = subroutine.domain.documents.create(
			session, project=inbox, title="Doomed", actor=None
		)
		remove = subroutine.domain.documents.delete

	else:
		row = subroutine.domain.projects.create(
			session,
			workspace_id=setup.workspace.id,
			key="doom",
			title="Doomed",
			owner_id=setup.user.id,
			actor=None,
		)
		remove = subroutine.domain.projects.delete

	session.flush()
	before = row.version

	remove(session, row, actor=None)
	session.flush()

	assert row.deleted_at is not None
	assert row.version > before, (
		f"{entity}.delete left version at {before}, so a stale expected_version would pass"
	)

	# Deleting twice is idempotent and must not keep moving the version either — the timestamp
	# of when something was thrown away is a fact worth not overwriting.
	again = row.version
	remove(session, row, actor=None)
	session.flush()

	assert row.version == again


#: How to change each parameter ``tasks.update`` accepts: what the task must already hold, and
#: what to send. **The population is checked against the signature** by
#: :func:`test_every_parameter_update_accepts_has_a_case`, which is what makes this a guard
#: rather than a sample.
#:
#: **It said it was kept honest and it was not** (`#1268`). The comment here read *"derived by
#: hand from the signature and kept honest by the test below — the list is not the guard, the
#: behaviour is"*, and nothing compared it to anything: it held nine of ``update``'s twenty-one
#: patchable parameters, and ``reminder`` — added with `#1211` — was simply absent, so the field
#: that recorded no event and moved no version had no case to fail.
#:
#: The second half of that sentence is still right and is why each entry drives the real service
#: rather than naming a column. The first half was a comment asserting a guard that was not
#: there, which is worse than no comment: it stops the next reader checking.
CHANGEABLE: dict[str, tuple[dict[str, typing.Any], typing.Any]] = {
	"title": ({}, "A different title"),
	"description": ({}, "Something to say"),
	"status_key": ({}, "in_progress"),
	"type_key": ({}, "bug"),
	"importance": ({}, 4),
	"urgency": ({}, 5),
	"estimate": ({}, "4h"),
	"reminder": ({}, "2w"),
	"due": ({}, "2026-09-01"),
	"due_is_all_day": ({"due": "2026-09-01"}, False),
	"starts": ({}, "2026-09-02"),
	"starts_is_all_day": ({"starts": "2026-09-02"}, False),
	# An end is checked against the start it belongs to, so there has to be one.
	"ends": ({"starts": "2026-09-02"}, "2026-09-03"),
	"snooze": ({}, "2026-08-30"),
	"snoozed_is_all_day": ({"snooze": "2026-08-30"}, False),
	"tags": ({}, ["ops"]),
	# A rule needs a day to anchor on, and *this now repeats* is the change `#1268` found
	# recording nothing at all.
	"recurrence": ({"starts": "2026-09-02"}, "every week"),
}

#: Parameters of ``tasks.update`` that write somewhere other than the row they are addressed to,
#: and so cannot be asked to record an event on it.
#:
#: **Measured rather than assumed** (`#1268`): setting either of these on a repeating task runs a
#: second ``update`` against the *template*, which bumps its version and records its own event —
#: 1 → 2 → 3 across two calls. The addressed occurrence genuinely does not change, so a guard
#: demanding an event on it would be demanding a lie.
WRITES_TO_THE_SERIES = {
	"recurrence_anchor": "changes how the series repeats, on the template that carries the rule",
	"recurrence_trigger": "the same, and refused outright on something that does not repeat",
}

#: Parameters that need a value only a live session can supply, and are driven by their own tests.
NEEDS_A_ROW = {
	"assignee_id": "a user id — `test_content_changes` drives it through the API by username",
	"project": "a Project object, and moving one is `move`'s own test",
}


def test_every_parameter_update_accepts_has_a_case () -> None:
	"""`#1268`. The population comes from the signature, which cannot fall behind the signature.

	This is the test the register above claimed to have. Without it a parameter added to
	``update`` is simply absent from the list, so the behavioural check below runs one fewer
	case and reports a clean run — *no cases failed* and *one case ran* being indistinguishable
	in a parametrisation, which is this project's own recorded lesson from `#405`.

	**Both excuse registers are named rather than inferred**, so a parameter lands in exactly
	one of three places and each of the two exceptions carries a reason somebody can re-ask.
	"""

	patchable = {
		name
		for name, parameter in inspect.signature(subroutine.domain.tasks.update).parameters.items()
		if parameter.default is subroutine.domain.patch.UNSET
	}

	assert len(patchable) > 15, (
		f"only {len(patchable)} patchable parameters were found, so this has stopped reading "
		f"the signature and every field would look covered"
	)

	covered = set(CHANGEABLE) | set(WRITES_TO_THE_SERIES) | set(NEEDS_A_ROW)

	assert patchable - covered == set(), (
		f"update accepts {sorted(patchable - covered)} and nothing here exercises them — add a "
		f"case, or an entry saying which register they belong in and why"
	)
	assert covered - patchable == set(), (
		f"{sorted(covered - patchable)} is no longer a parameter of update"
	)


@pytest.mark.parametrize(
	("field", "value"),
	[(name, value) for name, (_setup, value) in CHANGEABLE.items()],
	# **Insertion order, not sorted** — the cases are built by iterating the mapping, so a
	# sorted id list would label each failure with somebody else's field name.
	ids=list(CHANGEABLE),
)
def test_every_field_an_update_can_change_is_recorded_as_an_event (
	session: sqlalchemy.orm.Session, field: str, value: typing.Any
) -> None:
	"""docs/design.md §10.7 invariant 9: every entity mutation emits at least one event row.

	**`urgency` did not, for a day, and nothing noticed.** It was given a column, a CHECK
	constraint, a sort key and a cell on the compact line, and was left out of the one
	hand-written dict that decides what counts as a change (`tasks._snapshot`). That dict
	decides *whether an event is written at all*, so setting urgency bumped `version`,
	changed the row, and left no trace in the audit trail — a silent hole in the record of
	exactly the field a backlog ranking depends on.

	It was found by building `GET /v1/tasks/{ref}/events`, which is the argument for
	building a reader early: five modules had been writing this table since M1 and nothing
	had ever read it back, so nothing could see what was missing.

	Written per field rather than as a comparison against a list of names, because a list is
	the same kind of hand-maintained thing that failed. This changes each field for real and
	insists the feed says so.

	**What the list could not do is be complete, and it was not** (`#1268`). It held nine of
	``update``'s twenty-one patchable parameters, so ``reminder`` — which recorded no event and
	moved no version — had no case to fail with. The population comes off the signature now, in
	the test above; this half is still the behaviour, which is the part that was always right.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)
	task = subroutine.domain.tasks.create(
		session, project=project, title="Something to do", **CHANGEABLE[field][0]
	)
	session.flush()

	before = len(_events(session, workspace.id, "task", task.id))
	version = task.version

	subroutine.domain.tasks.update(session, task, **{field: value})
	session.flush()

	recorded = _events(session, workspace.id, "task", task.id)

	assert len(recorded) == before + 1, f"changing {field!r} wrote no event"

	changes = recorded[-1].changes or {}

	assert changes, f"the event for {field!r} records no changes at all"

	# **The version, and it is the half `#1268` was really about.** `update` returns before the
	# bump when nothing differs, so a field missing from `_snapshot` leaves §8.9's guard
	# comparing a number that never moves — a stale caller's `If-Match` then passes silently,
	# which is a worse outcome than a gap in the feed.
	assert task.version > version, (
		f"changing {field!r} left version at {version}, so a stale expected_version would pass"
	)


def test_an_update_that_changes_nothing_still_writes_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half of the same rule, and the reason it cannot be "always write one".

	A feed that reported every *request* rather than every *change* would make every client
	polling it filter the noise back out. The fix for the missing `urgency` had to keep this
	true, which is why it was a missing key rather than a missing branch.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)
	task = subroutine.domain.tasks.create(session, project=project, title="Something to do")
	session.flush()

	before = len(_events(session, workspace.id, "task", task.id))

	subroutine.domain.tasks.update(session, task, title="Something to do", urgency=None)
	session.flush()

	assert len(_events(session, workspace.id, "task", task.id)) == before


def test_a_document_is_filed_under_a_different_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``#294``. ``project`` was accepted on create and by nothing afterwards.

	So a conclusion written before anybody had decided where it belonged stayed in the Inbox
	permanently. Found when eleven decision documents — including a live migration runbook —
	could not be filed, and it is worse than the task version was: a document's project is
	what decides **who may read it** (§7.3a), so this was a permissions gap.
	"""

	workspace = _workspace(session)
	docs = _project(session, workspace, key="docs")

	# The workspace's own Inbox, not one made here: `#301` means every workspace has one, and
	# creating a second `INBOX` is now a duplicate key. This is also the real case — the
	# documents that could not be filed were the ones that landed in the Inbox.
	inbox = subroutine.domain.bootstrap.inbox_for(session, workspace)

	assert inbox is not None

	written = subroutine.domain.documents.create(
		session, project=inbox, title="A conclusion", body="Reasoning."
	)
	moved = subroutine.domain.documents.update(session, written, project=docs)

	assert moved.project_id == docs.id
	assert moved.ref == written.ref, "filing it elsewhere does not renumber it"

	# The history says where it went, because a project change is what somebody later asks
	# about — and the event names both ends rather than only the destination.
	recorded = session.scalars(
		sqlalchemy.select(subroutine.db.models.activity.Event)
		.where(subroutine.db.models.activity.Event.entity_id == written.id)
		.order_by(subroutine.db.models.activity.Event.seq.desc())
	).first()

	assert recorded is not None
	assert recorded.changes is not None
	assert recorded.changes["project_id"] == {"from": str(inbox.id), "to": str(docs.id)}


def test_a_document_cannot_be_filed_into_another_workspace (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The refusal ``tasks.update`` gives, for the reason `#297` sets out at length.

	A cross-workspace move rewrites the ref's tenancy (§6.2) *and* leaves the document
	pointing at another workspace's vocabulary — ``status`` and ``item_type`` are per
	workspace and their foreign keys are not scoped to the pair, so nothing in the schema
	would notice. Refused by name rather than half-done.

	Reachable only here, and that is worth saying: both clients resolve a project key inside
	the chosen workspace, so they answer "no such project" first. This is the guard that
	catches a caller holding a row.
	"""

	here = _workspace(session)
	elsewhere = _workspace(session)
	written = subroutine.domain.documents.create(
		session, project=_project(session, here), title="A conclusion"
	)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.documents.update(
			session, written, project=_project(session, elsewhere)
		)

	assert "another workspace" in refused.value.detail


def test_a_workspace_is_created_with_an_inbox_to_file_things_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``#301``. It was ``bootstrap``'s job, so only ``init`` produced a complete workspace.

	Every other route — ``POST /v1/workspaces`` since M1, and ``subroutine workspace create``
	as of `#300` — produced one that refused every task filed with no project, which is the
	ordinary way to file one (§6.14) and the whole of §1.4's capture path. The message it gave
	said setup had been "interrupted" and told you to run ``init`` again, which is advice
	`#264` and `#267` exist to stop being given.

	Asserted through ``tasks.create`` rather than by looking for the row, because "can I file
	something here" is the property, and a project called INBOX that was not flagged
	``is_inbox`` would pass a row check and fail this.
	"""

	workspace = _workspace(session)
	inbox = subroutine.domain.bootstrap.inbox_for(session, workspace)

	assert inbox is not None
	assert inbox.is_inbox

	filed = subroutine.domain.tasks.create(session, project=inbox, title="Buy milk")

	assert filed.project_id == inbox.id
	assert filed.ref == 1, "a new workspace starts its own numbering"


def test_init_does_not_leave_a_second_inbox_behind (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The half of `#301` that would be easy to get wrong in the other direction.

	``bootstrap`` used to create the Inbox itself. Moving that into ``workspaces.create``
	without removing the old call would leave two — and ``inbox_for`` returns one of them,
	so half the writes would land in a project nothing lists.
	"""

	workspace = _workspace(session)
	inboxes = session.scalars(
		sqlalchemy.select(subroutine.db.models.project.Project).where(
			subroutine.db.models.project.Project.workspace_id == workspace.id,
			subroutine.db.models.project.Project.is_inbox.is_(True),
		)
	).all()

	assert len(inboxes) == 1


def test_a_subtask_can_be_re_parented_and_takes_its_own_parts (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#44`. A task accepted a parent on create and never again, in either direction."""

	workspace = _workspace(session)
	project = _project(session, workspace)

	first = subroutine.domain.tasks.create(session, project=project, title="One")
	second = subroutine.domain.tasks.create(session, project=project, title="Two")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Under one", parent=first
	)
	grandchild = subroutine.domain.tasks.create(
		session, project=project, title="Under that", parent=child
	)

	rewritten = subroutine.domain.tasks.move(session, child, parent=second)

	assert rewritten == 2, "the moved task and its one descendant"

	session.refresh(child)
	session.refresh(grandchild)

	assert child.parent_task_id == second.id
	assert child.path == f"/{second.id}/{child.id}/"
	assert grandchild.path == f"/{second.id}/{child.id}/{grandchild.id}/"
	assert (child.depth, grandchild.depth) == (1, 2)

	events = _events(session, workspace.id, "task", child.id)

	assert [event.action for event in events] == ["created", "moved"]


def test_a_subtask_can_be_promoted_to_the_top_level (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other direction, and the one `#44`'s title names first.

	Worth its own case because ``parent=None`` is a *value* here rather than the absence of
	one — the whole reason the endpoint refuses a body that names no parent at all.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	assert child.depth == 1

	subroutine.domain.tasks.move(session, child, parent=None)
	session.refresh(child)

	assert child.parent_task_id is None
	assert child.path == f"/{child.id}/"
	assert child.depth == 0


def test_a_task_cannot_be_moved_inside_its_own_subtree (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The cycle check `#44` names as the failure mode that is invisible until the tree is walked.

	Both cases, because they fail for the same reason and only one of them looks like a
	mistake: under a descendant, and under itself.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	for target in (child, parent):
		with pytest.raises(subroutine.errors.Conflict) as refused:
			subroutine.domain.tasks.move(session, parent, parent=target)

		assert refused.value.code == "cycle_detected"

	session.refresh(parent)

	assert parent.parent_task_id is None, "a refused move must leave the tree alone"


def test_a_move_is_refused_against_the_deepest_thing_it_carries (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The ceiling applies to the subtree, not to the task named.

	`#44` says so explicitly, and it is the half a naive check gets wrong: the task being
	moved arrives with everything below it, so a move that is legal for the task alone can
	push a grandchild past the limit.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	somewhere = subroutine.domain.tasks.create(session, project=project, title="Somewhere")
	top = subroutine.domain.tasks.create(session, project=project, title="Top")
	middle = subroutine.domain.tasks.create(
		session, project=project, title="Middle", parent=top
	)
	subroutine.domain.tasks.create(session, project=project, title="Deep", parent=middle)

	# `top` alone would land at depth 1, inside a limit of 2. Its grandchild would land at 3.
	with pytest.raises(subroutine.errors.Conflict) as refused:
		subroutine.domain.tasks.move(session, top, parent=somewhere, max_depth=2)

	assert refused.value.code == "cycle_detected"
	assert "subtree" in str(refused.value)


def test_a_parent_in_another_project_is_refused_and_both_are_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A subtask belongs to its parent's project, and the refusal has to be actionable.

	**Carrying the subtree into the parent's project was the alternative and was declined**
	(`#44`): it changes a task's project without the caller ever naming the project, where
	``update`` already does that move when asked. So this refuses — and names both projects,
	because "it is in the wrong project" leaves a reader looking up two things before they
	can act.
	"""

	workspace = _workspace(session)
	here = _project(session, workspace, key="here")
	there = _project(session, workspace, key="there")

	moving = subroutine.domain.tasks.create(session, project=here, title="Moving")
	parent = subroutine.domain.tasks.create(session, project=there, title="Parent")

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.move(session, moving, parent=parent)

	reported = refused.value.errors[0]

	assert reported.field == "parent"
	assert "'here'" in (reported.message or "")
	assert "'there'" in (reported.message or "")
	assert "--project there" in (reported.hint or ""), "and says how to do it"


def test_moving_a_task_moves_every_etag_it_changed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`version` is the ETag (§8.9), and the bulk path rewrite sets none.

	The same pair `projects.move` carries. Without the second statement a client holding an
	ETag for a subtask cannot tell that the subtask's path changed underneath it.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	somewhere = subroutine.domain.tasks.create(session, project=project, title="Somewhere")
	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	versions = {row.id: row.version for row in (parent, child)}

	subroutine.domain.tasks.move(session, parent, parent=somewhere)

	for row in (parent, child):
		session.refresh(row)

		assert row.version > versions[row.id], f"{row.title}'s path changed and its ETag did not"


def test_a_document_can_be_made_a_section_of_another (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#44`'s worse half: nesting existed in the schema and in the view and nowhere else.

	No endpoint accepted a document's parent — not on create, not on update — so a
	specification could report itself as a section of something and could never be made one.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	whole = subroutine.domain.documents.create(
		session, project=project, title="The specification"
	)
	part = subroutine.domain.documents.create(session, project=project, title="A section")

	assert part.parent_id is None, "the state this item was filed about"

	subroutine.domain.documents.move(session, part, parent=whole)
	session.refresh(part)

	assert part.parent_id == whole.id
	assert part.path == f"/{whole.id}/{part.id}/"
	assert part.depth == 1

	events = _events(session, workspace.id, "document", part.id)

	assert [event.action for event in events] == ["created", "moved"]

	# And back out again, because a nesting that cannot be undone is half a feature.
	subroutine.domain.documents.move(session, part, parent=None)
	session.refresh(part)

	assert part.parent_id is None
	assert part.depth == 0


def test_a_move_that_changes_nothing_writes_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Re-asking for the place it already is must not bump a version or emit an event.

	``projects.move`` returns 0 for this and the two have to agree, or a client polling a
	feed sees a move nobody made every time something re-asserts a parent.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)

	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	before = child.version

	assert subroutine.domain.tasks.move(session, child, parent=parent) == 0

	session.refresh(child)

	assert child.version == before
	assert [event.action for event in _events(session, workspace.id, "task", child.id)] == [
		"created"
	]


def test_an_address_composed_from_ids_agrees_with_one_composed_from_parents (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Two implementations of one rule, held together rather than left to agree — `#957`.

	The server composes an address from ``project.path``, which is a materialised list of
	**ids** and is what makes a whole page one query. A client has no such field — §6.9 keeps
	it off the view deliberately — so ``directory.address`` walks ``parent_id`` instead. Same
	rule, two mechanisms, and this is the only thing stopping them drifting.

	Driven over a real tree rather than one node, because a walk and a prefix scan agree
	trivially at depth zero.
	"""

	workspace = _workspace(session)
	root = _project(session, workspace, key="substation")
	middle = _project(session, workspace, key="dist", parent=root)
	leaf = _project(session, workspace, key="wheels", parent=middle)
	alone = _project(session, workspace, key="websites")
	rows = [root, middle, leaf, alone]

	session.flush()

	from_ids = subroutine.domain.projects.paths_for(session, [row.id for row in rows])

	assert from_ids[leaf.id] == "substation/dist/wheels", "the seed is the deep case"

	for row in rows:
		assert from_ids[row.id] == subroutine.directory.address(row, rows), (
			f"the two composers disagree about {row.key!r}"
		)


def test_a_page_of_tasks_costs_the_same_number_of_queries_however_many_projects (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#512` asked for the address to be batch-loaded, and this is what says it was.

	**Counted rather than timed.** A ratio catches a query that grew expensive; a *count*
	catches the shape `#39` is about — one more statement per row, which on a fixture of four
	rows is too fast to measure and on a real page is the whole cost. So the assertion is that
	rendering ten tasks across five projects issues exactly what rendering one does.

	The number itself is not asserted, deliberately: it is `Vocabulary`'s business how many
	loads a page needs, and pinning it would fail on every unrelated addition. What must not
	move is the *difference*.
	"""

	workspace = _workspace(session)
	root = _project(session, workspace, key="substation")
	deep = [root]

	for depth in range(5):
		deep.append(_project(session, workspace, key=f"level{depth}", parent=deep[-1]))

	one = [
		subroutine.domain.tasks.create(session, project=deep[-1], title="Only one")
	]
	many = [
		subroutine.domain.tasks.create(
			session, project=deep[index % len(deep)], title=f"Task {index}"
		)
		for index in range(10)
	]
	session.flush()

	def rendered (rows: list[typing.Any]) -> int:
		"""Render a page and return how many statements it took."""

		counted = 0

		def count (*_arguments: typing.Any, **_keywords: typing.Any) -> None:
			"""Tally one statement."""

			nonlocal counted
			counted += 1

		sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", count)

		try:
			vocabulary = subroutine.views.Vocabulary.for_tasks(session, rows)

			for row in rows:
				subroutine.views.task(row, vocabulary)

		finally:
			sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", count)

		return counted

	assert rendered(many) == rendered(one), (
		"a page costs more queries than a single row, so something is loaded per row"
	)

	vocabulary = subroutine.views.Vocabulary.for_tasks(session, many)
	addresses = {subroutine.views.task(row, vocabulary).project_path for row in many}

	assert "substation/level0/level1/level2/level3/level4" in addresses, (
		"the seed is the deep case, or this measures a page of roots"
	)


def test_making_a_start_a_whole_day_is_recorded_even_though_the_instant_is_the_same (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1016`. ``starts_is_all_day`` was missing from ``tasks._snapshot`` and so was silent.

	Its two siblings were both there — ``due_is_all_day`` and ``snoozed_is_all_day`` — so this
	was an omission rather than a rule, and it is `urgency`'s defect from 2026-07-29 in the
	field next door.

	**The reachable case is the one where the instant does not move.** In UTC, an all-day start
	on 2 September and a timed start at midnight on 2 September are the same microsecond, so
	the only thing that changes is the flag. The row changes, ``version`` moves, an ETag a
	client is holding stops matching — and without this the history said nothing happened.

	**The guard beside this one structurally cannot see it.**
	``test_every_field_an_update_can_change_is_recorded_as_an_event`` iterates ``CHANGEABLE``,
	which is a register; a field missing from the register is missing from the test. That is
	`#405`'s two-directional lesson, and the other direction — deriving what ``update`` writes
	by walking its assignments — is `#427`'s method and a bigger piece than this.
	"""

	workspace = _workspace(session)
	project = _project(session, workspace)
	task = subroutine.domain.tasks.create(
		session,
		project=project,
		title="Something to do",
		starts=datetime.datetime(2026, 9, 2, 0, 0, tzinfo=datetime.UTC),
		timezone="UTC",
	)
	session.flush()

	# Read into locals before asserting. Asserting on the attribute itself narrows it to
	# `Literal[False]` for the rest of the function, and mypy has no reason to think `update`
	# moved it — so the second assertion below becomes unreachable and the test stops existing.
	was_all_day = task.starts_is_all_day
	instant = task.starts_at

	assert not was_all_day

	before = len(_events(session, workspace.id, "task", task.id))

	subroutine.domain.tasks.update(
		session, task, starts=datetime.date(2026, 9, 2), timezone="UTC"
	)
	session.flush()

	# The whole point of the fixture: nothing else moved, so a snapshot that forgets the flag
	# compares the task with itself and returns before writing anything.
	assert task.starts_at == instant
	assert task.starts_is_all_day

	recorded = _events(session, workspace.id, "task", task.id)

	assert len(recorded) == before + 1, "flipping only the all-day flag wrote no event"
	assert "starts_is_all_day" in (recorded[-1].changes or {})
