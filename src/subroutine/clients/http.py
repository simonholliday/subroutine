"""A connection to another instance, over HTTP.

The other half of docs/design.md §13.7. Everything it does, :mod:`subroutine.clients.local` does
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
import subroutine.installations
import subroutine.views

#: Any view model this client parses a response into.
Parsed = typing.TypeVar("Parsed", bound=pydantic.BaseModel)

#: How the next page of a collection is asked for, and there are exactly two.
#:
#: **A listing hands back an opaque ``next_cursor``**, which §8.4 makes a keyset cursor the
#: caller neither reads nor constructs.
#:
#: **The change feed's cursor is ``seq``**, published on every row, because §5.11 makes it a
#: number a client persists between polls and reasons about. So it has no ``next_cursor`` to
#: hand back and never should: offering one would be a second way to page a feed that already
#: says where it got to, and the two would be free to disagree about which end of a page they
#: name.
BY_CURSOR = "cursor"
BY_SEQ = "since"

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

		# **What this instance last said it was running** — `#250`. Recorded as responses go
		# past rather than fetched, because the call that needs it is the one that just failed:
		# a body this client could not read is still a body that says which release wrote it.
		self._instance_version: str | None = None

	# --- The protocol ------------------------------------------------------------------

	def reference (self, name: str) -> str:
		"""Return one of the instance's reference documents, as text."""

		return self._text("GET", f"/v1/docs/{name}")

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

		answer = self._call(
			verb,
			subroutine.clients.base.require_a_route(path),
			params=query,
			json=body,
		)

		return subroutine.clients.base.Answered(answer.status_code, answer.text)

	def meta (self, *, workspace: str | None = None) -> subroutine.views.Meta:
		"""Report what this installation calls things — `#486`."""

		return self._parsed(
			subroutine.views.Meta,
			self._json("GET", "/v1/meta", params=_given(workspace_id=workspace)),
		)

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
		date: datetime.date | str | None = None,
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
				date=_written(date),
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
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		due_before: datetime.datetime | None = None,
		due_after: datetime.datetime | None = None,
		filters: dict[str, str] | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Task]:
		"""List one workspace's tasks, newest first unless ``order`` says otherwise."""

		asking = _dated(
			filters,
			_given(
				workspace_id=workspace,
				limit=limit,
				# **Three-valued on the wire, not two.** Sending nothing for `False` would make
				# "no finished work" and "did not say" the same request, and `#710`'s refusal of
				# the contradiction with a finished `status_category` could never fire remotely.
				include_completed=(
					None if include_completed is None else str(include_completed).lower()
				),
				order=order,
				project=project,
				# Sent as written and resolved at the far end (`#501`). A username looked up
				# here would be a second copy of the rule and a second refusal to keep in step
				# with the local client's.
				assignee=assignee,
				status=status,
				status_category=status_category,
				type=type,
				due_before=None if due_before is None else due_before.isoformat(),
				due_after=None if due_after is None else due_after.isoformat(),
				subtree="true" if subtree else None,
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

		return self._collected(
			subroutine.views.Task,
			self._json("GET", "/v1/tasks", params=asking),
			endpoint="tasks",
			path="/v1/tasks",
			params=asking,
			wanted=limit,
		)

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
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		filters: dict[str, str] | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Document]:
		"""List one workspace's documents, newest first unless ``order`` says otherwise."""

		asking = _dated(
			filters,
			_given(
				workspace_id=workspace,
				limit=limit,
				order=order,
				project=project,
				q=q,
				deleted="true" if deleted else None,
				status=status,
				status_category=status_category,
				type=type,
			),
		)

		return self._collected(
			subroutine.views.Document,
			self._json("GET", "/v1/documents", params=asking),
			endpoint="documents",
			path="/v1/documents",
			params=asking,
			wanted=limit,
		)

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

	def backlinks (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Backlink]:
		"""Return everything whose prose refers to one item."""

		body = self._json(
			"GET",
			f"/v1/{_plural(entity_type)}/{ref}/backlinks",
			params=_given(workspace_id=workspace),
		)

		return self._collected(subroutine.views.Backlink, body, endpoint="backlinks")

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
	) -> subroutine.clients.base.Listing[subroutine.views.Event]:
		"""Return what has happened to one item, newest first."""

		asking = _given(workspace_id=workspace, limit=limit)

		return self._collected(
			subroutine.views.Event,
			self._json("GET", f"/v1/{_plural(entity_type)}/{ref}/events", params=asking),
			endpoint="events",
			path=f"/v1/{_plural(entity_type)}/{ref}/events",
			params=asking,
			wanted=limit,
		)

	def changes (
		self,
		*,
		since: int | None = None,
		mine: bool = False,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> subroutine.clients.base.Listing[subroutine.views.Event]:
		"""Return what has changed, oldest first, across everything this credential can see."""

		asking = _given(
			since=since,
			# The endpoint takes a word rather than a flag, so a later `?actor=<username>`
			# needs no second parameter and no deprecation.
			actor="me" if mine else None,
			newest=True if newest else None,
			workspace_id=workspace,
			limit=limit,
		)

		return self._collected(
			subroutine.views.Event,
			self._json("GET", "/v1/changes", params=asking),
			endpoint="changes",
			# **Followed forwards, and `newest` is the one call that is not** (`#1086`). With
			# `newest` set, `has_more` means there are *earlier* events — `domain.events.page`
			# says so — and a feed runs forwards by definition, so there is no way to ask for
			# them. Following anyway would request whatever came after the newest event, find
			# nothing, and turn a correct `has_more=True` into `False`: a worse answer than the
			# short page, because it claims to be complete.
			path=None if newest else "/v1/changes",
			params=None if newest else asking,
			wanted=limit,
			resume=BY_SEQ,
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

		asking = _given(
			workspace_id=workspace,
			limit=limit,
			parent=parent,
			visibility=visibility,
			include_archived="true" if include_archived else None,
			order=order,
		)

		return self._collected(
			subroutine.views.Project,
			self._json("GET", "/v1/projects", params=asking),
			endpoint="projects",
			path="/v1/projects",
			params=asking,
			wanted=limit,
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

	def create_login_link (
		self, *, username: str | None = None
	) -> subroutine.views.SignInLink:
		"""Mint a single-use sign-in link for a browser, and return it once (`#248`)."""

		return self._parsed(
			subroutine.views.SignInLink,
			self._json("POST", "/v1/login-links", json=_given(username=username)),
		)

	def sign_out_everywhere (self, *, username: str) -> subroutine.views.SignedOut:
		"""End every browser session an account holds, and report how many (`#248`)."""

		return self._parsed(
			subroutine.views.SignedOut,
			self._json("POST", f"/v1/users/{username}/signout"),
		)

	def revoke_token (self, *, id_or_prefix: str) -> subroutine.views.Token:
		"""Stop a credential working, now (`#348`)."""

		return self._parsed(
			subroutine.views.Token, self._json("DELETE", f"/v1/tokens/{id_or_prefix}")
		)

	def calendars (
		self, *, include_revoked: bool = False
	) -> list[subroutine.views.Calendar]:
		"""List your own calendar feeds, newest first (`#916`)."""

		return self._collected(
			subroutine.views.Calendar,
			self._json(
				"GET",
				"/v1/calendars",
				params=_given(include_revoked=include_revoked or None),
			),
			endpoint="calendars",
		)

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

		return self._parsed(
			subroutine.views.IssuedCalendar,
			self._json(
				"POST",
				"/v1/calendars",
				json=_given(
					title=title,
					workspace=workspace,
					project=project,
					audience=audience,
					# **Sent whenever it is a list, empty included.** `None` means every type
					# and `[]` means none, so dropping an empty one here would turn a refusal
					# into the opposite filter — which is the one mistake this field can make.
					item_types=None if item_types is None else list(item_types),
					expires=expires,
				),
			),
		)

	def reset_calendar (self, *, id_or_prefix: str) -> subroutine.views.IssuedCalendar:
		"""Give a feed a new URL, so the one somebody had stops working (`#916`)."""

		return self._parsed(
			subroutine.views.IssuedCalendar,
			self._json("POST", f"/v1/calendars/{id_or_prefix}/reset"),
		)

	def revoke_calendar (self, *, id_or_prefix: str) -> subroutine.views.Calendar:
		"""Stop a calendar feed for good, now (`#916`)."""

		return self._parsed(
			subroutine.views.Calendar,
			self._json("DELETE", f"/v1/calendars/{id_or_prefix}"),
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
		is_superuser: bool = False,
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
				"is_superuser": is_superuser,
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

	def set_active (self, *, username: str, active: bool) -> subroutine.views.User:
		"""Mark somebody as having left, or bring them back."""

		self._refuse_if_read_only()

		answer = self._json("PATCH", f"/v1/users/{username}", json={"is_active": active})

		return subroutine.views.User.model_validate(answer)

	def transfer_agent (self, *, username: str, to: str) -> subroutine.views.User:
		"""Hand an agent to somebody else, who becomes answerable for it."""

		self._refuse_if_read_only()

		answer = self._json("PATCH", f"/v1/users/{username}", json={"responsible": to})

		return subroutine.views.User.model_validate(answer)

	def set_timezone (
		self, *, username: str, timezone: str | None
	) -> subroutine.views.User:
		"""Say where somebody keeps their diary — your own account only."""

		answer = self._json("PATCH", f"/v1/users/{username}", json={"timezone": timezone})

		return subroutine.views.User.model_validate(answer)

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
		"""Change the fields beside a project's address, over the wire.

		The body is built by comparison against ``UNSET`` rather than by dropping empty values,
		for the reason :meth:`update` gives: ``None`` *clears* a field under §8.3, so a filter
		that removed it would turn "clear the description" into "change nothing" and answer 200.
		"""

		self._refuse_if_read_only()

		given = {
			"title": title,
			"description": description,
			"visibility": visibility,
			"status": status,
			"settings": settings,
		}
		body = self._json(
			"PATCH",
			f"/v1/projects/{project}",
			params=_given(workspace_id=workspace),
			json=_asked(given, expected_version),
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
		"""Change the fields beside a workspace's address, over the wire."""

		self._refuse_if_read_only()

		given = {
			"title": title,
			"description": description,
			"timezone": timezone,
			"prioritised_project": prioritised_project,
			"settings": settings,
		}
		body = self._json(
			"PATCH",
			f"/v1/workspaces/{workspace}",
			params=_given(workspace_id=workspace_id),
			json=_asked(given, expected_version),
		)

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
		status: str | None = None,
		project: str | None = None,
		workspace: str | None = None,
		tags: typing.Sequence[str] | None = None,
	) -> subroutine.views.Document:
		"""Write a document."""

		self._refuse_if_read_only()

		answered = self._json(
			"POST",
			"/v1/documents",
			json=_given(
				title=title,
				body=body,
				type=type,
				status=status,
				project=project,
				tags=None if tags is None else list(tags),
				workspace_id=workspace,
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
		parent: int | None = None,
		description: str | None = None,
		recurrence: str | None = None,
		recurrence_anchor: str | None = None,
		recurrence_trigger: str | None = None,
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
				parent_task_id=parent,
				# **Only when the line did not say.** A `+KEY` in the text is somebody being
				# explicit about this item and must beat a default that came from a file three
				# directories up, which they may not have known was there.
				project=None if subroutine.domain.capture.names_a_project(text) else project,
				# **Structured, because §6.13's `every …` span is reserved rather than read.**
				# The grammar claims the phrase so the date parser cannot steal `monday` out
				# of it, and leaves the words in the title — so a repeat has to arrive beside
				# the line rather than inside it.
				recurrence=recurrence,
				recurrence_anchor=recurrence_anchor,
				recurrence_trigger=recurrence_trigger,
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

	def read_repeat (
		self,
		*,
		text: str,
		start: datetime.datetime | None = None,
		timezone: str | None = None,
	) -> subroutine.views.Reading:
		"""Say what a written repeat means, without storing anything."""

		body = self._json(
			"POST",
			"/v1/recurrence/parse",
			json=_given(
				text=text,
				timezone=timezone,
				**({} if start is None else {"from": start.isoformat()}),
			),
		)

		return self._parsed(subroutine.views.Reading, body)


	def occurrences (
		self,
		*,
		ref: int,
		until: str | None = None,
		limit: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Occurrences:
		"""Say when a repeating task comes round, without materialising anything."""

		body = self._json(
			"GET",
			f"/v1/tasks/{ref}/occurrences",
			params=_given(until=until, limit=limit, workspace_id=workspace),
		)

		return self._parsed(subroutine.views.Occurrences, body)

	def skip (
		self,
		*,
		ref: int,
		workspace: str | None = None,
	) -> subroutine.views.Task:
		"""Let one occurrence of a repeat go by, and bring the next one."""

		self._refuse_if_read_only()

		body = self._json(
			"POST", f"/v1/tasks/{ref}/skip", params=_given(workspace_id=workspace)
		)

		return self._parsed(subroutine.views.Task, body)


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

		body = self._json(
			"POST",
			f"/v1/{_plural(entity_type)}/{ref}/move",
			# **Sent whatever it is, including null**, exactly as `move_project` sends it: the
			# endpoint refuses a body naming no parent at all, so "move to the top" has to be
			# said rather than implied, and `_given` would drop it.
			json={"parent": None if parent is None else str(parent)},
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
		assignee: str | None = subroutine.clients.base.UNSET,
		tags: typing.Sequence[str] | None = subroutine.clients.base.UNSET,
		due: str | None = subroutine.clients.base.UNSET,
		due_is_all_day: bool | None = subroutine.clients.base.UNSET,
		starts: str | None = subroutine.clients.base.UNSET,
		starts_is_all_day: bool | None = subroutine.clients.base.UNSET,
		snooze: str | None = subroutine.clients.base.UNSET,
		snoozed_is_all_day: bool | None = subroutine.clients.base.UNSET,
		recurrence: str | None = subroutine.clients.base.UNSET,
		recurrence_anchor: str | None = subroutine.clients.base.UNSET,
		recurrence_trigger: str | None = subroutine.clients.base.UNSET,
		timezone: str | None = subroutine.clients.base.UNSET,
		expected_version: int | None = None,
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
			"assignee": assignee,
			"tags": None if tags is None else (
				tags if tags is subroutine.clients.base.UNSET else list(tags)
			),
			"due": due,
			"due_is_all_day": due_is_all_day,
			# **All six, and the four dates were missing** (`#94`, found by widening this).
			# `#854` gave this method `starts` and `snooze` and they never reached the body,
			# so a caller setting either was answered 200 having changed nothing. `test_reach`
			# compares *signatures*, so the argument existing was enough to satisfy it —
			# `#149`'s blind spot, and the same shape as the `_is_all_day` flags `#195` found
			# being consulted by nothing.
			"starts": starts,
			"starts_is_all_day": starts_is_all_day,
			"snooze": snooze,
			"snoozed_is_all_day": snoozed_is_all_day,
			"recurrence": recurrence,
			"recurrence_anchor": recurrence_anchor,
			"recurrence_trigger": recurrence_trigger,
			"timezone": timezone,
		}
		body = self._json(
			"PATCH",
			f"/v1/tasks/{ref}",
			params=_given(workspace_id=workspace),
			json=_asked(given, expected_version),
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
		tags: typing.Sequence[str] | None = subroutine.clients.base.UNSET,
		expected_version: int | None = None,
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
			# **`None` survives here and `UNSET` does not**, which is the whole of §8.3 on the
			# wire: sending `"tags": null` clears them, and omitting the key leaves them alone.
			"tags": tags if tags is subroutine.clients.base.UNSET else list(tags or ()),
		}
		answered = self._json(
			"PATCH",
			f"/v1/documents/{ref}",
			params=_given(workspace_id=workspace),
			json=_asked(given, expected_version),
		)

		return self._parsed(subroutine.views.Document, answered)

	def schedule (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		starts: datetime.datetime | datetime.date | None = subroutine.clients.base.UNSET,
		snooze: datetime.datetime | datetime.date | None = subroutine.clients.base.UNSET,
	) -> subroutine.views.Task:
		"""Set when a task begins, or the day it stops being hidden.

		A field left out is unchanged and a field sent as null is cleared (§8.3), which is
		exactly what ``UNSET`` and ``None`` mean here — so the two map onto each other without
		anything being invented in between.
		"""

		self._refuse_if_read_only()

		changes: dict[str, typing.Any] = {}

		if starts is not subroutine.clients.base.UNSET:
			changes["starts"] = None if starts is None else starts.isoformat()

		if snooze is not subroutine.clients.base.UNSET:
			changes["snooze"] = None if snooze is None else snooze.isoformat()

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
				f"{model.__name__} could not be read from its response ({where}: {why})",
				body=body,
			) from None

	def _collected (
		self,
		model: type[Parsed],
		body: typing.Any,
		*,
		endpoint: str,
		path: str | None = None,
		params: dict[str, typing.Any] | None = None,
		wanted: int | None = None,
		resume: str = BY_CURSOR,
	) -> subroutine.clients.base.Listing[Parsed]:
		"""Read an enveloped collection into view models, following the cursor if there is one.

		Insists on the envelope rather than tolerating a bare array, even though one endpoint
		used to send one. Accepting both shapes would make this client the place where the
		§8.4 rule quietly stopped being true, and the next endpoint to forget it would be
		found by somebody else's client rather than by ours.

		**And then it threw the rest of the envelope away** (`#1037`). The API caps one response
		at the instance's ``max_page_size``, says ``has_more`` and hands back a keyset cursor;
		this read ``items`` and nothing else, so a caller asking for 500 rows got 200 and had no
		way to tell. **Worse than a plain cut**, because the way anything here detects a short
		answer is to ask for one more than it wants — ask for 501, receive 200, conclude that is
		all there is.

		**``limit`` is what the caller asked for; ``max_page_size`` still bounds one response.**
		That reading changes no published promise — the setting has always been about the size
		of a *page* — and it stops the setting silently becoming a cap on a *call*. The number
		of requests is bounded by the caller's own limit.

		**A caller that named no limit gets one page, and `has_more` says there is more**
		(`#1066`). Following an absent limit to the end of the table is what shipped first: the
		count check cannot fire on ``None``, so ``client.projects()`` walked the whole table
		while the local client returned its instance's default page — 120 rows against 50,
		measured on one database with the equivalence suite's own pair, on the one call that
		suite does not drive.

		**The two agree by deferring rather than by matching a number.** Omitting ``limit``
		leaves the instance to apply its own ``default_page_size``, which is the same setting
		the local client reads; a constant here would be a second copy of it, free to disagree
		with whichever instance this connection reaches. Simon's decision of 2026-08-22.

		``path`` and ``params`` are what makes following possible; without them this reads the
		one page it was handed, which is right for a collection that cannot be paged.

		**``resume`` is how the next page is asked for, and the change feed is not the odd one
		out — it is the one that publishes its cursor** (`#1086`). A listing hands back an
		opaque ``next_cursor``; the feed's cursor is ``seq``, on every row, because §5.11 makes
		it a number a caller stores and reasons about. So this followed every listing and read
		one page of the feed, and a caller asking for 500 changes got ``max_page_size`` — the
		defect `#1037` removed everywhere else, surviving in the one place the cursor was
		already in the caller's hands.
		"""

		if not isinstance(body, dict) or "items" not in body:
			raise self._not_an_instance(f"its /v1/…/{endpoint} response has no 'items'")

		collected = [self._parsed(model, item) for item in body["items"]]
		page = body.get("page") or {}
		has_more = bool(page.get("has_more"))
		cursor = page.get("next_cursor")

		# **Read from the first page and not refreshed while following** (`#1085`). It is a
		# property of the credential rather than of the page, so a later one saying something
		# different would mean the answer had changed under the caller — and an instance a
		# release behind sends no such key, which reads as "did not say" rather than "nothing".
		covers = tuple(body.get("covers") or ())

		while wanted is not None and path is not None and params is not None:
			# **Four ways to stop, and the last is the one that matters.** The caller has what
			# it asked for; the instance says there is no more; there is nothing to resume
			# from; or a page came back empty — which a correct instance cannot do and
			# anything else would spin on for ever.
			if len(collected) >= wanted:
				break

			if not has_more or not body["items"]:
				break

			carrying = _resumed(resume, collected, cursor)

			if carrying is None:
				break

			asking = dict(params)
			asking.update(carrying)

			if wanted is not None:
				asking["limit"] = wanted - len(collected)

			body = self._json("GET", path, params=asking)

			if not isinstance(body, dict) or "items" not in body:
				raise self._not_an_instance(f"its /v1/…/{endpoint} response has no 'items'")

			collected.extend(self._parsed(model, item) for item in body["items"])
			page = body.get("page") or {}
			has_more = bool(page.get("has_more"))
			cursor = page.get("next_cursor")

		return subroutine.clients.base.Listing(collected, has_more=has_more, covers=covers)

	def _not_an_instance (
		self, because: str, *, body: typing.Any | None = None
	) -> subroutine.errors.SubroutineError:
		"""Return the failure for a server that answered, but not as an instance.

		**When the versions disagree, that is the answer and this says so** (`#250`, `#341`).
		An instance one release behind answered `whoami` and was reported as *"not a Subroutine
		instance"* — which it plainly was. §13.7 makes several connections normal and each may
		run a different release, so the ordinary state was being described as a broken server,
		and the advice sent the reader to look at proxies.
		"""

		running = self._version_named_in(body) or self._instance_version

		if running is not None and running != subroutine.installations.program():
			return subroutine.errors.ServiceUnavailable(
				f"{self.connection.name} is running {running} and this program is "
				f"{subroutine.installations.program()}, so they disagree about what a "
				f"response contains: {because}.",
				hint="Update whichever is older. Until then this connection works for "
				"anything the two versions still agree about.",
			)

		return subroutine.errors.ServiceUnavailable(
			f"{self.connection.name} answered, but not as a Subroutine instance: {because}.",
			hint=f"Check what is serving {self.connection.url} — a proxy, a captive portal or "
			"an instance on a different API version will answer like this.",
		)

	@staticmethod
	def _version_named_in (body: typing.Any) -> str | None:
		"""Return the release a body says wrote it, if it says.

		**The body that failed to parse is the one that knows.** `#341`'s case is `/v1/me`
		refused for a field an older instance does not send — and `instance_version` is sitting
		beside the field that is missing. Read here rather than only from what an earlier call
		recorded, so the very first call of a session explains itself.
		"""

		if not isinstance(body, dict):
			return None

		named = body.get("instance_version")

		return named if isinstance(named, str) else None

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

	def _text (self, method: str, path: str, **options: typing.Any) -> str:
		"""Make one request and return what it answered, as text.

		Beside :meth:`_json` rather than inside it because the two failures differ: a plain-text
		endpoint answering JSON is a proxy, and a JSON endpoint answering text is the same thing
		— but only the JSON path can read a problem document out of the body, so sharing one
		reader would make a refusal here unreadable.
		"""

		response = self._call(method, path, **options)

		if response.is_success:
			return response.text

		# Refusals are JSON even from a text endpoint (§8.8), so the ordinary reader still
		# applies and gives the caller the sentence the instance actually wrote.
		self._read(response)

		raise subroutine.errors.ServiceUnavailable(
			f"{self.connection.name} answered {response.status_code} for {path}."
		)

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

			# `/v1/meta` and `/v1/me` both carry it, under one name so that this does not have
			# to know which endpoint answered (`#250`).
			seen = body.get("instance_version")

			if isinstance(seen, str):
				self._instance_version = seen

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


def _asked (
	given: dict[str, typing.Any], expected_version: int | None
) -> dict[str, typing.Any]:
	"""Return the fields a PATCH is actually changing, plus the version it expects.

	**Two different absences, which is why this is a function** (`#494`). A field left at
	``UNSET`` is one the caller said nothing about and is dropped; ``None`` is a *value* and
	is sent, because §8.3 makes it the way to clear something. ``expected_version`` inverts
	both: it is not a field being changed, and ``None`` there means *did not ask* rather than
	*asked and passed*, so it is sent only when it was given.

	Folding it into ``given`` would have made ``expected_version: None`` a request to clear a
	version, which the endpoint would refuse — and folding it in as ``UNSET`` would have made
	the one argument here whose ``None`` means silence behave like the ones whose ``None``
	means *clear this*.
	"""

	sending = {
		name: value
		for name, value in given.items()
		if value is not subroutine.clients.base.UNSET
	}

	if expected_version is not None:
		sending["expected_version"] = expected_version

	return sending


def _written (
	value: datetime.datetime | datetime.date | str | None,
) -> str | None:
	"""Render a date for the wire, leaving a written day exactly as it was typed.

	``"friday"`` is not something this can resolve and must not try (`#1088`): §6.5's chain
	lives on the instance, and a client that resolved first would be answering in whatever zone
	the machine it runs on happens to be set to. A ``date`` or ``datetime`` is already a
	decision somebody made and goes as ISO.
	"""

	if value is None or isinstance(value, str):
		return value

	return value.isoformat()


def _resumed (
	how: str, collected: typing.Sequence[typing.Any], cursor: typing.Any
) -> dict[str, typing.Any] | None:
	"""Return the parameters that ask for the page after the one just read, or ``None``.

	``None`` means *there is nothing to resume from*, which is not the same as *there is no
	more*: an instance may say ``has_more`` and hand back no cursor, and following an absent
	one would re-request the page just read, for ever.

	**The ``seq`` is taken one past the last row rather than at it.** §5.11 makes ``since``
	inclusive on purpose — a client that persists its cursor before it has finished processing
	a page must not lose the page — and that reasoning is about a *poll*, between which a
	client stores a number. Inside one call the page is already in hand, so re-asking for its
	last row would return a duplicate this would have to detect and drop. ``seq`` is an
	integer sequence, so ``+ 1`` skips nothing: any later event still satisfies ``>=``.
	"""

	if how == BY_SEQ:
		if not collected:
			return None

		# `seq` rather than an attribute this can name in a type: only the event model is ever
		# followed this way, and asking for it structurally is what stops a second model being
		# given this resume rule without anybody noticing it has no sequence.
		last = getattr(collected[-1], "seq", None)

		return None if last is None else {"since": last + 1}

	return None if cursor is None else {"cursor": cursor}


def _given (**values: typing.Any) -> dict[str, typing.Any]:
	"""Drop the parameters that were not supplied.

	``None`` means "not asked for" here, never "clear it" — every caller that needs to *send*
	a null builds its own body, because a helper that could not tell those apart is exactly
	how §8.3's distinction gets lost.
	"""

	return {name: value for name, value in values.items() if value is not None}


def _dated (
	filters: dict[str, str] | None, params: dict[str, typing.Any]
) -> dict[str, typing.Any]:
	"""Add §9.6's date comparisons to a listing's query string — `#815`.

	**Sent as written and refused at the far end**, like ``assignee`` and unlike ``deferred``.
	A name checked here would be a second copy of ``domain/filtering``'s registry, and the two
	would disagree the moment a field was added — where this way a client one release behind
	its instance can still ask a question the instance understands.

	A dotted name cannot collide with a flat one, since no endpoint declares a parameter with a
	separator in it; the merge is one-way regardless, so a filter can never overwrite ``limit``.
	"""

	if not filters:
		return params

	return {**params, **filters}


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
