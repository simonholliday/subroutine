"""Route ordering rules, enforced when the application is built rather than remembered.

Starlette matches routes in registration order and takes the first whose pattern fits, so
``GET /v1/tasks/next`` never runs if ``GET /v1/tasks/{id_or_ref}`` was registered first:
the parameterised route matches ``next`` perfectly well and answers with a 404 for a task
nobody asked for. Declaring literal sub-paths before parameterised ones is the fix
(docs/design.md §8.1). Measured against the installed FastAPI rather than assumed — it does not
reorder by specificity, and a route added in the wrong place really is unreachable.

A convention is a poor way to hold that. The two routes live in different modules, the
mistake produces a plausible 404 rather than an error, and nothing about reading either
file suggests anything is wrong — the shape of defect that survives a test suite written
by whoever made it. So the order is checked when the application is built, and a violation
refuses to start.

The words those literal routes use are reserved against identifiers as well, so that no
project can be keyed ``SEARCH``; that list lives in :mod:`subroutine.addressing`, which
knows nothing about HTTP because the service layer enforcing it runs for the CLI too.
"""

import typing

import fastapi
import fastapi.routing

import subroutine.addressing

#: A router as it is about to be included: the prefix it will be mounted under, and the
#: router itself. A router's *own* ``prefix=`` is already part of each route's path, but
#: one passed to ``include_router`` is applied at match time and has to be composed here.
Mounting = tuple[str, fastapi.APIRouter]


class Transactional(fastapi.routing.APIRoute):
	"""A route that commits its request's transaction **before the response is sent**.

	FastAPI closes a request's dependency exit stack *after* the application has emitted
	the response. Measured, not assumed: a probe recording the order printed ``handler
	body`` → ``response left the app`` → ``dependency exit``. So a session committed in a
	``yield`` dependency — which is where this one lived until 2026-07-30 — commits after
	the caller already holds its ``200``.

	Two things follow. A client that writes and immediately reads can beat its own commit,
	which is how this was found: one read of an item's history missed an event the previous
	request had just written. And, worse, **a commit that failed would fail after the
	caller had been told it succeeded** — a ``201`` for something that never happened, with
	no way for the client to notice.

	Committing here closes both. The handler has returned, so the work is done and the
	response is built; the response has not been sent, so a failure to commit can still be
	reported as one. A commit that raises is left to propagate deliberately: reporting a
	``500`` for a transaction that did not land is the whole point, and swallowing it would
	recreate the defect this class exists to remove.
	"""

	def get_route_handler (self) -> typing.Callable[..., typing.Any]:
		"""Wrap the ordinary handler so the transaction lands before the response leaves."""

		original = super().get_route_handler()

		async def handler (request: typing.Any) -> typing.Any:
			"""Run the endpoint, then commit what it did."""

			response = await original(request)
			opened = getattr(request.state, "session", None)

			# Not every route asks for a session — the health checks and the docs do not,
			# and neither should have to pretend to.
			if opened is not None and opened.in_transaction():
				opened.commit()

			return response

		return handler

#: One route, reduced to what deciding reachability needs.
Declaration = tuple[str, frozenset[str]]

#: The same, with the route object itself — for a caller that has to ask it something.
Mount = tuple[str, frozenset[str], typing.Any]



def mounted (routers: typing.Sequence[Mounting]) -> list[Mount]:
	"""Return every route these routers will register, with the route object itself.

	Read from the routers themselves rather than from the built application: FastAPI keeps
	an included router as an opaque object whose paths are composed at match time, and
	reaching into that would be a check written against a private shape. **Measured, and it
	is not a theoretical hazard**: walking ``app.routes`` finds eight routes where these
	routers declare more than sixty, and a guard built on it passes by looking at almost
	nothing (item ``#427``).

	:func:`declarations` is this without the route, which is all that deciding reachability
	needs. Both exist because one walk is what keeps them agreeing about what a route is.
	"""

	found: list[Mount] = []

	for prefix, router in routers:
		for route in router.routes:
			path = getattr(route, "path", None)
			methods = getattr(route, "methods", None)

			if path is None:
				raise RuntimeError(
					f"A router mounted at {prefix!r} contains something that is not a "
					f"route ({type(route).__name__}). Routers including other routers are "
					f"not checked; mount them on the application instead."
				)

			found.append((prefix + path, frozenset(methods or ()), route))

	return found


