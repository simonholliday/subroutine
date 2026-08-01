"""Credentials over HTTP — SPEC.md §7.4, item ``#208``.

**The gap this closes is the one `#196` was, one surface along.** ``POST /v1/users`` has been
able to add Ana since `#174`, and ``POST /v1/workspaces/{…}/members`` can say where she works —
and then nothing over HTTP could give her a way in. An administrator running the deployment
``docs/hosting.md`` describes, who has no shell on the server, could create an account that
could never be used.

``tests/test_reach.py`` could not see it. It walks the endpoints and asks whether each reaches
the CLI and MCP; a capability with no endpoint at all is absent from everything it enumerates,
so the guard was silent about the one direction it does not look in. That is the third instance
of the shape `#193` kept finding, and it is written down in `#208` rather than left as a
coincidence.

**Nothing here decides who may do what.** ``issue_token`` already refuses to mint a credential
wider than the one asking for it — wider scopes, a wider project scope, an unpinned workspace,
or another user without ``instance:user_create`` — and ``tokens.revoke`` already asks whether
this actor may. Both were written for the CLI and take an actor; what was missing was a caller.

**No account is created here, deliberately.** The CLI's ``--service-account`` makes a machine
identity as it goes because a person at a terminal is doing one thing; over HTTP,
``POST /v1/users`` already exists and says what it does. Two calls that each name their own act
beat one that quietly performs two — which is the reasoning `#207` applied to the flags, one
level down.
"""

import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.domain.tokens
import subroutine.domain.users
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/tokens",
	tags=["tokens"],
	route_class=subroutine.api.routing.Transactional,
)

