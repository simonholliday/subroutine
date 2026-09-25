"""What a workspace sends over OSC when something happens in it - design ``#2721``, item ``#2722``.

Each of Simon's decisions of 2026-09-25 is a test here: the address ``/subroutine/<type>/<word>``;
words by meaning, so a finish is ``done`` and a retitle ``edited``; a milestone sounding when a
person closes it; the values in their order; titles only where ``osc.titles`` is on; nothing from a
private project; and every event, as it happens. **And the promise under them all**: a write that
rolls back says nothing, and nothing about sending can break a write.

The sender is stood in for by one that keeps what it is handed, decoded, so each assertion reads as
the message a composition would receive; one test sends to a real socket, so the stand-in is not
the only thing that has ever heard one.
"""

import ast
import pathlib
import socket
import struct
import typing
import uuid

import pytest
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.comments
import subroutine.domain.events
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.sounds
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.osc

#: The code whose events are sent, for the guard that every kind of them has a word.
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"

#: A message as a composition receives it: its address and its values.
Heard = tuple[str, list[subroutine.osc.Value]]


class _Sender:
	"""Stands in for :data:`subroutine.osc.SENDER`: keeps what it is handed, decoded."""

	def __init__ (self) -> None:
		"""Start with nothing heard."""

		self.heard: list[Heard] = []
		self.destinations: list[tuple[str, int]] = []

	def send (self, host: str, port: int, datagrams: typing.Iterable[bytes]) -> None:
		"""Keep each datagram as the message it encodes, and where it was going."""

		self.destinations.append((host, port))
		self.heard.extend(_decoded(one) for one in datagrams)


class World(typing.NamedTuple):
	"""A workspace that sends, an open project and a private one, and the person who owns them."""

	session: sqlalchemy.orm.Session
	sender: _Sender
	owner: subroutine.db.models.identity.User
	acting: subroutine.domain.authentication.Principal
	workspace: subroutine.db.models.identity.Workspace
	open: subroutine.db.models.project.Project
	secret: subroutine.db.models.project.Project


@pytest.fixture
def world (session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch) -> World:
	"""Build a workspace whose administrator has set where to send, and clear what that sent."""

	sender = _Sender()
	monkeypatch.setattr(subroutine.osc, "SENDER", sender)
	owner = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:8]}")
	acting = subroutine.domain.authentication.Principal(user=owner)
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"studio-{uuid.uuid4().hex[:8]}", title="Studio", owner=owner
	)
	opened = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="open", title="Open", owner_id=owner.id
	)
	secret = subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=owner.id,
	)
	subroutine.domain.workspaces.update(
		session, workspace, settings={"osc.send_to": "studio.local:9000"}, actor=acting
	)
	session.commit()
	sender.heard.clear()
	sender.destinations.clear()

	return World(session, sender, owner, acting, workspace, opened, secret)


def test_a_filed_item_is_sent_with_its_details (world: World) -> None:
	"""Decisions 2 and 6: its type in the address, then number, importance, urgency, project, who."""

	bug = subroutine.domain.tasks.create(
		world.session,
		project=world.open,
		title="Clicks at the end of a loop",
		type_key="bug",
		importance=4,
		urgency=2,
		actor=world.acting,
	)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/bug/filed", [bug.ref, 4, 2, "open", "person"])]
	assert world.sender.destinations == [("studio.local", 9000)]


def test_an_update_says_whether_it_finished_cancelled_reopened_or_edited (world: World) -> None:
	"""Decision 4: words by meaning, since the feed calls all four ``updated``."""

	task = _filed(world, "Mix the second verse")
	steps: tuple[typing.Callable[[], object], ...] = (
		lambda: subroutine.domain.tasks.complete(world.session, task, actor=world.acting),
		lambda: subroutine.domain.tasks.update(
			world.session, task, status_key="open", actor=world.acting
		),
		lambda: subroutine.domain.tasks.update(
			world.session, task, status_key="cancelled", actor=world.acting
		),
		lambda: subroutine.domain.tasks.update(
			world.session, task, status_key="open", actor=world.acting
		),
		lambda: subroutine.domain.tasks.update(
			world.session, task, title="Mix the third verse", actor=world.acting
		),
	)

	for act in steps:
		act()
		world.session.commit()

	assert [address for address, _ in world.sender.heard] == [
		"/subroutine/task/done",
		"/subroutine/task/reopened",
		"/subroutine/task/cancelled",
		"/subroutine/task/reopened",
		"/subroutine/task/edited",
	]


