"""Building the application.

A factory, not a module-level application: importing this package should not open the
user's database, and a test needs an instance pointed at a temporary one. ``subroutine
serve`` and any ASGI server call :func:`create_app`.

The order of what happens here is load-bearing and is set out in :func:`create_app`.
"""

import contextlib
import typing

import fastapi
import fastapi.middleware.cors
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine
import subroutine.api.admin
import subroutine.api.agenda
import subroutine.api.changes
import subroutine.api.comments
import subroutine.api.documents
import subroutine.api.events
import subroutine.api.health
import subroutine.api.identity
import subroutine.api.limits
import subroutine.api.mcp
import subroutine.api.meta
import subroutine.api.middleware
import subroutine.api.policy
import subroutine.api.problems
import subroutine.api.projects
import subroutine.api.query
import subroutine.api.recurrence
import subroutine.api.routing
import subroutine.api.sessions
import subroutine.api.tasks
import subroutine.api.tokens
import subroutine.api.users
import subroutine.api.web
import subroutine.api.workspaces
import subroutine.config
import subroutine.db.migrate
import subroutine.db.session

#: Shown at ``/docs`` and in the generated OpenAPI document. The audience is a developer
#: or an agent deciding whether this endpoint does what they need, so it points at the two
#: places that answer that rather than describing the product again.
DESCRIPTION = """
Agent-native task management for your life, your projects and your team.

`GET /v1/meta` reports this installation's vocabulary — its statuses, link types, field
operators and date grammar — so a client can read them rather than assume them.
`GET /v1/docs/agent` is a guide written for an agent working through this API.

Errors are RFC 9457 problem documents and every one carries a stable `code`; the codes are
part of the public contract and are listed in `docs/errors.md`.
""".strip()

#: Every router, in the order they are registered — and that order is load-bearing, which
#: is why they are declared as data rather than as a run of ``include_router`` calls. A
#: router carrying literal sub-paths must come before one carrying ``{id_or_ref}`` in the
#: same space, and :func:`subroutine.api.routing.check` reads this list to enforce it.
ROUTERS: tuple[subroutine.api.routing.Mounting, ...] = (
	("", subroutine.api.health.router),
	# The browser app's page and its files. `/` and `/app/{name}` share a prefix with nothing —
	# `routing.check` is what says so rather than this comment. **Every other address the app
	# answers is a 404 fallback rather than a route** (`#648`): a catch-all here claimed
	# `/v1/nothing` and `GET /mcp`, and would have claimed anything registered after it.
	("", subroutine.api.web.router),
	# **Its own protocol, so its own path** — `/mcp` rather than `/v1/mcp` (`#516`). It shares
	# a prefix with nothing and is a literal, so it can sit anywhere `routing.check` allows;
	# beside the other root-level routes is where a reader will look for it.
	("", subroutine.api.mcp.router),
	("", subroutine.api.identity.router),
	# Signing in and out. `/signin` is a literal at the root, sharing a prefix with nothing;
	# `/v1/session` is a literal under `/v1` and cannot be shadowed by an id parameter,
	# because no router mounts `/v1/{something}`. `routing.check` is what says so.
	("", subroutine.api.sessions.router),
	("", subroutine.api.workspaces.router),
	("", subroutine.api.users.router),
	# The session sub-resource, after the router whose path it extends.
	("", subroutine.api.sessions.user_sessions),
	("", subroutine.api.tokens.router),
	("", subroutine.api.meta.router),
	("", subroutine.api.agenda.router),
	("", subroutine.api.recurrence.router),
	("", subroutine.api.tasks.router),
	# The link sub-resources come after the routers whose paths they extend. They cannot
	# shadow or be shadowed — `/{id_or_ref}/links` is longer than anything in either — but
	# `routing.check` is what says so rather than anybody's reading of it.
	("", subroutine.api.documents.task_links),
	("", subroutine.api.projects.router),
	("", subroutine.api.documents.router),
	("", subroutine.api.documents.document_links),
	# The comment sub-resources come after the routers whose paths they extend, like links.
	("", subroutine.api.comments.task_comments),
	("", subroutine.api.comments.project_comments),
	("", subroutine.api.comments.document_comments),
	("", subroutine.api.comments.router),
	# The feed reads the same rows as the histories below and shares a path with nothing:
	# `/v1/changes` is a literal under no entity's prefix.
	("", subroutine.api.changes.router),
	# The history sub-resources, likewise after the routers they extend.
	("", subroutine.api.events.task_events),
	("", subroutine.api.events.project_events),
	("", subroutine.api.events.document_events),
	("", subroutine.api.admin.router),
)


