"""Standing an MCP server up against one connection.

The thin part between the wire (:mod:`subroutine.mcp.protocol`) and the work
(:mod:`subroutine.mcp.tools`): resolve which instance to talk to, open a client for it, and
run the loop.

**One connection, not the fan-out the CLI does.** ``subroutine today`` merges every
configured instance because a person has one day and it does not care which server a dentist
appointment lives on. A tool call is the opposite: it writes somewhere, and "somewhere" has
to be a decision the caller can see rather than one this process makes on their behalf. So
the connection is chosen once, at startup, and named in the instructions the client reads.
"""

import typing

import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.errors
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

	# `context.resolve` answers with a *name*, since §13.7's current context is a pair of
	# names in a state file rather than objects. `require` turns it into a connection and
	# refuses one that has since been removed from the roster.
	wanted = connection or subroutine.context.resolve(roster).connection
	chosen = roster.require(wanted)
	client = subroutine.clients.opening.for_connection(chosen, roster, resolved)

	return subroutine.mcp.protocol.Server(
		subroutine.mcp.tools.catalogue(client),
		name="subroutine",
		version=subroutine.__version__,
		instructions=_instructions(chosen),
	)


def _instructions (connection: subroutine.connections.Connection) -> str:
	"""Return what a client is told about this server before it calls anything.

	Short on purpose — it is context every session carries. It says which instance is being
	written to, because a tool that files work somewhere invisible is worse than no tool; and
	it says what the two record types are for, because that is the one convention an agent
	cannot infer from a schema and gets wrong by default (§5.10).
	"""

	return (
		f"Shared project management for people and agents, on connection "
		f"'{connection.label}'. Items are addressed by a number written #42, unique per "
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
