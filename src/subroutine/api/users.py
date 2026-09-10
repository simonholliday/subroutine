"""Accounts over HTTP — docs/design.md §7.1, item ``#174``.

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
import sqlalchemy

import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.users
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/users",
	tags=["users"],
	route_class=subroutine.api.routing.Transactional,
)

#: What ``?fields=`` may name, read off the view so the two cannot drift (docs/design.md §14.10).
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

	#: Whether this account administers the installation — item ``#701``. **The only source of
	#: an instance-tier permission there is**: no role can carry one, because ``seed.py`` builds
	#: roles from :data:`subroutine.permissions.WORKSPACE_LEVEL`, so without this field an
	#: instance has exactly the one superuser ``init`` made and no way to a second. The view has
	#: reported it since M1 and nothing could set it.
	#:
	#: Refused for an agent asking, in the service layer: handing out administration is a
	#: person's act, and an administering agent making more of itself is `#356`'s amplification
	#: one tier up.
	is_superuser: bool = False


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
		is_superuser=body.is_superuser,
		actor=actor,
	)

	return subroutine.views.user(
		created,
		answers_to=subroutine.domain.accountability.answerable_name(session, created),
	)


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/users/{username}`` accepts.

	The two halves of somebody leaving, and they belong together: one records that they have
	gone, the other keeps the agents that would otherwise stop with them. Either alone is a
	control people work around — losing an agent is a price nobody pays willingly, so a leaver
	simply does not get marked as one.
	"""

	#: False marks somebody as having left. Every agent answerable to them stops working, which
	#: is decision `#473` and is the point rather than a side effect.
	is_active: bool | None = None

	#: Hand this agent to somebody else, who becomes answerable for it (`#478`). Named by
	#: username, like everything else a person types here. Only a person may take one on, and
	#: only a person may hand one over.
	responsible: str | None = None

	#: Where this person keeps their diary — §6.5's user level, and **their own account only**
	#: (`#994`, Simon's decision of 2026-08-18). Absent leaves it alone and null clears it, so
	#: the workspace's zone and then the instance's show through again (§8.3). Null is a value
	#: here rather than a gap: *not stated* is what makes the chain a chain.
	timezone: str | None = None


