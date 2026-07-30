"""Subroutine as an MCP server: the same work, reachable by an agent with no shell.

Implemented against the published specification (2025-06-18) rather than against the
reference SDK, and that is a decision rather than an expedient. ``scripts/check_licences.py``
exists because a copyleft dependency would bind the copyright holder even though their own
licence does not (§2.2a), so every addition to the runtime closure is a cost this project has
already decided to weigh. The part of MCP a tool server needs — newline-delimited JSON-RPC
over stdio, an ``initialize`` handshake, ``tools/list`` and ``tools/call`` — is small and
stable, and the message shapes here were taken from the specification text, not from memory.

**Nothing here talks to a database.** Every tool goes through ``subroutine.clients``, which
is what the CLI uses, so an MCP session against a remote instance behaves exactly as one
against the local database and neither can drift from the other. That is S3-07's rule applied
to a third transport.
"""