class Surface (typing.NamedTuple):
	"""One protocol a started instance answers, in the terms somebody starting it needs."""

	#: Where it answers. A prefix, so ``/v1`` covers every route mounted beneath it.
	path: str

	#: What it is, said plainly enough for an operator who has not met the acronym.
	what: str

	#: What a caller needs, or where to read next. The half that makes the line worth a line.
	note: str


#: What an instance serves, in the order somebody starting one should hear it.
#:
#: **A path here is a claim, and :func:`serving` checks it against the routes** — an
#: announcement can never name a transport this application does not answer. The judgement
#: is which of them an operator is owed a sentence about; the fact is whether it is mounted,
#: and only the second is written down twice.
#:
#: `#780` is the other direction and is why this exists at all. ``POST /mcp`` has been served
#: since `#516` — an agent reaches an instance with an address and a token and nothing
#: installed at its end — and neither the startup line nor the agent guide said so, so the
#: cheapest way into this product was discoverable only by reading the source. Decision `#499`
#: is the rule that was broken: the channel a reader is guaranteed must name the ones they
#: only get by going looking, and here both guaranteed channels were silent.
SURFACES: tuple[Surface, ...] = (
	Surface("/v1", "the HTTP API", "the guide written for an agent is at /v1/docs/agent"),
	# **"over HTTP" is doing real work.** There are two MCP paths into an instance (`#538`) and
	# they need different things: this one, and `subroutine mcp`, which is stdio and needs the
	# program installed on the caller's own machine. Saying which one just started is the
	# difference between an operator configuring a client correctly and configuring the other.
	Surface(
		subroutine.api.mcp.PATH,
		"MCP over HTTP",
		"an agent needs the address above and a token, and nothing installed",
	),
)


def serving (
	routers: typing.Sequence[subroutine.api.routing.Mounting] = ROUTERS,
) -> list[Surface]:
	"""Return the surfaces these routers answer, so a caller can say what is running.

	The routers are an argument rather than read from the module, which is what lets a test
	hand in a set with one of them missing and watch the answer lose a line. A scanner that
	cannot be given its subject can only ever confirm the arrangement it was written from
	(`#405`), and this one exists to notice an arrangement changing.
	"""

	answered = {path for path, _methods, _route in subroutine.api.routing.mounted(routers)}

	return [
		surface
		for surface in SURFACES
		if any(
			path == surface.path or path.startswith(f"{surface.path}/") for path in answered
		)
	]


