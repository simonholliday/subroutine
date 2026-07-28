"""The command line, which is the only interface Subroutine has until the API lands.

Two audiences share it, and the design rule is that neither is served by compromising for
the other (SPEC.md §12.2a): a person setting up a to-do list should never be shown a
workspace, and an agent should never have to parse prose to find out what happened.
"""
