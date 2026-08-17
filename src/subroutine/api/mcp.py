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

  **That sentence was written before the second type existed, and then the omission happened
  anyway** (`#809`). `#248` added the cookie resolver to the chain and this endpoint began
  answering a person's browser, for three days, until security review `#802` measured it. A
  browser session is refused now — see
  :func:`_refuse_a_credential_this_transport_does_not_take` for why that is a decision about
  *shape* rather than a hole being closed. **Predicting a decision is not taking one**, which
  is worth more here than the fix is.
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

#: The header a client announces its revision in, once the handshake has settled one.
VERSION_HEADER = "MCP-Protocol-Version"

#: What the transport says to assume when :data:`VERSION_HEADER` is absent. Named rather than
#: inlined because it is the reason :data:`SPOKEN` carries a revision this server does not
#: implement: refusing the value in its written form while serving it in its implied one would
#: be two answers to one question.
ASSUMED_WHEN_ABSENT = "2025-03-26"

#: The revisions this endpoint will answer to. ``_initialize`` agrees to
#: :data:`subroutine.mcp.protocol.PROTOCOL_VERSION` and answers with it for anything else, so a
#: session that completes the handshake is always on that one — which is what makes this a
#: short list rather than a policy.
SPOKEN: frozenset[str] = frozenset(
	{subroutine.mcp.protocol.PROTOCOL_VERSION, ASSUMED_WHEN_ABSENT}
)

