"""The connection that is this installation's own database.

Opened directly and driven through the service layer — the same services, the same
``authorize()`` and the same views the HTTP routers use, minus the HTTP. That is what
docs/design.md §13.7 means by the local database being a connection like any other: there is one
code path for ``subroutine agenda`` and it does not know which of its answers came over a
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
import urllib.parse
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
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.calendars
import subroutine.domain.capture
import subroutine.domain.claims
import subroutine.domain.comments
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.filtering
import subroutine.domain.hierarchy
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.local
import subroutine.domain.mentions
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.projects
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.search
import subroutine.domain.selection
import subroutine.domain.sessions
import subroutine.domain.tasks
import subroutine.domain.tokens
import subroutine.domain.users
import subroutine.domain.verifications
import subroutine.domain.versions
import subroutine.domain.vocabulary
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views


def _asked (**values: typing.Any) -> dict[str, typing.Any]:
	"""Drop what the caller did not name, so an omitted field is left alone.

	The local mirror of ``clients/http._given``, and with the same caveat: ``None`` means *not
	asked for* here, never *clear it*. Over HTTP that distinction is ``model_fields_set``; on
	this side a caller wanting to clear a field calls the service directly.
	"""

	return {name: value for name, value in values.items() if value is not None}


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
		principal: typing.Callable[
			[sqlalchemy.orm.Session], subroutine.domain.authentication.Principal
		]
		| None = None,
	) -> None:
		"""Open the database this installation owns.

		``session_factory`` is for tests, which have an engine of their own and a transaction
		they intend to roll back, and for the MCP endpoint, which hands over the application's
		own factory so no second engine is opened. When it is passed the engine is not this
		object's to dispose.

		``principal`` says who this client acts as, *instead of* resolving it from the
		environment — the served MCP endpoint (`#516`), which has already authenticated a
		bearer token and must not repeat §12.1a's local reasoning about it.

		**A callable rather than a ready-made principal, and that is not a style choice.** A
		:class:`Principal` carries ORM objects, so one built in the caller's session is
		detached the moment anything on it lazy-loads in ours. ``api/inprocess.acting_as``
		takes the same shape for the same reason and its docstring records what the other
		arrangement cost: a ``PATCH`` that answered ``200`` with the new title while the write
		was silently discarded.
		"""

		self.connection = connection
		self.settings = settings
		self._token = token
		self._resolve_principal = principal
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

	def call_api (
		self,
		*,
		method: str,
		path: str,
		body: typing.Any | None = None,
		query: dict[str, str] | None = None,
	) -> subroutine.clients.base.Answered:
		"""Make one request against a route this credential already allows — `#485`."""

		verb = subroutine.clients.base.require_a_method(method)

		if verb not in subroutine.clients.base.READING_VERBS:
			self._refuse_if_read_only()

		from subroutine.api import app as building
		from subroutine.api import inprocess

		# **No session is held open around this.** The request opens its own, exactly as it
		# would over a socket; wrapping it in one of ours nests its transaction inside a
		# savepoint we then discard, which reports success and writes nothing.
		answer = inprocess.call(
			building.create_app(settings=self.settings, session_factory=self._sessions),
			self._principal,
			method=verb,
			path=subroutine.clients.base.require_a_route(path),
			body=body,
			query=query,
		)

		return subroutine.clients.base.Answered(answer.status_code, answer.text)

	def meta (self, *, workspace: str | None = None) -> subroutine.views.Meta:
		"""Report what this installation calls things — `#486`.

		**The same assembly the endpoint uses**, reached through the same documented late import
		as :meth:`reference` and for the same reason: ``api`` pulls in FastAPI, measured at 0.3s
		of the CLI's 0.8s start, and paying that on every command for a rarely-asked question is
		the wrong trade.

		**An application is built to be reflected, not to be served.** ``meta`` reports what each
		listing accepts, read out of the OpenAPI document rather than from a description of it —
		so answering the question locally means having the thing that generates it. Built with
		this client's own session factory, so it opens no second engine.
		"""

		from subroutine.api import app as building
		from subroutine.api import meta as documents

		with self._opened() as (session, actor):
			return documents.document(
				session,
				actor,
				self.settings,
				workspace_id=workspace,
				application=building.create_app(
					settings=self.settings, session_factory=self._sessions
				),
			)

	def identity (self) -> subroutine.clients.base.Identity:
		"""Report this installation's identity and the workspaces the credential reaches."""

		with self._opened() as (session, actor):
			instance = subroutine.domain.instances.get(session)
			reachable = list(subroutine.domain.workspaces.readable(session, actor))

			# One lookup for all of them (`#986`) — this is what `World` is built from, so a
			# query per workspace would be `#39`'s N+1 on the first call of every command.
			focused = subroutine.domain.projects.prioritised_addresses(
				session, actor, workspace_ids=[workspace.id for workspace in reachable]
			)

			return subroutine.clients.base.Identity(
				instance=None if instance is None else subroutine.views.instance(instance),
				workspaces=tuple(
					subroutine.views.workspace_ref(
						workspace,
						prioritised=focused.get(workspace.id),
						# The same answer the HTTP transport publishes, from the same
						# function — `#1083`, decision `#1088`.
						reader_timezone=subroutine.domain.schedule.zone_for(
							user=actor.user, workspace=workspace, instance=instance
						),
					)
					for workspace in reachable
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
		date: datetime.date | str | None = None,
		timezone: str | None = None,
		horizon_days: int | None = None,
		unscheduled_limit: int | None = None,
		workspace: str | None = None,
		project: str | None = None,
	) -> subroutine.views.Agenda:
		"""Return the agenda's buckets, across every workspace this credential reaches."""

		with self._opened() as (session, actor):
			# **Refused here rather than resolved against a guess** (`#1215`). The endpoint
			# refuses the same pair for the same reason, and both have to: a project key is per
			# workspace, so `project="web"` with no workspace is a question with more than one
			# answer on any instance holding two.
			if project is not None and workspace is None:
				raise subroutine.errors.ValidationError(
					"'project' names a project inside one workspace, so it needs a workspace.",
					errors=[
						subroutine.errors.FieldError(
							field="project",
							code="invalid_field_value",
							message="'project' has no meaning without a workspace.",
							hint="Pass workspace as well, or drop project.",
						)
					],
				)

			zone = subroutine.domain.schedule.zone_for(
				user=actor.user,
				instance=subroutine.domain.instances.get(session),
				explicit=timezone,
			)

			chosen = (
				None
				if workspace is None
				else subroutine.domain.selection.workspace(session, actor, requested=workspace)
			)

			built = subroutine.domain.agenda.build(
				session,
				principal=actor,
				workspace_ids=(
					[chosen.id]
					if chosen is not None
					else [
						found.id
						for found in subroutine.domain.workspaces.readable(session, actor)
					]
				),
				project=(
					None
					if project is None or chosen is None
					else subroutine.domain.selection.project(session, actor, chosen, project)
				),
				now=subroutine.db.types.utcnow(),
				timezone=zone,
				# Read here rather than by the caller, so both transports resolve one word
				# the same way and in the account's zone — `#1083`, decision `#1088`.
				date=(
					date
					if date is None or isinstance(date, datetime.date)
					else subroutine.domain.schedule.interpret_written_day(
						date, timezone=zone, now=subroutine.db.types.utcnow(), field="date"
					)
				),
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
		include_completed: bool | None = None,
		order: str | None = None,
		project: str | None = None,
		deferred: str = subroutine.domain.readiness.DEFAULT_DEFERRAL,
		q: str | None = None,
		parent: int | None = None,
		subtree: bool = False,
		ready: bool = False,
		deleted: bool = False,
		assignee: str | None = None,
		claimed_by: str | None = None,
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		due_before: datetime.datetime | None = None,
		due_after: datetime.datetime | None = None,
		filters: dict[str, str] | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Task]:
		"""List one workspace's tasks, newest first unless ``order`` says otherwise."""

		model = subroutine.db.models.work.Task
		size = subroutine.domain.paging.asked_for(limit, self.settings)
		choice = subroutine.domain.readiness.refuse_unknown_deferral(deferred)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			# **Resolved once and read twice** (`#1032`): whether this listing reaches finished
			# work, and which status to narrow by.
			named = (
				None
				if status is None
				else subroutine.domain.tasks.status_for(session, chosen.id, status)
			)

			# The same rule `GET /v1/tasks` applies, from the same function — a narrowing that
			# widened on one transport and not the other is what `domain.ordering` exists to
			# prevent for sorting, one filter along.
			# **Asking *when* something was completed is asking for completed work** (`#818`),
			# and it is decided here rather than in the router so both transports agree.
			#
			# **It moved inside the session for `#1032`**, because naming the finished status by
			# its key is the fifth spelling of that same question and answering it needs the
			# workspace's own vocabulary — which is not in hand until the workspace is.
			completion = subroutine.domain.tasks.completion_wanted(
				status_category,
				include_completed,
				status_named=named,
				about_completion=subroutine.domain.filtering.about(
					filters or {}, subroutine.domain.filtering.COMPLETION_FIELD
				),
				about_activity=subroutine.domain.filtering.about(
					filters or {}, subroutine.domain.filtering.TOUCHED_AT
				),
				# **The trash is a question about deletion, not about status** (`#900`). Asking
				# what you deleted must reach something you had finished first, which is
				# entirely ordinary — three items here were reachable by `show` and by no
				# listing at all.
				about_deletion=deleted,
				# `#873`, and here for the reason the paragraph above gives: the terminal and
				# the endpoint have to reach the same rows for the same question.
				naming_one_item=q is not None
				and subroutine.domain.refs.parse_ref(q) is not None,
			)

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
				include_completed=completion,
				include_deleted=deleted,
			)

			# Narrowed to what was widened for: `include_deleted` widens, this asks only for
			# the trash. A mixed list is the one place nothing in a row says which it is.
			if deleted:
				statement = statement.where(model.deleted_at.is_not(None))

			if narrowed is not None:
				statement = statement.where(subroutine.domain.scoping.within_project(narrowed))

			# **One instant for this call**, for the reason `readiness.undeferred` takes it:
			# what is hidden as deferred, what `ready` hides, and what `?order=deferred` sinks
			# are three readings of one clock and must not disagree.
			now = subroutine.db.types.utcnow()

			# The ordering vocabulary this call will use. `deferred` is added here rather than
			# declared in `TASK_FIELDS` because its band depends on that instant (`#877`), and
			# it is replaced below when a search runs against a backend that can rank one.
			#
			# **What `-priority_score` means depends on this workspace's prioritised project**
			# (`#986`), and it is adjusted here for the reason every ordering rule is decided in
			# the domain: a bonus the endpoint applies and the terminal does not is exactly the
			# divergence `ordering.py` exists to prevent. Nothing prioritised leaves the map
			# alone.
			sortable: dict[str, subroutine.domain.ordering.Sortable] = (
				subroutine.domain.ordering.sinking(
					subroutine.domain.ordering.prioritising(
						subroutine.domain.ordering.TASK_FIELDS,
						prefixes=subroutine.domain.scoping.prioritised_paths(
							session, actor, workspace_ids=[chosen.id]
						),
					),
					model=model,
					now=now,
				)
			)
			# **Asked rather than decided here** (`#1150`). A listing narrowed to finished work
			# is ordered by when it finished, and the rule lives in the domain for the same
			# reason `sortable` is built there: a default the endpoint applies and the terminal
			# does not is exactly the divergence `ordering.py` exists to prevent — and this one
			# was already real, with the browser's *done* view carrying `-completed_at` as a
			# literal of its own while this path and every board's finished column did not.
			fallback: tuple[str, ...] = subroutine.domain.tasks.default_order(
				status=named, category=status_category
			)

			# **Built in steps rather than one chained expression, and `is not None` rather
			# than `or`.** A SQLAlchemy element raises on truth-testing, so `predicate or
			# true()` is not a shorter spelling of this — it is a `TypeError` at the one
			# moment a caller asked for narrowing.
			parked = subroutine.domain.readiness.deferred(model, now=now, choice=choice)

			if parked is not None:
				statement = statement.where(parked)

			if q:
				# **This client's settings, not the ambient ones** (`#883`). `chosen` falls
				# back to `config.load_settings()`, which re-reads the environment — the trap
				# already recorded about the CLI, one layer down in the client itself.
				backend = subroutine.domain.search.chosen(session, settings=self.settings)
				words = subroutine.domain.search.terms(q)
				# One clause, composed in the domain (`#892`) — see `api/tasks.py`.
				statement = statement.where(
					subroutine.domain.search.anywhere(
						q,
						identity=model.id,
						columns=(model.title, model.description),
						ref=model.ref,
						entity_type="task",
						backend=backend,
					)
				)

				# `#823`, and here for the reason every ordering rule is decided in the domain:
				# a ranking the endpoint applies and the terminal does not is the divergence
				# `ordering.py` exists to prevent, one sort field along.
				# **`words`, not `q`** (`#880`): `" "` is truthy and has no words in it.
				if words and backend == subroutine.domain.search.NATIVE:
					sortable = subroutine.domain.ordering.searching(
						sortable,
						terms=words,
						columns=[model.title, model.description],
						carried_on=model.relevance,
						ref=model.ref,
						numbered=subroutine.domain.refs.parse_ref(q),
					)
					fallback = (f"-{subroutine.domain.ordering.RELEVANCE}",)

			# **After the deferral filter, which it subsumes**, and in the same order the
			# endpoint applies them: `ready` already excludes anything parked, so combining the
			# two narrows rather than contradicts. One predicate, shared with `GET /v1/tasks`,
			# because a readiness that meant something different here would be worse than none.
			if ready:
				statement = statement.where(
					subroutine.domain.readiness.ready(model, now=now, by=actor.user.id)
				)

			if parent is not None:
				# Resolved through the same scoped statement the listing uses, so a parent
				# this caller cannot see is absent rather than forbidden — and the children
				# of something invisible are not disclosed by an empty list either.
				#
				# **Deleted parents included, for `_subject`'s reason** (`#700`). An item in
				# the trash is still an item, and the HTTP side answers this with an empty
				# list where this refused outright — so ``subroutine show`` on something
				# deleted failed here after it had already been found. Being unreadable is
				# what hides a parent; being deleted is not.
				#
				# **And templates, for the same reason again** (`#921`). This was `#700`'s
				# fourth site and it is this one's fourth too: `show` resolves the series,
				# reads its comments, and then asks here for its children — so the refusal
				# arrives *after* the item has been found, in the words of a listing nobody
				# ran. **A ref this product published resolves everywhere or nowhere**; a
				# lookup that refuses one of the four is a defect wearing a different
				# sentence each time, which is why `#700`'s own record says a changed
				# refusal is not a fixed one.
				above = session.scalars(
					subroutine.domain.scoping.readable_tasks(
						actor,
						workspace_ids=[chosen.id],
						include_completed=True,
						include_deleted=True,
						include_templates=True,
					).where(model.ref == parent)
				).first()

				if above is None:
					raise subroutine.errors.NotFound(
						f"There is no #{parent} here.",
						hint="Run 'subroutine list' to see what there is.",
					)

				# Widened to the whole tree when asked, exactly as `GET /v1/tasks?subtree=`
				# does it — a delegated piece of work broken into parts is a tree, and its
				# direct children alone are not the answer to "how is it going".
				statement = (
					statement.where(
						subroutine.domain.hierarchy.subtree(model, above), model.id != above.id
					)
					if subtree
					else statement.where(model.parent_task_id == above.id)
				)

			elif subtree:
				raise subroutine.errors.ValidationError(
					"'subtree' says how much of a parent's tree to return, so it needs a parent.",
					errors=[
						subroutine.errors.FieldError(
							field="subtree",
							code="invalid_field_value",
							message="'subtree' has no meaning without 'parent'.",
							hint="Pass parent=<ref> as well, or drop subtree.",
						)
					],
				)

			# **The same three lookups the endpoint makes, in the same order** (`#501`). Each
			# resolves through the domain rather than comparing a string to a column, so an
			# unknown status, type or account is refused by name here exactly as it is over
			# HTTP — which is what `tests/test_transport_equivalence.py` is for, and why these
			# read as duplication rather than being one.
			if named is not None:
				statement = statement.where(model.status_id == named.id)

			if status_category is not None:
				statement = statement.where(
					model.status_id.in_(
						subroutine.domain.tasks.statuses_in_category(
							session, chosen.id, status_category
						)
					)
				)

			if type is not None:
				statement = statement.where(
					model.type_id
					== subroutine.domain.tasks.item_type_for(session, chosen.id, type).id
				)

			if assignee is not None:
				statement = statement.where(
					model.assignee_id
					== subroutine.domain.selection.user(
						session, assignee, caller=actor.user
					).id
				)

			# **Held now, which is not the same as assigned** (`#1120`), and an expired claim is
			# not held — §10.7 invariant 10, the same reading `readiness` takes. The endpoint's
			# clause is the same two conditions, because a narrowing that differed between
			# transports is what this module keeps being about.
			if claimed_by is not None:
				statement = statement.where(
					model.claimed_by_id
					== subroutine.domain.selection.user(
						session, claimed_by, caller=actor.user
					).id,
					model.claim_expires_at > now,
				)

			if due_before is not None:
				statement = statement.where(
					model.due_at < due_before
				)

			if due_after is not None:
				statement = statement.where(
					model.due_at > due_after
				)

			# §9.6's date comparisons, compiled by the domain rather than approximated here
			# (`#815`) — so the two transports narrow by the same predicate, in the same zone,
			# with the same refusal for a field neither has.
			statement = statement.where(
				*subroutine.domain.filtering.asked(
					(filters or {}).items(),
					entity="task",
					now=subroutine.db.types.utcnow(),
					timezone=subroutine.domain.filtering.timezone_for(session, actor, chosen),
					session=session,
					caller=actor.user,
					workspace_ids=[chosen.id],
				)
			)

			# `#884`, and shared with the endpoint so both refuse it the same way: a name
			# `/v1/meta` publishes must not come back as an unknown field.
			subroutine.domain.ordering.refuse_ranking_without_a_search(
				order, searching=subroutine.domain.ordering.RELEVANCE in sortable
			)

			rows = list(
				session.scalars(
					# Built by the domain from the same vocabulary ``GET /v1/tasks`` uses,
					# rather than approximated here: two spellings of "newest first" is the
					# pair that comes to disagree, and this one used to be the *only* one,
					# which is why a client could not rank at all. NULLS LAST and the
					# tiebreaker are `ordering.clauses`' job now (docs/design.md §10.3).
					statement.options(
						# The ordering's computed values have to arrive on the row rather than
						# being worked out again here — there is no second copy to work them
						# out from since `#569`, and the merged sort reads them off the view.
						*subroutine.domain.ordering.options(
							order, allowed=sortable, default=fallback
						)
					)
					.order_by(
						*subroutine.domain.ordering.clauses(
							order, allowed=sortable, default=fallback, tiebreak=model.id
						)
					)
					.limit(size + 1)
				)
			)
			vocabulary = subroutine.views.Vocabulary.for_tasks(session, rows)

			# **One more row than asked for, and the extra is the answer to "is that all?"** (`#1037`).
			return subroutine.clients.base.Listing(
				[subroutine.views.task(row, vocabulary) for row in rows[:size]],
				has_more=len(rows) > size,
			)

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

	def _vocabulary_row (
		self, session: typing.Any, actor: typing.Any, model: typing.Any, which: str, what: str
	) -> typing.Any:
		"""Return one vocabulary row this credential can reach, or refuse by name.

		**A 404 rather than a 403 for a row in another workspace**, which is the same choice
		§7.3a makes about a private project: saying "forbidden" would confirm the id names
		something.
		"""

		found = session.get(model, uuid.UUID(which))
		reachable = {row.id for row in subroutine.domain.workspaces.readable(session, actor)}

		if found is None or found.workspace_id not in reachable:
			raise subroutine.errors.NotFound(f"There is no {what} with that id.")

		return found

	def statuses (
		self, *, workspace: str | None = None, entity_type: str | None = None
	) -> subroutine.views.Collection[subroutine.views.Status]:
		"""List this workspace's statuses, in the order a client should show them."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			rows = subroutine.domain.vocabulary.statuses(
				session, workspace_id=chosen.id, entity_type=entity_type
			)

			return subroutine.views.Collection[subroutine.views.Status](
				items=[subroutine.views.status(row) for row in rows],
				page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
			)

	def create_status (
		self,
		*,
		entity_type: str,
		key: str,
		label: str,
		category: str,
		is_default: bool = False,
		position: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Status:
		"""Add a status to this workspace's vocabulary."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			return subroutine.views.status(
				subroutine.domain.vocabulary.create_status(
					session,
					workspace_id=chosen.id,
					entity_type=entity_type,
					key=key,
					label=label,
					category=category,
					is_default=is_default,
					position=position,
					actor=actor,
				)
			)

	def update_status (
		self,
		*,
		which: str,
		key: str | None = None,
		label: str | None = None,
		is_default: bool | None = None,
		position: int | None = None,
	) -> subroutine.views.Status:
		"""Rename or reposition a status."""

		self._refuse_if_read_only()

		changes = _asked(key=key, label=label, is_default=is_default, position=position)

		with self._writing() as (session, actor):
			row = self._vocabulary_row(
				session, actor, subroutine.db.models.vocabulary.Status, which, "status"
			)

			return subroutine.views.status(
				subroutine.domain.vocabulary.update_status(session, row, actor=actor, **changes)
			)

	def delete_status (self, *, which: str) -> None:
		"""Remove a status nothing is in."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			subroutine.domain.vocabulary.delete_status(
				session,
				self._vocabulary_row(
					session, actor, subroutine.db.models.vocabulary.Status, which, "status"
				),
				actor=actor,
			)

	def link_types (
		self, *, workspace: str | None = None
	) -> subroutine.views.Collection[subroutine.views.LinkType]:
		"""List the ways two items can relate here."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			rows = subroutine.domain.vocabulary.link_types(session, workspace_id=chosen.id)

			return subroutine.views.Collection[subroutine.views.LinkType](
				items=[subroutine.views.link_type(row) for row in rows],
				page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
			)

	def create_link_type (
		self,
		*,
		key: str,
		title: str,
		inverse_title: str,
		category: str,
		is_symmetric: bool = False,
		workspace: str | None = None,
	) -> subroutine.views.LinkType:
		"""Add a way two items can relate."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			return subroutine.views.link_type(
				subroutine.domain.vocabulary.create_link_type(
					session,
					workspace_id=chosen.id,
					key=key,
					title=title,
					inverse_title=inverse_title,
					category=category,
					is_symmetric=is_symmetric,
					actor=actor,
				)
			)

	def update_link_type (
		self,
		*,
		which: str,
		key: str | None = None,
		title: str | None = None,
		inverse_title: str | None = None,
		category: str | None = None,
	) -> subroutine.views.LinkType:
		"""Rename a link type, reword either end of it, or say what it does."""

		self._refuse_if_read_only()

		changes = _asked(
			key=key, title=title, inverse_title=inverse_title, category=category
		)

		with self._writing() as (session, actor):
			row = self._vocabulary_row(
				session, actor, subroutine.db.models.vocabulary.LinkType, which, "link type"
			)

			return subroutine.views.link_type(
				subroutine.domain.vocabulary.update_link_type(
					session, row, actor=actor, **changes
				)
			)

	def delete_link_type (self, *, which: str) -> None:
		"""Remove a link type nothing is joined by."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			subroutine.domain.vocabulary.delete_link_type(
				session,
				self._vocabulary_row(
					session, actor, subroutine.db.models.vocabulary.LinkType, which, "link type"
				),
				actor=actor,
			)

	def tags (
		self, *, workspace: str | None = None
	) -> subroutine.views.Collection[subroutine.views.TagEntry]:
		"""List this workspace's tags as things to curate."""

		model = subroutine.db.models.vocabulary.Tag

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			rows = list(
				session.scalars(
					sqlalchemy.select(model)
					.where(model.workspace_id == chosen.id)
					.order_by(model.name_normalized)
				)
			)

			return subroutine.views.Collection[subroutine.views.TagEntry](
				items=[subroutine.views.tag_entry(row) for row in rows],
				page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
			)

	def create_tag (
		self, *, name: str, description: str | None = None, workspace: str | None = None
	) -> subroutine.views.TagEntry:
		"""Declare a tag before anybody uses it."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			return subroutine.views.tag_entry(
				subroutine.domain.vocabulary.create_tag(
					session,
					workspace_id=chosen.id,
					name=name,
					description=description,
					actor=actor,
				)
			)

	def update_tag (
		self, *, which: str, name: str | None = None, description: str | None = None
	) -> subroutine.views.TagEntry:
		"""Rename a tag, or write down what it means."""

		self._refuse_if_read_only()

		changes = _asked(name=name, description=description)

		with self._writing() as (session, actor):
			row = self._vocabulary_row(
				session, actor, subroutine.db.models.vocabulary.Tag, which, "tag"
			)

			return subroutine.views.tag_entry(
				subroutine.domain.vocabulary.update_tag(session, row, actor=actor, **changes)
			)

	def delete_tag (self, *, which: str) -> None:
		"""Remove a tag, and with it every application of it."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			subroutine.domain.vocabulary.delete_tag(
				session,
				self._vocabulary_row(
					session, actor, subroutine.db.models.vocabulary.Tag, which, "tag"
				),
				actor=actor,
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
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		filters: dict[str, str] | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Document]:
		"""List one workspace's documents, newest first unless ``order`` says otherwise."""

		model = subroutine.db.models.work.Document
		size = subroutine.domain.paging.asked_for(limit, self.settings)

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			narrowed = (
				None
				if project is None
				else subroutine.domain.selection.project(session, actor, chosen, project)
			)

			# Resolved through the domain in the same order `GET /v1/documents` resolves them,
			# so an unknown key is refused by name rather than matching nothing quietly (`#501`).
			wanted_type = (
				None
				if type is None
				else subroutine.domain.documents.item_type_for(session, chosen.id, type).id
			)
			wanted_status = (
				None
				if status is None
				else subroutine.domain.documents.status_for(session, chosen.id, status).id
			)
			# **A category, not a key** (`#1087`). Resolved here rather than compared as a
			# string because the answer is a set of ids and the refusal for an unknown category
			# lives in the domain, so both transports say the same sentence about the same
			# mistake.
			in_category = (
				None
				if status_category is None
				else subroutine.domain.documents.statuses_in_category(
					session, chosen.id, status_category
				)
			)

			# The same choice the task listing above makes, for the same reason (`#823`): a
			# search that can be ranked is ranked, and everything else keeps the vocabulary it
			# has always had. Resolved once rather than inside the query, so the predicate and
			# the ordering are built from one answer about the backend.
			backend = subroutine.domain.search.chosen(session, settings=self.settings)
			words = subroutine.domain.search.terms(q or "")

			# **`deferred` here too, answered with the one band a document can be in** (`#877`).
			# The endpoint says the same and says why: an order only one half of a merged
			# listing accepts drops the other half rather than widening the page.
			sortable: dict[str, subroutine.domain.ordering.Sortable] = (
				subroutine.domain.ordering.sinking(
					subroutine.domain.ordering.DOCUMENT_FIELDS
				)
			)
			fallback: tuple[str, ...] = tuple(
				subroutine.domain.ordering.DEFAULT_DOCUMENT_ORDER
			)

			if words and backend == subroutine.domain.search.NATIVE:
				sortable = subroutine.domain.ordering.searching(
					sortable,
					terms=words,
					columns=[model.title, model.body],
					carried_on=model.relevance,
					ref=model.ref,
					numbered=subroutine.domain.refs.parse_ref(q or ""),
				)
				fallback = (f"-{subroutine.domain.ordering.RELEVANCE}",)

			subroutine.domain.ordering.refuse_ranking_without_a_search(
				order, searching=subroutine.domain.ordering.RELEVANCE in sortable
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
						sqlalchemy.true() if wanted_type is None else model.type_id == wanted_type
					)
					.where(
						sqlalchemy.true()
						if wanted_status is None
						else model.status_id == wanted_status
					)
					.where(
						sqlalchemy.true()
						if in_category is None
						else model.status_id.in_(in_category)
					)
					.where(
						sqlalchemy.true()
						if not q
						# One clause, composed in the domain (`#892`) — see `api/tasks.py`.
						else subroutine.domain.search.anywhere(
							q,
							identity=model.id,
							columns=(model.title, model.body),
							ref=model.ref,
							entity_type="document",
							backend=backend,
						)
					)
					# §9.6's date comparisons (`#815`), compiled by the domain so that this and
					# `GET /v1/documents` narrow by one predicate rather than by two spellings.
					.where(
						*subroutine.domain.filtering.asked(
							(filters or {}).items(),
							entity="document",
							now=subroutine.db.types.utcnow(),
							timezone=subroutine.domain.filtering.timezone_for(
								session, actor, chosen
							),
							session=session,
							caller=actor.user,
							workspace_ids=[chosen.id],
						)
					)
					# Built by the domain from the vocabulary `GET /v1/documents` uses,
					# rather than spelled out here — the ordering, its NULLS LAST (§10.3) and
					# its tiebreaker are one rule and used to be two copies of it.
					# The loader option as well as the ordering, which this listing never
					# needed until a search could be ranked: a computed sort value exists only
					# in SQL and has to arrive on the row for the merge to read it.
					#
					# `#884`'s refusal is applied above, before any of this is built.
					.options(
						*subroutine.domain.ordering.options(
							order, allowed=sortable, default=fallback
						)
					)
					.order_by(
						*subroutine.domain.ordering.clauses(
							order, allowed=sortable, default=fallback, tiebreak=model.id
						)
					)
					.limit(size + 1)
				)
			)
			vocabulary = subroutine.views.Vocabulary.for_documents(session, rows)

			# **One more row than asked for, and the extra is the answer to "is that all?"** (`#1037`).
			return subroutine.clients.base.Listing(
				[subroutine.views.document(row, vocabulary) for row in rows[:size]],
				has_more=len(rows) > size,
			)

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
		tags: typing.Sequence[str] | None = subroutine.clients.base.UNSET,
		supersedes: int | None = subroutine.clients.base.UNSET,
		expected_version: int | None = None,
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
			"tags": tags,
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

			# **Resolved to a row here, for the reason `project` above gives** (`#1144`): the
			# service takes a `Document` and a caller carries a ref. Going through `_subject`
			# rather than a bare lookup is what makes an unreadable document refuse by name
			# instead of reporting that nothing replaced anything.
			#
			# `None` is passed straight through — that is §8.3 clearing the chain, and it is a
			# thing somebody does when a supersession turns out to have been wrong.
			if supersedes is not subroutine.clients.base.UNSET:
				changes["supersedes"] = (
					None
					if supersedes is None
					else session.get(
						subroutine.db.models.work.Document,
						self._subject(session, actor, chosen.id, "document", supersedes),
					)
				)

			revised = subroutine.domain.documents.update(session, row, actor=actor, expected_version=expected_version, **changes)

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

			return subroutine.views.links(
				session,
				subroutine.domain.links.around(
					session,
					actor,
					workspace_id=chosen.id,
					entity_type=entity_type,
					identifier=subject,
				),
			)

	def governing (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Governing]:
		"""Return the documents in force that govern one item."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			return subroutine.views.governing(
				session,
				subroutine.domain.links.governing(
					session,
					actor,
					workspace_id=chosen.id,
					entity_type=entity_type,
					identifier=subject,
				),
			)

	def verifications (
		self, *, ref: int, workspace: str | None = None
	) -> list[subroutine.views.Verification]:
		"""Return what has been checked against one task, newest first."""

		with self._opened() as (session, actor):
			task = self._require(session, actor, ref, workspace)
			found = list(
				session.scalars(subroutine.domain.verifications.against(task))
			)
			vocabulary = subroutine.views.Vocabulary(
				session,
				user_ids=[row.created_by for row in found if row.created_by is not None],
			)

			return [
				subroutine.views.verification(
					row,
					ref=task.ref,
					recorded_by=subroutine.views.username_in(vocabulary, row.created_by),
				)
				for row in found
			]

	def verify (
		self,
		*,
		ref: int,
		passed: bool,
		summary: str | None = None,
		output_excerpt: str | None = None,
		tree_hash: str | None = None,
		commit_sha: str | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Verification:
		"""Record what was checked against one task."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			task = self._require(session, actor, ref, workspace)
			written = subroutine.domain.verifications.record(
				session,
				task,
				passed=passed,
				summary=summary,
				output_excerpt=output_excerpt,
				tree_hash=tree_hash,
				commit_sha=commit_sha,
				actor=actor,
			)
			vocabulary = subroutine.views.Vocabulary(
				session,
				user_ids=[] if written.created_by is None else [written.created_by],
			)

			return subroutine.views.verification(
				written,
				ref=task.ref,
				recorded_by=subroutine.views.username_in(vocabulary, written.created_by),
			)

	def proposed_links (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Proposal]:
		"""Return the documents this item's writing suggests govern it."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			return subroutine.views.proposals(
				session,
				subroutine.domain.links.proposals(
					session,
					actor,
					workspace_id=chosen.id,
					entity_type=entity_type,
					identifier=subject,
				),
			)

	def backlinks (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Backlink]:
		"""Return everything whose prose refers to one item."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)
			subject = self._subject(session, actor, chosen.id, entity_type, ref)

			return [
				subroutine.views.Backlink(
					kind=one.kind,
					ref=one.ref,
					title=one.title,
					via=one.via,
					created_at=one.at,
				)
				for one in subroutine.domain.mentions.backlinks(
					session,
					principal=actor,
					workspace_id=chosen.id,
					target_type=entity_type,
					target_id=subject,
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
					return subroutine.views.links(session, [related])[0]

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

			# **The whole end rather than its id, because the event needs to say who acted**
			# (`#816`). This resolved only an id and then let `links.remove` fall back to the
			# link's *source* — so withdrawing an incoming link while reading its target
			# recorded the work against an item nobody opened. The endpoint has passed
			# `acted_on` since `#816`; this transport never did, and the equivalence suite
			# could not see it because both sides agree about the outcome and the disagreement
			# was in the event.
			near = self._end(session, actor, chosen, entity_type, ref)

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
							model.source_type == entity_type, model.source_id == near.id
						),
						sqlalchemy.and_(
							model.target_type == entity_type, model.target_id == near.id
						),
					),
				)
			).first()

			if found is None:
				raise subroutine.errors.NotFound(
					"There is no such link on that item.",
					hint="Run 'subroutine show <ref>' to see what it is joined to.",
				)

			subroutine.domain.links.remove(session, found, acted_on=near, actor=actor)

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

			# One query for every author on the page, not one per comment — `#636`, and
			# `#39`'s N+1 on the view whose whole job is reading what people recorded.
			vocabulary = subroutine.views.Vocabulary(
				session,
				user_ids=[row.author_id for row in rows if row.author_id is not None],
			)

			return [subroutine.views.comment(row, vocabulary) for row in rows]

	def history (
		self,
		*,
		ref: int,
		entity_type: str = "task",
		workspace: str | None = None,
		limit: int | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Event]:
		"""Return what has happened to one item, newest first.

		**No upper bound**, which is the one thing this shares with the route rather than with
		the feed: `seq` becomes visible at commit, so a watermark would mean commenting on an
		item and immediately reading its history shows nothing (§5.11a).
		"""

		size = subroutine.domain.paging.asked_for(limit, self.settings)

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
				).limit(size + 1)
			).all()
			described = subroutine.domain.events.descriptions(session, rows)

			# **One more row than asked for, and the extra is the answer to "is that all?"** (`#1037`).
			return subroutine.clients.base.Listing(
				[subroutine.views.event(row, described) for row in rows[:size]],
				has_more=len(rows) > size,
			)

	def changes (
		self,
		*,
		since: int | None = None,
		mine: bool = False,
		by: str | None = None,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Event]:
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

		size = subroutine.domain.paging.asked_for(limit, self.settings)

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

			rows, more = subroutine.domain.events.page(
				session,
				actor,
				workspace_ids=workspace_ids,
				size=size,
				since=since,
				mine=mine,
				# Resolved here rather than passed as a name, for the reason every other "who"
				# in this file is: a username that names nobody is refused with the members
				# listed, and the refusal is the domain's rather than one written twice.
				by=(
					None
					if by is None
					else subroutine.domain.selection.user(session, by, caller=actor.user).id
				),
				newest=newest,
			)
			described = subroutine.domain.events.descriptions(session, rows)

			# **The feed's own answer, which this was already being given and threw away**
			# (`#1037`). `events.page` returns `(rows, more)` and the second was bound to
			# `_more` — the underscore saying *deliberately unused* about the one fact a caller
			# needed. Everywhere else here the extra row is the answer; here it is a real
			# return value, and using the row count instead would be a second computation of
			# something already correct.
			return subroutine.clients.base.Listing(
				[subroutine.views.event(row, described) for row in rows],
				has_more=more,
				# The same function the route answers with, so the two transports cannot say
				# different things about one credential (`#1085`).
				covers=subroutine.domain.scoping.readable_event_kinds(actor),
			)

	def projects (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		parent: str | None = None,
		visibility: str | None = None,
		include_archived: bool = False,
		order: str | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Project]:
		"""List the projects this credential can see, parents before children."""

		size = subroutine.domain.paging.asked_for(limit, self.settings)
		model = subroutine.db.models.project.Project

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(session, actor, requested=workspace)

			# `readable_projects` and not a hand-written query: it applies the workspace scope,
			# the privacy inheritance of §7.3a and the token's own `project_scope` together,
			# and narrowing by hand is what left `subroutine ls` listing private projects to
			# non-members in shipped code.
			statement = subroutine.domain.scoping.readable_projects(
				actor, workspace_ids=[chosen.id], include_archived=include_archived
			)

			# Resolved through the same function `GET /v1/projects` uses, so a parent this
			# caller cannot see is not found rather than an empty list (`#501`).
			if parent is not None:
				statement = statement.where(
					model.parent_id
					== subroutine.domain.selection.project(session, actor, chosen, parent).id
				)

			if visibility is not None:
				statement = statement.where(model.visibility == visibility)

			# The same vocabulary `GET /v1/projects` sorts by, out of `domain/ordering.py` since
			# `#501` — a hand-written `order_by(model.path)` here was the reason the two could
			# not be compared, and `ordering.clauses` carries the NULLS LAST rule (§10.3) and
			# the tiebreak that keyset pagination needs rather than leaving them to be
			# remembered per call site.
			rows = list(
				session.scalars(
					statement.order_by(
						*subroutine.domain.ordering.clauses(
							order,
							allowed=subroutine.domain.ordering.PROJECT_FIELDS,
							default=subroutine.domain.ordering.DEFAULT_PROJECT_ORDER,
							tiebreak=model.id,
						)
					).limit(size + 1)
				)
			)

			vocabulary = subroutine.views.Vocabulary.for_projects(session, rows)

			# **One more row than asked for, and the extra is the answer to "is that all?"** (`#1037`).
			return subroutine.clients.base.Listing(
				[subroutine.views.project(row, vocabulary) for row in rows[:size]],
				has_more=len(rows) > size,
			)

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

		self._refuse_if_read_only()

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

	def create_login_link (
		self, *, username: str | None = None
	) -> subroutine.views.SignInLink:
		"""Mint a single-use sign-in link for a browser, and return it once (`#248`)."""

		self._refuse_if_read_only()

		# **Told, worked out, or refused** — `#1007`, and the middle branch is new.
		#
		# This used to refuse whenever `public_url` was unset, on the reasoning that there is
		# no request to take a host from and that inventing "localhost and a port nobody said"
		# would produce a link that looks right and goes nowhere. That is exactly right behind
		# a proxy and was applied to every case, including the one the README sends every new
		# self-hoster down: `subroutine serve`, then `subroutine login link`. **The port was
		# said.** It is `settings.port`, beside `settings.host` — the same two settings `serve`
		# binds to and prints back as `Serving on http://127.0.0.1:8471`. Not a guess; the
		# fact the neighbouring command already computed.
		#
		# `config.browsable_url` is where the three-way rule lives, so the refusal that
		# survives is the one that has to: bound wide with nothing configured, where the host
		# may be `0.0.0.0` and nobody can say which of this machine's addresses a reader will
		# use.
		told = (self.settings.public_url or "").strip()
		root = subroutine.config.browsable_url(self.settings)

		if not root:
			raise subroutine.errors.ValidationError(
				f"This instance listens on {self.settings.host!r}, which is not an address a "
				"browser can be sent to, and no public_url says where it is reached instead.",
				code="missing_field",
				hint="Set public_url in config.toml to the address a browser reaches this "
				"instance on. A link is only useful where the web UI is served.",
			)

		with self._writing() as (session, actor):
			for_whom = (
				subroutine.domain.selection.user(session, username)
				if username
				else actor.user
			)
			link, secret = subroutine.domain.sessions.mint_link(
				session, user=for_whom, actor=actor
			)

			return subroutine.views.SignInLink(
				url=f"{root}/signin?link={urllib.parse.quote(secret, safe='')}",
				username=for_whom.username,
				expires_at=link.expires_at,
				address_assumed=not told,
			)

	def sign_out_everywhere (self, *, username: str) -> subroutine.views.SignedOut:
		"""End every browser session an account holds, and report how many (`#248`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			for_whom = subroutine.domain.selection.user(session, username)
			stopped = subroutine.domain.sessions.sign_out_everywhere(
				session, user=for_whom, actor=actor
			)

			return subroutine.views.SignedOut(
				username=for_whom.username, sessions_ended=stopped
			)

	def revoke_token (self, *, id_or_prefix: str) -> subroutine.views.Token:
		"""Stop a credential working, now (`#348`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			found = subroutine.domain.tokens.mine(session, actor, id_or_prefix)
			stopped = subroutine.domain.tokens.revoke(session, found, actor=actor)
			owner = session.get(subroutine.db.models.identity.User, stopped.user_id)

			return subroutine.views.token(
				stopped, owner=owner, session=session, principal=actor
			)

	def calendars (
		self, *, include_revoked: bool = False
	) -> list[subroutine.views.Calendar]:
		"""List your own calendar feeds, newest first (`#916`)."""

		with self._opened() as (session, actor):
			return [
				subroutine.views.calendar(row, session=session, principal=actor)
				for row in subroutine.domain.calendars.feeds(
					session, actor.user, include_revoked=include_revoked
				)
			]

	def create_calendar (
		self,
		*,
		title: str,
		workspace: str | None = None,
		project: str | None = None,
		audience: str = "everything",
		item_types: typing.Sequence[str] | None = None,
		expires: str | None = None,
	) -> subroutine.views.IssuedCalendar:
		"""Mint a calendar feed and return its URL once (`#916`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			feed, minted = subroutine.domain.calendars.issue(
				session,
				actor,
				title=title,
				workspace=workspace,
				project=project,
				audience=audience,
				item_types=item_types,
				expires=expires,
				enabled=self.settings.calendars_enabled,
			)
			rendered = subroutine.views.calendar(
				feed,
				url=subroutine.domain.calendars.address(
					self.settings.public_url, minted
				),
				issued=True,
				session=session,
				principal=actor,
			)

		# The type the protocol promises. `views.calendar` answers with the base type when no
		# URL was asked for, and a cast here would be a claim rather than a check.
		assert isinstance(rendered, subroutine.views.IssuedCalendar)

		return rendered

	def reset_calendar (self, *, id_or_prefix: str) -> subroutine.views.IssuedCalendar:
		"""Give a feed a new URL, so the one somebody had stops working (`#916`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			found = subroutine.domain.calendars.mine(session, actor, id_or_prefix)
			minted = subroutine.domain.calendars.reset(
				session, found, enabled=self.settings.calendars_enabled
			)
			rendered = subroutine.views.calendar(
				found,
				url=subroutine.domain.calendars.address(
					self.settings.public_url, minted
				),
				issued=True,
				session=session,
				principal=actor,
			)

		assert isinstance(rendered, subroutine.views.IssuedCalendar)

		return rendered

	def revoke_calendar (self, *, id_or_prefix: str) -> subroutine.views.Calendar:
		"""Stop a calendar feed for good, now (`#916`)."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			found = subroutine.domain.calendars.mine(session, actor, id_or_prefix)

			subroutine.domain.calendars.revoke(session, found)

			return subroutine.views.calendar(found, session=session, principal=actor)

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
		is_superuser: bool = False,
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
				is_superuser=is_superuser,
				actor=actor,
			)

			return subroutine.views.user(created)

	def members (self, *, workspace: str | None = None) -> list[subroutine.views.Member]:
		"""List who belongs to one workspace."""

		with self._opened() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)

			focus = subroutine.domain.projects.prioritised_addresses(
				session, actor, workspace_ids=[chosen.id]
			).get(chosen.id)

			return [
				subroutine.views.member(
					row, account=account, role=role, within=chosen, prioritised=focus
				)
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
				membership,
				account=account,
				role=held,
				within=chosen,
				prioritised=subroutine.domain.projects.prioritised_addresses(
					session, actor, workspace_ids=[chosen.id]
				).get(chosen.id),
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

	def set_timezone (
		self, *, username: str, timezone: str | None
	) -> subroutine.views.User:
		"""Say where somebody keeps their diary — your own account only."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			account = subroutine.domain.users.by_username(session, username)

			subroutine.domain.users.set_timezone(
				session, account, timezone=timezone, actor=actor
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

	def update_project (
		self,
		project: str,
		*,
		title: str = subroutine.clients.base.UNSET,
		description: str | None = subroutine.clients.base.UNSET,
		visibility: str = subroutine.clients.base.UNSET,
		status: str = subroutine.clients.base.UNSET,
		settings: dict[str, typing.Any] = subroutine.clients.base.UNSET,
		workspace: str | None = None,
		expected_version: int | None = None,
	) -> subroutine.views.Project:
		"""Change the fields beside a project's address, in process."""

		self._refuse_if_read_only()

		given: dict[str, typing.Any] = {
			"title": title,
			"description": description,
			"visibility": visibility,
			"status_key": status,
			"settings": settings,
		}

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			found = subroutine.domain.selection.project(session, actor, chosen, project)
			changed = subroutine.domain.projects.update(
				session,
				found,
				actor=actor, expected_version=expected_version,
				**{
					name: value
					for name, value in given.items()
					if value is not subroutine.clients.base.UNSET
				},
			)

			return subroutine.views.project(
				changed, subroutine.views.Vocabulary.for_projects(session, [changed])
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

			return self._rendered_workspace(session, actor, created)

	def _rendered_workspace (
		self,
		session: sqlalchemy.orm.Session,
		actor: subroutine.domain.authentication.Principal,
		row: subroutine.db.models.identity.Workspace,
	) -> subroutine.views.Workspace:
		"""Render one workspace, resolving what it has prioritised (`#986`).

		``views.workspace`` requires the answer rather than defaulting it, so that a caller
		cannot quietly report *nothing prioritised* by forgetting to ask — and this transport
		reporting a different answer from the other one is the divergence §13.7 exists to
		prevent.
		"""

		focused = subroutine.domain.projects.prioritised_addresses(
			session, actor, workspace_ids=[row.id]
		)

		return subroutine.views.workspace(row, prioritised=focused.get(row.id))

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

			return self._rendered_workspace(session, actor, renamed)

	def update_workspace (
		self,
		workspace: str,
		*,
		title: str = subroutine.clients.base.UNSET,
		description: str | None = subroutine.clients.base.UNSET,
		timezone: str | None = subroutine.clients.base.UNSET,
		prioritised_project: str | None = subroutine.clients.base.UNSET,
		settings: dict[str, typing.Any] = subroutine.clients.base.UNSET,
		workspace_id: str | None = None,
		expected_version: int | None = None,
	) -> subroutine.views.Workspace:
		"""Change the fields beside a workspace's address, in process."""

		self._refuse_if_read_only()

		given: dict[str, typing.Any] = {
			"title": title,
			"description": description,
			"timezone": timezone,
			"settings": settings,
		}

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)

			# **Resolved to a project here, so an unknown one is refused by name** — the same
			# way `PATCH /v1/workspaces` refuses it, which is what keeps the two transports
			# saying one thing (`#986`). Null clears the priority and is a value rather than an
			# absence, so it is tested against the sentinel rather than for truth.
			if prioritised_project is not subroutine.clients.base.UNSET:
				given["prioritised_project"] = (
					None
					if prioritised_project is None
					else subroutine.domain.selection.project(
						session, actor, chosen, prioritised_project
					)
				)

			changed = subroutine.domain.workspaces.update(
				session,
				chosen,
				actor=actor, expected_version=expected_version,
				**{
					name: value
					for name, value in given.items()
					if value is not subroutine.clients.base.UNSET
				},
			)

			return self._rendered_workspace(session, actor, changed)

	def delete_workspace (self, workspace: str) -> subroutine.views.Workspace:
		"""Move a workspace to the trash, in process."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			chosen = subroutine.domain.selection.workspace(
				session, actor, requested=workspace
			)
			removed = subroutine.domain.workspaces.delete(session, chosen, actor=actor)

			return self._rendered_workspace(session, actor, removed)

	def restore_workspace (self, workspace: str) -> subroutine.views.Workspace:
		"""Take a workspace back out of the trash, in process."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			# **The one caller that reaches into the trash**, through the helper named for
			# it. Everything else resolves among the live ones, so naming a deleted workspace
			# anywhere else is "no such workspace" rather than a surprise.
			chosen = subroutine.domain.workspaces.for_restore(session, actor, workspace)
			back = subroutine.domain.workspaces.restore(session, chosen, actor=actor)

			return self._rendered_workspace(session, actor, back)

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
		status: str | None = None,
		project: str | None = None,
		workspace: str | None = None,
		tags: typing.Sequence[str] | None = None,
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
				status_key=status,
				# The writer owns what they write, as `projects.create` does — and for a
				# document it is the attribution that makes §5.10's "what you concluded" mean
				# anything, since a conclusion with no author is a rumour.
				owner_id=actor.user.id,
				tags=tags,
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
		parent: int | None = None,
		description: str | None = None,
		reminder: int | str | None = None,
		ends: str | None = None,
		recurrence: str | None = None,
		recurrence_anchor: str | None = None,
		recurrence_trigger: str | None = None,
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
				# **Structured, because §6.13's `every …` span is reserved rather than read.**
				# Passed through only when given, for the reason `description` is: an override
				# of `None` would beat the parsed fields with nothing.
				**typing.cast(
					dict[str, typing.Any],
					{
						name: value
						for name, value in (
							("reminder", reminder),
							("ends", ends),
							("recurrence", recurrence),
							("recurrence_anchor", recurrence_anchor),
							("recurrence_trigger", recurrence_trigger),
						)
						if value is not None
					},
				),
				# **Resolved here, the way `api/tasks._resolve` resolves it** (`#510`), so a
				# parent the caller cannot see is *not found* on both transports rather than
				# quietly dropped on one. Passed only when given, for `description`'s reason.
				**typing.cast(
					dict[str, typing.Any],
					{}
					if parent is None
					else {
						"parent": self._in_the_trash_too(
							session, actor, parent, workspace, "task"
						)
					},
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

	def read_repeat (
		self,
		*,
		text: str,
		start: datetime.datetime | None = None,
		timezone: str | None = None,
	) -> subroutine.views.Reading:
		"""Say what a written repeat means, without storing anything.

		**Read-only, so it is not refused on a read-only connection.** It creates nothing and
		names nothing that exists: handing it a phrase and being told what the phrase means is
		the same kind of act as asking what a date expression resolves to.
		"""

		with self._opened() as (session, actor):
			zone = timezone or subroutine.domain.schedule.zone_for(
				user=actor.user,
				workspace=subroutine.domain.selection.workspace(
					session, actor, requested=None
				),
				instance=subroutine.domain.instances.get(session),
			)

		read = subroutine.domain.recurrence.rule(text, field="text")
		moment = start or subroutine.db.types.utcnow()

		return subroutine.views.Reading(
			rule=read.rule,
			description=subroutine.domain.recurrence.describe(read.rule),
			text=read.text,
			occurrences=subroutine.domain.recurrence.occurrences(
				read.rule,
				start=moment,
				timezone=zone,
				limit=subroutine.domain.recurrence.AHEAD,
			),
		)


	def occurrences (
		self,
		*,
		ref: int,
		until: str | None = None,
		limit: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Occurrences:
		"""Say when a repeating task comes round, without materialising anything.

		**Read-only**, and that is not a technicality: nothing is stored, so a read-only
		connection may ask — which is the property that makes `#915`'s *one real occurrence,
		the rest computed* worth anything to a calendar.
		"""

		with self._opened() as (session, actor):
			row = self._require(session, actor, ref, workspace)
			series = subroutine.domain.tasks.series_of(session, row) or row

			if series.recurrence_rule is None:
				raise subroutine.errors.NotFound(
					f"#{row.ref} does not repeat, so there is nothing to expand.",
					hint="Give it a repeat first — 'subroutine update "
					f"{row.ref} --repeat \"every month\"'.",
				)

			zone = subroutine.domain.schedule.zone_for(
				user=actor.user,
				workspace=subroutine.domain.selection.workspace(
					session, actor, requested=workspace
				),
				instance=subroutine.domain.instances.get(session),
			)
			wanted = subroutine.domain.recurrence.AHEAD if limit is None else limit
			# One more than asked for, so *there are no more* and *I stopped counting* are
			# told apart without a second pass — a rule with no end never runs out.
			found = subroutine.domain.recurrence.occurrences(
				series.recurrence_rule,
				start=subroutine.domain.tasks.series_start(series),
				timezone=zone,
				until=(
					None
					if until is None
					else subroutine.domain.schedule.interpret(
						until,
						boundary=subroutine.domain.schedule.Boundary.END,
						timezone=zone,
						now=subroutine.db.types.utcnow(),
						field="until",
					).instant
				),
				limit=wanted + 1,
			)

			return subroutine.views.Occurrences(
				rule=series.recurrence_rule,
				description=subroutine.domain.recurrence.describe(
					series.recurrence_rule, anchor=series.recurrence_anchor
				),
				occurrences=found[:wanted],
				has_more=len(found) > wanted,
			)


	def skip (
		self,
		*,
		ref: int,
		workspace: str | None = None,
	) -> subroutine.views.Task:
		"""Let one occurrence of a repeat go by, and bring the next one."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)

			subroutine.domain.tasks.skip(
				session, row, now=subroutine.db.types.utcnow(), actor=actor
			)

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
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

			written = subroutine.domain.comments.create(
				session,
				entity_type=entity_type,
				entity_id=subject,
				body=body,
				settings=self.settings,
				actor=actor,
			)

			return subroutine.views.comment(
				written,
				subroutine.views.Vocabulary(
					session,
					user_ids=[] if written.author_id is None else [written.author_id],
				),
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

	def move (
		self,
		*,
		ref: int,
		parent: int | None,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Put an item under another one, or at the top level."""

		self._refuse_if_read_only()

		with self._writing() as (session, actor):
			row = self._in_the_trash_too(session, actor, ref, workspace, entity_type)
			# Resolved the same way as the item being moved, so an unknown parent is refused
			# here exactly as the endpoint refuses it and one in a project the caller cannot
			# see is absent rather than forbidden (§7.3a).
			under = (
				None
				if parent is None
				else self._in_the_trash_too(session, actor, parent, workspace, entity_type)
			)

			if entity_type == "document":
				subroutine.domain.documents.move(session, row, parent=under, actor=actor)

				return subroutine.views.document(
					row, subroutine.views.Vocabulary.for_documents(session, [row])
				)

			subroutine.domain.tasks.move(session, row, parent=under, actor=actor)

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
			)

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
		reminder: int | str | None = subroutine.clients.base.UNSET,
		project: str = subroutine.clients.base.UNSET,
		assignee: str | None = subroutine.clients.base.UNSET,
		tags: typing.Sequence[str] | None = subroutine.clients.base.UNSET,
		due: str | None = subroutine.clients.base.UNSET,
		due_is_all_day: bool | None = subroutine.clients.base.UNSET,
		starts: str | None = subroutine.clients.base.UNSET,
		starts_is_all_day: bool | None = subroutine.clients.base.UNSET,
		ends: str | None = subroutine.clients.base.UNSET,
		snooze: str | None = subroutine.clients.base.UNSET,
		snoozed_is_all_day: bool | None = subroutine.clients.base.UNSET,
		recurrence: str | None = subroutine.clients.base.UNSET,
		recurrence_anchor: str | None = subroutine.clients.base.UNSET,
		recurrence_trigger: str | None = subroutine.clients.base.UNSET,
		timezone: str | None = subroutine.clients.base.UNSET,
		expected_version: int | None = None,
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
			"reminder": reminder,
			"tags": tags,
			"due": due,
			"due_is_all_day": due_is_all_day,
			# **The same six the HTTP client was missing.** Both transports dropped `starts`
			# and `snooze` silently after `#854` widened the signature, which is why they are
			# named together here: one omission in two places is this codebase's signature
			# defect, and a guard reading signatures could see neither.
			"starts": starts,
			"starts_is_all_day": starts_is_all_day,
			"ends": ends,
			"snooze": snooze,
			"snoozed_is_all_day": snoozed_is_all_day,
			"recurrence": recurrence,
			"recurrence_anchor": recurrence_anchor,
			"recurrence_trigger": recurrence_trigger,
			"timezone": timezone,
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

			# **The same resolution the endpoint makes, and workspace-scoped** (`#493`). A task
			# cannot be handed to somebody who is not a member here, so this is
			# `tasks.assignee_for` and *not* `selection.user`, which spans the instance because
			# a listing filter must. Same grammar, two questions.
			if assignee is not subroutine.clients.base.UNSET:
				changes["assignee_id"] = (
					None
					if assignee is None
					else subroutine.domain.tasks.assignee_for(
						session, row.workspace_id, assignee
					).id
				)

			subroutine.domain.tasks.update(
				session,
				row,
				now=subroutine.db.types.utcnow(),
				actor=actor,
				expected_version=expected_version,
				settings=self.settings,
				**changes,
			)

			return subroutine.views.task(
				row, subroutine.views.Vocabulary.for_tasks(session, [row])
			)

	def schedule (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		starts: datetime.datetime | datetime.date | None = subroutine.clients.base.UNSET,
		ends: datetime.datetime | datetime.date | None = subroutine.clients.base.UNSET,
		snooze: datetime.datetime | datetime.date | None = subroutine.clients.base.UNSET,
	) -> subroutine.views.Task:
		"""Set when a task begins, or the day it stops being hidden."""

		self._refuse_if_read_only()

		changes: dict[str, typing.Any] = {}

		if starts is not subroutine.clients.base.UNSET:
			changes["starts"] = starts

		if ends is not subroutine.clients.base.UNSET:
			changes["ends"] = ends

		if snooze is not subroutine.clients.base.UNSET:
			changes["snooze"] = snooze

		with self._writing() as (session, actor):
			row = self._require(session, actor, ref, workspace)

			subroutine.domain.tasks.update(
				session,
				row,
				now=subroutine.db.types.utcnow(),
				actor=actor,
				settings=self.settings,
				**changes,
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
		"""Refuse to read a database whose shape this build does not match (docs/design.md §12.4a).

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
		``subroutine agenda``, including every remote that answered perfectly. The local database
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

		# **Before the branch below, which would answer the wrong question about it** (`#927`
		# H-12). A racing writer's `UPDATE` matching no row is a `StaleDataError`, which is a
		# `SQLAlchemyError` — so without this it was reported as *could not be read*, sending
		# the reader to check the database is reachable about a database that answered
		# perfectly. The narrower case first, which is this file's own recorded rule.
		except subroutine.domain.versions.RACED:
			raise subroutine.domain.versions.raced() from None

		except sqlalchemy.exc.SQLAlchemyError as error:
			if self.settings.has_no_instance_yet():
				# The connection's label is already printed in front of this, so naming it again
				# would read "Local: local has no…". The generic message below does exactly
				# that and is left alone: it carries the driver's own words, which are worth
				# more than the tidiness.
				raise subroutine.errors.no_instance_yet() from None

			raise subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} could not be read: "
				f"{getattr(error, 'orig', None) or error}",
				hint="Check that the database is reachable, and check 'database_url' in "
				"'subroutine config show'.",
			) from None

	def _principal (
		self, session: sqlalchemy.orm.Session
	) -> subroutine.domain.authentication.Principal:
		"""Return who this process is acting as here.

		**The override is checked first and never falls through**, which is the whole of why
		it exists as a separate parameter rather than as a token this class would resolve.
		:func:`subroutine.domain.local.principal` answers §12.1a: with no token it identifies
		the sole account, or the configured one, because on a personal machine the filesystem
		permission *is* the authentication. On a served instance that reasoning is exactly
		wrong — an empty credential there must be a refusal, not the owner of the database —
		and a caller passing an empty token would have got the second behaviour while looking
		like it asked for the first. So the two paths cannot reach each other.
		"""

		if self._resolve_principal is not None:
			return self._resolve_principal(session)

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

		**What separates this from `_row` is the kind and the workspace, not the trash.** This
		takes either kind and resolves a workspace by name; `_row` is tasks only and takes an id
		already resolved. The sentence here used to say `_row` "deliberately excludes deleted
		tasks", which was true when it was written and stopped being true at `#140` — so the
		stated reason for this function existing had been false for longer than the function had
		been right. Found while fixing `#921` two screens down, by reading it.

		The HTTP side has always resolved through a statement that includes the trash — "a
		reference to something in the trash is more useful than a dangling one" — so that half
		is the two transports agreeing rather than a local liberty.

		**Recurrence templates deliberately stop here**, unlike in `_row` and `_subject`. Those
		two answer *what does this ref name*, which is a read, and `#921` is that a ref we
		publish must resolve. This one serves `restore`, `undiscard` and `move` — so widening it
		would quietly make a series a legal parent to move work under, which is a decision about
		the model rather than about reading, and nobody has taken it. A deleted template is
		reachable only through the API in any case: stopping a series *completes* the template
		rather than deleting it (§6.7).
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

		**That sentence was a claim rather than a fact until `#700`**, and the exception was the
		trash. ``api/tasks._resolve`` has included deleted rows since `#140` — "a reference to
		something in the trash is more useful than a dangling one" — and this did not, so one
		database answered two ways in the same second: over HTTP the comments came back, and
		locally the *item* was reported not to exist. That message is what a reader saw, so
		``subroutine show`` on something in the trash denied it existed while ``list --trash``
		listed it and ``restore`` worked on it.

		Worth seeing rather than fixing quietly: the divergence was not in what either client
		*returns* — ``task()`` agrees on both, deleted or not — but in what one of them **asks
		for** one layer down, on the lookup that fetches an item's comments.

		**And it recurred with recurrence templates** (`#921`), which is why the exception is
		worth reading as a shape rather than as the trash: ``_resolve`` widens on *three* axes
		and this widened on two, so the third produced the identical symptom a release later.
		The general rule is the one the paragraph above states — a lookup by a ref answers what
		the ref names, and only *unreadable* is allowed to make it answer nothing.
		"""

		model: typing.Any = (
			subroutine.db.models.work.Task
			if entity_type == "task"
			else subroutine.db.models.work.Document
		)
		statement = (
			subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=[workspace_id],
				include_completed=True,
				include_archived=True,
				include_deleted=True,
				include_templates=True,
			)
			if entity_type == "task"
			else subroutine.domain.scoping.readable_documents(
				actor, workspace_ids=[workspace_id], include_archived=True, include_deleted=True
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

		**And recurrence templates, which was the same divergence one flag along** (`#921`).
		Every occurrence publishes ``recurrence_template_ref``, and ``views._from_a_live_series``
		says of it that it *"deliberately still resolves … and is how a client reaches the
		history"* — a sentence nothing here implemented. So ``show 2 --json`` handed back
		``recurrence_template_ref: 1`` and ``show 1`` answered *"There is no #1"*, while
		``GET /v1/tasks/1`` answered 200 with the series.

		**A listing still hides them and must** — a rule is not work (§6.7) — but a *lookup by a
		ref we published* is the opposite question, and answering it is what ``include_templates``
		is for. It narrows nothing: the workspace, project visibility and the credential's project
		scope are all still applied by the helper, so this reaches exactly what the HTTP caller
		already reaches.
		"""

		model = subroutine.db.models.work.Task

		return session.scalars(
			subroutine.domain.scoping.readable_tasks(
				actor,
				workspace_ids=[workspace_id],
				include_archived=True,
				include_deleted=True,
				include_templates=True,
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