def test_a_milestone_closing_has_an_address_of_its_own (world: World) -> None:
	"""Decision 5: a milestone sounds when a person closes it, and a fanfare is one mapping."""

	milestone = subroutine.domain.tasks.create(
		world.session, project=world.open, title="Release", type_key="milestone", actor=world.acting
	)
	world.session.commit()
	world.sender.heard.clear()
	subroutine.domain.tasks.complete(world.session, milestone, actor=world.acting)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/milestone/done", [milestone.ref, 0, 0, "open", "person"])]


def test_a_comment_takes_the_type_of_the_item_it_is_on (world: World) -> None:
	"""A comment on a bug is ``/subroutine/bug/commented``, carrying the bug's own details."""

	bug = _filed(world, "Pops on the snare", type_key="bug")
	subroutine.domain.comments.create(
		world.session, entity_type="task", entity_id=bug.id, body="Only at 48k.", actor=world.acting
	)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/bug/commented", [bug.ref, 0, 0, "open", "person"])]


def test_nothing_in_a_private_project_is_sent (world: World) -> None:
	"""Decision 7: not from a private project, not from beneath one, and not a link reaching one."""

	beneath = subroutine.domain.projects.create(
		world.session,
		workspace_id=world.workspace.id,
		key="inner",
		title="Inner",
		parent=world.secret,
		owner_id=world.owner.id,
	)
	hidden = subroutine.domain.tasks.create(
		world.session, project=world.secret, title="Acquire the rival label", actor=world.acting
	)
	subroutine.domain.tasks.create(
		world.session, project=beneath, title="Draft the offer", actor=world.acting
	)
	subroutine.domain.comments.create(
		world.session, entity_type="task", entity_id=hidden.id, body="Quietly.", actor=world.acting
	)
	world.session.commit()

	assert world.sender.heard == []

	seen = _filed(world, "Book the studio")
	subroutine.domain.links.create(
		world.session,
		workspace_id=world.workspace.id,
		source=_end(seen),
		target=_end(hidden),
		link_type_key="blocks",
		actor=world.acting,
	)
	world.session.commit()

	assert world.sender.heard == [], "a link to a private item was sent"


def test_a_title_is_sent_only_where_titles_are_on (world: World) -> None:
	"""Decision 6: off by default, because OSC is not encrypted; on, it comes last."""

	subroutine.domain.tasks.create(
		world.session, project=world.open, title="Tune the kick", actor=world.acting
	)
	world.session.commit()
	subroutine.domain.workspaces.update(
		world.session, world.workspace, settings={"osc.titles": True}, actor=world.acting
	)
	world.session.commit()
	world.sender.heard.clear()
	tuned = subroutine.domain.tasks.create(
		world.session, project=world.open, title="Tune the snare", actor=world.acting
	)
	world.session.commit()

	assert world.sender.heard == [
		("/subroutine/task/filed", [tuned.ref, 0, 0, "open", "person", "Tune the snare"])
	]


def test_a_project_is_named_by_its_keys_from_the_top (world: World) -> None:
	"""A key is unique only among its siblings, so ``open/ui`` rather than ``ui``."""

	beneath = subroutine.domain.projects.create(
		world.session,
		workspace_id=world.workspace.id,
		key="ui",
		title="Screens",
		parent=world.open,
		owner_id=world.owner.id,
	)
	world.session.commit()
	world.sender.heard.clear()
	task = subroutine.domain.tasks.create(
		world.session, project=beneath, title="Draw the mixer", actor=world.acting
	)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/task/filed", [task.ref, 0, 0, "open/ui", "person"])]


