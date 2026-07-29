"""The base every request body inherits, and the one rule it exists to hold.

**Unknown fields are refused, never ignored.** A caller that sends ``due`` where the field
is ``due_at`` gets a 422 naming the field and listing the ones that exist, rather than a
201 for a task with no due date and an agent that believes it set one (SPEC.md §8.1).

Pydantic's default is to drop what it does not recognise, so this has to be asked for —
which means it has to be asked for on every model, which means it belongs on a base class
rather than in a habit.
"""

import pydantic


class RequestModel(pydantic.BaseModel):
	"""Base for every body this API accepts."""

	model_config = pydantic.ConfigDict(extra="forbid")
