"""The command line: one of four ways in, and the one a person meets first.

The others are the HTTP API, the MCP tool surface an agent reaches, and the browser. This
said the command line was *"the only interface Subroutine has until the API lands"* long
after it landed — 107 mounted routes, which ``scripts/parity.py`` treats as the reference
surface the other three are measured against.

Two audiences share it, and the design rule is that neither is served by compromising for
the other (docs/design.md §12.2a): a person setting up a to-do list should never be shown a
workspace, and an agent should never have to parse prose to find out what happened.
"""
