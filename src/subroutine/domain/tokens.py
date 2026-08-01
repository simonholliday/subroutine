"""Looking after credentials after they are issued — SPEC.md §7.4, item ``#156``.

**A sibling of `authentication` rather than part of it**, and the import graph is what
decides that: `authorization` already imports `authentication` for its `Principal`, so
`authentication` cannot ask `authorization` who may do what. Revoking a token is an
authority question about an authentication object, so it belongs to neither and imports
both.

The column this writes has been read on every request since M1. `revoked_at` is checked at
authentication time rather than cached anywhere, which is why revocation is immediate and
why this module is short — the enforcement was never the missing part. Nothing could *set*
it, so an instance could issue credentials and never take one back.
"""

import datetime

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.errors
import subroutine.permissions


def issued_tokens (
	session: sqlalchemy.orm.Session, *, actor: subroutine.domain.authentication.Principal
) -> list[subroutine.db.models.identity.ApiToken]:
	"""Return the credentials this operator may see, newest first.

	**Narrowed the same way revocation is**, so a listing never shows something the caller
	could not act on — an inventory you can read and not revoke is a worse answer than one
	that is short.
	"""

	model = subroutine.db.models.identity.ApiToken
	statement = sqlalchemy.select(model).order_by(model.created_at.desc())

	if not _may_administer_credentials(actor):
		statement = statement.where(
			sqlalchemy.or_(
				model.user_id == actor.user.id, model.created_by == actor.user.id
			)
		)

	return list(session.scalars(statement))


def revoke (
	session: sqlalchemy.orm.Session,
	token: subroutine.db.models.identity.ApiToken,
	*,
	now: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.ApiToken:
	"""Stop a credential working, from this instant, if this actor may.

	**The mechanism is `authentication.revoke_token` and is not repeated here.** That function
	has existed since M1, is idempotent, and keeps the first revocation time; this adds the
	only thing it could not, which is the authority question — `authorization` imports
	`authentication`, so the check cannot live beside the write.

	The first version of this module *did* repeat it, having been written without looking. It
	is the ordinary way a second implementation of something arrives.

	**Who may**: the person the token was issued *for*, the person who issued it, or an
	instance administrator. The first two are what the case this was built for needs — a
	month's work on somebody else's instance, where the issuer has to be able to take access
	back without anybody's help, and the holder has to be able to burn their own if it leaks.
	"""

	if actor is not None and not _may_revoke(actor, token):
		raise subroutine.errors.Forbidden(
			"Only the person a credential was issued for, the person who issued it, or an "
			"instance administrator may revoke it."
		)

	subroutine.domain.authentication.revoke_token(token, at=now)
	session.flush()

	return token


def _may_administer_credentials (actor: subroutine.domain.authentication.Principal) -> bool:
	"""Whether this principal may act on credentials that are nothing to do with them."""

	try:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_USER_CREATE
		)

	except subroutine.errors.SubroutineError:
		return False

	return True


def _may_revoke (
	actor: subroutine.domain.authentication.Principal, token: subroutine.db.models.identity.ApiToken
) -> bool:
	"""Whether this principal may revoke that credential."""

	if token.user_id == actor.user.id or token.created_by == actor.user.id:
		return True

	return _may_administer_credentials(actor)
