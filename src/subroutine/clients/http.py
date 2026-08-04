"""A connection to another instance, over HTTP.

The other half of SPEC.md §13.7. Everything it does, :mod:`subroutine.clients.local` does
against this installation's own database, and the two return the same objects — which is the
requirement, not a coincidence, and is what ``tests/test_transport_equivalence.py`` asserts.

**A remote refusal arrives as the exception a local one would have raised.** The problem
document is read back through :func:`subroutine.errors.from_problem`, so a client fanning out
across a local database and a remote server has one vocabulary of failure rather than two.
Without that, every message it prints would have to say which *kind* of failure it was before
saying what went wrong.

**A transport failure is not a refusal, and is reported as its own thing.** The work VPN is
off, the laptop is on a train, the server is restarting: none of that means the request was
wrong, and §13.7 requires that none of it stops a person seeing their own list. So it becomes
a ``service_unavailable`` naming the connection, which the fan-out layer prints beside the
results it did get.
"""

import datetime
import types
import typing

import httpx
import pydantic

import subroutine.clients.base
import subroutine.connections
import subroutine.credentials
import subroutine.db.types
import subroutine.domain.capture
import subroutine.domain.readiness
import subroutine.errors
import subroutine.views

#: Any view model this client parses a response into.
Parsed = typing.TypeVar("Parsed", bound=pydantic.BaseModel)

#: What a problem document is served as. Anything else with a failing status is a proxy, a
#: load balancer or a captive portal answering instead of the instance — worth saying so,
#: because "not found" from nginx and "not found" from Subroutine mean very different things.
PROBLEM_MEDIA_TYPE = "application/problem+json"


