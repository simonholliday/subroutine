"""What this installation is called, and where it says it is — item `#1669`.

**Its own module rather than a second verb on ``/v1/meta``**, because meta is a read of many
things at once and this writes exactly one row. The address is a literal under ``/v1`` sharing
a prefix with nothing, so ``api.routing.check`` is content wherever it is mounted.

**The instance's name and zone are read through ``/v1/meta``**, which reports them to every
authenticated caller, so nothing here reads them a second time: two answers to one question is
the defect this project keeps finding. The ``GET`` routes here are inventories for an
administrator - every workspace, and what somebody leaving would make unreachable or leave with
nobody to administer.
"""

import typing

import fastapi

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.domain.instances
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.views

#: What ``?fields=`` may name on the workspace listing, read off the view.
ON_INSTANCE_FIELDS = subroutine.api.shaping.selectable(subroutine.views.WorkspaceOnInstance)

#: What ``?fields=`` may name on the listing of projects nobody can reach, read off the view.
UNREACHABLE_FIELDS = subroutine.api.shaping.selectable(subroutine.views.UnreachableProject)


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
	# **And an explicit null is passed on rather than dropped** — `SR#2295`. Filtering it here
	# undid the sentence above: `{"name": null}` answered 200 with the name unchanged while
	# `{"name": ""}` answered 422, so the two spellings of *set it to nothing* got opposite
	# replies and the silent one reported success. §8.3's convention is that a null is a value,
	# and `api/projects.py` and `api/workspaces.py` both pass one to the domain and let it
	# refuse — the two places that do filter carry a comment saying why, and this had none.
	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		field: getattr(body, field) for field in ("name", "timezone") if field in supplied
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
	not in, and until this route the owner's answer to *what is here* silently left it out -
	an empty list, which reads as nothing being there rather than as something unseen.

	Enveloped and unpaginated, like a workspace's members: the number of workspaces on
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


@router.get(
	"/unreachable-projects",
	summary="Private projects nobody here can reach",
	response_model=subroutine.views.Collection[subroutine.views.UnreachableProject],
)
def unreachable_projects (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	leaving: str | None = fastapi.Query(
		None,
		description="Instead, the ones that would be left unreachable if this person left.",
	),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the private projects that no member who can still act is able to see.

	Needs ``instance:admin``, which no role carries and only a superuser holds, and a credential
	that is neither pinned to one workspace nor narrowed to some projects.

	A private project is visible only to its members. When none of them can see it any more -
	each has been deactivated, answers to somebody who has, has been taken out of the workspace, or
	is hidden from it by a private project above - nothing can make it public or share it again.
	This lists those projects by address, title and membership count, and nothing inside them.
	``POST /v1/projects/{id_or_key}/members`` then lets somebody back in, and an administrator may
	do that for a project listed here; it is recorded like any other share. ``member_of_workspace``
	says whether you belong to the project's workspace: where you do not, join it first, with
	``POST /v1/workspaces/{id_or_slug}/members``.

	With ``leaving``, it answers the question to ask before deactivating somebody instead: which
	projects that somebody can see now would nobody be able to see afterwards.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=UNREACHABLE_FIELDS,
		entity="project",
		timezone=subroutine.views.reader_zone(session, actor),
	)
	rows = subroutine.domain.projects.unreachable(
		session,
		actor=actor,
		leaving=None if leaving is None else subroutine.domain.users.by_username(session, leaving),
	)

	return subroutine.api.shaping.response(
		[subroutine.views.unreachable_project(row) for row in rows],
		subroutine.views.Page(limit=len(rows), has_more=False, next_cursor=None, total=None),
		shape,
	)


@router.get(
	"/unadministered-workspaces",
	summary="Workspaces nobody here can administer",
	response_model=subroutine.views.Collection[subroutine.views.WorkspaceOnInstance],
)
def unadministered_workspaces (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	leaving: str | None = fastapi.Query(
		None,
		description="Instead, the ones that would be left with nobody to administer them if this "
		"person left.",
	),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the workspaces that no person who can act may administer.

	Needs ``instance:admin``, which no role carries and only a superuser holds, and a credential
	that is not pinned to one workspace.

	An administrator here is a person whose role in the workspace may administer it and who can
	still act: not deactivated. An agent is not counted, since it stops when its person goes, so a
	workspace only an agent administers is listed. A workspace with none still
	works for its members, and none of them can add, regrade or remove a member, or delete it. An
	administrator of this installation can make somebody its administrator again, themselves
	included, with ``POST /v1/workspaces/{id_or_slug}/members`` or by changing a member's role,
	whether or not they belong to it.

	With ``leaving``, it answers the question to ask before deactivating somebody instead: which
	workspaces would be left with no person to administer them, where they or an agent of theirs
	administers it now.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=ON_INSTANCE_FIELDS,
		entity="workspace",
		timezone=subroutine.views.reader_zone(session, actor),
	)
	rows = subroutine.domain.workspaces.unadministered(
		session,
		actor=actor,
		leaving=None if leaving is None else subroutine.domain.users.by_username(session, leaving),
	)

	return subroutine.api.shaping.response(
		[subroutine.views.workspace_on_instance(row) for row in rows],
		subroutine.views.Page(limit=len(rows), has_more=False, next_cursor=None, total=None),
		shape,
	)


@router.get(
	"/unowned-workspaces",
	summary="Workspaces with no owner who can act",
	response_model=subroutine.views.Collection[subroutine.views.WorkspaceOnInstance],
)
def unowned_workspaces (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	leaving: str | None = fastapi.Query(
		None,
		description="Instead, the ones that would be left with no owner who can act if this "
		"person left.",
	),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the workspaces with no owner who can act.

	Needs ``instance:admin``, which no role carries and only a superuser holds, and a credential
	that is not pinned to one workspace.

	An owner here is a person whose role in the workspace may delete it and who can still act. A
	workspace with none still works for its members, and nobody in it can make an owner or delete
	it - until a person who administers it makes somebody its owner, which is allowed only then.

	With ``leaving``, it answers the question to ask before deactivating somebody instead: which
	workspaces would be left with no owner who can act, where they or an agent of theirs owns it.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=ON_INSTANCE_FIELDS,
		entity="workspace",
		timezone=subroutine.views.reader_zone(session, actor),
	)
	rows = subroutine.domain.workspaces.unowned(
		session,
		actor=actor,
		leaving=None if leaving is None else subroutine.domain.users.by_username(session, leaving),
	)

	return subroutine.api.shaping.response(
		[subroutine.views.workspace_on_instance(row) for row in rows],
		subroutine.views.Page(limit=len(rows), has_more=False, next_cursor=None, total=None),
		shape,
	)
