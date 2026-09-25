"""What a workspace sends over OSC when something happens in it - design `#2721`, item `#2722`.

**Every event the change feed records**, in a workspace whose ``osc.send_to`` is set, becomes one
message at ``/subroutine/<type>/<word>`` - ``/subroutine/bug/filed``, ``/subroutine/milestone/done``
- carrying the item's number, importance, urgency and project and whether a person or an agent did
it, with the title only where ``osc.titles`` is on. **Nothing in a private project, or beneath one,
is sent.** Simon's decisions of 2026-09-25, each recorded on `#2721`.

**Composed as the transaction commits, and sent once it has.** ``events.record`` leaves each event
on its session. Just before the commit, this reads what each one says, in the same transaction
and with everything it names still to hand; just after, the datagrams go to
:data:`subroutine.osc.SENDER`, whose own thread sends them. So a write that rolls back sends
nothing, and no write waits for the network.

**Nothing here may break a write** (Simon: *it never holds anything up or breaks anything on a
fail*). Anything that goes wrong while composing drops that workspace's messages and nothing else,
and what this reads is rows by their keys - the ones the write has just touched, and the
vocabulary, projects and people they name.
"""

import collections
import contextlib
import typing
import uuid

import sqlalchemy
import sqlalchemy.event
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.hierarchy
import subroutine.domain.settings
import subroutine.osc

#: Where ``events.record`` leaves each event it records, on the event's session.
PENDING = "subroutine.domain.sounds.pending"

#: Where the datagrams composed for a commit wait for it, as ``(host, port, datagram)``.
READY = "subroutine.domain.sounds.ready"

#: Where every address starts, so a composition fed by several apps cannot mistake ours for
#: another's (Simon's decision 2 on `#2721`).
PREFIX = "/subroutine"

#: The status categories that finish an item: a move into one is ``done`` or ``cancelled``, and a
#: move out of one is ``reopened``.
FINISHED = frozenset({"done", "cancelled"})

#: What each kind of event is called in its address - Simon's decision 4 on `#2721`: **plain words
#: by meaning**, because the feed's own verbs cannot tell a finish from a retitle.
#:
#: **An item's update is not here**: it is ``done``, ``cancelled``, ``reopened`` or ``edited`` by
#: what it changed, which :func:`word` works out. ``tests/test_sounds.py`` holds this against every
#: kind of event the source records, so a kind added later cannot go out under its bare verb
#: unnoticed.
WORDS: dict[tuple[str, str], str] = {
	("task", "created"): "filed",
	("task", "moved"): "moved",
	("task", "deleted"): "deleted",
	("task", "restored"): "restored",
	("task", "claimed"): "claimed",
	("task", "released"): "released",
	("document", "created"): "written",
	("document", "updated"): "revised",
	("document", "moved"): "moved",
	("document", "deleted"): "deleted",
	("document", "restored"): "restored",
	("comment", "created"): "commented",
	("comment", "updated"): "comment-edited",
	("comment", "deleted"): "comment-deleted",
	("link", "created"): "linked",
	("link", "deleted"): "unlinked",
	("verification", "created"): "verified",
	("project", "created"): "created",
	("project", "updated"): "edited",
	("project", "moved"): "moved",
	("project", "deleted"): "deleted",
	("project", "restored"): "restored",
	("project_member", "created"): "joined",
	("project_member", "deleted"): "left",
	("workspace", "created"): "created",
	("workspace", "updated"): "edited",
	("workspace", "deleted"): "deleted",
	("workspace", "restored"): "restored",
	("workspace", "seeded"): "seeded",
	("workspace_member", "created"): "joined",
	("workspace_member", "updated"): "role-changed",
	("workspace_member", "deleted"): "left",
}


class About (typing.NamedTuple):
	"""What an event is about, as its message tells it: a type for the address, and details."""

	#: The item's type key - ``bug``, ``milestone``, ``decision`` - or ``project`` or ``workspace``.
	type: str

	#: The item's number, or 0 where the event is about no item.
	number: int

	importance: int

	urgency: int

	#: The project it is in, which decides whether it is private and what the message names.
	project_id: uuid.UUID | None

	title: str


def word (session: sqlalchemy.orm.Session, event: subroutine.db.models.activity.Event) -> str:
	"""Return what an event says happened, in the one word that ends its address.

	**By meaning rather than by the feed's own verb**, because ``updated`` is both a finish and a
	retitle: an item's update is ``done`` or ``cancelled`` where its status moves into a finished
	category, ``reopened`` where it moves out of one, and ``edited`` otherwise. The rest is
	:data:`WORDS`, and a kind nobody has named yet goes out under its verb rather than not at all.
	"""

	if (event.entity_type, event.action) == ("task", "updated"):
		return _an_update(session, event.changes or {})

	return WORDS.get((event.entity_type, event.action), event.action)


