"""Standing an MCP server up against one connection.

The thin part between the wire (:mod:`subroutine.mcp.protocol`) and the work
(:mod:`subroutine.mcp.tools`): resolve which instance to talk to, open a client for it, and
run the loop.

**One connection, not the fan-out the CLI does.** ``subroutine today`` merges every
configured instance because a person has one day and it does not care which server a dentist
appointment lives on. A tool call is the opposite: it writes somewhere, and "somewhere" has
to be a decision the caller can see rather than one this process makes on their behalf. So
the connection is chosen once, at startup, and named in the instructions the client reads.

**"A decision the caller can see" is the part that was not built** (`#276`). The binding fell
back to ``subroutine use`` — working state, machine-wide, moved between tasks — read once at
startup and held for the session, so which instance an agent wrote to depended on where that
pointed at the unrelated moment its process happened to start. Two sessions on one machine on
one day bound different instances, and neither could tell. The fallback is now
``default_connection``, which somebody sets in ``config.toml`` and can read back.
"""

import typing

import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.mcp.protocol
import subroutine.mcp.tools


def build (
	*,
	connection: str | None = None,
	settings: subroutine.config.Settings | None = None,
) -> subroutine.mcp.protocol.Server:
	"""Return a server bound to one connection, named or current."""

	resolved = settings or subroutine.config.load_settings()
	roster = subroutine.connections.roster(resolved)

	# **The fallback is the configured default, not the current context** (`#276`). Both are
	# names rather than objects, so `require` is what turns one into a connection and refuses
	# one since removed from the roster.
	#
	# `subroutine use` is deliberately not consulted. It is working state — a person moves it
	# between tasks — and a server reads it once, at startup, and holds the answer for the
	# whole session. That made which instance an agent wrote to depend on where the context
	# happened to point at the unrelated moment its process started, which is the opposite of
	# the property this module's docstring claims. `default_connection` in `config.toml` is a
	# decision somebody took and can see.
	wanted = connection or roster.default
	chosen = roster.require(wanted)
	client = subroutine.clients.opening.for_connection(chosen, roster, resolved)

	return subroutine.mcp.protocol.Server(
		subroutine.mcp.tools.catalogue(client),
		name="subroutine",
		version=subroutine.__version__,
		instructions=_instructions(chosen, roster),
	)


def _instructions (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
) -> str:
	"""Return what a client is told about this server before it calls anything.

	Short on purpose — it is context every session carries. It says which instance is being
	written to, because a tool that files work somewhere invisible is worse than no tool; and
	it says what the two record types are for, because that is the one convention an agent
	cannot infer from a schema and gets wrong by default (§5.10).

	**And it names the instances it is not reaching** (`#276`). Naming only the bound one is
	what let an agent be confident it knew where it was: the sentence is true, and nothing in
	it suggests the name is one of several. Added only when there *is* another — the same
	rule that keeps ``subroutine connections`` out of ``--help`` until then, and for the same
	reason, since an instruction about instances costs every session that will never have two.
	"""

	elsewhere = tuple(name for name in roster.names if name != connection.name)
	others = (
		""
		if not elsewhere
		else (
			f"Other instances are configured here ({', '.join(elsewhere)}) and this session "
			f"cannot reach them; one server reaches one. "
		)
	)

	return (
		f"Shared project management for people and agents, on connection "
		f"'{connection.label}'. {others}"
		f"Items are addressed by a number written #42, unique per "
		f"workspace and never reused, shared between tasks and documents. "
		f"A comment is what happened; a document is what you concluded — if the next "
		f"session would need to read it, it is a document."
	)


def run (
	incoming: typing.TextIO,
	outgoing: typing.TextIO,
	*,
	connection: str | None = None,
	settings: subroutine.config.Settings | None = None,
) -> None:
	"""Serve MCP over the given streams until the input closes."""

	subroutine.mcp.protocol.serve(
		build(connection=connection, settings=settings), incoming, outgoing
	)