def declarations (routers: typing.Sequence[Mounting]) -> list[Declaration]:
	"""Return every route these routers will register, in the order they will register it."""

	return [(path, methods) for path, methods, _route in mounted(routers)]


def accepted (routes: typing.Sequence[Declaration], path: str) -> frozenset[str]:
	"""Return every method this application accepts at ``path``.

	**Every route, not the first one that matched**, and that is the whole point. FastAPI
	registers one route per method, and Starlette answers a 405 out of whichever route it
	partially matched first — so ``PUT /v1/tasks`` was told ``Allow: POST``, with no mention
	of the ``GET`` registered beside it. A caller discovering the surface from its refusals
	learns something false, which is worse than learning nothing.

	Empty means no route claims the path at all, which is a 404 rather than a 405 and is not
	this function's question.
	"""

	found: set[str] = set()

	for declared, methods in routes:
		if subroutine.addressing.matches(declared, path):
			found |= methods

	return frozenset(found)


def shadowed (routes: typing.Sequence[Declaration]) -> list[str]:
	"""Return a description of every route an earlier one would swallow.

	Only a route with a fixed path can be shadowed — a parameterised path is a pattern, and
	asking whether one pattern matches another's source text answers nothing. Each entry
	names both routes and the methods they collide on, because "route ordering is wrong" is
	not something anybody can act on.
	"""

	problems: list[str] = []

	for index, (path, methods) in enumerate(routes):
		if "{" in path:
			continue

		for earlier_path, earlier_methods in routes[:index]:
			collisions = methods & earlier_methods

			if not collisions or not subroutine.addressing.matches(earlier_path, path):
				continue

			verbs = ", ".join(sorted(collisions))
			problems.append(
				f"{verbs} {path} is unreachable: {earlier_path} is registered before it "
				f"and matches the same request."
			)

	return problems


def swallowed (routes: typing.Sequence[Declaration]) -> list[str]:
	"""Return a description of every route an earlier **catch-all** would take first.

	:func:`shadowed` above answers this for a route with a fixed path and deliberately says
	nothing about a parameterised one, because one pattern matching another's source text
	means nothing. That was the whole truth while every parameter claimed a single segment:
	two such routes collide only when they are the same shape, which is a duplicate rather
	than an ordering fault.

	**A ``{name:path}`` parameter ends that** (decision `#957`, which gave a project an
	address spanning segments). It matches the rest of the URL, so
	``GET /v1/projects/{id_or_key:path}`` registered before
	``GET /v1/projects/{id_or_key:path}/comments`` answers that request itself — with a 404
	about a project called ``substation/comments``, which reads as the project having been
	deleted rather than as a route nobody can reach. `#25`'s recorded shape, in the one
	disguise the guard written for it could not see.

	Asked by making the later route concrete — :func:`subroutine.addressing.sample` fills
	each parameter in — and putting that path to the earlier pattern. A catch-all matching
	one request the later route claims is a catch-all that swallows it.
	"""

	problems: list[str] = []

	for index, (path, methods) in enumerate(routes):
		concrete = subroutine.addressing.sample(path)

		for earlier_path, earlier_methods in routes[:index]:
			collisions = methods & earlier_methods

			if not collisions or not subroutine.addressing.spans_segments(earlier_path):
				continue

			if not subroutine.addressing.matches(earlier_path, concrete):
				continue

			verbs = ", ".join(sorted(collisions))
			problems.append(
				f"{verbs} {path} is unreachable: {earlier_path} is registered before it and "
				f"its path parameter matches across '/', so it answers that request too."
			)

	return problems


def check (routers: typing.Sequence[Mounting]) -> None:
	"""Refuse to build an application in which a route cannot be reached.

	Raised rather than logged, and raised at construction rather than on the first request:
	a silently unreachable endpoint is indistinguishable from a missing one, and the caller
	who finds out is an agent that has concluded the task does not exist.
	"""

	declared = declarations(routers)
	problems = shadowed(declared) + swallowed(declared)

	if not problems:
		return

	listed = "\n  ".join(problems)

	raise RuntimeError(
		f"Routes are registered in an order that makes some of them unreachable:\n"
		f"  {listed}\n"
		f"Register literal paths before parameterised ones."
	)
