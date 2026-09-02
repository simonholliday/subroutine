"""What this installation is called, and where it says it is — item `#1669`.

**Its own module rather than a second verb on ``/v1/meta``**, because meta is a read of many
things at once and this writes exactly one row. The address is a literal under ``/v1`` sharing
a prefix with nothing, so ``api.routing.check`` is content wherever it is mounted.

**There is no ``GET`` here.** ``/v1/meta`` already reports the instance to every authenticated
caller, and a second reader would be two answers to one question — which is the defect this
project keeps finding, rather than a convenience.
"""

import typing

import fastapi

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.domain.instances
import subroutine.domain.workspaces
import subroutine.views

#: What ``?fields=`` may name on the workspace listing, read off the view.
ON_INSTANCE_FIELDS = subroutine.api.shaping.selectable(subroutine.views.WorkspaceOnInstance)


router = fastapi.APIRouter(
	prefix="/v1/instance",
	tags=["instance"],
	# **Every mounted router is transactional** (§8.1) — the transaction commits *before* the
	# response is sent, because FastAPI closes a request's dependency exit stack after the
	# application has emitted it. Without this a caller is told the name changed and then the
	# commit fails with nobody to tell.
	route_class=subroutine.api.routing.Transactional,
)


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/instance`` accepts."""

	#: What to call this installation. Not its identity — that is the id, which cannot move.
	name: str | None = None

	#: The last word in the timezone chain, for everybody who has not set their own and whose
	#: workspace has not set one either.
	timezone: str | None = None


@router.patch("", summary="Change what this installation is called")
def change (
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Instance:
	"""Change this installation's name or its timezone.

	Needs ``instance:admin``, which no role carries and only a superuser holds. Deciding what a
	whole installation is called is not something a member of one workspace in it should be
	able to do.

	An omitted field is left alone, and neither may be set to nothing.
	"""

	# **Read from ``model_fields_set``, never from the value** (§8.3). Omitted and explicitly
	# null are different requests, and reading `body.name is None` would make them the same —
	# which here would mean every `PATCH` that changed the timezone also tried to blank the
	# name. That is `#1444`'s own `Move.parent` defect, and it is what this pattern exists for.
	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		field: getattr(body, field)
		for field in ("name", "timezone")
		if field in supplied and getattr(body, field) is not None
	}

	changed = subroutine.domain.instances.update(session, actor=actor, **changes)

	return subroutine.views.instance(changed)


@router.get(
	"/workspaces",
	summary="Every workspace on this installation",
	response_model=subroutine.views.Collection[subroutine.views.WorkspaceOnInstance],
)
def workspaces (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List every workspace here, whether or not the caller is a member.

	Needs ``instance:admin``, which no role carries and only a superuser holds.

	**Discovery, not reach.** ``GET /v1/workspaces`` lists what this caller can *work in* and
	is unchanged; this says what *exists*, and nothing a workspace contains is widened by it.
	Anybody holding ``instance:workspace_create`` can make a workspace the instance owner is
	not in, and until this route the owner's answer to *what is here* silently left it out —
	an empty list, which reads as nothing being there rather than as something unseen.

	Enveloped and unpaginated, like a workspace's members (\u00a78.4): the number of workspaces on
	an installation is bounded by how many somebody made.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=ON_INSTANCE_FIELDS,
		entity="workspace",
		timezone=subroutine.views.reader_zone(session, actor),
	)
	rows = subroutine.domain.workspaces.on_instance(session, actor=actor)

	return subroutine.api.shaping.response(
		[subroutine.views.workspace_on_instance(row) for row in rows],
		subroutine.views.Page(limit=len(rows), has_more=False, next_cursor=None, total=None),
		shape,
	)
