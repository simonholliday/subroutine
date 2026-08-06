"""``POST /mcp`` — this instance serving its own agent surface, over Streamable HTTP.

**The point is that nothing has to be installed** (`#516`). Until this existed, an agent
reached a Subroutine instance over MCP only by running ``subroutine mcp`` on the caller's own
machine — so a freelancer told *"here is a URL and a token"* first had to install Python, install
the package, and learn what a connection is. Decision `#538` is the whole argument; this is the
half of it that has to exist before any of the rest can.

**Almost nothing here is new, and that is the design.** The tools are written against
:class:`subroutine.clients.base.Client` and cannot tell a database from a socket, and
:class:`subroutine.mcp.protocol.Server` "holds no connection and no session of its own" — so
what was missing was never the capability, only a transport. This module is that transport:
read a JSON-RPC message off a request, hand it to the same server the stdio loop drives, and
write the answer back.

Three properties come free from being an ordinary route, and each was expensive to get:

* **Authentication and rate limiting.** ``PrincipalDep`` is where §7.7's two limiters live, so
  declaring it gets both, and ``tests/test_api_authentication.py`` fails the build if a route
  ever loses it. The MCP surface is rate-limited per token exactly as ``/v1`` is.
* **The credential is resolved by the API's own resolver chain.** :func:`_acting_as` calls
  ``security.resolve``, not a bearer-token reader of its own, so a second credential type
  (§7.5) reaches this endpoint the day it reaches the others — or is refused here by a
  deliberate decision rather than by an omission nobody noticed.
* **Scope, workspace pin and permissions are untouched.** They live below the client, in the
  service layer, so an agent over HTTP is bounded by exactly what bounds it over stdio.

**What is deliberately *not* implemented**: the standalone ``GET`` event stream. Streamable
HTTP allows a server to answer a POST with a single JSON object rather than an SSE stream, and
this server has no server-initiated messages to send — the tools and resources are fixed at
build time and say so in their capabilities. Measured against ``claude-code/2.1.222``: it opens
the ``GET``, is answered ``405``, and proceeds without complaint.
"""

import json
import typing

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.security
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.instances
import subroutine.errors
import subroutine.mcp.protocol
import subroutine.mcp.session

#: Where this server answers. **At the root rather than under ``/v1``**, and that is a decision:
#: the MCP protocol version is negotiated in band, and ``/v1`` is the *HTTP API's* contract
#: version, so nesting one inside the other would put two version schemes on one path — a shape
#: this codebase has been bitten by often enough to name. It also makes the canonical resource
#: URI an OAuth deployment would use simply ``https://host/mcp`` (RFC 8707 §2).
PATH = "/mcp"

router = fastapi.APIRouter(
	tags=["mcp"],
	route_class=subroutine.api.routing.Transactional,
	dependencies=[subroutine.api.query.UnknownQueryDep],
)


async def _raw_body (request: starlette.requests.Request) -> bytes:
	"""Return the request body exactly as it arrived.

	**Declared as a dependency so the endpoint below can stay synchronous**, which is the
	whole reason this exists. Reading a body is asynchronous; the work behind it is
	SQLAlchemy and is not. FastAPI runs an async dependency on the event loop and a *sync*
	endpoint in a worker thread, so this arrangement gets both — where an ``async def``
	endpoint would block the loop on every database call.

	**And it must be raw.** Declaring the body as a model, or even as ``dict``, hands parsing
	to FastAPI — so a message that is not JSON is refused by the framework with a problem
	document, and the answer this server gives over HTTP stops being the answer it gives over
	stdio. `#530` is that defect already filed against this file. One parser
	(:func:`subroutine.mcp.protocol.answer`), reached the same way by both transports, is the
	only arrangement in which they cannot drift.
	"""

	return await request.body()


#: The body, unparsed. See :func:`_raw_body` for why it is not a model.
RawBodyDep = typing.Annotated[bytes, fastapi.Depends(_raw_body)]


@router.post(
	PATH,
	summary="Speak MCP to this instance",
	response_class=fastapi.Response,
	openapi_extra={
		"requestBody": {
			"required": True,
			"content": {"application/json": {"schema": {"type": "object"}}},
		}
	},
)
def call (
	request: starlette.requests.Request,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	body: RawBodyDep,
	workspace: str | None = None,
) -> fastapi.Response:
	"""Answer one MCP message.

	``workspace`` is a *default* for the tools' own argument rather than a pin, exactly as
	``subroutine mcp --workspace`` is: the credential is what pins (§7.3), and a session that
	could not look anywhere else would leave an agent unable to read a decision filed next
	door. A plugin puts it in the URL, which is the one place a static configuration file can
	put it.

	Left unset on a multi-workspace instance, every read is refused as ambiguous — which is
	`#496`, and the refusal there names the workspaces rather than merely complaining.
	"""

	_refuse_a_foreign_origin(request, settings)

	# **`actor` is declared and deliberately not passed on.** Declaring it is what authenticates
	# this route and what puts §7.7's limiters in front of it, and what
	# `tests/test_api_authentication.py` looks for. Handing the object itself to the client
	# would be the mistake `inprocess.acting_as` documents: a principal carries ORM objects, so
	# one built in this session detaches the moment anything on it loads in another. The
	# credential is resolved again, in the session that uses it.
	name = _instance_name(session)
	server = subroutine.mcp.session.over(
		_client(request, settings, name=name), label=name, workspace=workspace
	)

	answer = subroutine.mcp.protocol.answer(server, body)

	# **A notification is answered with nothing at all**, which the transport spells `202`.
	# Writing an empty body against a `200` would be a response to a client that is not
	# waiting for one — the same rule the stdio loop keeps by writing no line.
	if answer is None:
		return fastapi.Response(status_code=202)

	# A message we could not even parse is reported as a bad request, because it is one. A
	# message we parsed and refused — an unknown method, a bad argument — is a `200` carrying a
	# JSON-RPC error, because at that point the protocol worked and the *content* is the answer.
	failed_to_parse = answer.get("error", {}).get("code") in (
		subroutine.mcp.protocol.PARSE_ERROR,
		subroutine.mcp.protocol.INVALID_REQUEST,
	)

	return fastapi.Response(
		content=json.dumps(answer, separators=(",", ":")),
		media_type="application/json",
		status_code=400 if failed_to_parse else 200,
	)


