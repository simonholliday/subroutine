"""Opening the right kind of client for a connection.

One line of logic, in a module of its own, because two callers need it and neither should
import the other. It lived in ``cli/personal.py`` until 2026-07-30, which made it reachable
only by importing the personal CLI — and with it ``rich`` and ``typer``, a cost an MCP
server or any future front end has no reason to pay.

A sibling of ``local`` and ``http`` rather than their parent's ``__init__``: those two both
import ``subroutine.connections``, so anything they import must not import them back, and a
package ``__init__`` is exactly where that kind of cycle is hardest to see.
"""

import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections


def for_connection (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
) -> subroutine.clients.base.Client:
	"""Open whichever kind of client this connection needs.

	``local`` reaches this installation's own database through the service layer; everything
	else goes over the wire. Both answer the same questions with the same objects (§13.7),
	which is what lets a caller fan out across them without knowing which is which.
	"""

	if connection.is_local:
		return subroutine.clients.local.opened(
			connection, settings, default_connection=roster.default
		)

	return subroutine.clients.http.opened(connection, default_connection=roster.default)
