"""A private project in the trash is counted, named and rescued like a live one - `SR#5313`.

A bullet of L-1 of the cold review of 2026-11-11, decided in `#5285`: jo's public `outer` holds
a private `inner` only pat sees. Making `outer` private was refused, until `inner` went into the
trash; then it was allowed, and nobody could see `inner`, restore it or find it again. **The
last-one checks count it, the listings name it as in the trash, and the rescue restores it before
sharing it.** The hints say how it comes out first (Simon's answer of 2026-11-21): restoring takes
a role that may delete projects, and only pat sees it.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.projects
import subroutine.domain.repair
import subroutine.domain.selection
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views
import test_sight_follows_the_chain as chain

User = subroutine.db.models.identity.User
Project = subroutine.db.models.project.Project


class Place(typing.NamedTuple):
	"""jo's MetaCortex, pat in it, and pat's private `inner` in the trash."""

	workspace: subroutine.db.models.identity.Workspace
	jo: User
	pat: User
	outer: Project
	inner: Project


def _place (
	session: sqlalchemy.orm.Session, *, role: str = "member", private: bool = False
) -> Place:
	"""Build it: `outer` is jo's, and shared with pat where it is private."""

	jo = chain._person(session, "jo")
	pat = chain._person(session, "pat")
	workspace = chain._metacortex(session, jo, pat=(pat, role))
	outer = chain._project(
		session, workspace, "outer", jo, visibility="private" if private else "public"
	)

	if private:
		subroutine.domain.projects.share(session, outer, pat, actor=chain._as(jo))

	inner = chain._project(session, workspace, "inner", pat, parent=outer)
	session.flush()
	subroutine.domain.projects.delete(session, inner)
	session.flush()

	return Place(workspace, jo, pat, outer, inner)


def _refused (act: typing.Callable[[], object]) -> subroutine.errors.ValidationError:
	"""Return what an act is refused with."""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		act()

	return refused.value


#: The hint where pat's role may restore it, and where it may not.
MAY_RESTORE = "Ask {pat} to restore it from the trash and share it with somebody else first."
MAY_NOT = (
	"{pat}'s role cannot restore it from the trash. Give {pat} a role that may delete projects "
	"first, then ask them to restore it and share it with somebody else."
)


def test_making_its_parent_private_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""The verifier's case: allowed once `inner` was in the trash, and refused now."""

	place = _place(session)
	refused = _refused(
		lambda: subroutine.domain.projects.update(
			session, place.outer, visibility="private", actor=chain._as(place.jo)
		)
	)

	assert refused.detail == (
		"Making outer private would leave no person able to reach something you cannot see "
		"inside it."
	), refused.detail
	assert place.outer.visibility == "public"


@pytest.mark.parametrize(
	("role", "hint"), [("member", MAY_NOT), ("admin", MAY_RESTORE)], ids=["member", "admin"]
)
def test_taking_its_only_viewer_out_is_refused_saying_what_comes_first (
	session: sqlalchemy.orm.Session, role: str, hint: str
) -> None:
	"""The hint offered sharing it or making it public, which nothing in the trash takes."""

	place = _place(session, role=role)
	refused = _refused(
		lambda: subroutine.domain.workspaces.remove_member(
			session, place.workspace, place.pat, actor=chain._as(place.jo)
		)
	)

	assert "something you cannot see" in refused.detail, refused.detail
	assert refused.hint == hint.format(pat=place.pat.username), refused.hint


def test_unsharing_its_only_viewer_from_above_it_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`outer` is private and shared with pat, who alone sees the trashed `inner` inside it."""

	place = _place(session, private=True)
	refused = _refused(
		lambda: subroutine.domain.projects.unshare(
			session, place.outer, place.pat, actor=chain._as(place.jo)
		)
	)

	assert refused.hint == MAY_NOT.format(pat=place.pat.username), refused.hint


def test_once_nobody_can_see_it_it_is_listed_and_named_as_in_the_trash (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A deactivation names it, and the administrator's listing gives its ``deleted_at``."""

	place = _place(session)
	laurence = subroutine.domain.users.create(session, username="laurence-5313", is_superuser=True)
	session.flush()
	named = subroutine.domain.projects.unreachable(session, leaving=place.pat)

	assert [row.project.id for row in named] == [place.inner.id]

	subroutine.domain.users.set_active(session, place.pat, active=False)
	session.flush()
	(listed,) = [
		subroutine.views.unreachable_project(row)
		for row in subroutine.domain.projects.unreachable(session, actor=chain._as(laurence))
		if row.project.id == place.inner.id
	]

	assert listed.deleted_at == place.inner.deleted_at
	assert listed.model_dump(mode="json")["deleted_at"] is not None
	assert listed.label() == f"{listed.address()} (in the trash)"


def test_the_rescue_restores_it_and_then_shares_it (session: sqlalchemy.orm.Session) -> None:
	"""Decided in `#5285`: an item in the trash changes only by restoring it, so the rescue does."""

	place = _place(session)
	laurence = subroutine.domain.users.create(session, username="laurence-5313", is_superuser=True)
	subroutine.domain.users.set_active(session, place.pat, active=False)
	session.flush()
	# **Named by its address, as both transports name it**, through the rescue's own lookup.
	found = subroutine.domain.selection.project_to_share(
		session, chain._as(laurence), place.workspace, "outer/inner"
	)
	subroutine.domain.projects.share(session, found, place.jo, actor=chain._as(laurence))
	session.flush()

	assert found.id == place.inner.id

	assert place.inner.deleted_at is None
	assert subroutine.domain.repair.repairers(session, place.inner) == {place.jo.id}