router = fastapi.APIRouter(
	tags=["mcp"],
	route_class=subroutine.api.routing.Transactional,
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


@router.get(
	PATH,
	summary="This transport does not stream",
	response_class=fastapi.Response,
	include_in_schema=False,
)
def no_stream () -> fastapi.Response:
	"""Refuse the standalone event stream, in writing rather than by absence.

	The refusal itself is not new: a client opening this ``GET`` has always been answered
	``405``, and that was *measured* against ``claude-code/2.1.222`` rather than assumed. What
	is new is that it now comes from a route instead of from there being no route.

	**The difference matters because an absence can be claimed by somebody else.** The browser
	app later added ``GET /{workspace}``, which matches every single-segment path — so this
	``GET`` began returning an HTML page, and a 405 that had only ever existed as a coincidence
	stopped being true. ``api/routing.check`` could not see it: nothing became *unreachable*,
	which is the question it asks.

	``Allow: POST`` because that is what a 405 owes the caller, and because it says out loud
	what this endpoint is for.
	"""

	return fastapi.Response(status_code=405, headers={"allow": "POST"})


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
	deliberate, and the refusal names the workspaces rather than merely complaining.
	"""

	_refuse_a_foreign_origin(request, settings)
	_refuse_a_revision_this_server_does_not_speak(request)
	_refuse_a_credential_this_transport_does_not_take(actor)

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


def _refuse_a_credential_this_transport_does_not_take (
	actor: subroutine.domain.authentication.Principal,
) -> None:
	"""Refuse a browser session here — decided with Simon on 2026-08-11, item `#809`.

	**This endpoint accepted one because nobody ever said what it should accept.** `#516` built
	it reading the ``Authorization`` header itself; `#539` replaced that with
	:func:`subroutine.api.security.resolve` — correctly, because a second copy of the
	authentication rule sitting on an authentication path is this codebase's signature defect —
	and `#248` then added the cookie resolver to that chain. Two right decisions and one
	absence, and the result was a transport built for agents answering a person's browser.

	**It was never an escalation, so this is not a hole being closed.** Security review `#802`
	measured every way it could have been one: same person, same permissions, origin-checked by
	both this module's list and the cookie's, rate-limited, and no `#565` deadlock because
	:class:`subroutine.api.security.Resolver` is a protocol that forces every credential type to
	honour ``record_use``.

	**It is refused because the one plausible use for it is the one to reject** (`#808`). The
	cheap way to expose this product to an agent standing in a browser is to declare the
	fourteen MCP tools as page tools and have each one post here with the cookie — attractive
	because that surface already exists and is budgeted. It is wrong because the surface includes
	``subroutine_call_api``, an escape hatch reaching any route the credential allows, driven by
	an agent reading item text that **anybody with a credential may have written, including on
	somebody else's item**. Page tools, if they are ever built, want to be narrow and to call
	``/v1``, which needs none of this.

	**A refusal about which credential a transport takes, not about how one is resolved.** The
	chain is untouched, which is what keeps `#539`'s argument intact: there is still one place
	that decides who a caller is, and this only says that one kind of answer does not belong at
	this door.

	Forbidden rather than unauthenticated, because the credential is real and was presented
	properly. Nothing is wrong with it except where it was sent.
	"""

	if actor.session is None:
		return

	raise subroutine.errors.Forbidden(
		"This transport does not accept a browser session.",
		hint="MCP is reached with an API token: send 'Authorization: Bearer sr_…', and create "
		"one with 'subroutine token create'. A page served by this instance should call /v1 "
		"directly, which is what its own browser app does.",
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

		# **Already counted, and counting it again deadlocks the request** (`#565`).
		# `PrincipalDep` authenticated this caller in the request's own session and dirtied
		# `token.last_used_at` there; `api/routing.Transactional` holds that transaction until
		# after the handler returns, so the row lock outlives this call. Writing it a second
		# time here — in the client's separate session, on the same row — blocked on the lock
		# the same request was holding, with no concurrency involved at all.
		return subroutine.api.security.resolve(session, request, record_use=False)

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


def _refuse_a_revision_this_server_does_not_speak (
	request: starlette.requests.Request,
) -> None:
	"""Refuse a client announcing an MCP revision this server cannot answer — `#941`.

	**The transport makes this mandatory**, and until `#927`'s M-31 the header was read by
	nothing: `banana`, `2099-01-01` and no header at all were all answered ``200``. Absent is
	allowed and read as :data:`ASSUMED_WHEN_ABSENT`, which is what the transport says to
	assume; present-and-unknown is refused.

	**The refusal names the revision this server speaks**, because a 400 whose body says only
	*no* leaves the caller to guess whether to retry lower, and because the whole point of the
	status here is to let an old server and a new client find each other.

	**Driven before it was written, because the risk was locking a working plugin out of a
	working instance.** A header-logging probe against ``claude-code/2.1.226`` shows the shape
	that makes this safe: its *first* request is a new-era ``server/discover`` carrying
	``2026-07-28`` — which this refuses — and every request after the handshake carries the
	revision it negotiated, which is ours. Re-run against a probe that refuses exactly as this
	does, it took the 400, fell back to ``initialize``, agreed ``2025-06-18`` and proceeded,
	with a trace identical to the permissive run. A 400 there is what the status is *for*.

	**The ``GET`` is deliberately not checked.** It answers ``405`` whatever it carries, so a
	version check in front of it could only change which refusal a caller reads.
	"""

	announced = request.headers.get(VERSION_HEADER)

	if announced is None or announced in SPOKEN:
		return

	raise subroutine.errors.UnsupportedProtocolVersion(
		f"This server speaks MCP revision {subroutine.mcp.protocol.PROTOCOL_VERSION}, "
		f"and the request announced {announced!r}.",
		hint=(
			f"Negotiate with 'initialize', which answers "
			f"{subroutine.mcp.protocol.PROTOCOL_VERSION}, and send that in "
			f"'{VERSION_HEADER}' afterwards."
		),
	)


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

	**And deliberately *not* the address this request arrived at**, which the cookie's version
	of this check does trust (`#639`). Rebinding is an attacker choosing that name, so allowing
	it would be allowing the attack; a cookie is host-only, so there the same value is a fact
	rather than a claim. The mechanism is shared and the two lists are not.
	"""

	answered: set[str | None] = {
		subroutine.api.security.origin_of(one) or one.strip() for one in settings.cors_origins
	}

	answered.add(subroutine.api.security.origin_of(settings.public_url))

	subroutine.api.security.refuse_an_unanswered_origin(
		request,
		allowed=answered,
		hint=(
			"A page in a browser sent this. If one there is meant to reach this endpoint, "
			"add its origin to 'cors_origins'; an agent's own client sends no Origin at all "
			"and is unaffected."
		),
	)