def test_an_agent_is_named_as_one (world: World) -> None:
	"""Whether a person or an agent did it is the last value but the title."""

	agent = subroutine.domain.users.create(
		world.session,
		username=f"smith-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		responsible_user_id=world.owner.id,
	)
	subroutine.domain.workspaces.add_member(world.session, world.workspace, agent, role_key="member")
	world.session.commit()
	world.sender.heard.clear()
	task = subroutine.domain.tasks.create(
		world.session,
		project=world.open,
		title="Bounce the stems",
		actor=subroutine.domain.authentication.Principal(user=agent),
	)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/task/filed", [task.ref, 0, 0, "open", "agent"])]


def test_a_rolled_back_write_says_nothing (world: World) -> None:
	"""Composed as the transaction commits, so what never committed is never sent.

	**An edit to an item that stays, as well as an item that never was.** The first needs the
	rollback to be heard: the item is still there at the next commit, so an event left over from
	the rolled-back edit would be described and sent with it.
	"""

	earlier = _filed(world, "The first take")
	subroutine.domain.tasks.create(
		world.session, project=world.open, title="A take that was scrapped", actor=world.acting
	)
	subroutine.domain.tasks.update(
		world.session, earlier, title="A retitle that was scrapped", actor=world.acting
	)
	world.session.rollback()
	kept = subroutine.domain.tasks.create(
		world.session, project=world.open, title="The take that was kept", actor=world.acting
	)
	world.session.commit()

	assert world.sender.heard == [("/subroutine/task/filed", [kept.ref, 0, 0, "open", "person"])]


