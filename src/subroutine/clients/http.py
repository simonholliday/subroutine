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

	def tasks (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		include_completed: bool = False,
	) -> list[subroutine.views.Task]:
		"""List one workspace's tasks, newest first."""

		body = self._json(
			"GET",
			"/v1/tasks",
			params=_given(
				workspace_id=workspace,
				limit=limit,
				include_completed="true" if include_completed else None,
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
		self, *, workspace: str | None = None, limit: int | None = None
	) -> list[subroutine.views.Document]:
		"""List one workspace's documents, newest first."""

		body = self._json(
			"GET", "/v1/documents", params=_given(workspace_id=workspace, limit=limit)
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

	def capture (
		self, *, text: str, workspace: str | None = None, timezone: str | None = None
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
			json=_given(text=text, workspace_id=workspace, timezone=timezone),
		)

		return subroutine.clients.base.Captured(
			task=self._parsed(subroutine.views.Task, body),
			unparsed=subroutine.domain.capture.parse(
				text, now=subroutine.db.types.utcnow()
			).unparsed,
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

	def complete (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Mark a task finished."""

		self._refuse_if_read_only()

		body = self._json(
			"POST", f"/v1/tasks/{ref}/complete", params=_given(workspace_id=workspace)
		)

		return self._parsed(subroutine.views.Task, body)

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
