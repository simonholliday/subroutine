"""Signing in from a browser: a link that is spent once, and the session it buys.

Decision `#364` settled the credential shape and item `#248` builds it. Two credentials,
because they answer two questions and have two lifetimes: a **login link** lasts minutes and
proves that somebody reached the address, once; a **web session** lasts days and proves that
this browser is still the person who did.

**Nothing here sends email.** A link is minted and handed over — by a command at a terminal
today (§12.4's recovery property, which is what makes this safe to ship at all), and by an
endpoint that mails it once `#599` is built. That split is deliberate: an unauthenticated
route that mails an address a stranger chooses is where every danger `#364` §3 enumerates
actually lives, and none of it is needed to let four people sign in to their own instance.
"""

import datetime
import typing

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.errors
import subroutine.permissions

#: How long a link works for. Short because it travels in a URL — into browser history, a
#: referrer header, and whatever carried the message. Long enough to survive a mail relay
#: having a bad afternoon and somebody reading their inbox after lunch.
LINK_LIFETIME = datetime.timedelta(minutes=30)

#: How long a browser stays signed in. A working fortnight: long enough that a person is
#: not signing in every morning, short enough that a laptop left on a train stops being a
#: way in without anybody having to notice and act.
SESSION_LIFETIME = datetime.timedelta(days=14)

#: How often a session's ``last_used_at`` is written. Same reasoning as the token's, and
#: the same defect avoided: a write per request is a row lock held for the length of the
#: request, which `#565` proved can deadlock one request against itself.
LAST_USED_INTERVAL = datetime.timedelta(minutes=5)


def mint_link (
	session: sqlalchemy.orm.Session,
	*,
	user: subroutine.db.models.identity.User,
	actor: subroutine.domain.authentication.Principal | None = None,
	now: datetime.datetime | None = None,
) -> tuple[subroutine.db.models.identity.LoginLink, str]:
	"""Issue a single-use login link for somebody, returning the row and its secret.

	The secret is readable exactly once, here, and is returned rather than stored: what is
	kept is a hash, so nothing — including this program — can produce it again afterwards.

	``actor`` is who asked, recorded for the audit trail. ``None`` is somebody at a terminal
	with the database file, which §12.1a says is a caller no check narrows.
	"""

	_refuse_administering_somebody_else(actor, user, doing="issue a sign-in link for")
	_refuse_an_account_that_cannot_sign_in(user)

	moment = now if now is not None else subroutine.db.types.utcnow()
	minted = subroutine.auth.generate_token(kind=subroutine.auth.LOGIN_KIND)

	link = subroutine.db.models.identity.LoginLink(
		user_id=user.id,
		token_prefix=minted.prefix,
		token_hash=minted.token_hash,
		expires_at=moment + LINK_LIFETIME,
		created_by=None if actor is None else actor.user.id,
	)

	session.add(link)
	session.flush()

	return link, minted.value.get_secret_value()


def redeem (
	session: sqlalchemy.orm.Session,
	presented: str,
	*,
	now: datetime.datetime | None = None,
) -> tuple[subroutine.db.models.identity.WebSession, str]:
	"""Spend a login link and return the session it buys, with its secret.

	**The link is marked spent before the session exists**, so two browsers racing the same
	link cannot both get one: the write is conditional on the row still being unredeemed and
	loses for whoever arrives second. Read-decide-write here would be `#354` again, where two
	workers both claimed one task under a commit message saying they could not.

	Every refusal raises the same
	:class:`~subroutine.domain.authentication.AuthenticationError` as a bad token does.
	Telling "no such link" from "already used" apart would say, to somebody holding a link
	they should not have, whether they were too late or simply wrong.
	"""

	parsed = subroutine.auth.parse_token(presented, kind=subroutine.auth.LOGIN_KIND)

	if parsed is None:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.MALFORMED
		)

	prefix, secret = parsed
	moment = now if now is not None else subroutine.db.types.utcnow()

	model = subroutine.db.models.identity.LoginLink
	link = session.scalars(
		sqlalchemy.select(model).where(model.token_prefix == prefix)
	).one_or_none()

	if link is None or not subroutine.auth.token_matches(secret, link.token_hash):
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.UNKNOWN, prefix=prefix
		)

	if link.expires_at <= moment:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.EXPIRED, prefix=prefix
		)

	# One conditional UPDATE, so the database decides who won rather than this process. A
	# link that is already spent matches nothing and the row count is zero.
	#
	# Cast because DML is typed as a plain `Result` and only a cursor result carries the row
	# count, which here is the entire answer — the same shape as `claims.claim`.
	spent = typing.cast(
		"sqlalchemy.CursorResult[typing.Any]",
		session.execute(
			sqlalchemy.update(model)
			.where(model.id == link.id, model.redeemed_at.is_(None))
			.values(redeemed_at=moment)
		),
	)

	# The statement went round the ORM, so the loaded row is stale until it is told so.
	session.expire(link)

	if spent.rowcount != 1:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.REVOKED, prefix=prefix
		)

	user = session.get(subroutine.db.models.identity.User, link.user_id)

	if user is None:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.USER_INACTIVE,
			prefix=prefix,
		)

	_refuse_an_account_that_cannot_sign_in(user, prefix=prefix)

	minted = subroutine.auth.generate_token(kind=subroutine.auth.SESSION_KIND)

	opened = subroutine.db.models.identity.WebSession(
		user_id=user.id,
		token_prefix=minted.prefix,
		token_hash=minted.token_hash,
		expires_at=moment + SESSION_LIFETIME,
		login_link_id=link.id,
	)

	session.add(opened)
	session.flush()

	return opened, minted.value.get_secret_value()


