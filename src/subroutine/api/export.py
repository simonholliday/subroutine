"""``GET /v1/export/<kind>``: everything of one kind a credential may take away, a page at a time.

Decision ``#4049``. One route per kind, so each answers a :class:`~subroutine.views.Collection`
of that kind's own view and OpenAPI can say which. **Paged like every listing here, not
streamed**: a route's transaction commits before its response is sent (``routing.Transactional``),
so a body written after it would read outside it. ``subroutine.exporting`` decides the rows and
renders them; this signs a cursor around a page.
"""

import types
import typing

import fastapi

import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.security
import subroutine.domain.paging
import subroutine.domain.selection
import subroutine.exporting
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/export",
	tags=["export"],
	route_class=subroutine.api.routing.Transactional,
)

WORKSPACE = fastapi.Query(
	None, description="Which workspace, by id or short name. Needed when you can reach several."
)


def _route (
	kind: subroutine.exporting.Kind,
) -> typing.Callable[..., typing.Any]:
	"""Return the route that exports one kind."""

	keys = (subroutine.api.pagination.SortKey(name=kind.order.key, column=kind.order),)
	collection = f"export-{kind.name}"

	def exported (
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		settings: subroutine.api.dependencies.SettingsDep,
		workspace_id: str | None = WORKSPACE,
		limit: int | None = fastapi.Query(
			None,
			# No `ge=1`: `domain.paging.size` is the one arbiter, as on every listing.
			description=subroutine.api.pagination.LIMIT_DESCRIPTION,
		),
		cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	) -> typing.Any:
		"""Return everything of this kind you can read in a workspace, a page at a time.

		Each item is the object the API returns for it everywhere else. Done, archived and
		deleted items are included, and so are the templates repeating items are made from; a
		private project you are not a member of is not, nor anything in it. Follow
		``next_cursor`` until ``has_more`` is false, and the pages together are the whole of it.
		"""

		workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		size = subroutine.domain.paging.size(limit, settings)
		after = None

		if cursor is not None:
			after = subroutine.api.pagination.decode(
				settings.require_secret_key(), keys, cursor, collection=collection
			)[0]

		found = subroutine.exporting.page(
			session, actor, workspace_id=workspace.id, kind=kind.name, after=after, size=size
		)

		# **From the last row fetched, not the last item shown** - `exporting.Page.resume`
		# says why a page can be short with more to come.
		last = types.SimpleNamespace(**{kind.order.key: found.resume})

		return subroutine.views.Collection[typing.Any](
			items=found.items,
			page=subroutine.views.Page(
				limit=size,
				has_more=found.has_more,
				next_cursor=(
					subroutine.api.pagination.encode(
						settings.require_secret_key(), keys, last, collection=collection
					)
					if found.has_more
					else None
				),
				total=None,
			),
		)

	return exported


#: The envelope every listing answers in, to be given each kind's view as the routes are made.
#: Held as ``typing.Any`` because the view is a value here, which a type checker cannot read as
#: the type parameter it becomes.
_ENVELOPE: typing.Any = subroutine.views.Collection

for _kind in subroutine.exporting.KINDS.values():
	router.add_api_route(
		f"/{_kind.name}",
		_route(_kind),
		methods=["GET"],
		response_model=_ENVELOPE[_kind.view],
		name=f"export_{_kind.name}",
		summary=f"Export the {_kind.name.replace('_', ' ')} you can read",
	)
