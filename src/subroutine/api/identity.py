"""``GET /v1/me`` — who the caller is, and exactly what they may do.

The point of this endpoint is that an agent should not have to discover its own authority by
being refused things (docs/design.md §13.1). What it reports, and why the empty-scope sentinel is
spelled out rather than left to be interpreted, is documented on
:func:`subroutine.views.me` — which is where the answer is assembled, so that a client asking
over a socket and a client asking its own database get the same one (§13.7).

**A transport, and nothing else.** It had the assembly and the four response models until
``#336``, which is why no client could reach it: the shapes lived in the ``api`` package, so
the local client had nothing to return and the HTTP client had nothing to parse into. The
same divergence ``views.py`` exists to prevent, met once more one endpoint at a time.
"""

import fastapi

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1",
	tags=["identity"],
	route_class=subroutine.api.routing.Transactional,
)


@router.get(
	"/me",
	summary="Who am I, and what may I do?",
)
def me (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Me:
	"""Report the caller's identity, credential and effective permissions."""

	return subroutine.views.me(session, actor)