def _client (
	request: starlette.requests.Request,
	settings: subroutine.config.Settings,
	*,
	name: str,
) -> subroutine.clients.local.Client:
	"""Return a client onto this instance, acting as whoever made this request.

	**Its own session, from the application's own factory.** The request's session is already
	open — ``PrincipalDep`` authenticated in it — and handing that one over would be wrong in
	both directions: the client closes what it opens, so it would close the request's session
	out from under :class:`~subroutine.api.routing.Transactional`; and the client commits its
	own unit of work, so the two would be committing the same transaction twice. One engine,
	one pool, two sessions, and each ends where it began.

	**`Transactional`'s guarantee still holds, and more directly than elsewhere.** The client
	commits before it returns, which is before this handler returns, which is before the
	response is built — so a commit that fails is still reported as a failure rather than
	arriving after the caller has been told it worked.
	"""

	return subroutine.clients.local.Client(
		subroutine.connections.Connection(name=name),
		settings,
		session_factory=request.app.state.session_factory,
		principal=_acting_as(request),
	)


def _acting_as (
	request: starlette.requests.Request,
) -> typing.Callable[
	[sqlalchemy.orm.Session], subroutine.domain.authentication.Principal
]:
	"""Return a resolver that identifies this request's caller in whatever session it is given.

	**Through ``security.resolve``, which is the API's own chain**, rather than through a
	bearer-token reader written here. §7.5 will add a second credential type, and a copy of the
	rule in this module would be a copy that has to be remembered — this codebase's signature
	defect, and it would sit on an authentication path.

	**It raises rather than falling through**, which is the property that matters most.
	``domain.local.principal`` answers the same question for a personal machine and, given no
	credential, identifies the sole account — §12.1a, where the filesystem permission *is* the
	authentication. On a served instance that reasoning is exactly inverted, so the two never
	meet: see :meth:`subroutine.clients.local.Client._principal`.

	**And it authenticates the way the application does, override included** (`#539`). Reading
	the request's header directly is right for a served instance and wrong for the one caller
	that has no request to read: ``subroutine mcp`` drives this app in process for a local
	connection, where §12.1a applies and there is no credential to present. That caller says who
	it is by overriding :func:`subroutine.api.security.principal`, which is FastAPI's own lever
	for it and the one :mod:`subroutine.api.inprocess` already uses — so this asks the
	application rather than going around it. Two authentications per request, disagreeing, is
	what the previous shape would have produced.
	"""

	def resolve (
		session: sqlalchemy.orm.Session,
	) -> subroutine.domain.authentication.Principal:
		"""Identify the caller against this session, as this application would."""

		instead = request.app.dependency_overrides.get(subroutine.api.security.principal)

		if instead is not None:
			return typing.cast(
				subroutine.domain.authentication.Principal, instead(session)
			)

		return subroutine.api.security.resolve(session, request)

	return resolve


def _instance_name (session: sqlalchemy.orm.Session) -> str:
	"""Return what this instance calls itself, for the instructions to name.

	**The caller's own name for this connection is not available here and must not be
	guessed.** A connection name is a private alias invented per machine — `#330` is the item
	about how private — so a served endpoint naming one would be naming something the caller
	may never have written. It names itself instead, and `#539` is where a proxy rewrites that
	clause into the caller's vocabulary on the way back.
	"""

	return subroutine.domain.instances.require(session).name


def _refuse_a_foreign_origin (
	request: starlette.requests.Request, settings: subroutine.config.Settings
) -> None:
	"""Refuse a browser-originated request from somewhere this instance does not answer.

	**The transport specification requires this**, and the attack is DNS rebinding: a page on
	any origin resolves a name to ``127.0.0.1`` and posts to a loopback MCP server, which
	answers because it never asked who was calling. An agent's own client sends no ``Origin``
	at all — measured — so this costs a real caller nothing and closes the one that is not one.

	Absent is allowed and present-but-unknown is refused, which is the asymmetry the rule turns
	on: only a browser sends this header, and only a browser is the threat. What counts as
	known is ``cors_origins`` — the same list that decides whether a browser may read a reply —
	plus this instance's own ``public_url``, because a UI served from the instance is the one
	origin that needs no configuring.
	"""

	origin = request.headers.get("origin")

	if origin is None:
		return

	allowed = {*settings.cors_origins}

	if settings.public_url:
		allowed.add(settings.public_url.rstrip("/"))

	if origin.rstrip("/") in allowed or "*" in allowed:
		return

	raise subroutine.errors.Forbidden(
		f"This instance does not answer requests from {origin!r}.",
		hint=(
			"A page in a browser sent this. If one there is meant to reach this endpoint, "
			"add its origin to 'cors_origins'; an agent's own client sends no Origin at all "
			"and is unaffected."
		),
	)
