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
import subroutine.db.migrate
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.capture
import subroutine.domain.claims
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.local
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.projects
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.search
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.domain.tokens
import subroutine.domain.users
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
		token_source: str | None = None,
	) -> None:
		"""Open the database this installation owns.

		``session_factory`` is for tests, which have an engine of their own and a transaction
		they intend to roll back. Nothing else passes it, and when it is passed the engine is
		not this object's to dispose.
		"""

		self.connection = connection
		self.settings = settings
		self._token = token
		# Where the credential came from, so a refusal can name the thing the operator
		# actually has to change (`#175`). Without it the message said "SUBROUTINE_TOKEN was
		# set" about a token read from credentials.toml, and told them to unset a variable
		# that was never set.
		self._token_source = token_source
		self._engine = None if session_factory is not None else subroutine.db.session.create_engine(
			settings.database_url
		)
		self._sessions = session_factory or sqlalchemy.orm.sessionmaker(
			bind=self._engine, expire_on_commit=False
		)
		self._schema_checked = False

	# --- The protocol ------------------------------------------------------------------

	def reference (self, name: str) -> str:
		"""Return one of the instance's reference documents, as text.

		**A late import, using the house style's documented exception** — the same one ``serve``
		takes. ``api`` pulls in FastAPI, measured at 0.3s of the CLI's 0.8s start, and paying
		that on every command so that a rarely-read document is available is the wrong trade.
		The nested form is ``from … import … as …`` deliberately: a nested
		``import subroutine.api.meta`` binds ``subroutine`` as a *local* name and shadows every
		other use of it in this method.

		The text belongs outside ``api`` on ``views.py``'s argument and is not there yet — it
		reaches ``UNBUILT``, ``GUIDE_TOPICS`` and ``EXAMPLES``, the last guarded by its own test
		file, so moving it is its own piece of work rather than a detail of `#483`.
		"""

		from subroutine.api import meta as documents

		if name == "agent":
			return documents.guide_text()

		if name == "examples":
			return documents.examples_text()

		raise subroutine.errors.NotFound(f"There is no reference document called {name!r}.")

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

	def me (self) -> subroutine.views.Me:
		"""Report who this installation thinks the caller is, and what they may do (`#336`).

		``credential`` comes back null unless a token was presented, which locally is the
		ordinary case: §12.1a says the filesystem permission is the authentication here. A
		token in the environment still narrows, through the same principal every other read
		goes through, so a scoped agent gets a scoped answer whichever transport it uses.
		"""

		with self._opened() as (session, actor):
			return subroutine.views.me(session, actor)

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

	def count_tasks (
		self, *, workspace: str | None = None, project: str | None = None
	) -> int:
		"""Return how many tasks a project holds, completed ones included (`#296`)."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			narrowed = (
				None
				if project is None
				else subroutine.domain.selection.project(session, actor, chosen, project)
			)

			statement = subroutine.domain.scoping.readable_tasks(
				actor, workspace_ids=[chosen.id], include_completed=True
			)

			if narrowed is not None:
				statement = statement.where(
					subroutine.domain.scoping.within_project(narrowed)
				)

			# Counted over the narrowed statement as a subquery rather than by loading rows,
			# which is the whole reason this is not `len(tasks(...))`.
			return int(
				session.scalar(
					sqlalchemy.select(sqlalchemy.func.count()).select_from(
						statement.subquery()
					)
				)
				or 0
			)

	def tasks (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		include_completed: bool = False,
		order: str | None = None,
		project: str | None = None,
		deferred: str = subroutine.domain.readiness.DEFAULT_DEFERRAL,
		q: str | None = None,
		parent: int | None = None,
		ready: bool = False,
		deleted: bool = False,
	) -> list[subroutine.views.Task]:
		"""List one workspace's tasks, newest first unless ``order`` says otherwise."""

		model = subroutine.db.models.work.Task
		size = subroutine.domain.paging.size(limit, self.settings)
		choice = subroutine.domain.readiness.refuse_unknown_deferral(deferred)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			# Resolved through the domain, so an unknown key is refused here exactly as
			# `GET /v1/tasks?project=` refuses it — and a private project somebody is not a
			# member of is not found rather than forbidden.
			narrowed = (
				None
				if project is None
				else subroutine.domain.selection.project(session, actor, chosen, project)
			)

			statement = subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=[chosen.id],
				include_completed=include_completed,
				include_deleted=deleted,
			)

			# Narrowed to what was widened for: `include_deleted` widens, this asks only for
			# the trash. A mixed list is the one place nothing in a row says which it is.
			if deleted:
				statement = statement.where(model.deleted_at.is_not(None))

			if narrowed is not None:
				statement = statement.where(subroutine.domain.scoping.within_project(narrowed))

			# **Built in steps rather than one chained expression, and `is not None` rather
			# than `or`.** A SQLAlchemy element raises on truth-testing, so `predicate or
			# true()` is not a shorter spelling of this — it is a `TypeError` at the one
			# moment a caller asked for narrowing.
			parked = subroutine.domain.readiness.deferred(
				model, now=subroutine.db.types.utcnow(), choice=choice
			)

			if parked is not None:
				statement = statement.where(parked)

			if q:
				statement = statement.where(
					subroutine.domain.search.matching(q, model.title, model.description)
				)

			# **After the deferral filter, which it subsumes**, and in the same order the
			# endpoint applies them: `ready` already excludes anything parked, so combining the
			# two narrows rather than contradicts. One predicate, shared with `GET /v1/tasks`,
			# because a readiness that meant something different here would be worse than none.
			if ready:
				statement = statement.where(
					subroutine.domain.readiness.ready(
						model, now=subroutine.db.types.utcnow(), by=actor.user.id
					)
				)

			if parent is not None:
				# Resolved through the same scoped statement the listing uses, so a parent
				# this caller cannot see is absent rather than forbidden — and the children
				# of something invisible are not disclosed by an empty list either.
				above = session.scalars(
					subroutine.domain.scoping.readable_tasks(
						actor, workspace_ids=[chosen.id], include_completed=True
					).where(model.ref == parent)
				).first()

				if above is None:
					raise subroutine.errors.NotFound(
						f"There is no #{parent} here.",
						hint="Run 'subroutine list' to see what there is.",
					)

				statement = statement.where(model.parent_task_id == above.id)

			rows = list(
				session.scalars(
					# Built by the domain from the same vocabulary ``GET /v1/tasks`` uses,
					# rather than approximated here: two spellings of "newest first" is the
					# pair that comes to disagree, and this one used to be the *only* one,
					# which is why a client could not rank at all. NULLS LAST and the
					# tiebreaker are `ordering.clauses`' job now (SPEC.md §10.3).
					statement.order_by(
						*subroutine.domain.ordering.clauses(
							order,
							allowed=subroutine.domain.ordering.TASK_FIELDS,
							default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
							tiebreak=model.id,
						)
					).limit(size)
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
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		order: str | None = None,
		project: str | None = None,
		q: str | None = None,
		deleted: bool = False,
	) -> list[subroutine.views.Document]:
		"""List one workspace's documents, newest first unless ``order`` says otherwise."""

		model = subroutine.db.models.work.Document
		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			narrowed = (
				None
				if project is None
				else subroutine.domain.selection.project(session, actor, chosen, project)
			)

			rows = list(
				session.scalars(
					subroutine.domain.scoping.readable_documents(
						actor, workspace_ids=[chosen.id], include_deleted=deleted
					)
					.where(
						sqlalchemy.true() if not deleted else model.deleted_at.is_not(None)
					)
					.where(
						sqlalchemy.true()
						if narrowed is None
						else subroutine.domain.scoping.within_project(narrowed)
					)
					.where(
						sqlalchemy.true()
						if not q
						else subroutine.domain.search.matching(q, model.title, model.body)
					)
					# Built by the domain from the vocabulary `GET /v1/documents` uses,
					# rather than spelled out here — the ordering, its NULLS LAST (§10.3) and
					# its tiebreaker are one rule and used to be two copies of it.
					.order_by(
						*subroutine.domain.ordering.clauses(
							order,
							allowed=subroutine.domain.ordering.DOCUMENT_FIELDS,
							default=subroutine.domain.ordering.DEFAULT_DOCUMENT_ORDER,
							tiebreak=model.id,
						)
					)
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

			# Deleted documents resolve, exactly as `api/documents._resolve` has always let
			# them and as `_row` does for a task: a reference to something in the trash is
			# more useful than a dangling one, and `restore` cannot reach what it cannot find.
			row = session.scalars(
				subroutine.domain.scoping.readable_documents(
					actor,
					workspace_ids=[chosen.id],
					include_archived=True,
					include_deleted=True,
				).where(model.ref == ref)
			).one_or_none()

			if row is None:
				return None

			return subroutine.views.document(
				row, subroutine.views.Vocabulary.for_documents(session, [row])
			)

	def update_document (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		title: str = subroutine.clients.base.UNSET,
		body: str | None = subroutine.clients.base.UNSET,
		type: str = subroutine.clients.base.UNSET,
		status: str = subroutine.clients.base.UNSET,
		project: str = subroutine.clients.base.UNSET,
	) -> subroutine.views.Document:
		"""Revise a document, through the same service the endpoint calls."""

		self._refuse_if_read_only()

		# Compared against UNSET rather than filtered for falsey values, because `None` is
		# meaningful — it is how §8.3 says "clear this" — and `body=None` on a document is a
		# thing somebody genuinely does.
		given: dict[str, typing.Any] = {
			"title": title,
			"body": body,
			"status_key": status,
			"type_key": type,
		}
		changes: dict[str, typing.Any] = {
			name: value
			for name, value in given.items()
			if value is not subroutine.clients.base.UNSET
		}

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			row = session.get(
				subroutine.db.models.work.Document,
				self._subject(session, actor, chosen.id, "document", ref),
			)

			# `_subject` refuses a ref that names nothing, so this cannot be None — and
			# asserting it is cheaper than a second refusal that could word it differently.
			assert row is not None

			if project is not subroutine.clients.base.UNSET:
				# Resolved here for the reason `update` gives: the service takes a row and a
				# command line carries a key, and handing the key straight through raises
				# `AttributeError` on `.id` rather than refusing by name.
				changes["project"] = subroutine.domain.selection.project(
					session, actor, chosen, project
				)

			revised = subroutine.domain.documents.update(session, row, actor=actor, **changes)

			return subroutine.views.document(
				revised, subroutine.views.Vocabulary.for_documents(session, [revised])
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

	def link (
		self,
		*,
		ref: int,
		link_type: str,
		target: int,
		entity_type: str = "task",
		target_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Link:
		"""Join two items."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			near = self._end(session, actor, chosen, entity_type, ref)
			far = self._end(session, actor, chosen, target_type, target)

			created = subroutine.domain.links.create(
				session,
				workspace_id=chosen.id,
				source=near,
				target=far,
				link_type_key=link_type,
				actor=actor,
			)

			# **Read back through `around`, exactly as the endpoint does.** Which end is "the
			# other one" and which way the label reads are the domain's job, and `views.link`
			# takes a `Related` rather than the stored row for that reason. Rendering the row
			# here would be a second answer to a question already answered.
			for related in subroutine.domain.links.around(
				session, actor, workspace_id=chosen.id, entity_type=entity_type, identifier=near.id
			):
				if related.id == created.id:
					return subroutine.views.link(related)

			raise subroutine.errors.InternalError(
				"The link was created but cannot be read back."
			)

	def unlink (
		self, *, ref: int, link_id: str, entity_type: str = "task", workspace: str | None = None
	) -> None:
		"""Withdraw a link."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			model = subroutine.db.models.work.Link
			found = session.scalars(
				sqlalchemy.select(model).where(
					model.id == uuid.UUID(link_id),
					model.workspace_id == chosen.id,
					model.deleted_at.is_(None),
					# **Both ends, because a link is withdrawn from either side.** Narrowed to
					# the item named rather than to any link with that id, so a caller cannot
					# withdraw a link between two things it never mentioned.
					sqlalchemy.or_(
						sqlalchemy.and_(
							model.source_type == entity_type, model.source_id == subject
						),
						sqlalchemy.and_(
							model.target_type == entity_type, model.target_id == subject
						),
					),
				)
			).first()

			if found is None:
				raise subroutine.errors.NotFound(
					"There is no such link on that item.",
					hint="Run 'subroutine show <ref>' to see what it is joined to.",
				)

			subroutine.domain.links.remove(session, found, actor=actor)

	def _end (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		workspace: typing.Any,
		entity_type: str,
		ref: int,
	) -> subroutine.domain.links.End:
		"""Describe one side of a link, resolving the ref the way the endpoint does."""

		row = self._in_the_trash_too(session, actor, ref, workspace.slug, entity_type)

		return subroutine.domain.links.End(
			entity_type=entity_type,
			id=row.id,
			ref=row.ref,
			title=row.title,
			project_id=row.project_id,
		)

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

	def history (
		self,
		*,
		ref: int,
		entity_type: str = "task",
		workspace: str | None = None,
		limit: int | None = None,
	) -> list[subroutine.views.Event]:
		"""Return what has happened to one item, newest first.

		**No upper bound**, which is the one thing this shares with the route rather than with
		the feed: `seq` becomes visible at commit, so a watermark would mean commenting on an
		item and immediately reading its history shows nothing (§5.11a).
		"""

		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			# Resolving the subject **is** the permission check, exactly as it is on the route:
			# it goes through the entity's own narrowed statement, so one the caller may not
			# see is absent rather than forbidden, and everything hanging off it is then safe.
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			statement = subroutine.domain.events.selected(
				workspace_ids=[chosen.id], entity_type=entity_type, entity_id=subject
			)
			rows = session.scalars(
				statement.order_by(
					subroutine.db.models.activity.Event.seq.desc()
				).limit(size)
			).all()
			described = subroutine.domain.events.descriptions(session, rows)

			return [subroutine.views.event(row, described) for row in rows]

	def changes (
		self,
		*,
		since: int | None = None,
		mine: bool = False,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> list[subroutine.views.Event]:
		"""Return what has changed, oldest first, across everything this credential can see.

		**The watermark, the scoping, both cursor refusals and "``since`` overrules ``newest``"
		all come from :mod:`subroutine.domain.events`** rather than being restated here. That is
		the whole reason they were moved out of the route: a feed that withheld the last second
		over HTTP and not locally would lose events on one transport only, and the transport is
		the last place anybody would look for a missing change.

		Two of those arrived late and by the route this predicts (`#309`, `#310`) — the ``since``
		floor was checked in the endpoint alone, so an uninitialised cursor met a ``422`` there
		and a ``410`` here claiming events had been pruned.

		Spans every readable workspace unless one is named, which is what makes "what did I
		miss" answerable in one call by somebody working across two.
		"""

		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			if workspace is None:
				chosen = subroutine.domain.workspaces.readable(session, actor)

			else:
				chosen = [
					subroutine.domain.selection.workspace(session, actor, requested=workspace)
				]

			workspace_ids = [each.id for each in chosen]

			subroutine.domain.events.refuse_unusable_cursor(
				session, since=since, workspace_ids=workspace_ids
			)

			rows, _more = subroutine.domain.events.page(
				session,
				actor,
				workspace_ids=workspace_ids,
				size=size,
				since=since,
				mine=mine,
				newest=newest,
			)
			described = subroutine.domain.events.descriptions(session, rows)

			return [subroutine.views.event(row, described) for row in rows]

	def projects (
		self, *, workspace: str | None = None, limit: int | None = None
	) -> list[subroutine.views.Project]:
		"""List the projects this credential can see, parents before children."""

		size = subroutine.domain.paging.size(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			# `readable_projects` and not a hand-written query: it applies the workspace scope,
			# the privacy inheritance of §7.3a and the token's own `project_scope` together,
			# and narrowing by hand is what left `subroutine ls` listing private projects to
			# non-members in shipped code.
			rows = list(
				session.scalars(
					subroutine.domain.scoping.readable_projects(
						actor, workspace_ids=[chosen.id]
					)
					.order_by(subroutine.db.models.project.Project.path)
					.limit(size)
				)
			)

			vocabulary = subroutine.views.Vocabulary.for_projects(session, rows)

			return [subroutine.views.project(row, vocabulary) for row in rows]

	def create_project (
		self,
		*,
		key: str,
		title: str,
		description: str | None = None,
		parent: str | None = None,
		visibility: str = "public",
		workspace: str | None = None,
	) -> subroutine.views.Project:
		"""Create a project."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			above = (
				None
				if parent is None
				else subroutine.domain.selection.project(session, actor, chosen, parent)
			)

			created = subroutine.domain.projects.create(
				session,
				workspace_id=chosen.id,
				key=key,
				title=title,
				description=description,
				parent=above,
				visibility=visibility,
				# **The creator owns what they create**, which is also what makes a private
				# project visible to them: §7.3a grants sight only to holders of a
				# `project_member` row, and `projects.create` writes one for the owner.
				# Omitting this is how private projects came to be invisible to the people
				# who made them.
				owner_id=actor.user.id,
				actor=actor,
			)

			return subroutine.views.project(
				created, subroutine.views.Vocabulary.for_projects(session, [created])
			)

	def tokens (self) -> list[subroutine.views.Token]:
		"""List the credentials this caller may act on, newest first (`#348`)."""

		with self._opened() as (session, actor):
			found = subroutine.domain.tokens.issued_tokens(session, actor=actor)
			owners = subroutine.domain.tokens.owners(session, found)

			return [
				subroutine.views.token(
					row, owner=owners.get(row.user_id), session=session, principal=actor
				)
				for row in found
			]

	def issue_token (
		self,
		*,
		title: str | None = None,
		username: str | None = None,
		service_account: str | None = None,
		workspace: str | None = None,
		scopes: typing.Sequence[str] = (),
		projects: typing.Sequence[str] | None = None,
		writes: typing.Sequence[str] | None = None,
		expires: str | None = None,
	) -> subroutine.views.IssuedToken:
		"""Mint a credential and return it once, secret included (`#348`)."""

		with self._writing() as (session, actor):
			row, owner, issued, created = subroutine.domain.tokens.issue(
				session,
				actor=actor,
				title=title,
				username=username,
				service_account=service_account,
				workspace=workspace,
				scopes=scopes,
				projects=projects,
				writes=writes,
				expires=expires,
			)
			rendered = subroutine.views.token(
				row,
				owner=owner,
				secret=issued.value.get_secret_value(),
				account_created=created,
				session=session,
				principal=actor,
			)

		# The type the protocol promises. `views.token` answers with the base type when no
		# secret was asked for, and a cast here would be a claim rather than a check.
		assert isinstance(rendered, subroutine.views.IssuedToken)

		return rendered

	def revoke_token (self, *, id_or_prefix: str) -> subroutine.views.Token:
		"""Stop a credential working, now (`#348`)."""

		with self._writing() as (session, actor):
			found = subroutine.domain.tokens.mine(session, actor, id_or_prefix)
			stopped = subroutine.domain.tokens.revoke(session, found, actor=actor)
			owner = session.get(subroutine.db.models.identity.User, stopped.user_id)

			return subroutine.views.token(
				stopped, owner=owner, session=session, principal=actor
			)

	def users (self) -> list[subroutine.views.User]:
		"""List the accounts on this instance."""

		with self._opened() as (session, actor):
			return [
				subroutine.views.user(row)
				for row in subroutine.domain.users.listed(session, actor=actor)
			]

	def create_user (
		self,
		*,
		username: str,
		display_name: str | None = None,
		email: str | None = None,
		timezone: str | None = None,
		is_service_account: bool = False,
	) -> subroutine.views.User:
		"""Add a person, or a machine identity, to this instance."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			created = subroutine.domain.users.create(
				session,
				username=username,
				display_name=display_name,
				email=email,
				timezone=timezone,
				is_service_account=is_service_account,
				actor=actor,
			)

			return subroutine.views.user(created)

	def members (self, *, workspace: str | None = None) -> list[subroutine.views.Member]:
		"""List who belongs to one workspace."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)

			return [
				subroutine.views.member(row, account=account, role=role, within=chosen)
				for row, account, role in subroutine.domain.workspaces.members(
					session, chosen, actor=actor
				)
			]

	def add_member (
		self, *, username: str, role: str, workspace: str | None = None
	) -> subroutine.views.Member:
		"""Give somebody a role in a workspace."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			account = subroutine.domain.users.by_username(session, username)
			membership = subroutine.domain.workspaces.add_member(
				session, chosen, account, role_key=role, actor=actor
			)
			held = subroutine.domain.workspaces.find_role(session, chosen.id, role)

			return subroutine.views.member(
				membership, account=account, role=held, within=chosen
			)

	def set_active (self, *, username: str, active: bool) -> subroutine.views.User:
		"""Mark somebody as having left, or bring them back."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			account = subroutine.domain.users.by_username(session, username)

			subroutine.domain.users.set_active(
				session, account, active=active, actor=actor
			)

			return subroutine.views.user(account)

	def transfer_agent (self, *, username: str, to: str) -> subroutine.views.User:
		"""Hand an agent to somebody else, who becomes answerable for it."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			agent = subroutine.domain.users.by_username(session, username)

			subroutine.domain.users.transfer(
				session,
				agent,
				to=subroutine.domain.users.by_username(session, to),
				actor=actor,
			)

			return subroutine.views.user(agent)

	def remove_member (self, *, username: str, workspace: str | None = None) -> None:
		"""Take somebody out of a workspace."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			account = subroutine.domain.users.by_username(session, username)

			subroutine.domain.workspaces.remove_member(
				session, chosen, account, actor=actor
			)

	def rename_project (
		self, project: str, *, key: str, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Give a project a different short name."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			found = subroutine.domain.selection.project(session, actor, chosen, project)
			renamed = subroutine.domain.projects.update(
				session, found, key=key, actor=actor
			)

			return subroutine.views.project(
				renamed, subroutine.views.Vocabulary.for_projects(session, [renamed])
			)

	def create_workspace (
		self, *, slug: str, title: str, timezone: str | None = None
	) -> subroutine.views.Workspace:
		"""Make another workspace, through the same service the endpoint calls."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			created = subroutine.domain.workspaces.create(
				session,
				slug=slug,
				title=title,
				# The creator owns what they create, which is what makes them able to
				# administer it — a workspace with no owner is not a state worth reaching.
				owner=actor.user,
				timezone=timezone or "UTC",
				actor=actor,
			)

			return subroutine.views.workspace(created)

	def rename_workspace (self, workspace: str, *, slug: str) -> subroutine.views.Workspace:
		"""Give a workspace a different short name."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			renamed = subroutine.domain.workspaces.update(
				session, chosen, slug=slug, actor=actor
			)

			return subroutine.views.workspace(renamed)

	def move_project (
		self, project: str, *, parent: str | None, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Reparent a project, taking everything under it."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			found = subroutine.domain.selection.project(session, actor, chosen, project)
			# Resolved through the same function as the project being moved, so an unknown
			# parent is refused here exactly as the endpoint refuses it, and a private one
			# somebody is not a member of is absent rather than forbidden.
			under = (
				None
				if parent is None
				else subroutine.domain.selection.project(session, actor, chosen, parent)
			)

			subroutine.domain.projects.move(session, found, parent=under, actor=actor)

			return subroutine.views.project(
				found, subroutine.views.Vocabulary.for_projects(session, [found])
			)

	def create_document (
		self,
		*,
		title: str,
		body: str | None = None,
		type: str | None = None,
		project: str | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Document:
		"""Write a document."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			created = subroutine.domain.documents.create(
				session,
				project=subroutine.domain.selection.project(session, actor, chosen, project),
				title=title,
				body=body,
				type_key=type or "note",
				# The writer owns what they write, as `projects.create` does — and for a
				# document it is the attribution that makes §5.10's "what you concluded" mean
				# anything, since a conclusion with no author is a rumour.
				owner_id=actor.user.id,
				actor=actor,
			)

			return subroutine.views.document(
				created, subroutine.views.Vocabulary.for_documents(session, [created])
			)

	def capture (
		self,
		*,
		text: str,
		workspace: str | None = None,
		timezone: str | None = None,
		type: str | None = None,
		project: str | None = None,
		description: str | None = None,
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
				# **Only when given.** `create_from_text` merges overrides over the parsed
				# fields and `create` defaults `type_key` to "task", so passing `None` through
				# would override the default with nothing rather than leave it alone. Typed
				# `Any` because the signature ends in `**overrides`, which mypy cannot match
				# against a mapping it has not seen the keys of.
				**typing.cast(dict[str, typing.Any], {} if type is None else {"type_key": type}),
				# **Left out entirely when nobody said one** (`#424`), for the reason above:
				# `fields.update(overrides)` is what decides §6.13's "structured wins over
				# parsed", so a null passed through would be a caller overriding with nothing.
				# The grammar has no sigil for a description and is not getting one — this is
				# reasoning about the sentence rather than part of it.
				**typing.cast(
					dict[str, typing.Any],
					{} if description is None else {"description": description},
				),
				# **A named parameter and not an override**, because `create_from_text` derives
				# a project of its own and an override of that name collides with the argument
				# — which is a `TypeError` rather than anything useful, and is exactly what the
				# first version of this did. Resolved here, the same way the endpoint resolves
				# it, so both transports refuse an unknown key identically.
				#
				# **Only when the line did not say**: a `+KEY` in the text is somebody being
				# explicit about this item and must beat a default from a file three
				# directories up that they may not know is there.
				project=(
					subroutine.domain.selection.project(session, actor, chosen, project)
					if project is not None
					and not subroutine.domain.capture.names_a_project(text)
					else None
				),
				now=subroutine.db.types.utcnow(),
				timezone=zone,
				actor=actor,
			)

			return subroutine.clients.base.Captured(
				task=subroutine.views.task(
					row, subroutine.views.Vocabulary.for_tasks(session, [row])
				),
				unparsed=captured.unparsed,
				summary=subroutine.domain.capture.summarise(captured),
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

	def uncomment (
		self,
		*,
		ref: int,
		comment_id: str,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> None:
		"""Withdraw a comment from an item's record."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			model = subroutine.db.models.activity.Comment
			found = session.scalars(
				sqlalchemy.select(model).where(
					model.id == uuid.UUID(comment_id),
					model.workspace_id == chosen.id,
					model.deleted_at.is_(None),
					# **Narrowed to the item named, like `unlink`.** A comment id alone would
					# let a caller withdraw one from something it never mentioned — and the
					# ids are handed out by a listing, so "the one I just read" is the only
					# provenance a caller has for them.
					model.entity_type == entity_type,
					model.entity_id == subject,
				)
			).first()

			if found is None:
				raise subroutine.errors.NotFound(
					"There is no such comment on that item.",
					hint=f"Run 'subroutine show {ref}' to see what is recorded against it.",
				)

			subroutine.domain.comments.delete(session, found, actor=actor)

	def discard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Move an item to the trash."""

		self._refuse_if_read_only()

		return self._moved(ref, entity_type, workspace, into_the_trash=True)

	def undiscard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Take an item back out of the trash."""

		self._refuse_if_read_only()

		return self._moved(ref, entity_type, workspace, into_the_trash=False)

	def _moved (
		self, ref: int, entity_type: str, workspace: str | None, *, into_the_trash: bool
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Move one item into or out of the trash. One body, because they differ in one word."""

		with self._writing() as (session, actor):
			row = self._in_the_trash_too(session, actor, ref, workspace, entity_type)

			if entity_type == "document":
				service = (
					subroutine.domain.documents.delete
					if into_the_trash
					else subroutine.domain.documents.restore
				)

				return subroutine.views.document(
					service(session, row, actor=actor),
					subroutine.views.Vocabulary.for_documents(session, [row]),
				)

			acted = (
				subroutine.domain.tasks.delete
				if into_the_trash
				else subroutine.domain.tasks.restore
			)

			return subroutine.views.task(
				acted(session, row, actor=actor),
				subroutine.views.Vocabulary.for_tasks(session, [row]),
			)

	def claim (
		self, *, ref: int, minutes: int | None = None, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Take a lease on a task, or renew one this credential holds (`#350`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)
			held = subroutine.domain.claims.claim(
				session, row, minutes=minutes, settings=self.settings, actor=actor
			)

			return subroutine.views.task(
				held, subroutine.views.Vocabulary.for_tasks(session, [held])
			)

	def release (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Give a task back, so somebody else can take it (`#350`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)
			freed = subroutine.domain.claims.release(session, row, actor=actor)

			return subroutine.views.task(
				freed, subroutine.views.Vocabulary.for_tasks(session, [freed])
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

	def update (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		title: str = subroutine.clients.base.UNSET,
		description: str | None = subroutine.clients.base.UNSET,
		status: str = subroutine.clients.base.UNSET,
		type: str = subroutine.clients.base.UNSET,
		importance: int | None = subroutine.clients.base.UNSET,
		urgency: int | None = subroutine.clients.base.UNSET,
		estimate: int | str | None = subroutine.clients.base.UNSET,
		project: str = subroutine.clients.base.UNSET,
	) -> subroutine.views.Task:
		"""Change a task's own fields, through the same service the API calls."""

		self._refuse_if_read_only()

		# `status` is `status_key` in the service, and the rest are spelled the same. Built by
		# comparison against UNSET rather than by filtering falsey values, because `None` is a
		# meaningful value here — it is how §8.3 says "clear this".
		given: dict[str, typing.Any] = {
			"title": title,
			"description": description,
			"status_key": status,
			"type_key": type,
			"importance": importance,
			"urgency": urgency,
			"estimate": estimate,
		}
		changes: dict[str, typing.Any] = {
			name: value
			for name, value in given.items()
			if value is not subroutine.clients.base.UNSET
		}

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)

			# **Resolved here, because the service takes a row and the caller has a key.**
			# The endpoint does the same with `selection.project`. This is the second time
			# today a key was handed straight through and raised `AttributeError` on `.id`;
			# the first was `capture` (`#159`). The shape to watch: every service argument
			# naming another entity is a *row*, so the route and this client each have to
			# resolve it, and this client is where it gets forgotten.
			if project is not subroutine.clients.base.UNSET:
				changes["project"] = subroutine.domain.selection.project(
					session,
					actor,
					subroutine.domain.selection.workspace(session, actor, requested=workspace),
					project,
				)

			subroutine.domain.tasks.update(
				session, row, now=subroutine.db.types.utcnow(), actor=actor, **changes
			)

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
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
			self._require_a_schema_this_build_understands(session)

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
			self._require_a_schema_this_build_understands(session)

			try:
				yield session, self._principal(session)

			except BaseException:
				session.rollback()

				raise

			session.commit()

	def _require_a_schema_this_build_understands (self, session: sqlalchemy.orm.Session) -> None:
		"""Refuse to read a database whose shape this build does not match (SPEC.md §12.4a).

		**The gap decision `#97` names.** ``/readyz`` has always made this comparison and
		refuses to serve on a mismatch, naming the remedy; the CLI made it nowhere. Running any
		command against a database one migration behind gave ``no such column:
		workspace.next_ref_number`` — a sentence about our internals, arriving at the one moment
		somebody has least patience for one. Whether the *last* release needed a migration is
		not something a person is expected to remember, so the program has to say.

		**Which direction it goes decides the remedy**, which is why this is not one message.
		Behind is migrable and says so; ahead was written by a later release and cannot be
		reached from here, so the answer is to update the software. An empty database is neither
		and gets ``init``.

		Checked once per client, on the first session rather than in ``__init__``: constructing
		a connection must not touch the database, or a broken local instance would refuse the
		fan-out before a perfectly good remote one had been asked.

		Here rather than in the CLI because this is the only thing that opens the local database
		for a task or a project (§13.7), and the administrative commands must keep working when
		it fails — ``db backup``, ``db restore`` and ``upgrade`` itself are what somebody reaches
		for once this fires, and a check that blocked them would be a lock with the key inside.
		"""

		if self._schema_checked:
			return

		self._schema_checked = True

		current = subroutine.db.migrate.revision_on(session.connection())
		expected = subroutine.db.migrate.head_revision()
		mismatch = subroutine.db.migrate.mismatch_reason(current, expected)

		if mismatch is None:
			return

		detail, hint = mismatch

		raise subroutine.errors.SchemaMismatch(detail, hint=hint)

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

		**An instance that was never created is told apart from one that cannot be reached**
		(`#165`). Those have opposite remedies and the generic message named the wrong one: an
		agent meeting a freshly installed plugin got "unable to open database file" and advice
		to check ``database_url``, when the answer is ``subroutine init``. It is very likely the
		first thing anybody sees, and it dead-ended.

		The check has to be here rather than one layer in. ``_require_a_schema_this_build_
		understands`` already says "run init" for a database with no schema — and it never runs
		on a database that does not open, which is what a new installation has.
		"""

		try:
			yield

		except sqlalchemy.exc.SQLAlchemyError as error:
			if self.settings.has_no_instance_yet():
				# The connection's label is already printed in front of this, so naming it again
				# would read "Local: local has no…". The generic message below does exactly
				# that and is left alone: it carries the driver's own words, which are worth
				# more than the tidiness.
				raise subroutine.errors.ServiceUnavailable(
					"no Subroutine instance has been set up here yet.",
					hint="Run 'subroutine init' to create one. It takes no arguments.",
				) from None

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
			session,
			token=self._token,
			local_user=self.settings.local_user,
			token_source=self._token_source,
		)

	def _require (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		ref: int,
		workspace: str | None,
	) -> subroutine.db.models.work.Task:
		"""Return the task this ref names here, or refuse the way the API does.

		**"The way the API does" is a claim, and it was false for a day** (`#488`). The API
		learned to say *"#480 is a document, not a task"* and this did not, so the same request
		got a different answer depending on whether the connection was local — and the local one
		is what a standalone SQLite install uses, which is the zero-configuration machine an
		agent meets first. A docstring asserting a correspondence is not one.
		"""

		chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
		row = self._row(session, actor, chosen.id, ref)

		if row is None:
			instead = subroutine.domain.scoping.the_other_kind(
				session, actor, workspace_id=chosen.id, ref=ref, asked_for="task"
			)

			if instead is not None:
				raise subroutine.errors.NotFound(
					f"{subroutine.domain.refs.format_ref(instead.ref)} is a document, not a "
					f"task — {instead.title}",
					hint="Revise it with 'subroutine doc edit "
					f"{instead.ref}', or read it with 'subroutine show {instead.ref}'.",
				)

			raise subroutine.errors.NotFound(
				f"There is no task {subroutine.domain.refs.format_ref(ref)} in "
				f"{chosen.slug}.",
				hint="Run 'subroutine list' to see what there is.",
			)

		return row

	def _in_the_trash_too (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		ref: int,
		workspace: str | None,
		entity_type: str,
	) -> typing.Any:
		"""Return the task or document this ref names, **including one already in the trash**.

		`_row` deliberately excludes deleted tasks, which is right for every other caller and
		exactly wrong for the two that exist to move an item in and out of it: the one row
		`undiscard` is for is the one `_row` cannot see. The HTTP side has always resolved
		through a statement that includes the trash — "a reference to something in the trash is
		more useful than a dangling one" — so this is the two transports agreeing rather than a
		local liberty.
		"""

		chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
		documents = entity_type == "document"
		model: typing.Any = (
			subroutine.db.models.work.Document if documents else subroutine.db.models.work.Task
		)
		statement = (
			subroutine.domain.scoping.readable_documents(
				actor, workspace_ids=[chosen.id], include_deleted=True, include_archived=True
			)
			if documents
			else subroutine.domain.scoping.readable_tasks(
				actor, workspace_ids=[chosen.id], include_deleted=True, include_archived=True
			)
		)
		row = session.scalars(statement.where(model.ref == ref)).one_or_none()

		if row is None:
			raise subroutine.errors.NotFound(
				f"There is no {entity_type} {subroutine.domain.refs.format_ref(ref)} in "
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

		**And deleted ones, which was a live divergence until `#140`.** ``api/tasks._resolve``
		has always included them — "a reference to something in the trash is more useful than a
		dangling one" — and this did not, so ``client.task(ref=…)`` answered the same question
		with the task over HTTP and ``None`` locally. Nothing noticed, because nothing had ever
		looked one up after deleting it: there was no way to delete one.

		Found by building ``restore`` and watching it fail to find the item it exists for.
		"""

		model = subroutine.db.models.work.Task

		return session.scalars(
			subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=[workspace_id],
				include_archived=True,
				include_deleted=True,
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
		connection,
		settings,
		session_factory=session_factory,
		token=resolved.token,
		token_source=resolved.source,
	)