def _an_update (session: sqlalchemy.orm.Session, changes: dict[str, typing.Any]) -> str:
	"""Say whether an item's update finished it, cancelled it, reopened it, or edited it."""

	moved = changes.get("status_id")

	if isinstance(moved, dict):
		before = _category(session, moved.get("from"))
		after = _category(session, moved.get("to"))

		if after in FINISHED and before not in FINISHED:
			return "done" if after == "done" else "cancelled"

		if before in FINISHED and after not in FINISHED:
			return "reopened"

	return "edited"


def _category (session: sqlalchemy.orm.Session, status_id: typing.Any) -> str | None:
	"""Return the category of a status named by its id as the feed records it, or ``None``."""

	if not isinstance(status_id, str):
		return None

	found = session.get(subroutine.db.models.vocabulary.Status, uuid.UUID(status_id))

	return None if found is None else found.category


def _composed (session: sqlalchemy.orm.Session) -> None:
	"""Before a commit: turn the events it records into datagrams, for each workspace that sends.

	**Nothing here may change how a failing write fails.** Asking the database anything flushes
	what is pending first, and a flush can fail - a duplicate, a missing row - so a lookup made
	inside the guard below would swallow the write's own error, and the commit would then report
	a different one. So the settings are read without flushing, and only where a workspace sends
	is the flush done here, outside the guard: the flush the commit was about to make anyway,
	failing with its own error exactly as it would have there.
	"""

	pending: list[subroutine.db.models.activity.Event] | None = session.info.pop(PENDING, None)

	if not pending:
		return

	by_workspace: dict[uuid.UUID, list[subroutine.db.models.activity.Event]] = (
		collections.defaultdict(list)
	)

	for event in pending:
		by_workspace[event.workspace_id].append(event)

	sending: list[tuple[list[subroutine.db.models.activity.Event], tuple[str, int, bool]]] = []

	with session.no_autoflush:
		for workspace_id, events in by_workspace.items():
			# **One workspace's trouble is its own**: a destination that no longer reads drops
			# that workspace's messages and leaves the commit alone.
			with contextlib.suppress(Exception):
				found = _destination(session, workspace_id)

				if found is not None:
					sending.append((events, found))

	if not sending:
		return

	session.flush()

	with session.no_autoflush:
		for events, (host, port, titles) in sending:
			# An event this cannot describe drops its workspace's messages, and nothing else.
			with contextlib.suppress(Exception):
				session.info.setdefault(READY, []).extend(
					_datagrams(session, events, host=host, port=port, titles=titles)
				)


