"""Driving the API in tests without a server, and without Starlette's TestClient.

``starlette.testclient`` warns on import that its httpx backend is deprecated, and this
suite runs with warnings as errors — for good reason, since that setting has caught real
resource leaks here. Rather than silence the warning or add a dependency to satisfy it,
the requests go through httpx's own ASGI transport, which is a stable public API and is
the thing TestClient wraps anyway.

The result is a plain synchronous ``call(...)`` that returns an ``httpx.Response``, so the
tests read like tests rather than like async plumbing.
"""

import asyncio
import contextlib
import typing

import fastapi
import httpx
import sqlalchemy.orm

import subroutine.api.app
import subroutine.api.inprocess
import subroutine.config

BASE_URL = "http://testserver"


def build_app (
	session_factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session],
	**overrides: typing.Any,
) -> fastapi.FastAPI:
	"""Build an application against a session factory the test controls.

	``dev_mode`` by default, so nothing here needs a signing key written to the real
	configuration file; a test that cares passes its own settings.
	"""

	settings = subroutine.config.Settings(dev_mode=True, **overrides)

	return subroutine.api.app.create_app(settings=settings, session_factory=session_factory)


def factory_for (
	session: sqlalchemy.orm.Session,
) -> sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session]:
	"""Return a factory yielding sessions that share the test's transaction.

	Every request made against the resulting application sees what the test set up and
	writes nothing that survives it, because they are all bound to the one connection the
	``session`` fixture rolls back.
	"""

	return sqlalchemy.orm.sessionmaker(
		bind=session.connection(),
		expire_on_commit=False,
		future=True,
		join_transaction_mode="create_savepoint",
	)


def call (
	application: fastapi.FastAPI,
	method: str,
	path: str,
	*,
	lifespan: bool = False,
	cookies: dict[str, str] | None = None,
	**kwargs: typing.Any,
) -> httpx.Response:
	"""Make one request against an application and return the response.

	``raise_app_exceptions`` is off because a bug is a thing under test here: Starlette's
	outermost handler produces the 500 and then re-raises, and a test asserting the shape
	of that response has to be allowed to see it.

	``lifespan`` runs startup and shutdown around the request, which only matters for a
	test that wants the application's own engine opened and disposed.

	``cookies`` is taken here and set on the *client* rather than passed through to the
	request, because httpx deprecates the per-request form — and this suite runs with
	warnings as errors, so passing it through would turn a browser test into a collection
	failure with a message about httpx rather than about the thing under test.
	"""

	async def run () -> httpx.Response:
		transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)

		async with (
			httpx.AsyncClient(
				transport=transport, base_url=BASE_URL, cookies=cookies
			) as client,
			_maybe_lifespan(application, lifespan),
		):
			return await client.request(method, path, **kwargs)

	return asyncio.run(run())


#: The bridge, which moved into the product for `#485` — a local ``call_api`` drives the
#: application with no socket, which is what this was doing for the equivalence test all along.
#: Named here rather than re-declared, so there is one definition.
SyncTransport = subroutine.api.inprocess.SyncTransport


@contextlib.asynccontextmanager
async def _maybe_lifespan (
	application: fastapi.FastAPI, wanted: bool
) -> typing.AsyncIterator[None]:
	"""Run the application's lifespan, or nothing at all."""

	if not wanted:
		yield

		return

	async with application.router.lifespan_context(application):
		yield
