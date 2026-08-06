"""What may be used as an identifier inside an address.

A project key becomes a path segment — ``/v1/projects/SR`` — and so does a task ref. Some
segments are already spoken for by an endpoint: ``/v1/tasks/search`` is the search
endpoint, not a task called ``search``. Anything that would land on one of those is
refused when it is created, because the alternative is a project that exists, is listed,
and cannot be opened (SPEC.md §8.1).

Deliberately free of any HTTP framework. The rule is enforced in the service layer, which
runs for the CLI as well, and a domain module reaching into the API package to find out
what a valid key is would have the dependency exactly the wrong way round.
"""

import re

#: Words a literal route already claims in the task and project path spaces. Listed here
#: rather than derived from the routing table, because a key is refused at creation —
#: possibly by a CLI with no application built — and because the list is a promise about
#: future endpoints too: ``sync`` has no route yet, and reserving it now costs nothing
#: while un-reserving it later would cost somebody their project key.
RESERVED_PATH_WORDS = frozenset({"batch", "next", "parse", "search", "sync"})


#: Words a *workspace* short name may not take, because a workspace slug became part of an
#: address in §13.7 — ``connection/workspace/ref``. Position tells a connection from a
#: workspace, so there is no structural ambiguity; what these prevent is the human kind. A
#: workspace called ``local`` beside the implicit ``local`` connection makes ``use local``
#: mean two things, and ``all``/``none``/``me`` read as instructions rather than places.
#:
#: Reserved now rather than later, on the same reasoning as :data:`RESERVED_PATH_WORDS`:
#: reserving costs nothing today and un-reserving would cost somebody their workspace name.
RESERVED_WORKSPACE_WORDS = frozenset(
	{"all", "default", "here", "local", "me", "mine", "none", "self"}
)


def is_reserved_workspace_word (value: str) -> bool:
	"""Report whether a workspace short name would be confusing as an address segment."""

	return value.strip().lower() in RESERVED_WORKSPACE_WORDS


def is_reserved_word (value: str) -> bool:
	"""Report whether a value would be swallowed by a literal route.

	Compared case-insensitively. Identifiers in a path resolve case-insensitively
	(SPEC.md §8.1), so a project keyed ``SEARCH`` is reachable at the same address as the
	search endpoint whether or not the letters match.
	"""

	return value.strip().lower() in RESERVED_PATH_WORDS


#: One ``{name}`` or ``{name:converter}`` in a path template.
_PARAMETER = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")


def matches (template: str, path: str) -> bool:
	"""Report whether a path template would match a fixed path.

	The conversion is deliberately ours rather than the framework's compiled matcher: the
	matcher belongs to an included router that composes its paths at request time, and a check
	that has to open that up would break on an upgrade without saying so. The behaviour it
	stands in for is small — a ``{name}`` matches one segment, a ``{name:path}`` matches the
	rest — and ``tests/test_api_routing.py`` asserts the two agree by putting real requests
	through a real application.

	**Here rather than in ``api/routing``, where it was written** (`#528`). Two readers now: that
	module's shadowing check, and the MCP deny-list, which has to decide whether a raw path names
	a route it will not reach. The second is why it moved — ``api/routing`` imports FastAPI, and
	making the MCP server load a web framework to run a regex is 0.3s spent on nothing for every
	session against a *remote* instance, which never otherwise builds an application.

	This module says in its own first paragraph that it is free of any HTTP framework, which is
	the property that makes it the right home rather than merely a possible one.
	"""

	pattern: list[str] = []
	position = 0

	for parameter in _PARAMETER.finditer(template):
		pattern.append(re.escape(template[position : parameter.start()]))
		pattern.append(".+" if parameter.group(2) == "path" else "[^/]+")
		position = parameter.end()

	pattern.append(re.escape(template[position:]))

	return re.fullmatch("".join(pattern), path) is not None
