"""Credentials over HTTP — SPEC.md §7.4, item ``#208``.

**The gap this closes is the one `#196` was, one surface along.** ``POST /v1/users`` has been
able to add Thomas since `#174`, and ``POST /v1/workspaces/{…}/members`` can say where they
work — and then nothing over HTTP could give them a way in. An administrator running the deployment
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

import fastapi

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.db.models.identity
import subroutine.domain.tokens
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

	title: str | None = None

	#: Who it is for, by the name ``GET /v1/users`` shows. Omitted means the caller themselves,
	#: which is the ordinary case and needs no permission beyond having authenticated. Naming
	#: somebody else needs ``instance:user_create`` — the same authority as creating the
	#: account, since an account plus a credential for it is one act in two steps.
	username: str | None = None

	#: Name a machine identity, creating one if there is none. Distinct from ``username``,
	#: which never creates: naming a person here is refused rather than quietly issuing their
	#: credential under an argument whose stated subject is machines (`#207`).
	service_account: str | None = None

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

	_row, owner, issued, created = subroutine.domain.tokens.issue(
		session,
		actor=actor,
		title=body.title,
		username=body.username,
		service_account=body.service_account,
		workspace=body.workspace,
		scopes=body.scopes or (),
		projects=body.project_scope,
		expires=body.expires,
	)

	rendered = subroutine.views.token(
		_row,
		owner=owner,
		secret=issued.value.get_secret_value(),
		account_created=created,
		session=session,
		principal=actor,
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
	owners = subroutine.domain.tokens.owners(session, found)

	return subroutine.api.shaping.response(
		[
			subroutine.views.token(
				row, owner=owners.get(row.user_id), session=session, principal=actor
			)
			for row in found
		],
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

	found = subroutine.domain.tokens.mine(session, actor, id_or_prefix)
	stopped = subroutine.domain.tokens.revoke(session, found, actor=actor)
	owner = session.get(subroutine.db.models.identity.User, stopped.user_id)

	return subroutine.views.token(stopped, owner=owner, session=session, principal=actor)