#: What ``?fields=`` may name, read off the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Token)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/tokens`` accepts.

	Every field narrows. There is no field that widens, and there could not be: §7.4's whole
	least-privilege story rests on a credential staying at most as wide as the one that asked
	for it, which ``issue_token`` enforces rather than this model.
	"""

	title: str

	#: Who it is for, by the name ``GET /v1/users`` shows. Omitted means the caller themselves,
	#: which is the ordinary case and needs no permission beyond having authenticated. Naming
	#: somebody else needs ``instance:user_create`` — the same authority as creating the
	#: account, since an account plus a credential for it is one act in two steps.
	username: str | None = None

	#: Pin it to one workspace, by id or slug. Null means every workspace its owner belongs to.
	#: **Never pinned by default** (§7.4, §13.7): narrowing a credential to shorten an address,
	#: or because one workspace is the common case, is letting a convenience decide the access
	#: model.
	workspace: str | None = None

	#: Narrow it to these permissions. ``[]`` and null both mean *no narrowing*, not "no
	#: permissions" — read as literal set algebra they would mean the opposite, which is the
	#: easiest way to issue a credential that can do nothing at all.
	scopes: list[str] | None = None

	#: Restrict it to these projects and their subtrees, by id. Null means every project the
	#: owner can reach. An **empty list is refused** rather than guessed at: one reading widens
	#: it to everything and the other denies it everything, and picking either on the caller's
	#: behalf gets a security control wrong in silence.
	project_scope: list[str] | None = None

	#: When it stops working, in §9.3's grammar — ``2026-09-01`` or ``now+30d``. It names a
	#: whole day and the credential works through the end of it, the same reading a deadline
	#: gets.
	expires: str | None = None


@router.post("", status_code=201, summary="Issue a credential")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.IssuedToken:
	"""Mint a credential and return it once.

	**The secret is in this response and in nothing else, ever.** Only a hash is stored, so
	nothing recovers it afterwards — including this instance. Store it when you receive it.

	A credential may never grant more than the one that asked for it: wider scopes, a wider set
	of projects, or an unpinned workspace where the caller's own token is pinned are all
	refused, as is issuing for somebody else without ``instance:user_create``.
	"""

	owner = actor.user if body.username is None else _account(session, body.username)
	pinned = (
		None
		if body.workspace is None
		else subroutine.domain.selection.workspace(session, actor, requested=body.workspace)
	)

	_row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=owner,
		title=body.title,
		workspace_id=None if pinned is None else pinned.id,
		scopes=body.scopes or (),
		project_scope=body.project_scope,
		expires_at=subroutine.domain.tokens.expires_on(
			body.expires,
			timezone=subroutine.domain.schedule.zone_for(
				user=actor.user, instance=subroutine.domain.instances.get(session)
			),
		),
		created_by=actor.user.id,
		actor=actor,
	)

	rendered = subroutine.views.token(
		_row, owner=owner, secret=issued.value.get_secret_value()
	)

	# Narrowed to the type the endpoint promises. `views.token` answers with the base type when
	# no secret was asked for, and a `cast` here would be a claim rather than a check.
	assert isinstance(rendered, subroutine.views.IssuedToken)

	return rendered


@router.get(
	"",
	summary="List the credentials you can act on",
	dependencies=[subroutine.api.query.UnknownQueryDep],
	response_model=subroutine.views.Collection[subroutine.views.Token],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""The credentials this caller may see, newest first.

	**Narrowed the same way revoking is**, so nothing appears here that the caller could not
	then act on: your own, the ones you issued, and — for an instance administrator —
	everything. An inventory you can read and not revoke is a worse answer than a short one.

	Not paginated, for the reason ``GET /v1/users`` gives: an instance's credentials are
	bounded by how many somebody issued.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="token"
	)
	found = subroutine.domain.tokens.issued_tokens(session, actor=actor)
	owners = _owners(session, found)

	return subroutine.api.shaping.response(
		[subroutine.views.token(row, owner=owners.get(row.user_id)) for row in found],
		subroutine.views.Page(
			limit=len(found), has_more=False, next_cursor=None, total=len(found)
		),
		shape,
	)


@router.delete("/{id_or_prefix}", summary="Revoke a credential")
def revoke (
	id_or_prefix: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Token:
	"""Stop a credential working, now.

	Immediate: ``revoked_at`` is checked on every request rather than cached anywhere, so there
	is no session to wait out. That is what makes this the answer when a token has leaked.

	Idempotent, and it keeps the first revocation time — when a credential stopped being
	trusted is a fact worth not overwriting, and a caller retrying should not change it. The
	revoked credential is returned rather than an empty 204 so that a repeat call is
	distinguishable from a first one.

	Addressed by id or by the public prefix, because the prefix is what a listing prints and
	what somebody has in front of them when a token turns up in a log.
	"""

	found = _mine(session, actor, id_or_prefix)
	stopped = subroutine.domain.tokens.revoke(session, found, actor=actor)
	owner = session.get(subroutine.db.models.identity.User, stopped.user_id)

	return subroutine.views.token(stopped, owner=owner)


def _account (
	session: sqlalchemy.orm.Session, username: str
) -> subroutine.db.models.identity.User:
	"""Return the account a credential is being issued for, or say why there is none.

	**Inactive is as good as absent**, the same rule the CLI applies (`#207`): authentication
	refuses a token whose owner is not active, so issuing one for a deactivated account mints a
	credential that is accepted here and refused the first time it is presented.
	"""

	model = subroutine.db.models.identity.User
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == subroutine.domain.users.normalize(username),
			model.deleted_at.is_(None),
			model.is_active.is_(True),
		)
	).one_or_none()

	if found is not None:
		return found

	raise subroutine.errors.NotFound(
		f"There is no active account called {username!r} here.",
		errors=[
			subroutine.errors.FieldError(
				field="username",
				code="not_found",
				message=f"No usable account is called {username!r}.",
				hint="GET /v1/users lists them. A deactivated account cannot hold a working "
				"credential, so one is not issued for it.",
			)
		],
	)


def _mine (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	id_or_prefix: str,
) -> subroutine.db.models.identity.ApiToken:
	"""Find a credential this caller may act on, or report that there is no such thing.

	Resolved out of :func:`subroutine.domain.tokens.issued_tokens`, which is the set they may
	already read — so a credential belonging to somebody else is *absent* rather than
	forbidden, and this endpoint discloses nothing a listing would not.
	"""

	wanted = id_or_prefix.strip()

	for candidate in subroutine.domain.tokens.issued_tokens(session, actor=actor):
		if candidate.token_prefix == wanted or str(candidate.id) == wanted:
			return candidate

	raise subroutine.errors.NotFound(
		f"There is no credential {id_or_prefix!r} you can act on.",
		errors=[
			subroutine.errors.FieldError(
				field="id_or_prefix",
				code="not_found",
				message=f"No credential answers to {id_or_prefix!r}.",
				hint="GET /v1/tokens lists the ones you can see, with the prefix this takes. "
				"Never send a whole token here — the prefix is the public half.",
			)
		],
	)


def _owners (
	session: sqlalchemy.orm.Session,
	rows: typing.Sequence[subroutine.db.models.identity.ApiToken],
) -> dict[uuid.UUID, subroutine.db.models.identity.User]:
	"""Return the accounts these credentials belong to, in one query rather than per row."""

	wanted = {row.user_id for row in rows}

	if not wanted:
		return {}

	model = subroutine.db.models.identity.User

	return {
		found.id: found
		for found in session.scalars(sqlalchemy.select(model).where(model.id.in_(wanted)))
	}
