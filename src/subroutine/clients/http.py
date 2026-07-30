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

import subroutine.clients.base
import subroutine.connections
import subroutine.credentials
import subroutine.db.types
import subroutine.domain.capture
import subroutine.errors
import subroutine.views

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
		instance = body.get("instance")

		return subroutine.clients.base.Identity(
			instance=(
				None if instance is None else subroutine.views.Instance.model_validate(instance)
			),
			workspaces=tuple(
				subroutine.views.WorkspaceRef.model_validate(item)
				for item in body.get("workspaces", [])
			),
		)

	def agenda (
		self,
		*,
		date: datetime.date | None = None,
		timezone: str | None = None,
		horizon_days: int | None = None,
		unscheduled_limit: int | None = None,
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
			),
		)

		return subroutine.views.Agenda.model_validate(body)

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

		return [
			subroutine.views.Task.model_validate(item) for item in body.get("items", [])
		]

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

		return subroutine.views.Task.model_validate(self._read(response))

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

		body = self._json(
			"POST",
			"/v1/tasks",
			json=_given(text=text, workspace_id=workspace, timezone=timezone),
		)

		return subroutine.clients.base.Captured(
			task=subroutine.views.Task.model_validate(body),
			unparsed=subroutine.domain.capture.parse(
				text, now=subroutine.db.types.utcnow()
			).unparsed,
		)

	def complete (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Mark a task finished."""

		body = self._json(
			"POST", f"/v1/tasks/{ref}/complete", params=_given(workspace_id=workspace)
		)

		return subroutine.views.Task.model_validate(body)

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

		return subroutine.views.Task.model_validate(body)

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
	default_connection: str | None = None,
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
