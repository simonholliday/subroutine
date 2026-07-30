"""Reaching an instance, whether it is this one or one across a network.

The package is deliberately empty. Re-export aliases in a package ``__init__`` reintroduce
the circular-import problem that ``import x``-only plus fully-qualified names otherwise
avoids, so every module here is imported by its own name.
"""
