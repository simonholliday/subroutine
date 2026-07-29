"""The base every request body inherits, and the one rule it exists to hold.

**Unknown fields are refused, never ignored.** A caller that sends ``due`` where the field
is ``due_at`` gets a 422 naming the field and listing the ones that exist, rather than a
201 for a task with no due date and an agent that believes it set one (SPEC.md §8.1).

Pydantic's default is to drop what it does not recognise, so this has to be asked for —
which means it has to be asked for on every model, which means it belongs on a base class
rather than in a habit.
"""

import pydantic

#: A field that names another work item, by ref or by id.
#:
#: A ref is an integer in every response this API sends (SPEC.md §6.2), so an integer is
#: accepted here as well as a string: a client should be able to send back what it was
#: given without converting it, and a rule that says "read a number, quote it, send it" is
#: a rule somebody will get wrong. An id is a UUID and arrives as a string.
Reference = int | str


class RequestModel(pydantic.BaseModel):
	"""Base for every body this API accepts."""

	model_config = pydantic.ConfigDict(extra="forbid")
