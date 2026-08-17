"""What may be used as an identifier inside an address.

A project key becomes a path segment — ``/v1/projects/SR`` — and so does a task ref. Some
segments are already spoken for by an endpoint: ``/v1/tasks/search`` is the search
endpoint, not a task called ``search``. Anything that would land on one of those is
refused when it is created, because the alternative is a project that exists, is listed,
and cannot be opened (docs/design.md §8.1).

Deliberately free of any HTTP framework. The rule is enforced in the service layer, which
runs for the CLI as well, and a domain module reaching into the API package to find out
what a valid key is would have the dependency exactly the wrong way round.
"""

import re
import typing

#: Words a literal route already claims in the task and project path spaces. Listed here
#: rather than derived from the routing table, because a key is refused at creation —
#: possibly by a CLI with no application built — and because the list is a promise about
#: future endpoints too: ``sync`` has no route yet, and reserving it now costs nothing
#: while un-reserving it later would cost somebody their project key.
#:
#: **``comments``, ``events``, ``move`` and ``restore`` joined it with decision `#957`**, and
#: they were safe to leave out until then only by accident of arithmetic. A project's address
#: was one segment, so a sub-resource literal was always the *second* and could never be
#: mistaken for a key. Now the address spans segments, ``substation/events`` reads equally as
#: a project keyed ``events`` inside ``substation`` and as ``substation``'s history — and the
#: route wins, so the project would exist, be listed, and be reachable by nothing.
#:
#: ``tests/test_api_routing.py`` derives what these must contain from the real routers, so a
#: sub-resource added later cannot leave this behind. It asserts containment rather than
#: equality, which is what leaves room for ``sync``.
RESERVED_PATH_WORDS = frozenset(
	{"batch", "comments", "events", "move", "next", "parse", "restore", "search", "sync"}
)


#: Words that would confuse a *person* reading an address, rather than a program resolving
#: one. A workspace slug became part of an address in §13.7 — ``connection/workspace/ref`` —
#: where position tells a connection from a workspace, so nothing here is structurally
#: ambiguous. A workspace called ``local`` beside the implicit ``local`` connection still
#: makes ``use local`` mean two things, and ``all``/``none``/``me`` read as instructions
#: rather than as places.
#:
#: Reserved now rather than later, on the same reasoning as :data:`RESERVED_PATH_WORDS`:
#: reserving costs nothing today and un-reserving would cost somebody their workspace name.
#: This is the list where a word may be added ahead of anything claiming it, because it is
#: a judgement about reading rather than a fact about routes.
_CONFUSING_WORKSPACE_WORDS = frozenset(
	{"all", "default", "here", "local", "me", "mine", "none", "self"}
)


#: Words that this application's own root paths already claim. Since a slug became the
#: *first* segment of a browser URL, a workspace named after one of these has an address
#: that opens something else entirely — ``mcp`` answers a protocol and ``healthz`` a health
#: check, so the workspace exists, is listed, and can never be reached (item ``#678``).
#:
#: **Listed here and derived in a test**, which is the arrangement rather than an oversight.
#: This module refuses a slug at creation, possibly for a CLI that never builds an
#: application, so it cannot read the routing table — see this file's own first paragraph.
#: ``tests/test_api_routing.py`` compares this set against the root segments of the real
#: routers and fails if either side moves, so a route added later cannot leave it behind.
#: That is what makes it one rule with two copies checked, rather than two rules.
#:
#: **Nothing may be reserved here ahead of its route**, unlike :data:`RESERVED_PATH_WORDS`
#: above, whose ``sync`` has no endpoint yet. That test asserts equality, so a word listed
#: for something that does not exist fails it. The asymmetry is the price of deriving, and
#: it is worth paying: this list falling behind is the whole defect, and reserving ahead is
#: exactly what would make falling behind unmeasurable.
ROUTED_WORKSPACE_WORDS = frozenset({"app", "healthz", "mcp", "readyz", "signin", "v1"})


#: Everything a workspace short name may not be, and the list the refusal offers back to
#: whoever tried. Two lists behind it because there are two reasons, and only one of them is
#: a fact a test can go and check; a caller asking whether a name is free does not care
#: which, so this is the name every caller uses.
RESERVED_WORKSPACE_WORDS = _CONFUSING_WORKSPACE_WORDS | ROUTED_WORKSPACE_WORDS


def is_reserved_workspace_word (value: str) -> bool:
	"""Report whether a workspace short name would be confusing as an address segment."""

	return value.strip().lower() in RESERVED_WORKSPACE_WORDS


def is_reserved_word (value: str) -> bool:
	"""Report whether a value would be swallowed by a literal route.

	Compared case-insensitively. Identifiers in a path resolve case-insensitively
	(docs/design.md §8.1), so a project keyed ``SEARCH`` is reachable at the same address as the
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


#: What one parameter is filled in with when asking whether a route can be reached. Any
#: segment does: the question is whether some *other* pattern also matches, and a ``{name}``
#: matches every segment alike.
_ANY_SEGMENT = "x"


def filled (template: str, values: typing.Mapping[str, str]) -> str:
	"""Return this template with each parameter replaced by the value named for it.

	**A parameter with no value is left as it was written**, converter and all, so a caller
	can see which it failed to supply. Substituting something plausible instead is how a route
	quietly stops being exercised — and the placeholder is what a check has to notice, since a
	path still holding a brace cannot be requested.

	Here rather than in the one test that wants it, because the spelling of a placeholder is
	this module's business: ``{name}`` and ``{name:path}`` are one parameter under two
	spellings, and a caller matching on the first would miss every catch-all.
	"""

	return _PARAMETER.sub(
		lambda found: values.get(found.group(1), found.group(0)), template
	)


def sample (template: str) -> str:
	"""Return one fixed path this template would match.

	So that two parameterised routes can be compared at all. :func:`matches` answers whether
	a template matches a *path*, and asking whether one template matches another's source
	text answers nothing — ``{ref}`` is not a path. Filling one in makes the question
	concrete: *is there a request this route claims that an earlier one would take first?*
	"""

	return _PARAMETER.sub(_ANY_SEGMENT, template)


def spans_segments (template: str) -> bool:
	"""Report whether any parameter here matches across ``/`` — a greedy catch-all.

	The distinction routing order turns on. A ``{name}`` claims one segment and can only
	shadow a route of the same length; a ``{name:path}`` claims the rest of the URL and so
	swallows every longer route registered after it, whatever its shape.
	"""

	return any(parameter.group(2) == "path" for parameter in _PARAMETER.finditer(template))