def test_a_workspace_that_sends_nowhere_sends_nothing (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Off by default: a workspace nobody has pointed anywhere hands the sender nothing at all."""

	sender = _Sender()
	monkeypatch.setattr(subroutine.osc, "SENDER", sender)
	owner = subroutine.domain.users.create(session, username=f"carrie-{uuid.uuid4().hex[:8]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"quiet-{uuid.uuid4().hex[:8]}", title="Quiet", owner=owner
	)
	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="notes", title="Notes", owner_id=owner.id
	)
	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Buy milk",
		actor=subroutine.domain.authentication.Principal(user=owner),
	)
	session.commit()

	assert sender.heard == [] and sender.destinations == []


def test_nothing_about_sending_can_break_a_write (
	world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""**Fire and forget** (Simon): a message that cannot be composed is dropped, never raised.

	The composition is made to fail outright; the write must commit regardless, and nothing be
	sent for it.
	"""

	def broken (*_: typing.Any, **__: typing.Any) -> None:
		"""Fail as a defect in composing would."""

		raise RuntimeError("a defect in composing")

	monkeypatch.setattr(subroutine.domain.sounds, "_message", broken)
	task = subroutine.domain.tasks.create(
		world.session, project=world.open, title="Survive a broken sender", actor=world.acting
	)
	world.session.commit()

	assert world.session.get(subroutine.db.models.work.Task, task.id) is not None
	assert world.sender.heard == []


def test_a_sender_that_fails_after_the_commit_leaves_the_write_committed (
	world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""**A write that has happened is never reported as failed** because sending went wrong.

	The hand-over runs after the commit, so an error out of it would come out of ``commit()``
	about a write that is already in the database. Here the sender cannot even start.
	"""

	class Unstartable:
		"""A sender whose thread cannot be started, as in a process out of threads."""

		def send (self, host: str, port: int, datagrams: typing.Iterable[bytes]) -> None:
			"""Fail as starting a thread fails."""

			raise RuntimeError("can't start new thread")

	monkeypatch.setattr(subroutine.osc, "SENDER", Unstartable())
	task = subroutine.domain.tasks.create(
		world.session, project=world.open, title="Saved whatever the sender does", actor=world.acting
	)
	world.session.commit()

	assert world.session.get(subroutine.db.models.work.Task, task.id) is not None


@pytest.mark.parametrize("sends", [True, False], ids=["sends", "sends-nothing"])
def test_a_write_that_fails_fails_with_its_own_error (world: World, sends: bool) -> None:
	"""**Nothing about sending may change how a failing write fails.**

	Asking the database anything flushes what is pending first, and a flush that failed inside the
	guard composing runs under would be swallowed - so the commit would then report some other
	error than the write's own, and code that turns the write's error into a clear answer would
	never see it. Asked with nothing already in the session, so the lookup has to reach the
	database, which is where it would flush.
	"""

	if not sends:
		subroutine.domain.workspaces.update(
			world.session, world.workspace, settings={"osc.send_to": None}, actor=world.acting
		)
		world.session.commit()

	workspace_id = world.workspace.id
	slug = world.workspace.slug
	world.session.expunge_all()
	world.session.add(subroutine.db.models.identity.Workspace(slug=slug, title="A copy"))
	subroutine.domain.events.record(
		world.session,
		workspace_id=workspace_id,
		entity_type="workspace",
		entity_id=workspace_id,
		action=subroutine.domain.events.EventAction.UPDATED,
	)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		world.session.commit()

	world.session.rollback()


def test_a_real_socket_hears_a_filed_item (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The whole way, with no stand-in: a write, a commit, the sender's thread, and a datagram."""

	with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
		listener.bind(("127.0.0.1", 0))
		listener.settimeout(5)
		port = listener.getsockname()[1]
		owner = subroutine.domain.users.create(session, username=f"hugo-{uuid.uuid4().hex[:8]}")
		acting = subroutine.domain.authentication.Principal(user=owner)
		workspace = subroutine.domain.workspaces.create(
			session, slug=f"live-{uuid.uuid4().hex[:8]}", title="Live", owner=owner
		)
		project = subroutine.domain.projects.create(
			session, workspace_id=workspace.id, key="set", title="Set", owner_id=owner.id
		)
		session.commit()
		subroutine.domain.workspaces.update(
			session, workspace, settings={"osc.send_to": f"127.0.0.1:{port}"}, actor=acting
		)
		session.commit()

		# **Setting it is itself something that happens in the workspace**, so it is heard first:
		# a musician pointing a workspace at a synth hears straight away that it arrives.
		assert _decoded(listener.recvfrom(65_535)[0])[0] == "/subroutine/workspace/edited"

		task = subroutine.domain.tasks.create(
			session, project=project, title="Soundcheck", actor=acting
		)
		session.commit()

		assert _decoded(listener.recvfrom(65_535)[0]) == (
			"/subroutine/task/filed",
			[task.ref, 0, 0, "set", "person"],
		)


def test_where_to_send_is_refused_saying_what_it_looks_like (world: World) -> None:
	"""A destination that is not one is refused by name, with the form it takes."""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.workspaces.update(
			world.session, world.workspace, settings={"osc.send_to": "studio"}, actor=world.acting
		)

	said = refused.value.errors[0]

	assert said.field == "osc.send_to"
	assert "ends in no port" in said.message
	assert "studio.local:9000" in (said.hint or "")


def test_only_an_administrator_may_say_where_to_send (world: World) -> None:
	"""It sends the workspace's activity to a machine on the network: a capability, not a look."""

	member = subroutine.domain.users.create(
		world.session, username=f"gloria-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(world.session, world.workspace, member, role_key="member")

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.domain.workspaces.update(
			world.session,
			world.workspace,
			settings={"osc.send_to": "elsewhere.local:9000"},
			actor=subroutine.domain.authentication.Principal(user=member),
		)


def test_every_kind_of_event_the_code_records_has_a_word () -> None:
	"""Decision 3 sends everything, so everything needs a word - and a new kind, one of its own.

	**Read from the calls that record events**, as ``tests/test_events_scoping.py`` reads their
	kinds, so a kind added later fails here until it is named, rather than going out under its
	bare verb unnoticed.
	"""

	recorded = _recorded()

	assert len(recorded) >= 30, f"only {len(recorded)} kinds were found, so the scan reads too little"

	unnamed = sorted(
		f"{kind}/{action} at {', '.join(where)}"
		for (kind, action), where in recorded.items()
		if (kind, action) not in subroutine.domain.sounds.WORDS and (kind, action) != ("task", "updated")
	)

	assert not unnamed, "these kinds of event have no word in sounds.WORDS:\n  " + "\n  ".join(unnamed)


def test_the_word_guard_sees_a_kind_nobody_named (tmp_path: pathlib.Path) -> None:
	"""The scan above, fed a defect through its own entry point (`#405`)."""

	(tmp_path / "gizmos.py").write_text(
		"import subroutine.domain.events\n"
		"subroutine.domain.events.record(\n"
		"\tsession, entity_type='gizmo', action=subroutine.domain.events.EventAction.CREATED,\n"
		")\n",
		encoding="utf-8",
	)

	assert _recorded(tmp_path) == {("gizmo", "created"): ["gizmos.py:2"]}


def test_every_word_is_one_the_code_can_record () -> None:
	"""And the other direction: a word for a kind nothing records is a stale entry."""

	stale = sorted(set(subroutine.domain.sounds.WORDS) - set(_recorded()))

	assert not stale, f"words for kinds nothing records: {stale}"


def _filed (world: World, title: str, *, type_key: str = "task") -> subroutine.db.models.work.Task:
	"""File an item in the open project, commit it, and forget that it was heard."""

	task = subroutine.domain.tasks.create(
		world.session, project=world.open, title=title, type_key=type_key, actor=world.acting
	)
	world.session.commit()
	world.sender.heard.clear()

	return task


def _end (task: subroutine.db.models.work.Task) -> subroutine.domain.links.End:
	"""Return an item as one end of a link."""

	return subroutine.domain.links.End(
		entity_type="task", id=task.id, ref=task.ref, title=task.title, project_id=task.project_id
	)


def _recorded (root: pathlib.Path = SOURCE) -> dict[tuple[str, str], list[str]]:
	"""Return every kind and verb passed to ``events.record``, and where from.

	**The tree is an argument, so the guard can be shown a defect.** A call whose kind or verb is
	not written out is reported as ``?``, since a value this cannot read is one it cannot check.
	"""

	found: dict[tuple[str, str], list[str]] = {}

	for path in sorted(root.rglob("*.py")):
		if "migrations" in path.parts:
			continue

		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
			if not (
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and node.func.attr == "record"
				and isinstance(node.func.value, ast.Attribute)
				and node.func.value.attr == "events"
			):
				continue

			given = {keyword.arg: keyword.value for keyword in node.keywords}
			kind = given.get("entity_type")
			verb = given.get("action")
			entity = kind.value if isinstance(kind, ast.Constant) and isinstance(kind.value, str) else "?"
			action = (
				subroutine.domain.events.EventAction[verb.attr].value
				if isinstance(verb, ast.Attribute) and verb.attr in subroutine.domain.events.EventAction.__members__
				else "?"
			)
			found.setdefault((entity, action), []).append(f"{path.relative_to(root)}:{node.lineno}")

	return found


def _decoded (datagram: bytes) -> Heard:
	"""Read an OSC message back into its address and values, so an assertion reads like one."""

	address, at = _string(datagram, 0)
	tags, at = _string(datagram, at)
	values: list[subroutine.osc.Value] = []

	for tag in tags[1:]:
		if tag == "i":
			values.append(struct.unpack(">i", datagram[at:at + 4])[0])
			at += 4

		else:
			value, at = _string(datagram, at)
			values.append(value)

	return address, values


def _string (datagram: bytes, at: int) -> tuple[str, int]:
	"""Read one padded OSC string, returning it and where the next thing starts."""

	end = datagram.index(b"\x00", at)

	return datagram[at:end].decode("utf-8"), at + ((end - at) // 4 + 1) * 4
