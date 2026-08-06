"""Driving this application without a socket, for a connection that has no server — `#485`.

``call_api`` lets an agent reach any route its credential already allows. Over HTTP that is an
ordinary request. **On a local connection there is nothing to send it to** — a standalone SQLite
install runs no server, and that is the zero-configuration machine an agent meets first, so an
escape hatch that refused there would be missing exactly where it is most needed.

So the application is driven in process. Two things make that honest rather than a shortcut:

* **The caller is resolved, not invented.** ``api/security.RESOLVERS`` is ``from_bearer_token``
  alone, and §12.1a says the filesystem permission *is* the authentication locally — so an
  in-process call carries no header and every route would answer 401. What stands in is
  :func:`subroutine.domain.local.principal`, the same resolution every other local client method
  uses, honouring ``SUBROUTINE_TOKEN_<NAME>`` and carrying the token's scopes. The rule is
  unchanged; only where the credential is read from is.
* **What is skipped is named.** §7.7's two limiters live inside that dependency, so overriding it
  skips them. That is correct here and not merely tolerable: a limiter exists to bound what a
  *remote* caller can spend, and there is no socket, no address and no second party.
"""

import asyncio
import typing

import fastapi
import httpx
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.security
import subroutine.domain.authentication


class SyncTransport(httpx.BaseTransport):
	"""Drive an ASGI application from synchronous httpx code.

	:mod:`subroutine.clients.http` is deliberately synchronous — a CLI has no event loop and
	should not grow one to print a list — while ``httpx.ASGITransport`` is async only. This
	bridges the two, running each request in its own loop.

	**Lives here rather than in the test helper it was written in.** It was the tests' way of
	pointing the real client at the real application over one database, and `#485` needs exactly
	that in the product; a second copy would be the divergence this codebase spends most of its
	time on. ``tests/api_support`` now names this one.
	"""

	def __init__ (self, application: fastapi.FastAPI) -> None:
		"""Wrap an application."""

		self._transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)

	def handle_request (self, request: httpx.Request) -> httpx.Response:
		"""Run one request through the application and return its complete response."""

		# Read before entering the loop: the outgoing body is a synchronous stream here, and
		# the ASGI transport will only iterate it asynchronously.
		request.read()

		async def run () -> httpx.Response:
			"""Make the call and drain the response inside the loop that opened it."""

			streamed = await self._transport.handle_async_request(request)

			try:
				body = await streamed.aread()

			finally:
				await streamed.aclose()

			return httpx.Response(
				streamed.status_code,
				headers=streamed.headers,
				content=body,
				request=request,
			)

		return asyncio.run(run())


def acting_as (
	application: fastapi.FastAPI,
	resolve: typing.Callable[
		[sqlalchemy.orm.Session], subroutine.domain.authentication.Principal
	],
) -> fastapi.FastAPI:
	"""Return the application with ``resolve`` standing in for reading a credential off a request.

	**A resolver rather than a ready-made principal**, and the difference is not stylistic. A
	``Principal`` carries ORM objects; built in the caller's session and handed to a request that
	opens its own, it is detached the moment anything lazy-loads. Worse, holding that outer
	session open around the call nests the request's transaction inside it — so the request
	commits, the outer context closes, and **the write is silently discarded**. Measured: a
	``PATCH`` answered 200 with the new title and every subsequent read returned the old one.

	Overriding :func:`subroutine.api.security.principal` rather than adding a resolver to
	``RESOLVERS``, which is the tempting alternative and is worse: that would change how the
	*served* instance authenticates every caller, to solve a problem that only exists where
	there is no request to read a header from.
	"""

	def override (
		session: subroutine.api.dependencies.SessionDep,
	) -> subroutine.domain.authentication.Principal:
		"""Resolve the caller against the session this request is already using."""

		return resolve(session)

	application.dependency_overrides[subroutine.api.security.principal] = override

	return application


def call (
	application: fastapi.FastAPI,
	resolve: typing.Callable[
		[sqlalchemy.orm.Session], subroutine.domain.authentication.Principal
	],
	*,
	method: str,
	path: str,
	body: typing.Any | None = None,
	query: dict[str, str] | None = None,
	content: bytes | None = None,
) -> httpx.Response:
	"""Make one request against ``application``, resolving the caller inside it.

	``content`` sends bytes exactly as given, instead of serialising ``body`` — for `#539`'s
	proxy, which forwards a JSON-RPC message it has deliberately not parsed. **Not parsing it is
	the point**: a malformed message has to reach the far end and be refused there, or the two
	transports answer it differently and the adapter has quietly become a second implementation
	of the protocol, which is the whole thing this change removes.
	"""

	with httpx.Client(
		transport=SyncTransport(acting_as(application, resolve)),
		base_url="http://local",
	) as client:
		if content is not None:
			return client.request(
				method,
				path,
				params=query,
				content=content,
				headers={"Content-Type": "application/json"},
			)

		return client.request(method, path, params=query, json=body)
