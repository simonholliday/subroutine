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
	workspace: str | None = None,
	settings: subroutine.config.Settings | None = None,
) -> subroutine.mcp.protocol.Server:
	"""Return a server bound to one connection, named or current, and one workspace.

	``workspace`` is a *default* for the tools' existing argument rather than a pin — the
	credential is what pins (§7.3), and a session that could not look anywhere else would make
	an agent unable to read a decision filed next door.

	**Not validated at startup, deliberately.** Checking the name means asking the instance,
	and a server that will not start when the instance is briefly down is worse than one whose
	first call is refused with a message naming the workspaces that do exist — which is what
	`#333` was found by and is already well worded. Starting and serving are separate moments
	and only the second one reports (`#236`).
	"""

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
		subroutine.mcp.tools.catalogue(client, workspace=workspace),
		name="subroutine",
		version=subroutine.__version__,
		instructions=_instructions(chosen, roster, workspace),
		resources=subroutine.mcp.tools.references(client, workspace=workspace),
	)


def _instructions (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	workspace: str | None = None,
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

	# **Named here as well as passed to the tools** (`#333`). It costs one short clause and
	# it is the difference between an agent knowing where its work lands and finding out from
	# the first call that disagrees with its assumption. Silent when there is none, so a
	# single-workspace instance carries no sentence about a concept it does not need (§1.4).
	where = (
		""
		if workspace is None
		else f"Work goes to the '{workspace}' workspace unless a call says otherwise. "
	)

	elsewhere = tuple(name for name in roster.names if name != connection.name)
	others = (
		""
		if not elsewhere
		else (
			f"Other instances are configured here ({', '.join(elsewhere)}) and this session "
			f"cannot reach them; one server reaches one. "
		)
	)

	# **The pointer comes first, and it is here because this text is the problem** (`#378`).
	# These instructions are in context for every session and they *teach* — refs, and the
	# comment-versus-document rule below. An agent on its first contact said exactly what that
	# costs: "a paragraph of correct guidance in context makes the skill feel redundant". It
	# then listed, searched and recommended what to file, all without opening the skill, and
	# called a bare `list()` where `ready=true` is the whole point.
	#
	# **Conditional, because the server runs without the plugin.** `subroutine mcp` started by
	# hand has no skill to read, and an instruction naming one that is not there is the kind of
	# confident wrongness §13.1 exists to prevent. Phrased as a condition the reader can check.
	#
	# **And the second pointer is `#480`**, which is the same failure one level out: an agent
	# that believes the tools *are* the product stops at the first thing they cannot do. Measured
	# — a third-party agent could not revise a document, concluded documents were immutable, and
	# changed how it worked, giving one-item-in-one-place as the reason. It found `doc edit` in
	# under a minute once told the command line existed.
	#
	# It points and does not teach, for `#378`'s reason: the *why* — a schema is context every
	# session carries, so a tool is expensive in a way a command is not — is in the skill, where
	# it costs nothing per session. Conditional for the same reason as the line above it, and a
	# different one: a client reaching this over HTTP may have no shell at all.
	return (
		f"Shared project management for people and agents, on connection "
		f"'{connection.label}'. {where}{others}"
		f"If a 'subroutine' skill is available, read it before your first call — it carries "
		f"the conventions these tool descriptions do not. "
		f"These tools are a deliberate budget rather than the whole product: if you can run "
		f"commands, 'subroutine --help' is the complete surface. "
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
	workspace: str | None = None,
	settings: subroutine.config.Settings | None = None,
) -> None:
	"""Serve MCP over the given streams until the input closes."""

	subroutine.mcp.protocol.serve(
		build(connection=connection, workspace=workspace, settings=settings),
		incoming,
		outgoing,
	)