def authenticate (
	session: sqlalchemy.orm.Session,
	presented: str,
	*,
	now: datetime.datetime | None = None,
	record_use: bool = True,
) -> subroutine.domain.authentication.Principal:
	"""Resolve a presented session cookie into the principal holding it.

	The same shape as
	:func:`subroutine.domain.authentication.authenticate` and refusing for the same reasons
	in the same words, because a browser session is a credential and everything true of a
	token's refusals is true of one of these.
	"""

	parsed = subroutine.auth.parse_token(presented, kind=subroutine.auth.SESSION_KIND)

	if parsed is None:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.MALFORMED
		)

	prefix, secret = parsed
	moment = now if now is not None else subroutine.db.types.utcnow()

	model = subroutine.db.models.identity.WebSession
	opened = session.scalars(
		sqlalchemy.select(model).where(model.token_prefix == prefix)
	).one_or_none()

	if opened is None or not subroutine.auth.token_matches(secret, opened.token_hash):
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.UNKNOWN, prefix=prefix
		)

	if opened.revoked_at is not None:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.REVOKED, prefix=prefix
		)

	if opened.expires_at <= moment:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.EXPIRED, prefix=prefix
		)

	user = session.get(subroutine.db.models.identity.User, opened.user_id)

	if user is None:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.USER_INACTIVE,
			prefix=prefix,
		)

	_refuse_an_account_that_cannot_sign_in(user, prefix=prefix)

	if record_use and (
		opened.last_used_at is None or moment - opened.last_used_at >= LAST_USED_INTERVAL
	):
		opened.last_used_at = moment

	return subroutine.domain.authentication.Principal(user=user, session=opened)


def sign_out (
	opened: subroutine.db.models.identity.WebSession,
	*,
	now: datetime.datetime | None = None,
) -> None:
	"""Stop a browser session working, immediately.

	Revocation is a row rather than a wait, which is the property a self-describing signed
	credential could not have offered — §7.4's argument, and the reason `#364` struck the
	JWT half of §7.5.
	"""

	if opened.revoked_at is None:
		opened.revoked_at = now if now is not None else subroutine.db.types.utcnow()


def sign_out_everywhere (
	session: sqlalchemy.orm.Session,
	*,
	user: subroutine.db.models.identity.User,
	actor: subroutine.domain.authentication.Principal | None = None,
	now: datetime.datetime | None = None,
) -> int:
	"""Revoke every live session a person holds, and report how many that was.

	This is what "I have lost my laptop" needs, and it is one statement rather than a loop so
	that a session opened while it runs cannot slip between the read and the write.

	**Unspent links are revoked too**, because a link is a session that has not happened yet
	and stopping the sessions while leaving the links would be a control that looks complete
	and is not.
	"""

	_refuse_administering_somebody_else(actor, user, doing="sign out")

	moment = now if now is not None else subroutine.db.types.utcnow()

	links = subroutine.db.models.identity.LoginLink

	session.execute(
		sqlalchemy.update(links)
		.where(links.user_id == user.id, links.redeemed_at.is_(None))
		.values(redeemed_at=moment)
	)

	model = subroutine.db.models.identity.WebSession

	stopped = typing.cast(
		"sqlalchemy.CursorResult[typing.Any]",
		session.execute(
			sqlalchemy.update(model)
			.where(model.user_id == user.id, model.revoked_at.is_(None))
			.values(revoked_at=moment)
		),
	)

	return int(stopped.rowcount)


def _refuse_administering_somebody_else (
	actor: subroutine.domain.authentication.Principal | None,
	user: subroutine.db.models.identity.User,
	*,
	doing: str,
) -> None:
	"""Refuse acting on another person's sign-in unless the caller may administer accounts.

	**The same gate as issuing a credential for somebody else**, and deliberately so: handing
	out a link that signs in as them, and revoking the sessions they are working in, are both
	acts on another person's access rather than on their work. A caller acting on their own
	needs nothing — you may always sign yourself out.

	``None`` is §12.1a, a person at a terminal holding the database file, and is not narrowed.
	"""

	if actor is None or actor.is_local or actor.user.id == user.id:
		return

	# Imported here rather than at the top: `domain.authorization` imports
	# `domain.authentication`, so a module-level import here would be a cycle. The alias is
	# what stops `subroutine` being rebound as a local name for the rest of this function.
	from subroutine.domain import authorization as permits

	permits.authorize_instance(actor, subroutine.permissions.INSTANCE_USER_CREATE)


def _refuse_an_account_that_cannot_sign_in (
	user: subroutine.db.models.identity.User, *, prefix: str | None = None
) -> None:
	"""Refuse an account that is deleted, inactive, or not a person at all.

	**A service account may not hold a browser session**, and that is the same rule as
	`#487`'s rather than a new one: an agent's credential is issued deliberately, with a
	scope and a reach, and a session carries neither. Minting one for a service account
	would be a way to hand an agent unbounded authority by signing in as it.
	"""

	if user.deleted_at is not None or not user.is_active:
		raise subroutine.domain.authentication.AuthenticationError(
			subroutine.domain.authentication.AuthenticationFailure.USER_INACTIVE,
			prefix=prefix,
		)

	if user.is_service_account:
		raise subroutine.errors.Forbidden(
			f"{user.username!r} is a service account, which cannot sign in to a browser.",
			hint="Service accounts work through API tokens, which carry the scope and "
			"reach a session does not. Create one with 'subroutine token create "
			f"--service-account {user.username}'.",
		)
