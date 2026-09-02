"""Every kind of event the code emits is narrowed by something — item ``#303``.

**This file was cited by two docstrings before it existed.** ``scoping.visible_events`` said
*"``tests/test_events_scoping.py`` fails the build when a module emits an ``entity_type`` this
file has no rule for"*, and ``api/changes.py`` repeated the claim to a reader of the endpoint.
Neither was true: the file was not there, and ``FEED_ENTITY_TYPES`` — the allow-list both
sentences rested on — was read by nothing at all. Removing ``"link"`` from it left the feed
tests passing, which is how it was found.

**The property was real and the mechanism was not**, which is the distinction worth keeping.
``visible_events`` builds a clause per kind it knows and ``or``s them, so an ``entity_type``
nobody wrote a clause for matches nothing and is invisible. Fail-closed holds by construction.
What did not exist was anything making the addition of a *new* kind a deliberate act — or
anything that would tell somebody they had added one.

So the set is gone rather than wired. It was a second declaration of what the clauses already
say, it had been wrong once, and a constraint restating them would be one more place for the
two to disagree. What replaces it is derived: the kinds are read out of the calls that emit
them, and each one's narrowing is **measured against a real feed** rather than declared.

This is the third instance of a control that is specified, documented and inert — after
`#247` and `#251` — and all three were found the same way: by changing the control and
noticing that nothing happened.
"""

import ast
import datetime
import pathlib
import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.domain.authentication
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces

SOURCE = pathlib.Path(subroutine.__file__).parent

#: How ``scoping.visible_events`` narrows each kind of event, one entry per kind the code
#: emits. **Not an allow-list**: nothing reads this to decide what is visible, and adding an
#: entry grants nothing. It is a claim about the clauses, and every entry is checked against a
#: real feed below.
#:
#: - ``entity`` — matched by ``entity_type`` and ``entity_id`` against the rows the caller may
#:   read, so the event is exactly as visible as the thing it is about.
#: - ``subject`` — the entity is not something anybody can see on its own (a comment, a link),
#:   so it is matched through ``subject_type``/``subject_id``, the item it happened on.
#: - ``workspace`` — belonging to the workspace is the whole of the test. There is no project
#:   standing between the caller and the fact that somebody was added to one.
NARROWED_BY: dict[str, str] = {
	"task": "entity",
	"project": "entity",
	"document": "entity",
	"comment": "subject",
	"link": "subject",
	# **The task it is about, like a comment and a link** (`#1121`). A verification row names
	# a record and nothing can decide who may see the event from that alone, so the event
	# carries the task as its subject and `scoping.visible_events` narrows on the pair without
	# knowing what kind of thing wrote it.
	"verification": "subject",
	"workspace": "workspace",
	"workspace_member": "workspace",
	# **The project it is about, like a comment and a link** (`#1444`). A project membership
	# row names a project and a person and nothing can decide who may see the event from that
	# alone — and it must not be `workspace`, which is how a *workspace* membership is
	# narrowed: that would publish who has been let into a private project to everybody in the
	# workspace, which is the disclosure the row itself exists to control.
	"project_member": "subject",
}


def _emitted (root: pathlib.Path = SOURCE) -> dict[str | None, list[str]]:
	"""Return every ``entity_type`` passed to ``events.record``, and where from.

	**The tree is an argument so that the guard can be shown a defect** (`#405`): a scanner
	that stopped matching would report no new kinds in exactly the way a healthy tree does.

	A call whose ``entity_type`` is not a literal is reported under ``None``, because a value
	this cannot read is a kind it cannot check — and silently skipping it would be the hole
	rather than a limitation of it.
	"""

	found: dict[str | None, list[str]] = {}

	for path in sorted(root.rglob("*.py")):
		if "migrations" in path.parts:
			continue

		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue

			owner = node.func.value

			if node.func.attr != "record" or not isinstance(owner, ast.Attribute):
				continue

			if owner.attr != "events":
				continue

			for keyword in node.keywords:
				if keyword.arg != "entity_type":
					continue

				kind = (
					keyword.value.value
					if isinstance(keyword.value, ast.Constant)
					and isinstance(keyword.value.value, str)
					else None
				)
				where = f"{path.relative_to(root)}:{node.lineno}"
				found.setdefault(kind, []).append(where)

	return found


