"""Accounts over HTTP — SPEC.md §7.1, item ``#174``.

**The gap this closes was a page called "Running it for a team" that could not add the second
member of one.** ``init`` made exactly one account; the only other identity anybody could
create was a service account, which the CLI itself calls a machine identity — so a five-person
team shared one login, or every colleague was modelled as a robot.

That is worse than a missing feature, because several things the product already advertises
cannot be exercised without it. Private projects grant sight through a membership row (§7.3a);
roles exist and are seeded per workspace; every write is attributed. None of it means anything
on an instance that can hold one person.

**Creating an account is an instance-tier act** (§7.1): it happens outside every workspace, so
no role can carry ``instance:user_create`` and only a superuser holds it. A token still narrows
it, which is what makes it safe to give an agent a credential that may do this and nothing
else.

**Listing is not.** Anyone authenticated may read the directory, because adding a colleague to
a workspace means naming them and a name nobody can look up has to be passed along out of band.
Decision ``#161`` is what makes that safe to say: identifiers are unique and public, content is
neither — and :class:`subroutine.views.User` carries no email address and no content at all.
"""

import typing

import fastapi

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.domain.users
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/users",
	tags=["users"],
	route_class=subroutine.api.routing.Transactional,
)

#: What ``?fields=`` may name, read off the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.User)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/users`` accepts.

	**No password**, and that is a decision rather than an omission. Subroutine authenticates
	with bearer tokens (§7.4); a password field here would imply a login this build does not
	have, and would put a credential in a request body for no one to use. A new account is
	given a token with ``subroutine token create --username``.
	"""

	username: str
	display_name: str | None = None
	email: str | None = None
	timezone: str | None = None

	#: A machine identity rather than a person. Reported everywhere it is read, because a list
	#: mixing the two with nothing to tell them apart is one where somebody adds the agent to
	#: the stand-up.
	is_service_account: bool = False


@router.post("", status_code=201, summary="Create an account")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.User:
	"""Add a person, or a machine identity, to this instance.

	Needs ``instance:user_create``. The new account belongs to no workspace yet — joining it to
	one is a separate act with a separate permission, because deciding that somebody exists and
	deciding where they may work are different decisions and often different people.
	"""

	created = subroutine.domain.users.create(
		session,
		username=body.username,
		email=body.email,
		display_name=body.display_name,
		timezone=body.timezone,
		is_service_account=body.is_service_account,
		actor=actor,
	)

	return subroutine.views.user(created)


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/users/{username}`` accepts.

	Only ``is_active`` for now, and that is the whole of `#475`: the column was enforced in four
	places and written in none, so *this person has left* was a state the product could not
	reach while four code paths were written as though it could.
	"""

	#: False marks somebody as having left. Every agent answerable to them stops working, which
	#: is decision `#473` and is the point rather than a side effect.
	is_active: bool


@router.patch("/{username}", summary="Mark somebody as having left, or bring them back")
def update (
	username: str,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.User:
	"""Change whether an account is active.

	Needs ``instance:user_create`` — the same grant as making an account, because deciding
	somebody works here and deciding they no longer do are the same decision twice.

	**Deactivating stops every agent answerable to that person**, at their next call, wherever
	they are running. Ask for the list first with ``GET /v1/users`` and
	``responsible_user_id``: the CLI names them before it does it, and a caller here should
	too. The last person who can administer the instance is refused, because an instance
	nobody can administer cannot be repaired from inside and would have stopped every agent on
	it.
	"""

	account = subroutine.domain.users.by_username(session, username)

	subroutine.domain.users.set_active(session, account, active=body.is_active, actor=actor)

	return subroutine.views.user(account)


@router.get(
	"",
	summary="List the accounts on this instance",
	dependencies=[subroutine.api.query.UnknownQueryDep],
	response_model=subroutine.views.Collection[subroutine.views.User],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Who is on this instance, oldest first.

	**Not paginated, and that is a statement rather than a shrug.** An instance's people are
	bounded by how many somebody hired — the same argument §8.4 makes for a task's links, where
	``has_more`` is always false for the same reason. A ceiling is applied anyway so that a
	directory cannot become an unbounded response by accident, and it is far above any real
	instance.

	Oldest first, because the first account is the one ``init`` made and a reader is usually
	looking for the ones that came after it.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="user"
	)
	found = subroutine.domain.users.listed(session, actor=actor)

	return subroutine.api.shaping.response(
		[subroutine.views.user(row) for row in found],
		subroutine.views.Page(limit=len(found), has_more=False, next_cursor=None, total=None),
		shape,
	)
