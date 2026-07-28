"""Business rules: the layer that knows what the application means.

Sits between the database models, which know only shapes, and the API and CLI, which know
only requests. Nothing here imports either of those, so a rule can be exercised without a
server and reused unchanged by both front ends.

No names are re-exported, for the same reason as ``subroutine.db.models``: modules are
addressed by their fully-qualified names.
"""
