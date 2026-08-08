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
import subroutine.api.problems
import subroutine.api.projects
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
Project management for people and agents, in equal measure.

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
	# The browser app's page and its files. Registered early because `/` and `/app/{name}`
	# share a prefix with nothing — `routing.check` is what says so rather than this comment,
	# and it fails the build if that ever stops being true. **The app's item addresses are a
	# different router and go last**, at the bottom of this tuple.
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
	# **Last, and it has to be** (`#638`). The browser app's item addresses are
	# `/{workspace}/{ref:int}` and `/{workspace}/{project}/{ref:int}`, and `/v1/tasks/42` is
	# also three segments ending in a number — so anything registered after these would be
	# shadowed. The converter keeps them off `/v1/me` and `/v1/docs/agent`; the position is
	# what keeps them off everything that does end in a ref. `routing.check` refuses the
	# application if either stops being true, which is how the first version of this was
	# caught: it named thirteen routes it had made unreachable, at startup, before a single
	# request arrived.
	("", subroutine.api.web.addresses),
)


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
		summary="Project management for people and agents, in equal measure.",
		description=DESCRIPTION,
		version=subroutine.API_VERSION,
		openapi_url="/v1/openapi.json",
		docs_url="/docs",
		redoc_url="/redoc",
		lifespan=_lifespan,
	)

	application.state.settings = resolved

	# **Built once, here, because a token bucket rebuilt per request counts nothing** (§7.7).
	# `host` decides only the *default*: unset `rate_limit` means on unless the bind keeps the
	# socket on one machine, and an instance that never serves anybody else has nobody to
	# limit. Read from settings rather than from the `serve` flag, so an application started
	# by gunicorn or by a test gets the same answer as one started by the CLI.
	application.state.limits = subroutine.api.limits.Limits(resolved, host=resolved.host)

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

	# Checked before anything is mounted, so a violation is a refusal to build rather than
	# an application that starts with an endpoint nobody can reach (SPEC.md §8.1).
	subroutine.api.routing.check(ROUTERS)

	for prefix, router in ROUTERS:
		application.include_router(router, prefix=prefix)

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