def create_app (
	*,
	settings: subroutine.config.Settings | None = None,
	session_factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session] | None = None,
) -> fastapi.FastAPI:
	"""Build the application.

	``session_factory`` is for tests, which supply one bound to a transaction they can roll
	back. When it is not given the engine is built here from ``settings`` and disposed when
	the application shuts down.
	"""

	resolved = settings or subroutine.config.load_settings()

	application = fastapi.FastAPI(
		title="Subroutine",
		summary="Agent-native task management for your life, your projects and your team.",
		description=DESCRIPTION,
		version=subroutine.API_VERSION,
		openapi_url="/v1/openapi.json",
		# **No Swagger and no ReDoc, because this instance's own policy blocks them** (`#927`
		# H-18). Both answered 200 with their single `<script>` pointing at
		# `cdn.jsdelivr.net`, against `script-src 'self' <hashes>` — a blank page that looked
		# like a served feature, advertised as *Built* in the README and published by
		# `/v1/meta` as `"human"`.
		#
		# Three ways out, and this is the one that is not a decision about the product.
		# Vendoring `swagger-ui-dist` is ~1MB of third-party JavaScript in a closure
		# `check_licences.py` walks `importlib.metadata` and cannot see — the trade already
		# refused for shadcn and Tailwind — and admitting the CDN would widen the policy
		# `#805` exists to hold. **Nothing that worked stops working**: the page was blank.
		#
		# `/v1/openapi.json` is unchanged and is the contract; any viewer can be pointed at it.
		docs_url=None,
		redoc_url=None,
		lifespan=_lifespan,
	)

	application.state.settings = resolved

	# **Built once, here, because a token bucket rebuilt per request counts nothing** (§7.7).
	# `host` decides only the *default*: unset `rate_limit` means on unless the bind keeps the
	# socket on one machine, and an instance that never serves anybody else has nobody to
	# limit. Read from settings rather than from the `serve` flag, so an application started
	# by gunicorn or by a test gets the same answer as one started by the CLI.
	application.state.limits = subroutine.api.limits.Limits(resolved, host=resolved.host)

	# **Built once, here, because it is derived from the page this instance serves** (`#805`).
	# The import map is inline by necessity and is allowed by hash, so the policy depends on the
	# shell's bytes — which are read at import and cannot change while the process runs.
	# `api/middleware.apply_headers` reads this off the state rather than importing the module
	# that computes it, which is what keeps `web` -> `problems` -> `middleware` from closing
	# into a cycle.
	application.state.policy_headers = subroutine.api.policy.headers()

	application.state.engine = None
	application.state.session_factory = session_factory

	# Read once. It comes from the migration scripts shipped in this package, which cannot
	# change while the process runs, and the readiness check should not walk a directory
	# every time a load balancer asks whether it is alive.
	application.state.schema_head = subroutine.db.migrate.head_revision()

	if session_factory is None:
		engine = subroutine.db.session.create_engine(resolved.database_url)

		application.state.engine = engine
		application.state.session_factory = subroutine.db.session.create_session_factory(engine)

	application.middleware("http")(subroutine.api.middleware.correlate)

	# Outside everything, because a body too large to read is not a request this application
	# should be building objects for.
	application.add_middleware(
		subroutine.api.middleware.BodyLimit, limit=resolved.max_body_bytes
	)

	# Outermost, because it decides what the request *is* before anything else reads the
	# method. Added after `correlate` for that reason: Starlette runs the last one first.
	application.middleware("http")(subroutine.api.middleware.answer_head_with_get)

	if resolved.cors_origins:
		# Only when configured. A browser is not the primary client here, and a default
		# that allows credentialed cross-origin requests is a hole nobody asked for.
		application.add_middleware(
			fastapi.middleware.cors.CORSMiddleware,
			allow_origins=resolved.cors_origins,
			allow_credentials=True,
			allow_methods=["*"],
			allow_headers=["*"],
			expose_headers=[
				subroutine.api.middleware.REQUEST_ID_HEADER,
				subroutine.api.middleware.API_VERSION_HEADER,
			],
		)

	subroutine.api.problems.install(application)

	# After `problems`, which it delegates to: an address nothing claimed is the app's
	# when a browser asked, and the same problem document as ever when a program did.
	subroutine.api.web.install(application)

	# Checked before anything is mounted, so a violation is a refusal to build rather than
	# an application that starts with an endpoint nobody can reach (docs/design.md §8.1).
	subroutine.api.routing.check(ROUTERS)

	# What this application accepts, per path, for the two answers that have to know: a 405
	# saying which methods a path really takes, and `HEAD` reaching the `GET` beside it. Read
	# off the declared routers rather than the built application, for `mounted`'s recorded
	# reason — an included router is opaque and composes its paths at match time.
	application.state.declared_routes = subroutine.api.routing.declarations(ROUTERS)

	for prefix, router in ROUTERS:
		# **Every route refuses a query parameter it does not declare, from here** (`#898`).
		# Attached at the mounting loop rather than route by route because route by route is a
		# list, and that list fell behind three times — `#676` and `#897` were both found by
		# accident, months apart. A route that must answer whatever it is asked says so in
		# `api/query.NOT_REFUSED`, which is reviewable in a way "somebody remembered" is not.
		application.include_router(
			router, prefix=prefix, dependencies=[subroutine.api.query.UnknownQueryDep]
		)

	return application


@contextlib.asynccontextmanager
async def _lifespan (application: fastapi.FastAPI) -> typing.AsyncIterator[None]:
	"""Hold the application's own resources for as long as it is serving.

	Only an engine this module built is disposed. A session factory handed in by a test
	belongs to the test, and disposing its engine underneath it would break the fixture
	that owns it.
	"""

	try:
		yield

	finally:
		engine = application.state.engine

		if isinstance(engine, sqlalchemy.engine.Engine):
			engine.dispose()