def _destination (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> tuple[str, int, bool] | None:
	"""Return where a workspace sends and whether titles go, or ``None`` where it sends nothing."""

	workspace = session.get(subroutine.db.models.identity.Workspace, workspace_id)

	if workspace is None:
		return None

	stored = [workspace.settings or {}]
	written = subroutine.domain.settings.in_force(
		subroutine.domain.settings.OSC_SEND_TO, stored=stored
	)

	if not written:
		return None

	host, port = subroutine.osc.destination(written)
	titles = (
		subroutine.domain.settings.in_force(subroutine.domain.settings.OSC_TITLES, stored=stored)
		is True
	)

	return host, port, titles


def _datagrams (
	session: sqlalchemy.orm.Session,
	events: list[subroutine.db.models.activity.Event],
	*,
	host: str,
	port: int,
	titles: bool,
) -> list[tuple[str, int, bytes]]:
	"""Return the datagrams one workspace's events make, for where it sends."""

	known: dict[uuid.UUID, list[subroutine.db.models.project.Project]] = {}
	datagrams: list[tuple[str, int, bytes]] = []

	for event in events:
		message = _message(session, event, titles=titles, known=known)

		if message is not None:
			datagrams.append((host, port, subroutine.osc.encode(*message)))

	return datagrams


def _message (
	session: sqlalchemy.orm.Session,
	event: subroutine.db.models.activity.Event,
	*,
	titles: bool,
	known: dict[uuid.UUID, list[subroutine.db.models.project.Project]],
) -> tuple[str, list[subroutine.osc.Value]] | None:
	"""Return one event's address and values, or ``None`` where it is not to be sent.

	**Not sent where it is about something private** (Simon's decision 7): an item, document or
	project in a private project or beneath one - and a link where either end is.
	"""

	about = _about(session, event)

	if about is None:
		return None

	lineage = _lineage(session, about.project_id, known)
	other = _subject(session, event.subject_b_type, event.subject_b_id)

	if _private(lineage) or (
		other is not None and _private(_lineage(session, other.project_id, known))
	):
		return None

	values: list[subroutine.osc.Value] = [
		about.number,
		about.importance,
		about.urgency,
		"/".join(one.key for one in lineage),
		_who(session, event),
	]

	if titles:
		values.append(about.title)

	return f"{PREFIX}/{about.type}/{word(session, event)}", values


def _about (
	session: sqlalchemy.orm.Session, event: subroutine.db.models.activity.Event
) -> About | None:
	"""Return what an event is about: its own entity, or the item or project it happened on.

	A comment, a link, a verification and a project's member are each about the thing they are
	on (Simon's decision 2: *a comment or a link takes the type of the item it is on*), which is
	what the event's subject already names for the change feed.
	"""

	if event.entity_type in ("task", "document", "project"):
		return _subject(session, event.entity_type, event.entity_id)

	if event.entity_type in ("workspace", "workspace_member"):
		return _workspace(session, event.workspace_id)

	return _subject(session, event.subject_type, event.subject_id)


def _subject (
	session: sqlalchemy.orm.Session, kind: str | None, identifier: uuid.UUID | None
) -> About | None:
	"""Return an item, a document or a project as its message tells it, or ``None``."""

	if identifier is None:
		return None

	if kind == "project":
		project = session.get(subroutine.db.models.project.Project, identifier)

		if project is None:
			return None

		return About(
			type="project", number=0, importance=0, urgency=0, project_id=project.id,
			title=project.title,
		)

	if kind not in ("task", "document"):
		return None

	found: subroutine.db.models.work.Task | subroutine.db.models.work.Document | None = (
		session.get(subroutine.db.models.work.Task, identifier)
		if kind == "task"
		else session.get(subroutine.db.models.work.Document, identifier)
	)

	if found is None:
		return None

	typed = session.get(subroutine.db.models.vocabulary.ItemType, found.type_id)
	task = found if isinstance(found, subroutine.db.models.work.Task) else None

	return About(
		type=kind if typed is None else typed.key,
		number=found.ref,
		importance=0 if task is None else task.importance or 0,
		urgency=0 if task is None else task.urgency or 0,
		project_id=found.project_id,
		title=found.title,
	)


def _workspace (session: sqlalchemy.orm.Session, identifier: uuid.UUID) -> About | None:
	"""Return a workspace as its message tells it."""

	found = session.get(subroutine.db.models.identity.Workspace, identifier)

	if found is None:
		return None

	return About(
		type="workspace", number=0, importance=0, urgency=0, project_id=None,
		title=found.title or found.slug,
	)


def _lineage (
	session: sqlalchemy.orm.Session,
	project_id: uuid.UUID | None,
	known: dict[uuid.UUID, list[subroutine.db.models.project.Project]],
) -> list[subroutine.db.models.project.Project]:
	"""Return a project and every project above it, outermost first, asking once per project.

	**One answer to two questions**: whether it is private - it is where any of these is, which is
	:func:`subroutine.domain.authorization.visible_projects`'s rule - and what the message calls
	it, its keys joined as ``subroutine/ui``, since a key is unique only among its siblings.
	"""

	if project_id is None:
		return []

	if project_id in known:
		return known[project_id]

	model = subroutine.db.models.project.Project
	project = session.get(model, project_id)
	ids = (
		[]
		if project is None
		else [uuid.UUID(one) for one in subroutine.domain.hierarchy.path_segments(project.path)]
	)
	found = (
		{}
		if not ids
		else {one.id: one for one in session.scalars(sqlalchemy.select(model).where(model.id.in_(ids)))}
	)
	known[project_id] = [found[one] for one in ids if one in found]

	return known[project_id]


def _private (lineage: list[subroutine.db.models.project.Project]) -> bool:
	"""Say whether a project, given with every project above it, is private or beneath one."""

	return any(one.visibility == "private" for one in lineage)


def _who (session: sqlalchemy.orm.Session, event: subroutine.db.models.activity.Event) -> str:
	"""Return ``person`` or ``agent`` for whoever made an event, or nothing for the system."""

	if event.actor_user_id is None:
		return ""

	user = session.get(subroutine.db.models.identity.User, event.actor_user_id)

	if user is None:
		return ""

	return "agent" if user.is_service_account else "person"


def _sent (session: sqlalchemy.orm.Session) -> None:
	"""After a commit: hand what it composed to the sender, and return at once.

	**Nothing here may raise**, because the write has already committed: an error out of this
	hook would come out of ``commit()`` itself, and tell whoever made a write that has happened
	that it failed. A sender that cannot even start - a process out of threads, say - loses these
	messages and nothing else.
	"""

	ready: list[tuple[str, int, bytes]] | None = session.info.pop(READY, None)

	if not ready:
		return

	grouped: dict[tuple[str, int], list[bytes]] = collections.defaultdict(list)

	for host, port, datagram in ready:
		grouped[(host, port)].append(datagram)

	for (host, port), datagrams in grouped.items():
		with contextlib.suppress(Exception):
			subroutine.osc.SENDER.send(host, port, datagrams)


def _forgotten (session: sqlalchemy.orm.Session, previous_transaction: typing.Any) -> None:
	"""After a rollback: what the rolled-back writes would have said is not said."""

	session.info.pop(PENDING, None)
	session.info.pop(READY, None)


# **On the class, so every session has them** - a served instance's, a local command's and a
# test's alike - and each costs a dictionary lookup where a transaction recorded nothing.
sqlalchemy.event.listen(sqlalchemy.orm.Session, "before_commit", _composed)
sqlalchemy.event.listen(sqlalchemy.orm.Session, "after_commit", _sent)
sqlalchemy.event.listen(sqlalchemy.orm.Session, "after_soft_rollback", _forgotten)
