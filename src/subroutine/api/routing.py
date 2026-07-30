"""Route ordering rules, enforced when the application is built rather than remembered.

Starlette matches routes in registration order and takes the first whose pattern fits, so
``GET /v1/tasks/next`` never runs if ``GET /v1/tasks/{id_or_ref}`` was registered first:
the parameterised route matches ``next`` perfectly well and answers with a 404 for a task
nobody asked for. Declaring literal sub-paths before parameterised ones is the fix
(SPEC.md §8.1). Measured against the installed FastAPI rather than assumed — it does not
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

import re
import typing

import fastapi
import fastapi.routing

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

_PARAMETER = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")


def declarations (routers: typing.Sequence[Mounting]) -> list[Declaration]:
	"""Return every route these routers will register, in the order they will register it.

	Read from the routers themselves rather than from the built application: FastAPI keeps
	an included router as an opaque object whose paths are composed at match time, and
	reaching into that would be a check written against a private shape.
	"""

	found: list[Declaration] = []

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

			found.append((prefix + path, frozenset(methods or ())))

	return found


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

			if not collisions or not _matches(earlier_path, path):
				continue

			verbs = ", ".join(sorted(collisions))
			problems.append(
				f"{verbs} {path} is unreachable: {earlier_path} is registered before it "
				f"and matches the same request."
			)

	return problems


def check (routers: typing.Sequence[Mounting]) -> None:
	"""Refuse to build an application in which a route cannot be reached.

	Raised rather than logged, and raised at construction rather than on the first request:
	a silently unreachable endpoint is indistinguishable from a missing one, and the caller
	who finds out is an agent that has concluded the task does not exist.
	"""

	problems = shadowed(declarations(routers))

	if not problems:
		return

	listed = "\n  ".join(problems)

	raise RuntimeError(
		f"Routes are registered in an order that makes some of them unreachable:\n"
		f"  {listed}\n"
		f"Register literal paths before parameterised ones."
	)


def _matches (template: str, path: str) -> bool:
	"""Report whether a path template would match a fixed path.

	The conversion is deliberately ours rather than the framework's compiled matcher: the
	matcher belongs to an included router that composes its paths at request time, and a
	check that has to open that up would break on an upgrade without saying so. The
	behaviour it stands in for is small — a ``{name}`` matches one segment, a ``{name:path}``
	matches the rest — and ``tests/test_api_routing.py`` asserts the two agree by putting
	real requests through a real application.
	"""

	pattern: list[str] = []
	position = 0

	for parameter in _PARAMETER.finditer(template):
		pattern.append(re.escape(template[position : parameter.start()]))
		pattern.append(".+" if parameter.group(2) == "path" else "[^/]+")
		position = parameter.end()

	pattern.append(re.escape(template[position:]))

	return re.fullmatch("".join(pattern), path) is not None
