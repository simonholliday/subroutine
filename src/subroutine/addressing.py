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

#: Words a literal route already claims in the task and project path spaces. Listed here
#: rather than derived from the routing table, because a key is refused at creation —
#: possibly by a CLI with no application built — and because the list is a promise about
#: future endpoints too: ``sync`` has no route yet, and reserving it now costs nothing
#: while un-reserving it later would cost somebody their project key.
RESERVED_PATH_WORDS = frozenset({"batch", "next", "parse", "search", "sync"})


def is_reserved_word (value: str) -> bool:
	"""Report whether a value would be swallowed by a literal route.

	Compared case-insensitively. Identifiers in a path resolve case-insensitively
	(SPEC.md §8.1), so a project keyed ``SEARCH`` is reachable at the same address as the
	search endpoint whether or not the letters match.
	"""

	return value.strip().lower() in RESERVED_PATH_WORDS
