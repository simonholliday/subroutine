"""The connection that is this installation's own database.

Opened directly and driven through the service layer — the same services, the same
``authorize()`` and the same views the HTTP routers use, minus the HTTP. That is what
SPEC.md §13.7 means by the local database being a connection like any other: there is one
code path for ``subroutine today`` and it does not know which of its answers came over a
socket.

**A session per operation, and that is deliberate.** It matches what a request does, so the
two transports behave the same way when something is read twice — and it is what lets the
client return *view models* rather than ORM rows, which is what makes the two transports
comparable at all. A detached ORM object handed back from ``lookup`` and then written to in
``complete`` would work here and be impossible over a network.

**There is no local password prompt** (§12.1a). Anyone who can read the database file can
read every row in it with ``sqlite3`` whatever this program asks them for, so the filesystem
permission is the authentication; and §1.4 forbids making somebody setting up a to-do list
meet a token. A token in the environment still narrows, through the same
:func:`subroutine.domain.local.principal` the CLI has always used.
"""

import contextlib
import datetime
import types
import typing
import uuid

import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.clients.base
import subroutine.config
import subroutine.connections
import subroutine.credentials
import subroutine.db.models.work
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.comments
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.local
import subroutine.domain.paging
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views


class Client:
	"""This installation's own database, presented as a connection."""

	def __init__ (
		self,
		connection: subroutine.connections.Connection,
		settings: subroutine.config.Settings,
		*,
		session_factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session] | None = None,
		token: str | None = None,
	) -> None:
		"""Open the database this installation owns.

		``session_factory`` is for tests, which have an engine of their own and a transaction
		they intend to roll back. Nothing else passes it, and when it is passed the engine is
		not this object's to dispose.
		"""

		self.connection = connection
		self.settings = settings
		self._token = token
		self._engine = None if session_factory is not None else subroutine.db.session.create_engine(
			settings.database_url
		)
		self._sessions = session_factory or sqlalchemy.orm.sessionmaker(
			bind=self._engine, expire_on_commit=False
		)

	# --- The protocol ------------------------------------------------------------------

	def identity (self) -> subroutine.clients.base.Identity:
		"""Report this installation's identity and the workspaces the credential reaches."""

		with self._opened() as (session, actor):
			instance = subroutine.domain.instances.get(session)

			return subroutine.clients.base.Identity(
				instance=None if instance is None else subroutine.views.instance(instance),
				workspaces=tuple(
					subroutine.views.workspace_ref(workspace)
					for workspace in subroutine.domain.workspaces.readable(session, actor)
				),
			)

	def agenda (
		self,
		*,
		date: datetime.date | None = None,
		timezone: str | None = None,
		horizon_days: int | None = None,
		unscheduled_limit: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Agenda:
		"""Return the four buckets, across every workspace this credential reaches."""

		with self._opened() as (session, actor):
			zone = subroutine.domain.schedule.zone_for(
				user=actor.user,
				instance=subroutine.domain.instances.get(session),
				explicit=timezone,
			)

			built = subroutine.domain.agenda.build(
				session,
				principal=actor,
				workspace_ids=(
					[
						subroutine.domain.selection.workspace(
							session, actor, requested=workspace
						).id
					]
					if workspace is not None
					else [
						found.id
						for found in subroutine.domain.workspaces.readable(session, actor)
					]
				),
				now=subroutine.db.types.utcnow(),
				timezone=zone,
				date=date,
				horizon_days=horizon_days,
				unscheduled_limit=(
					subroutine.domain.agenda.DEFAULT_UNSCHEDULED_LIMIT
					if unscheduled_limit is None
					else unscheduled_limit
				),
			)

			return subroutine.views.agenda(session, built)

	def tasks (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		include_completed: bool = False,
	) -> list[subroutine.views.Task]:
		"""List one workspace's tasks, newest first."""

		model = subroutine.db.models.work.Task
		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			rows = list(
				session.scalars(
					subroutine.domain.scoping.readable_tasks(
						actor,
						workspace_ids=[chosen.id],
						include_completed=include_completed,
					)
					# The same ordering ``GET /v1/tasks`` applies by default, spelled out
					# rather than approximated: newest first, with the id breaking ties in
					# the same direction so that equal timestamps stay in one order. NULLS
					# LAST is stated because the two backends disagree about the default
					# (SPEC.md §10.3).
					.order_by(
						model.created_at.desc().nullslast(), model.id.desc().nullslast()
					)
					.limit(size)
				)
			)
			vocabulary = subroutine.views.Vocabulary.for_tasks(session, rows)

			return [subroutine.views.task(row, vocabulary) for row in rows]

	def task (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task | None:
		"""Return one task by ref, or ``None`` if there is no such task here."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			row = self._row(session, actor, chosen.id, ref)

			if row is None:
				return None

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
			)

	def documents (
		self, *, workspace: str | None = None, limit: int | None = None
	) -> list[subroutine.views.Document]:
		"""List one workspace's documents, newest first."""

		model = subroutine.db.models.work.Document
		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			rows = list(
				session.scalars(
					subroutine.domain.scoping.readable_documents(
						actor, workspace_ids=[chosen.id]
					)
					# Spelled out to match `GET /v1/documents`, including the NULLS LAST the
					# two backends disagree about (§10.3) — the same reasoning as `tasks`.
					.order_by(model.created_at.desc().nullslast(), model.id.desc().nullslast())
					.limit(size)
				)
			)
			vocabulary = subroutine.views.Vocabulary.for_documents(session, rows)

			return [subroutine.views.document(row, vocabulary) for row in rows]

	def document (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Document | None:
		"""Return one document by ref, or ``None`` if there is no such document here."""

		model = subroutine.db.models.work.Document

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			row = session.scalars(
				subroutine.domain.scoping.readable_documents(
					actor, workspace_ids=[chosen.id], include_archived=True
				).where(model.ref == ref)
			).one_or_none()

			if row is None:
				return None

			return subroutine.views.document(
				row, subroutine.views.Vocabulary.for_documents(session, [row])
			)

	def links (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Link]:
		"""Return every link touching one item, labelled from that item's point of view."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			return [
				subroutine.views.link(related)
				for related in subroutine.domain.links.around(
					session,
					actor,
					workspace_id=chosen.id,
					entity_type=entity_type,
					identifier=subject,
				)
			]

	def comments (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Comment]:
		"""Return one item's record of what happened, oldest first."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			rows = session.scalars(
				subroutine.domain.comments.listing(
					session, entity_type=entity_type, entity_id=subject, actor=actor
				)
			).all()

			return [subroutine.views.comment(row) for row in rows]

	def capture (
		self, *, text: str, workspace: str | None = None, timezone: str | None = None
	) -> subroutine.clients.base.Captured:
		"""Create a task from a line of text."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			zone = timezone or subroutine.domain.schedule.zone_for(
				user=actor.user,
				workspace=chosen,
				instance=subroutine.domain.instances.get(session),
			)

			row, captured = subroutine.domain.tasks.create_from_text(
				session,
				workspace=chosen,
				text=text,
				now=subroutine.db.types.utcnow(),
				timezone=zone,
				actor=actor,
			)

			return subroutine.clients.base.Captured(
				task=subroutine.views.task(
					row, subroutine.views.Vocabulary.for_tasks(session, [row])
				),
				unparsed=captured.unparsed,
			)

	def remark (
		self,
		*,
		ref: int,
		body: str,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Comment:
		"""Add one entry to an item's record of what happened."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			return subroutine.views.comment(
				subroutine.domain.comments.create(
					session,
					entity_type=entity_type,
					entity_id=subject,
					body=body,
					actor=actor,
				)
			)

	def complete (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Mark a task finished.

		Unconditional, exactly as ``POST /v1/tasks/{ref}/complete`` is. Whether to say
		"already done" instead of doing it again is a decision the *caller* makes from the
		task it looked up, and it has to be, because the endpoint has no way to make it
		differently and two transports that disagreed here would disagree about the one case
		this matters in — an absent-minded repeat of a command.
		"""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)

			finished = subroutine.domain.tasks.complete(
				session, row, now=subroutine.db.types.utcnow(), actor=actor
			)

			return subroutine.views.task(
				finished, subroutine.views.Vocabulary.for_tasks(session, [finished])
			)

	def schedule (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		planned_for: datetime.date | None = subroutine.clients.base.UNSET,
		start: datetime.date | None = subroutine.clients.base.UNSET,
	) -> subroutine.views.Task:
		"""Set the day a task is planned for, or the day it becomes visible."""

		self._refuse_if_read_only()

		changes: dict[str, typing.Any] = {}

		if planned_for is not subroutine.clients.base.UNSET:
			changes["planned_for"] = planned_for

		if start is not subroutine.clients.base.UNSET:
			changes["start"] = start

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)

			subroutine.domain.tasks.update(
				session, row, now=subroutine.db.types.utcnow(), actor=actor, **changes
			)

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
			)

	def close (self) -> None:
		"""Dispose the engine, if this object made one.

		The engine reference is kept rather than cleared. Nulling it made a second ``close()`` a
		no-op *and* left the sessionmaker bound to a disposed engine — which silently reopens a
		fresh pool on the next use, one that nothing then disposes. Idempotent because
		``dispose()`` is.
		"""

		if self._engine is not None:
			self._engine.dispose()

	def __enter__ (self) -> "Client":
		"""Return this client, ready to use."""

		return self

	def __exit__ (
		self,
		kind: type[BaseException] | None,
		value: BaseException | None,
		traceback: types.TracebackType | None,
	) -> None:
		"""Dispose the engine, if this object made one."""

		self.close()

	# --- Inside ------------------------------------------------------------------------

	@contextlib.contextmanager
	def _opened (
		self,
	) -> typing.Iterator[
		tuple[sqlalchemy.orm.Session, subroutine.domain.authentication.Principal]
	]:
		"""Yield a session and who is acting, for a read."""

		with self._sessions() as session, self._reported():
			yield session, self._principal(session)

	@contextlib.contextmanager
	def _writing (
		self,
	) -> typing.Iterator[
		tuple[sqlalchemy.orm.Session, subroutine.domain.authentication.Principal]
	]:
		"""Yield a session and who is acting, committing if nothing raised.

		Rolled back on any failure, including one this program raised on purpose: a refusal
		that left half a change behind would be a worse outcome than the refusal.

		**The commit is inside the guard too.** It was outside until 2026-07-30, so a database
		that refused it — a locked SQLite file past its busy timeout, a ref-allocation race
		between two processes — raised a bare SQLAlchemy error rather than a named failure, and
		went straight past the fan-out's containment.
		"""

		with self._sessions() as session, self._reported():
			try:
				yield session, self._principal(session)

			except BaseException:
				session.rollback()

				raise

			session.commit()

	@contextlib.contextmanager
	def _reported (self) -> typing.Iterator[None]:
		"""Turn a database failure into this program's own vocabulary.

		**A connection is allowed to fail; it is not allowed to escape.** ``fanout._attempt``
		catches only :class:`~subroutine.errors.SubroutineError`, deliberately — so a bare
		``OperationalError`` from *this* connection would take down the whole of
		``subroutine today``, including every remote that answered perfectly. The local database
		is a connection like any other (§13.7), and that has to include how it fails.

		Reported as ``service_unavailable`` for the same reason the HTTP client does: the
		request was fine and the thing behind it is not answering.
		"""

		try:
			yield

		except sqlalchemy.exc.SQLAlchemyError as error:
			raise subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} could not be read: "
				f"{getattr(error, 'orig', None) or error}",
				hint="Check that the database is reachable, and check 'database_url' in "
				"'subroutine config show'.",
			) from None

	def _principal (
		self, session: sqlalchemy.orm.Session
	) -> subroutine.domain.authentication.Principal:
		"""Return who this process is acting as here."""

		return subroutine.domain.local.principal(
			session, token=self._token, local_user=self.settings.local_user
		)

	def _require (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		ref: int,
		workspace: str | None,
	) -> subroutine.db.models.work.Task:
		"""Return the task this ref names here, or refuse the way the API does."""

		chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
		row = self._row(session, actor, chosen.id, ref)

		if row is None:
			raise subroutine.errors.NotFound(
				f"There is no task {subroutine.domain.refs.format_ref(ref)} in "
				f"{chosen.slug}.",
				hint="Run 'subroutine list' to see what there is.",
			)

		return row

	def _subject (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		workspace_id: typing.Any,
		entity_type: str,
		ref: int,
	) -> uuid.UUID:
		"""Turn a ref into the id of the task or document it names, or refuse.

		Links and comments hang off an *id*, because a project has no ref to be named by, while
		a command line only ever carries a ref. Resolving it here rather than in the caller
		keeps the two transports asking the same question: the HTTP client hands the ref
		straight to a route that resolves it identically, so a ref that names nothing must fail
		the same way on both sides.
		"""

		model: typing.Any = (
			subroutine.db.models.work.Task
			if entity_type == "task"
			else subroutine.db.models.work.Document
		)
		statement = (
			subroutine.domain.scoping.readable_tasks(
				actor, workspace_ids=[workspace_id], include_completed=True, include_archived=True
			)
			if entity_type == "task"
			else subroutine.domain.scoping.readable_documents(
				actor, workspace_ids=[workspace_id], include_archived=True
			)
		)

		row = session.scalars(statement.where(model.ref == ref)).one_or_none()

		if row is None:
			raise subroutine.errors.NotFound(
				f"There is no {entity_type} {subroutine.domain.refs.format_ref(ref)} here.",
				hint="Run 'subroutine list' to see what there is.",
			)

		found: uuid.UUID = row.id

		return found

	def _row (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		workspace_id: typing.Any,
		ref: int,
	) -> subroutine.db.models.work.Task | None:
		"""Fetch one task through the scoping helper, never around it.

		Completed tasks are included on purpose: running ``done 42`` twice should say the
		thing is already done, not that there is no such task.
		"""

		model = subroutine.db.models.work.Task

		return session.scalars(
			subroutine.domain.scoping.readable_tasks(
				actor, workspace_ids=[workspace_id], include_archived=True
			).where(model.ref == ref)
		).one_or_none()

	def _refuse_if_read_only (self) -> None:
		"""Refuse a write to a connection configured read-only."""

		if self.connection.read_only:
			subroutine.clients.base.refuse_a_write(self.connection)


def opened (
	connection: subroutine.connections.Connection,
	settings: subroutine.config.Settings,
	*,
	default_connection: str,
	session_factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session] | None = None,
) -> Client:
	"""Build a local client, resolving its token the same way a remote one's is resolved.

	A token is not required and usually absent. Where one *is* present it narrows exactly as
	it would over HTTP — which is what lets an agent be constrained without running a server
	(§12.1a): hand it a project-scoped token and the CLI refuses out-of-scope work at the same
	place, with the same message.
	"""

	resolved = subroutine.credentials.resolve(
		connection, default_connection=default_connection
	)

	return Client(
		connection, settings, session_factory=session_factory, token=resolved.token
	)