def test_every_kind_of_event_the_code_emits_is_narrowed_by_something () -> None:
	"""The check both docstrings claimed and neither had.

	A kind added by a later feature is the failure this exists for, and it fails *open*: the
	feed is the one place in this API where forgetting a rule publishes rather than hides —
	except that it does not, because an unmatched kind matches no clause. What it does instead
	is go missing from the feed silently, for ever, which is the other half of the same
	mistake.
	"""

	emitted = _emitted()
	# `None` is the unreadable case and has its own test below; sorting it beside strings
	# would be the guard failing on its own housekeeping rather than on a finding.
	unclassified = sorted(
		kind for kind in emitted if kind is not None and kind not in NARROWED_BY
	)

	assert not unclassified, (
		f"these kinds of event are emitted and NARROWED_BY says nothing about them: "
		f"{[(kind, emitted[kind]) for kind in unclassified]}"
	)


def test_no_kind_is_recorded_with_a_type_this_cannot_read () -> None:
	"""``entity_type=whatever`` would leave a kind nothing here can check.

	Not forbidden in principle — a polymorphic writer is a reasonable thing to have — but it
	has to be a decision rather than something that happens, because this whole file rests on
	being able to enumerate what is emitted.
	"""

	dynamic = _emitted().get(None, [])

	assert not dynamic, f"these calls pass an entity_type this guard cannot read: {dynamic}"


def test_nothing_is_classified_that_the_code_no_longer_emits () -> None:
	"""And the direction that keeps the classification honest.

	An entry for a kind nothing writes any more reads as a considered decision and is noise
	hiding signal — the property `#290` added to every allow-list here after three stale
	exemptions sat naming an item apiece.
	"""

	stale = sorted(set(NARROWED_BY) - set(_emitted()))

	assert not stale, f"NARROWED_BY names kinds nothing emits: {stale}"


def test_the_scan_notices_a_new_kind (tmp_path: pathlib.Path) -> None:
	"""Shown a module that records something nobody has classified — item ``#405``.

	Both tests above pass by finding nothing, which is the same green a scanner that stopped
	matching would produce. This is what tells them apart, and it goes through ``_emitted``
	rather than through a copy of its rule.
	"""

	(tmp_path / "later.py").write_text(
		"import subroutine.domain.events\n"
		"def note (session, workspace_id, thing):\n"
		"\tsubroutine.domain.events.record(\n"
		"\t\tsession,\n"
		"\t\tworkspace_id=workspace_id,\n"
		"\t\tentity_type='reminder',\n"
		"\t\tentity_id=thing.id,\n"
		"\t\taction='created',\n"
		"\t)\n",
		encoding="utf-8",
	)

	found = _emitted(tmp_path)

	assert set(found) == {"reminder"}
	assert "reminder" not in NARROWED_BY, "which is what the test above would report"


def test_the_scan_notices_a_type_it_cannot_read (tmp_path: pathlib.Path) -> None:
	"""And a call that passes a variable, which is the kind this file cannot enumerate."""

	(tmp_path / "polymorphic.py").write_text(
		"import subroutine.domain.events\n"
		"def note (session, workspace_id, kind, thing):\n"
		"\tsubroutine.domain.events.record(\n"
		"\t\tsession,\n"
		"\t\tworkspace_id=workspace_id,\n"
		"\t\tentity_type=kind,\n"
		"\t\tentity_id=thing.id,\n"
		"\t\taction='created',\n"
		"\t)\n",
		encoding="utf-8",
	)

	assert list(_emitted(tmp_path)) == [None]


def test_the_scan_ignores_a_record_that_is_not_the_events_one (
	tmp_path: pathlib.Path,
) -> None:
	"""``something_else.record(entity_type=…)`` is not this table being written to.

	Without this, a matcher keyed on the method name alone would collect kinds from anything
	that happened to have a ``record`` — and the classification would grow entries for events
	that are not events, which is how an allow-list stops meaning anything.
	"""

	(tmp_path / "elsewhere.py").write_text(
		"import subroutine.domain.audit\n"
		"def note (session, thing):\n"
		"\tsubroutine.domain.audit.record(session, entity_type='reminder', entity_id=thing.id)\n",
		encoding="utf-8",
	)

	assert _emitted(tmp_path) == {}