class Client:
	"""Another instance, reached over HTTP."""

	def __init__ (
		self,
		connection: subroutine.connections.Connection,
		*,
		token: str,
		transport: httpx.BaseTransport | None = None,
		base_url: str | None = None,
	) -> None:
		"""Open a connection to a remote instance.

		``transport`` and ``base_url`` are for tests, which drive the application in-process
		through an ASGI transport rather than over a socket. That is not a shortcut: it is
		the only way the equivalence test can run both transports against *the same
		database*, which is what makes a difference between them a difference in the code
		rather than in the fixtures.
		"""

		self.connection = connection
		self._client = httpx.Client(
			base_url=base_url or typing.cast(str, connection.url),
			timeout=connection.timeout_seconds,
			transport=transport,
			headers={
				# Never in a query string (§7.4): a URL lands in access logs, in
				# `Referer` headers and in browser history, and a token that has been
				# logged is a token that has been shared.
				"Authorization": f"Bearer {token}",
				"Accept": "application/json",
				"User-Agent": f"subroutine/{subroutine.API_VERSION}",
			},
		)

	# --- The protocol ------------------------------------------------------------------

	def identity (self) -> subroutine.clients.base.Identity:
		"""Report which instance this is and which workspaces the credential reaches.

		``/v1/meta`` rather than ``/v1/me``, because meta answers both halves in one request
		and is the one endpoint documented not to refuse an ambiguous workspace — a client's
		first call is often this one, before it knows what workspaces there are.
		"""

		body = self._json("GET", "/v1/meta")

		# `workspaces` is *required*, not defaulted. Reading a missing key as an empty list
		# turned a wrong-shaped 200 — a captive portal, a JSON-serving proxy, a typo'd url —
		# into a reachable, un-initialised, empty instance: no failure line, no duplicate-id
		# refusal, and a listing that was silently short. §13.7's contract is that a partial
		# view announces itself.
		if "workspaces" not in body:
			raise self._not_an_instance("its /v1/meta response has no 'workspaces'")

		instance = body.get("instance")

		return subroutine.clients.base.Identity(
			instance=(
				None if instance is None else self._parsed(subroutine.views.Instance, instance)
			),
			workspaces=tuple(
				self._parsed(subroutine.views.WorkspaceRef, item)
				for item in body["workspaces"]
			),
		)

	def me (self) -> subroutine.views.Me:
		"""Report who this instance thinks the caller is, and what they may do (`#336`)."""

		return self._parsed(subroutine.views.Me, self._json("GET", "/v1/me"))

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

		body = self._json(
			"GET",
			"/v1/agenda",
			params=_given(
				date=None if date is None else date.isoformat(),
				timezone=timezone,
				horizon_days=horizon_days,
				unscheduled_limit=unscheduled_limit,
				workspace_id=workspace,
			),
		)

		return self._parsed(subroutine.views.Agenda, body)

	def count_tasks (
		self, *, workspace: str | None = None, project: str | None = None
	) -> int:
		"""Return how many tasks a project holds, completed ones included (`#296`)."""

		body = self._json(
			"GET",
			"/v1/tasks",
			params=_given(
				workspace_id=workspace,
				project=project,
				include_completed="true",
				include_total="true",
				# One row rather than none: `ge=1` is not declared on the endpoint, but a page
				# of zero is a request for nothing and the total is what is being asked for.
				limit=1,
			),
		)
		total = body.get("page", {}).get("total")

		if not isinstance(total, int):
			raise self._not_an_instance("its /v1/tasks response carried no total when asked")

		return total

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

		body = self._json(
			"GET",
			"/v1/tasks",
			params=_given(
				workspace_id=workspace,
				limit=limit,
				include_completed="true" if include_completed else None,
				order=order,
				project=project,
				# Refused here as well as at the far end, so a typo costs no round trip and
				# is named the same way whether or not a server was reachable.
				deferred=(
					None
					if deferred == subroutine.domain.readiness.DEFAULT_DEFERRAL
					else subroutine.domain.readiness.refuse_unknown_deferral(deferred)
				),
				q=q,
				ready="true" if ready else None,
				deleted="true" if deleted else None,
				parent=parent,
			),
		)

		if "items" not in body:
			raise self._not_an_instance("its /v1/tasks response has no 'items'")

		return [self._parsed(subroutine.views.Task, item) for item in body["items"]]

	def task (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task | None:
		"""Return one task by ref, or ``None`` if there is no such task here."""

		response = self._call(
			"GET", f"/v1/tasks/{ref}", params=_given(workspace_id=workspace)
		)

		# A 404 is the expected answer, not a failure: resolving an address across several
		# connections asks this of all of them and expects most to say no.
		if response.status_code == 404:
			return None

		return self._parsed(subroutine.views.Task, self._read(response))

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

		body = self._json(
			"GET",
			"/v1/documents",
			params=_given(
				workspace_id=workspace,
				limit=limit,
				order=order,
				project=project,
				q=q,
				deleted="true" if deleted else None,
			),
		)

		return self._collected(subroutine.views.Document, body, endpoint="documents")

	def document (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Document | None:
		"""Return one document by ref, or ``None`` if there is no such document here."""

		response = self._call(
			"GET", f"/v1/documents/{ref}", params=_given(workspace_id=workspace)
		)

		if response.status_code == 404:
			return None

		return self._parsed(subroutine.views.Document, self._read(response))

	def links (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Link]:
		"""Return every link touching one item, labelled from that item's point of view."""

		body = self._json(
			"GET",
			f"/v1/{_plural(entity_type)}/{ref}/links",
			params=_given(workspace_id=workspace),
		)

		return self._collected(subroutine.views.Link, body, endpoint="links")

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

		body = self._json(
			"POST",
			f"/v1/{_plural(entity_type)}/{ref}/links",
			params=_given(workspace_id=workspace),
			json={"target": target, "link_type": link_type, "target_type": target_type},
		)

		return self._parsed(subroutine.views.Link, body)

	def unlink (
		self, *, ref: int, link_id: str, entity_type: str = "task", workspace: str | None = None
	) -> None:
		"""Withdraw a link."""

		self._refuse_if_read_only()

		self._json(
			"DELETE",
			f"/v1/{_plural(entity_type)}/{ref}/links/{link_id}",
			params=_given(workspace_id=workspace),
		)

	def uncomment (
		self,
		*,
		ref: int,
		comment_id: str,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> None:
		"""Withdraw a comment from an item's record.

		**The item is checked here rather than by the route**, and that is the one thing this
		method does beyond a single request. ``DELETE /v1/comments/{id}`` addresses a comment
		by its own id, so unlike ``unlink`` — whose ref is in the path — the server cannot
		refuse a caller naming the wrong item. The local client narrows in SQL; without the
		lookup below the two transports would enforce different things, which is the
		divergence ``views.py`` sits outside ``api/`` to prevent.

		A round trip on a rare operation, for a refusal that reads the same either way.
		"""

		self._refuse_if_read_only()

		recorded = self.comments(ref=ref, entity_type=entity_type, workspace=workspace)

		if not any(str(one.id) == comment_id for one in recorded):
			raise subroutine.errors.NotFound(
				"There is no such comment on that item.",
				hint=f"Run 'subroutine show {ref}' to see what is recorded against it.",
			)

		self._json("DELETE", f"/v1/comments/{comment_id}")

	def comments (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Comment]:
		"""Return one item's record of what happened, oldest first."""

		body = self._json(
			"GET",
			f"/v1/{_plural(entity_type)}/{ref}/comments",
			params=_given(workspace_id=workspace),
		)

		return self._collected(subroutine.views.Comment, body, endpoint="comments")

	def history (
		self,
		*,
		ref: int,
		entity_type: str = "task",
		workspace: str | None = None,
		limit: int | None = None,
	) -> list[subroutine.views.Event]:
		"""Return what has happened to one item, newest first."""

		body = self._json(
			"GET",
			f"/v1/{_plural(entity_type)}/{ref}/events",
			params=_given(workspace_id=workspace, limit=limit),
		)

		return self._collected(subroutine.views.Event, body, endpoint="events")

	def changes (
		self,
		*,
		since: int | None = None,
		mine: bool = False,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> list[subroutine.views.Event]:
		"""Return what has changed, oldest first, across everything this credential can see."""

		body = self._json(
			"GET",
			"/v1/changes",
			params=_given(
				since=since,
				# The endpoint takes a word rather than a flag, so a later `?actor=<username>`
				# needs no second parameter and no deprecation.
				actor="me" if mine else None,
				newest=True if newest else None,
				workspace_id=workspace,
				limit=limit,
			),
		)

		return self._collected(subroutine.views.Event, body, endpoint="changes")

	def projects (
		self, *, workspace: str | None = None, limit: int | None = None
	) -> list[subroutine.views.Project]:
		"""List the projects this credential can see, parents before children."""

		body = self._json(
			"GET", "/v1/projects", params=_given(workspace_id=workspace, limit=limit)
		)

		return self._collected(subroutine.views.Project, body, endpoint="projects")

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

		# **`visibility` is passed rather than left to the server's default**, unlike the
		# nullable fields around it. `_given` drops a `None`, and a default is a decision made
		# in two places the moment one of them changes — the whole reason both transports go
		# through one protocol.
		body = self._json(
			"POST",
			"/v1/projects",
			json={
				**_given(
					key=key,
					title=title,
					description=description,
					parent=parent,
					workspace_id=workspace,
				),
				"visibility": visibility,
			},
		)

		return subroutine.views.Project.model_validate(body)

	def tokens (self) -> list[subroutine.views.Token]:
		"""List the credentials this caller may act on, newest first (`#348`)."""

		return self._collected(
			subroutine.views.Token, self._json("GET", "/v1/tokens"), endpoint="tokens"
		)

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

		return self._parsed(
			subroutine.views.IssuedToken,
			self._json(
				"POST",
				"/v1/tokens",
				json=_given(
					title=title,
					username=username,
					service_account=service_account,
					workspace=workspace,
					# **Sent only when they narrow something.** `[]` and null both mean "no
					# narrowing" for scopes, and an empty `project_scope` is refused outright
					# rather than guessed at — so a client that sent either would be asking a
					# question the caller did not ask.
					scopes=list(scopes) or None,
					project_scope=None if projects is None else list(projects),
					project_write_scope=None if writes is None else list(writes),
					expires=expires,
				),
			),
		)

	def revoke_token (self, *, id_or_prefix: str) -> subroutine.views.Token:
		"""Stop a credential working, now (`#348`)."""

		return self._parsed(
			subroutine.views.Token, self._json("DELETE", f"/v1/tokens/{id_or_prefix}")
		)

	def users (self) -> list[subroutine.views.User]:
		"""List the accounts on this instance."""

		body = self._json("GET", "/v1/users")

		return [
			subroutine.views.User.model_validate(row) for row in body.get("items", [])
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

		body = self._json(
			"POST",
			"/v1/users",
			json={
				**_given(
					username=username,
					display_name=display_name,
					email=email,
					timezone=timezone,
				),
				# Passed rather than left to the server's default, for the reason
				# `create_project` gives about `visibility`: `_given` drops a false, and a
				# default in two places is two places to change.
				"is_service_account": is_service_account,
			},
		)

		return subroutine.views.User.model_validate(body)

	def members (self, *, workspace: str | None = None) -> list[subroutine.views.Member]:
		"""List who belongs to one workspace."""

		body = self._json("GET", f"/v1/workspaces/{self._workspace(workspace)}/members")

		return [
			subroutine.views.Member.model_validate(row) for row in body.get("items", [])
		]

	def add_member (
		self, *, username: str, role: str, workspace: str | None = None
	) -> subroutine.views.Member:
		"""Give somebody a role in a workspace."""

		self._refuse_if_read_only()

		body = self._json(
			"POST",
			f"/v1/workspaces/{self._workspace(workspace)}/members",
			json={"username": username, "role": role},
		)

		return subroutine.views.Member.model_validate(body)

	def remove_member (self, *, username: str, workspace: str | None = None) -> None:
		"""Take somebody out of a workspace."""

		self._refuse_if_read_only()

		self._json(
			"DELETE",
			f"/v1/workspaces/{self._workspace(workspace)}/members/{username}",
		)

	def rename_project (
		self, project: str, *, key: str, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Give a project a different short name."""

		self._refuse_if_read_only()

		body = self._json(
			"PATCH",
			f"/v1/projects/{project}",
			json={"key": key},
			params=_given(workspace_id=workspace),
		)

		return subroutine.views.Project.model_validate(body)

	def create_workspace (
		self, *, slug: str, title: str, timezone: str | None = None
	) -> subroutine.views.Workspace:
		"""Make another workspace, over the wire."""

		self._refuse_if_read_only()

		body = self._json(
			"POST",
			"/v1/workspaces",
			json=_given(slug=slug, title=title, timezone=timezone),
		)

		return subroutine.views.Workspace.model_validate(body)

	def rename_workspace (self, workspace: str, *, slug: str) -> subroutine.views.Workspace:
		"""Give a workspace a different short name."""

		self._refuse_if_read_only()

		body = self._json("PATCH", f"/v1/workspaces/{workspace}", json={"slug": slug})

		return subroutine.views.Workspace.model_validate(body)

	def move_project (
		self, project: str, *, parent: str | None, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Reparent a project, taking everything under it."""

		self._refuse_if_read_only()

		body = self._json(
			"POST",
			f"/v1/projects/{project}/move",
			# **Sent whatever it is, including null.** `_given` drops a None, which is right
			# for a filter and wrong for a field whose null is an instruction — the endpoint
			# refuses a body that names no parent at all, precisely so that "move to root"
			# has to be said rather than implied.
			json={"parent": parent},
			params=_given(workspace_id=workspace),
		)

		return subroutine.views.Project.model_validate(body)

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

		answered = self._json(
			"POST",
			"/v1/documents",
			json=_given(
				title=title, body=body, type=type, project=project, workspace_id=workspace
			),
		)

		return subroutine.views.Document.model_validate(answered)

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
		"""Create a task from a line of text.

		**What the grammar declined to read is worked out here rather than reported by the
		server**, because ``POST /v1/tasks`` returns the task and nothing else — a bare entity
		is §8.4's rule and a create response is not the place to break it. The capture grammar
		is pure text processing with no database behind it, so parsing the same line locally
		gives the same answer.

		The assumption that makes that safe is that both ends run the same grammar, and it is
		an assumption rather than a fact. ``/v1/meta`` publishes the grammar for exactly this
		reason; comparing them is filed in Appendix A rather than done here, because the only
		thing at stake is one advisory line and the alternative is a second round trip on
		every ``subroutine add``.
		"""

		self._refuse_if_read_only()

		body = self._json(
			"POST",
			"/v1/tasks",
			json=_given(
				text=text,
				workspace_id=workspace,
				timezone=timezone,
				type=type,
				# **The endpoint has taken this beside `text` since M1** (`#424`). Nothing new
				# is being asked of the server; what was missing was any way to say it from
				# here, which is why no test of the route could have found it.
				description=description,
				# **Only when the line did not say.** A `+KEY` in the text is somebody being
				# explicit about this item and must beat a default that came from a file three
				# directories up, which they may not have known was there.
				project=None if subroutine.domain.capture.names_a_project(text) else project,
			),
		)

		# **One parse, two uses.** Both of these are answers about the *line*, not about the
		# task the server made, so they come from the same local `Capture` — and parsing twice
		# would be two chances to disagree with each other about one sentence.
		read = subroutine.domain.capture.parse(text, now=subroutine.db.types.utcnow())

		return subroutine.clients.base.Captured(
			task=self._parsed(subroutine.views.Task, body),
			unparsed=read.unparsed,
			summary=subroutine.domain.capture.summarise(read),
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

		answered = self._json(
			"POST",
			f"/v1/{_plural(entity_type)}/{ref}/comments",
			params=_given(workspace_id=workspace),
			json={"body": body},
		)

		return self._parsed(subroutine.views.Comment, answered)

	def discard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Move an item to the trash."""

		self._refuse_if_read_only()

		body = self._json(
			"DELETE",
			f"/v1/{_plural(entity_type)}/{ref}",
			params=_given(workspace_id=workspace),
		)

		return self._as_item(entity_type, body)

	def undiscard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Take an item back out of the trash."""

		self._refuse_if_read_only()

		body = self._json(
			"POST",
			f"/v1/{_plural(entity_type)}/{ref}/restore",
			params=_given(workspace_id=workspace),
		)

		return self._as_item(entity_type, body)

	def _as_item (
		self, entity_type: str, body: typing.Any
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Parse a response as whichever kind the caller named."""

		if entity_type == "document":
			return self._parsed(subroutine.views.Document, body)

		return self._parsed(subroutine.views.Task, body)

	def claim (
		self, *, ref: int, minutes: int | None = None, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Take a lease on a task, or renew one this credential holds (`#350`)."""

		return self._parsed(
			subroutine.views.Task,
			self._json(
				"POST",
				f"/v1/tasks/{ref}/claim",
				params=_given(minutes=minutes, workspace_id=workspace),
			),
		)

	def release (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Give a task back, so somebody else can take it (`#350`)."""

		return self._parsed(
			subroutine.views.Task,
			self._json(
				"POST", f"/v1/tasks/{ref}/release", params=_given(workspace_id=workspace)
			),
		)

	def complete (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Mark a task finished."""

		self._refuse_if_read_only()

		body = self._json(
			"POST", f"/v1/tasks/{ref}/complete", params=_given(workspace_id=workspace)
		)

		return self._parsed(subroutine.views.Task, body)

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
		"""Change a task's own fields, over the wire.

		The body is built by comparison against ``UNSET`` rather than by dropping empty
		values, because ``None`` is a meaningful value: §8.3 makes it the way to *clear* a
		field, so a filter that removed it would turn "unset the estimate" into "change
		nothing" — silently, and with a 200 to say so. ``_given`` cannot be used here for
		exactly that reason; it drops nulls, which is right for a query string and wrong for
		a PATCH body.
		"""

		self._refuse_if_read_only()

		given = {
			"title": title,
			"description": description,
			"status": status,
			"type": type,
			"importance": importance,
			"urgency": urgency,
			"estimate": estimate,
			"project": project,
		}
		body = self._json(
			"PATCH",
			f"/v1/tasks/{ref}",
			params=_given(workspace_id=workspace),
			json={
				name: value
				for name, value in given.items()
				if value is not subroutine.clients.base.UNSET
			},
		)

		return self._parsed(subroutine.views.Task, body)

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
		"""Revise a document, over the wire.

		Built against ``UNSET`` for the reason ``update`` gives above, and it matters more
		here: ``body=None`` is how §8.3 clears a document's body, and a filter that dropped
		nulls would answer "empty this" with a 200 and no change.
		"""

		self._refuse_if_read_only()

		given = {
			"title": title,
			"body": body,
			"type": type,
			"status": status,
			"project": project,
		}
		answered = self._json(
			"PATCH",
			f"/v1/documents/{ref}",
			params=_given(workspace_id=workspace),
			json={
				name: value
				for name, value in given.items()
				if value is not subroutine.clients.base.UNSET
			},
		)

		return self._parsed(subroutine.views.Document, answered)

	def schedule (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		planned_for: datetime.date | None = subroutine.clients.base.UNSET,
		start: datetime.date | None = subroutine.clients.base.UNSET,
	) -> subroutine.views.Task:
		"""Set the day a task is planned for, or the day it becomes visible.

		A field left out is unchanged and a field sent as null is cleared (§8.3), which is
		exactly what ``UNSET`` and ``None`` mean here — so the two map onto each other without
		anything being invented in between.
		"""

		self._refuse_if_read_only()

		changes: dict[str, typing.Any] = {}

		if planned_for is not subroutine.clients.base.UNSET:
			changes["planned_for"] = None if planned_for is None else planned_for.isoformat()

		if start is not subroutine.clients.base.UNSET:
			changes["start"] = None if start is None else start.isoformat()

		body = self._json(
			"PATCH",
			f"/v1/tasks/{ref}",
			params=_given(workspace_id=workspace),
			json=changes,
		)

		return self._parsed(subroutine.views.Task, body)

	def close (self) -> None:
		"""Give back the pooled connections."""

		self._client.close()

	def __enter__ (self) -> "Client":
		"""Return this client, ready to use."""

		return self

	def __exit__ (
		self,
		kind: type[BaseException] | None,
		value: BaseException | None,
		traceback: types.TracebackType | None,
	) -> None:
		"""Give back the pooled connections."""

		self.close()

	# --- Inside ------------------------------------------------------------------------

	def _parsed (self, model: type[Parsed], body: typing.Any) -> Parsed:
		"""Read one object into a view model, or say the instance answered something else.

		**Every parse on this client goes through here, and that is the point.**
		``model_validate`` raises :class:`pydantic.ValidationError`, which is not a
		:class:`~subroutine.errors.SubroutineError` — so it escaped ``fanout._attempt`` (which
		catches only those, deliberately) and took down the *whole* fan-out. One instance on a
		mismatched version, one typo'd url or one JSON-serving proxy replaced a person's entire
		agenda, including the local half sitting on the same machine, with a traceback.

		Reported as ``service_unavailable`` rather than as a bad request, because that is the
		truth: the request was fine and the thing that answered is not an instance this client
		can talk to.
		"""

		try:
			return model.model_validate(body)

		except pydantic.ValidationError as error:
			# The first error names the field, which is the useful half. The whole report is
			# hundreds of lines for a wrong-shaped response and would bury the connection name.
			reported = error.errors()
			where = (
				".".join(str(part) for part in reported[0]["loc"]) if reported else ""
			) or "the body"
			why = reported[0]["msg"] if reported else "unexpected shape"

			raise self._not_an_instance(
				f"{model.__name__} could not be read from its response ({where}: {why})"
			) from None

	def _collected (
		self, model: type[Parsed], body: typing.Any, *, endpoint: str
	) -> list[Parsed]:
		"""Read an enveloped collection into view models.

		Insists on the envelope rather than tolerating a bare array, even though one endpoint
		used to send one. Accepting both shapes would make this client the place where the
		§8.4 rule quietly stopped being true, and the next endpoint to forget it would be
		found by somebody else's client rather than by ours.
		"""

		if not isinstance(body, dict) or "items" not in body:
			raise self._not_an_instance(f"its /v1/…/{endpoint} response has no 'items'")

		return [self._parsed(model, item) for item in body["items"]]

	def _not_an_instance (self, because: str) -> subroutine.errors.SubroutineError:
		"""Return the failure for a server that answered, but not as an instance."""

		return subroutine.errors.ServiceUnavailable(
			f"{self.connection.name} answered, but not as a Subroutine instance: {because}.",
			hint=f"Check what is serving {self.connection.url} — a proxy, a captive portal or "
			"an instance on a different API version will answer like this.",
		)

	def _workspace (self, given: str | None) -> str:
		"""Return the workspace to address, refusing to guess when none was named.

		**The one place the two transports genuinely cannot behave the same way.** Everywhere
		else a workspace is a *parameter* the server resolves through
		``domain.selection.workspace``, which knows the caller's context and their single
		workspace; here it is a path segment, and there is nothing to resolve it against
		before the request is made.

		So it refuses rather than picking one. Guessing would put a membership change — the act
		that decides who can see a private project — in a workspace nobody named, and the
		mistake would be invisible until somebody read a listing they should not have. Callers
		pass the slug they already have: the CLI resolves it from the current context first.
		"""

		if given and given.strip():
			return given.strip()

		raise subroutine.errors.ValidationError(
			"Which workspace? Membership belongs to one, so it has to be named.",
			hint=(
				"Pass --workspace, or run 'subroutine use <workspace>' to set a current one."
			),
		)

	def _refuse_if_read_only (self) -> None:
		"""Refuse a write to a connection configured read-only, before the request leaves.

		**This is the transport the setting exists for**, and it had no check at all until
		2026-07-30 — `read_only = true` on an employer's instance accepted `subroutine add`,
		while the local connection, where the setting is nearly pointless, enforced it. §13.7
		calls this a client-side control precisely because the company's server cannot be asked
		to arrange it on the agent-owner's behalf, so a client that does not enforce it is the
		whole feature missing.
		"""

		if self.connection.read_only:
			subroutine.clients.base.refuse_a_write(self.connection)

	def _json (self, method: str, path: str, **options: typing.Any) -> dict[str, typing.Any]:
		"""Make one request and return the object it answered with."""

		return self._read(self._call(method, path, **options))

	def _call (self, method: str, path: str, **options: typing.Any) -> httpx.Response:
		"""Make one request, turning a transport failure into a sentence."""

		try:
			return self._client.request(method, path, **options)

		except httpx.TimeoutException:
			raise subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} did not answer within "
				f"{self.connection.timeout_seconds:g} seconds.",
				hint=f"Raise 'timeout_seconds' on [connections.{self.connection.name}] if "
				"that instance is simply slow.",
			) from None

		except httpx.HTTPError as error:
			raise subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} could not be reached at {self.connection.url}: "
				f"{error}",
				hint="Check that the instance is running and that you are on a network that "
				"can reach it.",
			) from None

	def _read (self, response: httpx.Response) -> dict[str, typing.Any]:
		"""Return one response's object, or raise the failure it describes."""

		if response.is_success:
			# **204 is a success with nothing to read**, and until `#141` nothing here had ever
			# met one: every endpoint a client called returned its entity. Withdrawing a link
			# is the first that does not, and without this the client would have reported a
			# successful delete as "answered 204 with something that is not a JSON object" —
			# a message about a proxy, on the one path where nothing was wrong.
			if response.status_code == 204 or not response.content:
				return {}

			body = _parsed(response)

			if body is None:
				raise subroutine.errors.ServiceUnavailable(
					f"{self.connection.name} answered {response.status_code} with something "
					"that is not a JSON object.",
					hint=f"Check that {self.connection.url} is a Subroutine instance and not "
					"a proxy or a login page.",
				)

			return body

		document = _parsed(response)
		media = response.headers.get("content-type", "").split(";")[0].strip()

		if document is None or media != PROBLEM_MEDIA_TYPE:
			# Something in the path answered instead of the instance. Saying so is the
			# difference between "your token expired" and "your VPN is redirecting you".
			raise subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} answered {response.status_code}, and not in this "
				"program's error format.",
				hint=f"Check what is serving {self.connection.url} — a proxy or a captive "
				"portal will answer like this.",
			)

		raise subroutine.errors.from_problem(document, status=response.status_code)


def _parsed (response: httpx.Response) -> dict[str, typing.Any] | None:
	"""Return a response body as an object, or ``None`` if it is not one."""

	try:
		body = response.json()

	except ValueError:
		return None

	return body if isinstance(body, dict) else None


def _plural (entity_type: str) -> str:
	"""Return the path segment an entity type is addressed under.

	Three names rather than a rule, because the segment is a *route* and a route is not
	derivable from a noun: adding an ``s`` would invent ``/v1/verifications`` for something
	that has no endpoint, and would do it silently.
	"""

	segments = {"task": "tasks", "document": "documents", "project": "projects"}

	if entity_type not in segments:
		raise subroutine.errors.ValidationError(
			f"{entity_type!r} is not something this client can address.",
			errors=[
				subroutine.errors.FieldError(
					field="entity_type",
					code="invalid_field_value",
					message=f"Expected one of: {', '.join(segments)}.",
				)
			],
		)

	return segments[entity_type]


def _given (**values: typing.Any) -> dict[str, typing.Any]:
	"""Drop the parameters that were not supplied.

	``None`` means "not asked for" here, never "clear it" — every caller that needs to *send*
	a null builds its own body, because a helper that could not tell those apart is exactly
	how §8.3's distinction gets lost.
	"""

	return {name: value for name, value in values.items() if value is not None}


def opened (
	connection: subroutine.connections.Connection,
	*,
	default_connection: str,
	transport: httpx.BaseTransport | None = None,
	base_url: str | None = None,
) -> Client:
	"""Build an HTTP client, refusing plainly when there is no token for it."""

	resolved = subroutine.credentials.resolve(
		connection, default_connection=default_connection
	)

	if resolved.token is None:
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} has no token, so there is no way to identify "
			"you to it.",
			hint=f"Put one in {subroutine.credentials.credentials_file_path()} under "
			f"[{connection.name}], or export "
			f"{subroutine.credentials.variable_for(connection.name)}.",
		)

	return Client(
		connection, token=resolved.token, transport=transport, base_url=base_url
	)