@router.patch(
	"/{username}", summary="Mark somebody as having left, hand an agent over, or say where you are"
)
def update (
	username: str,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.User:
	"""Mark somebody as having left or brought back, or hand an agent to somebody else.

	**Both in one call, and the order is deliberate**: handing the agents over happens before the
	deactivation, so somebody clearing up after a leaver in a single request keeps what they
	meant to keep.

	Needs ``instance:user_create`` — the same grant as making an account, because deciding
	somebody works here and deciding they no longer do are the same decision twice.

	**Except ``timezone``, which needs no permission and is refused for anybody but yourself.**
	The check is *are you this person*, not something anybody can be granted: §6.5's user level
	records where somebody keeps their diary, and a permission to write it would be a
	permission to be wrong on their behalf.

	**Deactivating stops every agent answerable to that person**, at their next call, wherever
	they are running. Ask for the list first with ``GET /v1/users?answers_to=<username>``: the
	CLI names them before it does it, and a caller here should too. **That used to say to read
	``GET /v1/users`` and pick the rows out by ``responsible_user_id``** — which was a whole
	directory fetched to answer one question, and it stopped being possible the moment that
	listing was paged (`SR#2384`, `SR#2387`). It also only ever found the agents answerable
	*directly*; the filter walks the chain, which is what "answerable to that person" means
	one sentence above.

	The last person who can administer the instance is refused, because an instance nobody can
	administer cannot be repaired from inside and would have stopped every agent on it.
	"""

	account = subroutine.domain.users.by_username(session, username)

	if body.responsible is not None:
		subroutine.domain.users.transfer(
			session,
			account,
			to=subroutine.domain.users.by_username(session, body.responsible),
			actor=actor,
		)

	if body.is_active is not None:
		subroutine.domain.users.set_active(
			session, account, active=body.is_active, actor=actor
		)

	# **Read from `model_fields_set` rather than from the value**, because null is a value here:
	# clearing a timezone puts the reader back on the workspace's, which is §8.3's whole
	# distinction and the reason the two fields above cannot be written this way.
	if "timezone" in body.model_fields_set:
		subroutine.domain.users.set_timezone(
			session, account, timezone=body.timezone, actor=actor
		)

	return subroutine.views.user(
		account,
		answers_to=subroutine.domain.accountability.answerable_name(session, account),
	)


@router.get(
	"",
	summary="List the accounts on this instance",
	response_model=subroutine.views.Collection[subroutine.views.User],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	answers_to: str | None = fastapi.Query(
		None,
		description="Only the agents answerable to this person, directly or through another.",
	),
	limit: int | None = fastapi.Query(
		None,
		# No `ge=1`: `domain.paging.size` is the one arbiter, so this and the local client
		# refuse an impossible page identically, naming `limit` rather than `query.limit`.
		description=subroutine.api.pagination.LIMIT_DESCRIPTION,
	),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(False, description="Count the whole result."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Who is on this instance, oldest first.

	**Paged like every other listing here** (`SR#2384`). It used to take a ceiling of 200 rows
	from the domain, supply no ``limit`` of its own, and answer with a literal
	``has_more: false`` — so past two hundred accounts it dropped rows and stated there were no
	more, with a null total that could not contradict it. The docstring said ``has_more`` was
	always false *for the same reason a task's links are*, and that reason does not survive a
	ceiling: a task's links are bounded by what somebody typed, where this was bounded by a
	number we chose.

	**``answers_to`` names a person and returns the agents answerable to them** (`SR#2387`),
	directly or through another. It exists because paging this listing would otherwise break the
	one thing that reads it whole: ``subroutine user deactivate`` says *"this also stops N
	agent(s)"* before it acts, and a confirmation that under-reports is worse than the truncation
	this route was fixed for. A caller wanting one account by name wants
	``GET /v1/users/{username}`` (`SR#2386`) rather than this listing and a filter in their own
	code.

	**``total`` is opt-in**, which is §8.4's rule; the old envelope answered null and meant
	nothing by it.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=SELECTABLE,
		entity="user",
		timezone=subroutine.views.reader_zone(session, actor),
	)

	model = subroutine.db.models.identity.User
	statement = subroutine.domain.users.readable(session, actor=actor, answers_to=answers_to)

	keys = subroutine.api.pagination.parse_order(
		None,
		allowed=subroutine.domain.ordering.USER_FIELDS,
		default=subroutine.domain.ordering.DEFAULT_USER_ORDER,
		tiebreak=model.id,
	)
	size = subroutine.domain.paging.size(limit, settings)
	total = None

	if include_total:
		total = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(statement.subquery())
		)

	if cursor is not None:
		statement = statement.where(
			subroutine.api.pagination.after(
				keys,
				subroutine.api.pagination.decode(
					settings.require_secret_key(), keys, cursor, collection="users"
				),
			)
		)

	found = list(
		session.scalars(statement.order_by(*[key.ordering() for key in keys]).limit(size + 1))
	)
	has_more = len(found) > size
	found = found[:size]

	# **One walk for the whole page** (`#1420`). Resolving a chain per row is §8.4's N+1
	# wearing a rendering hat, and this listing is where a fleet of agents shows up.
	answerable = subroutine.domain.accountability.answerable_for_many(
		session, [row.id for row in found]
	)

	return subroutine.api.shaping.response(
		[
			subroutine.views.user(row, answers_to=answerable.get(row.id))
			for row in found
		],
		subroutine.views.Page(
			limit=size,
			has_more=has_more,
			next_cursor=(
				subroutine.api.pagination.encode(
					settings.require_secret_key(), keys, found[-1], collection="users"
				)
				if has_more and found
				else None
			),
			total=total,
		),
		shape,
	)


@router.get(
	"/{username}",
	summary="Read one account",
	response_model=subroutine.views.User,
)
def one (
	username: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.User:
	"""Read the account with this name — `SR#2386`.

	**The API was behind its own domain here.** ``domain.users.by_username`` has always existed
	and both ``POST`` and ``PATCH`` resolve through it; nothing published a way to *read* one
	account, so a caller wanting ``si`` fetched the whole directory and filtered it themselves.
	That is a client re-implementing a lookup the server owns, and it is why paging the listing
	could not be done on its own (`SR#2384`).

	**Readable by anyone authenticated**, for the same reason the listing is: an identifier is
	unique and public where content is neither, and this view carries no email address and no
	content at all.

	Case-insensitive, because ``by_username`` resolves through the normalised column — ``Simon``
	and ``simon`` being two accounts would be a trap rather than a feature.
	"""

	account = subroutine.domain.users.by_username(session, username)

	return subroutine.views.user(
		account,
		answers_to=subroutine.domain.accountability.answerable_name(session, account),
	)
