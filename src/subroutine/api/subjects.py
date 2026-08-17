"""Resolving the thing a sub-resource hangs off, once rather than per sub-resource.

``/v1/tasks/{ref}/comments`` and ``/v1/tasks/{ref}/events`` ask exactly the same question
before they do anything else: *which task is this, and may this caller see it?* So do the
project and document forms of both. Six routers asking it six times is six chances for one
of them to ask it slightly differently — and "slightly differently" here means a listing
that shows an item somebody may not see.

**Resolving the subject is the permission check.** Each of these goes through the narrowed
statement its entity already has (``readable_tasks``, ``readable_projects``,
``readable_documents``), so an item the caller cannot see is reported as *absent* rather
than forbidden — §7.3a's existence rule, which a "forbidden" would break by confirming the
item exists. Everything hanging off the subject is then safe to return, because a
sub-resource is exactly as visible as the thing it hangs off.

That property is what makes a per-entity history cheap and the change feed expensive: a
history has a subject to resolve, and the feed has to compose the same predicates itself
(docs/design.md §5.11a).
"""

import typing

import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.selection

#: What each entity type calls its path parameter. A project is addressed by key and the
#: other two by ref, which is why this is a lookup rather than one name.
ADDRESS = {"task": "id_or_ref", "project": "id_or_key", "document": "id_or_ref"}


def resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	entity_type: str,
	address: str,
	workspace_id: str | None = None,
) -> typing.Any:
	"""Return the task, project or document an address names, or report it as absent.

	Imported inside the function rather than at module scope, and that is not laziness:
	``api.tasks``, ``api.projects`` and ``api.documents`` each mount routers that will want
	*this* module, so importing them here at module scope is a cycle. The house style's
	nested-import exception exists for exactly this, and the alternative — moving three
	resolvers out of the routers that own them — would put the definition of "which tasks
	exist" further from the endpoint that answers it.
	"""

	import subroutine.api.documents
	import subroutine.api.projects
	import subroutine.api.tasks

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)

	if entity_type == "task":
		return subroutine.api.tasks._resolve(session, actor, workspace, address)

	if entity_type == "project":
		return subroutine.api.projects.resolve(session, actor, workspace, address)

	if entity_type == "document":
		return subroutine.api.documents._resolve(session, actor, workspace, address)

	raise ValueError(f"{entity_type!r} has no sub-resources here.")
