"""Standing an MCP server up over a client somebody else opened.

The thin part between the wire (:mod:`subroutine.mcp.protocol`) and the work
(:mod:`subroutine.mcp.tools`): bind a catalogue to a client and say what the session is for.

**Nothing here chooses the client any more** (`#539`). It used to, and that was the seam where
two implementations of a tool call could differ — the one in the caller's installed package and
the one on the server. ``subroutine mcp`` is a transport adapter now
(:mod:`subroutine.mcp.relay`) and this module runs server-side, so there is one implementation
and it is the instance's.

**One connection, not the fan-out the CLI does.** ``subroutine agenda`` merges every configured
instance because a person has one day and it does not care which server a dentist appointment
lives on. A tool call is the opposite: it writes somewhere, and "somewhere" has to be a
decision the caller can see rather than one a process makes on their behalf.
"""

import typing

import subroutine.clients.base
import subroutine.mcp.protocol
import subroutine.mcp.tools


def over (
	client: subroutine.clients.base.Client,
	*,
	label: str,
	workspace: str | None = None,
	elsewhere: typing.Sequence[str] = (),
) -> subroutine.mcp.protocol.Server:
	"""Return a server over a client somebody else opened.

	**The seam `#516` needed and did not have to invent.** The tools are written against
	:class:`subroutine.clients.base.Client` and do not know a database from a socket, so the
	only thing standing between "an MCP server on this machine" and "an MCP server this
	instance serves" was that :func:`build` resolved the client itself. It no longer does.

	``label`` is what the instructions call the thing being written to. Over stdio that is the
	caller's own connection name; on a served endpoint it is the instance's, because the
	caller's alias for it is private and this side has never heard it (`#330`).
	"""

	return subroutine.mcp.protocol.Server(
		subroutine.mcp.tools.catalogue(client, workspace=workspace),
		name="subroutine",
		version=subroutine.__version__,
		instructions=_instructions(label, elsewhere, workspace),
		resources=subroutine.mcp.tools.references(client, workspace=workspace),
	)


def _instructions (
	label: str,
	elsewhere: typing.Sequence[str] = (),
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
	# **And the signpost is `#498`, on the rule decision `#499` settles: the channel that is
	# guaranteed must name every channel that is not.** Measured — this text and the tool schemas
	# reach an agent unconditionally, and named none of the three documents `#483` and `#486`
	# built. So 9.5 KB written for exactly this reader was unreachable because nothing said it
	# was there, which is the inert-control defect (`#247`, `#251`, `#303`) applied to prose.
	#
	# **Both routes to each, because one of them is client-dependent.** A `subroutine://` URI is
	# useless to a client that does not read resources — measured against one that exposed
	# listing them as a tool the agent had to go looking for — and `call_api` reaches all three
	# as ordinary routes. Naming both is what makes the pointer universal.
	#
	# **Signpost, never teach.** `#378` found that instructions which teach make the skill feel
	# redundant, in an agent's own words. The cost of over-correcting here is a paragraph that
	# replaces the thing it points at.
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
		f"'{label}'. You are a principal here rather than a tool being driven: what "
		f"you write is attributed to you and outlives your context. {where}{others}"
		f"If a 'subroutine' skill is available, read it before your first call — it carries "
		f"the conventions these tool descriptions do not. "
		f"Read subroutine://conventions before your first write: it is everything in force "
		f"here — decided, specified, designed or closed off — and it binds you. "
		f"/v1/documents?status=active is the same question for a client without resources. "
		f"There is more than these tools: read subroutine://docs/agent for what this is worth "
		f"and how it works, subroutine://docs/examples for worked calls, and subroutine://meta "
		f"for this workspace's own keys — or fetch them with subroutine_call_api at "
		f"/v1/docs/agent, /v1/docs/examples and /v1/meta. "
		f"These tools are a deliberate budget rather than the whole product: if you can run "
		f"commands, 'subroutine --help' is the complete surface. "
		f"Items are addressed by a number written #42, unique per "
		f"workspace and never reused, shared between tasks and documents. "
		f"A comment is what happened; a document is what you concluded — if the next "
		f"session would need to read it, it is a document."
	)