class World(typing.NamedTuple):
	"""A workspace holding one private project, and somebody who is not in it."""

	workspace: subroutine.db.models.identity.Workspace
	owner: subroutine.db.models.identity.User
	outsider: subroutine.db.models.identity.User
	task: subroutine.db.models.work.Task
	project: subroutine.db.models.project.Project
	document: subroutine.db.models.work.Document

	#: A task outside the private project, so a link can be made to span the boundary. `#302`
	#: is about exactly that shape and nothing here could build one before.
	visible: subroutine.db.models.work.Task


@pytest.fixture(autouse=True)
def _no_watermark (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report an event the moment it is written, for the length of this file.

	The feed withholds the last second by design (`#404`) — ``seq`` is allocated at insert and
	becomes visible at commit, so a resumable cursor must not be handed a number past rows
	that were still uncommitted. Everything here is written and read in the same breath, so
	without this every test would be measuring the clock.

	Set rather than waited out, which is the lesson `#404` cost: the version that slept was
	flaky on a loaded runner and told nobody anything about the feed. ``test_api_changes.py``
	asserts the *shipped* value is not zero, so switching it off here cannot hide a change to
	it.
	"""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""Build a private project two members of one workspace can see differently."""

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
	task = subroutine.domain.tasks.create(
		session,
		project=private,
		title="Acquire the rival company",
		actor=subroutine.domain.authentication.Principal(user=owner),
	)
	document = subroutine.domain.documents.create(
		session,
		project=private,
		title="How we would do it",
		body="Carefully.",
		actor=subroutine.domain.authentication.Principal(user=owner),
	)
	visible = subroutine.domain.tasks.create(
		session,
		project=subroutine.domain.projects.create(
			session,
			workspace_id=workspace.id,
			key="open",
			title="Open",
			owner_id=owner.id,
		),
		title="Book the venue",
		actor=subroutine.domain.authentication.Principal(user=owner),
	)
	session.flush()

	return World(
		workspace=workspace,
		owner=owner,
		outsider=outsider,
		task=task,
		project=private,
		document=document,
		visible=visible,
	)


def _reaches (
	session: sqlalchemy.orm.Session, world: World, user: typing.Any, seq: int
) -> bool:
	"""Report whether one principal's feed carries the event with that ``seq``.

	**Through ``events.feed``, not through the predicate alone.** The workspace-level kinds are
	narrowed by which workspaces the caller reaches rather than by anything inside the clause,
	so a test that called ``visible_events`` directly would be handing it the answer.
	"""

	principal = subroutine.domain.authentication.Principal(user=user)
	statement = subroutine.domain.events.feed(
		principal,
		workspace_ids=[
			row.id for row in subroutine.domain.workspaces.readable(session, principal)
		],
	)

	return seq in {row.seq for row in session.scalars(statement)}


def _recorded (session: sqlalchemy.orm.Session, world: World, kind: str) -> int:
	"""Write one event of that kind, about something only the owner can see."""

	made: dict[str, typing.Any] = {
		"task": {"entity_type": "task", "entity_id": world.task.id},
		"project": {"entity_type": "project", "entity_id": world.project.id},
		"document": {"entity_type": "document", "entity_id": world.document.id},
		"comment": {
			"entity_type": "comment",
			"entity_id": uuid.uuid4(),
			"subject_type": "task",
			"subject_id": world.task.id,
		},
		"link": {
			"entity_type": "link",
			"entity_id": uuid.uuid4(),
			"subject_type": "task",
			"subject_id": world.task.id,
		},
		# Narrowed by its subject, like the two above and for the same reason (`#1121`): the
		# row names a record and nothing can decide who may see one from that alone.
		"verification": {
			"entity_type": "verification",
			"entity_id": uuid.uuid4(),
			"subject_type": "task",
			"subject_id": world.task.id,
		},
		"workspace": {"entity_type": "workspace", "entity_id": world.workspace.id},
		"workspace_member": {
			"entity_type": "workspace_member",
			"entity_id": uuid.uuid4(),
		},
		# Narrowed by the project it happened on, so it reaches exactly those who can see that
		# project — which for a private one is the people already in it (`#1444`).
		"project_member": {
			"entity_type": "project_member",
			"entity_id": uuid.uuid4(),
			"subject_type": "project",
			"subject_id": world.project.id,
		},
	}[kind]

	event = subroutine.domain.events.record(
		session, workspace_id=world.workspace.id, action="created", **made
	)
	session.flush()

	return event.seq


@pytest.mark.parametrize(
	"kind", sorted(key for key, how in NARROWED_BY.items() if how != "workspace")
)
def test_an_event_about_a_private_thing_reaches_only_those_who_may_see_it (
	session: sqlalchemy.orm.Session, world: World, kind: str
) -> None:
	"""The claim ``NARROWED_BY`` makes, measured against a real feed rather than declared.

	Every kind here is about something inside a private project, so the owner sees the event
	and the other member of the same workspace does not. A kind whose clause was never written
	would fail the first half; one matched too broadly would fail the second.

"""

	seq = _recorded(session, world, kind)

	assert _reaches(session, world, world.owner, seq), f"{kind} never reaches its own author"
	assert not _reaches(session, world, world.outsider, seq), (
		f"a {kind} event about a private project reaches somebody who cannot see it"
	)


@pytest.mark.parametrize(
	"kind", sorted(key for key, how in NARROWED_BY.items() if how == "workspace")
)
def test_a_workspace_level_event_reaches_the_whole_workspace (
	session: sqlalchemy.orm.Session, world: World, kind: str
) -> None:
	"""Belonging to the workspace is the whole of the test for these two.

	There is no project standing between a member and the fact that somebody was added to one,
	so both members see it — and that is the classification being checked, not an oversight.
	Somebody outside the workspace never reaches it, because the feed is handed only the
	workspaces they can read.
	"""

	seq = _recorded(session, world, kind)

	assert _reaches(session, world, world.owner, seq)
	assert _reaches(session, world, world.outsider, seq), "both are members"

	stranger = subroutine.domain.users.create(
		session, username=f"nobody-{uuid.uuid4().hex[:8]}"
	)
	session.flush()

	assert not _reaches(session, world, stranger, seq), "and nobody outside the workspace"


def test_an_event_of_an_unknown_kind_reaches_nobody (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""Fail-closed, measured — the property ``FEED_ENTITY_TYPES`` claimed and did not provide.

	This is what makes deleting the set the right answer rather than the cheap one. The
	clauses *are* the allow-list: a kind nobody wrote a rule for matches none of them, so it
	is invisible to everybody including the person who caused it. A second declaration could
	only ever restate that, and would be one more thing to keep in step.

	It is invisible to the **owner** as well, which is worth asserting rather than assuming: a
	new kind of event does not leak, and it also does not appear, so the failure a later
	feature meets is a silent absence rather than a disclosure.
	"""

	event = subroutine.domain.events.record(
		session,
		workspace_id=world.workspace.id,
		entity_type="reminder",
		entity_id=uuid.uuid4(),
		action="created",
	)
	session.flush()

	assert not _reaches(session, world, world.owner, event.seq)
	assert not _reaches(session, world, world.outsider, event.seq)


def test_an_event_on_two_things_reaches_only_somebody_who_may_see_both (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""`SR#302`. One subject expresses one item's visibility, and some writes touch two.

	The event here is about a **visible** task, so every clause above lets it through — and its
	second subject is inside the private project. Before the conjunction the outsider was told
	that something they can see is joined to something they cannot, and `changes` carried the
	hidden item's ref: a number rather than a title, but the *relationship* is new information
	and refs are close to guessable already.

	**The owner still sees it**, which is the half that stops the fix being "hide link events".
	"""

	event = subroutine.domain.events.record(
		session,
		workspace_id=world.workspace.id,
		entity_type="link",
		entity_id=uuid.uuid4(),
		subject_type="task",
		subject_id=world.visible.id,
		subject_b_type="task",
		subject_b_id=world.task.id,
		action="created",
	)
	session.flush()

	assert _reaches(session, world, world.owner, event.seq), (
		"the owner may see both ends and is told about neither"
	)
	assert not _reaches(session, world, world.outsider, event.seq), (
		"an event joining a visible item to a private one reached somebody who may see one end"
	)


def test_the_second_subject_narrows_a_kind_that_is_not_a_link (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""The rule must be *a second subject must be visible*, never *a link's far end must be*.

	``scoping.visible_events`` is built so that no clause in it knows about any particular
	kind — a kind is narrowed through its own identity, and the moment one clause names one the
	next has a precedent. So this asserts the property on a **comment**, which sets no second
	subject anywhere in the code today: nothing about the mechanism may depend on links, and
	whatever next happens to two items inherits the conjunction without a line being written.
	"""

	event = subroutine.domain.events.record(
		session,
		workspace_id=world.workspace.id,
		entity_type="comment",
		entity_id=uuid.uuid4(),
		subject_type="task",
		subject_id=world.visible.id,
		subject_b_type="document",
		subject_b_id=world.document.id,
		action="created",
	)
	session.flush()

	assert _reaches(session, world, world.owner, event.seq)
	assert not _reaches(session, world, world.outsider, event.seq), (
		"the conjunction is implemented for links rather than for a second subject"
	)


def test_a_link_across_the_boundary_is_written_with_both_ends (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""End to end: the write path has to set the pair or the predicate guards an empty column.

	`SR#303`'s lesson is the one being applied — a control that is specified, documented and
	inert reads exactly like one that works. The two tests above hand the predicate a second
	subject and prove it narrows; this one never mentions the column, and fails if
	``links.create`` stops filling it.

	**Made from the visible end, and that direction is the whole test.** The event's subject is
	then the task the outsider *may* see, so every clause in the disjunction lets it through and
	the second subject is the only thing that can hide it. Written the other way round — source
	private, target visible — this passes against the unfixed code, because the subject alone
	already excludes them.
	"""

	acting = subroutine.domain.authentication.Principal(user=world.owner)

	before = {row.seq for row in session.scalars(
		subroutine.domain.events.feed(acting, workspace_ids=[world.workspace.id])
	)}

	near = subroutine.domain.links.resolve(
		session, acting, workspace_id=world.workspace.id,
		entity_type="task", identifier=world.visible.id,
	)
	far = subroutine.domain.links.resolve(
		session, acting, workspace_id=world.workspace.id,
		entity_type="task", identifier=world.task.id,
	)

	# `resolve` answers ``None`` for an end this caller may not see, and the owner may see
	# both — so this is the assertion that stops a mis-set fixture testing an empty feed.
	assert near is not None and far is not None

	joined = subroutine.domain.links.create(
		session,
		workspace_id=world.workspace.id,
		source=near,
		target=far,
		link_type_key="blocks",
		actor=acting,
	)
	session.flush()

	after = {row.seq for row in session.scalars(
		subroutine.domain.events.feed(acting, workspace_ids=[world.workspace.id])
	)}

	(made,) = sorted(after - before)

	assert _reaches(session, world, world.owner, made), "the author was not told about their own link"
	assert not _reaches(session, world, world.outsider, made), (
		"a link from a visible task to a private one reached somebody who may see only the "
		"visible end — so the write path is not recording the second subject"
	)

	# **And the withdrawal, which discloses nothing and is still narrowed.** ``links.remove``
	# records no ``changes`` at all, so an unlink never named the far end — but a reader who
	# was not told the link was made and *is* told it went away has learned the same thing one
	# step later. The visibility model is uniform or it is a hole with a delay on it.
	subroutine.domain.links.remove(session, joined, actor=acting)
	session.flush()

	withdrawn = {row.seq for row in session.scalars(
		subroutine.domain.events.feed(acting, workspace_ids=[world.workspace.id])
	)} - after - before

	(gone,) = sorted(withdrawn)

	assert _reaches(session, world, world.owner, gone)
	assert not _reaches(session, world, world.outsider, gone), (
		"the unlink reached somebody the link itself was hidden from"
	)
