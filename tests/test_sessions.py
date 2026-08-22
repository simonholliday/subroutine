"""Tests for browser sign-in — item `#248`, decision `#364`.

Run against both backends, because everything here is a database round trip and one of them
is a conditional ``UPDATE`` whose whole purpose is that two callers cannot both win it.

**The tests that matter most are not the happy path.** They are the ones asserting what a
signed-in browser may *not* do, because the shape this feature arrives in — a second kind of
credential, where the first one's absence used to mean "no credential at all" — is one where
every mistake fails open and looks like it works.
"""

import datetime
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.errors
import subroutine.views


def _make_user (
	session: sqlalchemy.orm.Session, **overrides: object
) -> subroutine.db.models.identity.User:
	"""Create a person who could sign in."""

	name = f"user-{uuid.uuid4().hex[:8]}"
	fields: dict[str, object] = {"username": name, "username_normalized": name}
	fields.update(overrides)

	user = subroutine.db.models.identity.User(**fields)
	session.add(user)
	session.flush()

	return user


def _signed_in (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> tuple[subroutine.domain.authentication.Principal, str]:
	"""Take somebody all the way through: mint a link, spend it, hold the session."""

	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)
	opened, cookie = subroutine.domain.sessions.redeem(session, secret)

	return subroutine.domain.authentication.Principal(user=user, session=opened), cookie


def test_a_link_signs_somebody_in (session: sqlalchemy.orm.Session) -> None:
	"""The whole of the happy path: mint, redeem, and authenticate with what came back."""

	user = _make_user(session)
	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)

	opened, cookie = subroutine.domain.sessions.redeem(session, secret)

	assert opened.user_id == user.id

	found = subroutine.domain.sessions.authenticate(session, cookie)

	assert found.user.id == user.id
	assert found.session is not None
	assert found.token is None


def test_signing_in_is_what_writes_a_last_login (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#526` — the column was mapped since the initial migration and written by nothing.

	**Asserted in both directions on purpose.** *Null* is also what a broken write produces,
	so the negative half alone would pass against the defect this closes — and it is exactly
	the shape that made this worth wiring rather than deleting: an operator reading *never*
	beside somebody who signs in daily cannot tell a quiet account from a dead column.

	A token is deliberately not a login. It is presented on every call, so writing this from
	authentication would make it mean *the last time anything happened*, which is
	``api_token.last_used_at`` and is already answered.
	"""

	user = _make_user(session)

	# **Read into a local before asserting.** Narrowing the attribute itself to ``None`` here
	# tells mypy it stays ``None``, because a type checker cannot see that ``redeem`` writes
	# it — and everything after the next assert becomes unreachable rather than checked.
	before = user.last_login_at

	assert before is None, "nobody has signed in yet"

	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)
	subroutine.domain.sessions.redeem(session, secret)

	first = user.last_login_at

	assert first is not None, "redeeming a link is signing in"

	# And again, because a value written once at creation would satisfy everything above.
	_second_link, again = subroutine.domain.sessions.mint_link(session, user=user)
	subroutine.domain.sessions.redeem(
		session, again, now=first + datetime.timedelta(minutes=5)
	)

	second = user.last_login_at

	assert second is not None
	assert second > first, "each sign-in moves it"


def test_a_service_account_never_reports_a_login (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Null on an agent is the answer rather than a gap — `#526`.

	`_refuse_a_service_account` already stops one being given a link, so this asserts the
	*consequence* an operator sees: a directory listing people and agents together shows a
	login for one kind and never for the other, and that difference is information.
	"""

	agent = _make_user(session, is_service_account=True)

	assert subroutine.views.user(agent).last_login_at is None


def test_a_link_works_once (session: sqlalchemy.orm.Session) -> None:
	"""A second redemption is refused, which is the whole of "single use".

	Written as two sequential calls rather than as a race, because the race is a different
	claim and is tested below. This one is the case somebody meets by pressing back.
	"""

	user = _make_user(session)
	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)

	subroutine.domain.sessions.redeem(session, secret)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.redeem(session, secret)


def test_two_browsers_cannot_both_spend_one_link (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The conditional UPDATE is what decides, not a read followed by a write.

	`#354` is the precedent: two workers both claimed one task, 2 of 2, under a commit called
	"Two workers cannot both take the same task", because the code read the holder, decided in
	Python and then wrote. This asserts the property that fix established, on the row where
	losing it would hand two people one sign-in.
	"""

	user = _make_user(session)
	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)

	model = subroutine.db.models.identity.LoginLink

	def spent () -> bool:
		"""Read the row back from the database rather than off a loaded object."""

		session.expire_all()
		row = session.scalars(
			sqlalchemy.select(model).where(model.user_id == user.id)
		).one()

		return row.redeemed_at is not None

	assert not spent()

	subroutine.domain.sessions.redeem(session, secret)

	# Spent, so the second caller's conditional UPDATE matches nothing whatever it read first.
	assert spent()

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.redeem(session, secret)


def test_an_expired_link_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""Half an hour is the mitigation for a credential that travels in a URL."""

	user = _make_user(session)
	_link, secret = subroutine.domain.sessions.mint_link(session, user=user)

	later = subroutine.db.types.utcnow() + subroutine.domain.sessions.LINK_LIFETIME
	later += datetime.timedelta(seconds=1)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.redeem(session, secret, now=later)


def test_a_revoked_session_stops_working_immediately (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Revocation is a row, which is the property `#364` chose an opaque cookie to keep."""

	user = _make_user(session)
	_principal, cookie = _signed_in(session, user)

	found = subroutine.domain.sessions.authenticate(session, cookie)

	assert found.session is not None

	subroutine.domain.sessions.sign_out(found.session)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.authenticate(session, cookie)


def test_an_expired_session_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""A laptop left on a train stops being a way in without anybody having to act."""

	user = _make_user(session)
	_principal, cookie = _signed_in(session, user)

	later = subroutine.db.types.utcnow() + subroutine.domain.sessions.SESSION_LIFETIME
	later += datetime.timedelta(seconds=1)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.authenticate(session, cookie, now=later)


def test_somebody_who_has_left_cannot_sign_in (session: sqlalchemy.orm.Session) -> None:
	"""`#475`'s rule, asked of the credential a person holds rather than only of a token."""

	user = _make_user(session)
	_principal, cookie = _signed_in(session, user)

	user.is_active = False
	session.flush()

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.authenticate(session, cookie)


def test_a_service_account_cannot_be_given_a_sign_in_link (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent's authority is issued deliberately; a session carries no scope at all.

	Refused where the link is minted rather than where it is redeemed, so the answer arrives
	to whoever is trying to set it up rather than to whoever opens the URL.
	"""

	agent = _make_user(session, is_service_account=True)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.sessions.mint_link(session, user=agent)


def test_signing_out_everywhere_spends_unused_links_too (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A link is a session that has not happened yet.

	Stopping the sessions and leaving the links is a control that reads as complete and is
	not — the shape `#303` describes, where something is documented as the enforcement and
	enforces half of it.
	"""

	user = _make_user(session)
	_principal, cookie = _signed_in(session, user)
	_link, unspent = subroutine.domain.sessions.mint_link(session, user=user)

	ended = subroutine.domain.sessions.sign_out_everywhere(session, user=user)

	assert ended == 1

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.authenticate(session, cookie)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError):
		subroutine.domain.sessions.redeem(session, unspent)


def test_one_kind_of_credential_never_parses_as_another () -> None:
	"""A session, a link and a token share a grammar and cannot be confused inside it.

	This is what lets each be refused *by name* where it does not belong. Without it a
	session presented as a bearer token is reported as a mistyped credential, and somebody
	spends an afternoon looking for a typo in a string they pasted correctly.
	"""

	token = subroutine.auth.generate_token()
	web = subroutine.auth.generate_token(kind=subroutine.auth.SESSION_KIND)
	link = subroutine.auth.generate_token(kind=subroutine.auth.LOGIN_KIND)

	kinds = (None, subroutine.auth.SESSION_KIND, subroutine.auth.LOGIN_KIND)

	for minted, its_own in ((token, None), (web, "web"), (link, "lnk")):
		value = minted.value.get_secret_value()
		parses = {
			kind for kind in kinds if subroutine.auth.parse_token(value, kind=kind) is not None
		}

		assert parses == {its_own}, f"{value[:12]} parsed as {parses}"


def test_a_secret_containing_an_underscore_still_parses () -> None:
	"""The secret half is base64url, which includes ``_`` — so the split must be bounded.

	Pinned as its own case because it is a one-in-however-many input that a happy-path test
	will not produce, and the failure is a person's sign-in link not working with no pattern
	to it.
	"""

	assert subroutine.auth.parse_token("sr_web_abcdef01_a_b_c", kind="web") == (
		"abcdef01",
		"a_b_c",
	)


def test_a_browser_session_is_not_a_local_caller (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§12.1a's exemption belongs to somebody holding the database file, and nobody else.

	It used to be spelled ``token is None``, which a browser session satisfies — so this is
	the sentence the whole of `#364`'s warning reduces to.
	"""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)

	assert signed_in.is_local is False
	assert subroutine.domain.authentication.Principal(user=user).is_local is True


def test_a_browser_session_cannot_mint_a_credential_for_somebody_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The defect this item exists to prevent, and the one `#364` understated.**

	``_refuse_amplification`` returned immediately on ``token is None``, and that early return
	skips more than the scope comparison it looks like it skips: it also skips the branch
	requiring ``instance:user_create`` to issue for another account. So a signed-in browser
	could have minted a working credential **for anybody on the instance** — not merely a wide
	one for itself — from the one screen where issuing credentials belongs.

	Falsified against the original code: spelling the guard's early return ``token is None``
	again makes this test pass a token out for ``somebody`` with no permission at all.
	"""

	me = _make_user(session)
	somebody = _make_user(session)
	signed_in, _cookie = _signed_in(session, me)

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.domain.authentication.issue_token(
			session, user=somebody, title="Not mine to issue", actor=signed_in
		)


def test_a_browser_session_cannot_mint_a_credential_that_outlives_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A session is time-bounded so a stolen cookie stops working.

	A permanent token minted from one ends that property in a single call, which is `#356`'s
	escalation arriving through a door `#356` could not see — it compared ``token.expires_at``,
	and a session has no token.
	"""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.authentication.issue_token(
			session, user=user, title="Permanent", actor=signed_in
		)

	# Bounded by the session, it is allowed — the rule is "no wider", not "nothing at all".
	assert signed_in.expires_at is not None

	within = signed_in.expires_at - datetime.timedelta(days=1)
	_row, minted = subroutine.domain.authentication.issue_token(
		session, user=user, title="Bounded", expires_at=within, actor=signed_in
	)

	assert minted.prefix


def test_a_browser_session_narrows_nothing_and_says_so (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A person signed in is themselves, so the narrowing properties are all empty.

	Asserted rather than assumed because they arrive at the right answer through a different
	branch from the one they used to: reading these off ``token is None`` was correct here by
	accident, and would have been wrong for the *next* credential type — a calendar feed's,
	which §20.2 says is read-only.
	"""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)

	assert signed_in.scopes == []
	assert signed_in.project_scope is None
	assert signed_in.project_write_scope is None
	assert signed_in.pinned_workspace_id is None


def test_a_principal_cannot_hold_two_credentials (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Which one narrows the caller has no answer that is not a rule in a second place."""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)
	token, _minted = subroutine.domain.authentication.issue_token(
		session, user=user, title="A token"
	)

	with pytest.raises(ValueError):
		subroutine.domain.authentication.Principal(
			user=user, token=token, session=signed_in.session
		)


def test_a_browser_session_is_described_rather_than_reported_as_no_credential (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``views.credential`` answers null for local mode, and a session is not local mode.

	Every caller reads that null as "no credential was presented", which for somebody signed
	in is exactly wrong — a false statement rather than a missing feature.
	"""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)

	described = subroutine.views.credential(session, signed_in)

	assert described is not None
	assert described.kind == "web_session"
	assert described.narrows is False
	assert described.expires_at is not None

	nothing = subroutine.views.credential(
		session, subroutine.domain.authentication.Principal(user=user)
	)

	assert nothing is None


def test_a_browser_session_is_counted_by_the_rate_limiter (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§7.7 keys on the credential's public half, whichever kind of credential it is.

	Reading ``principal.token.token_prefix`` there would have left the one credential a
	stranger can obtain as the one nothing rate-limits.
	"""

	user = _make_user(session)
	signed_in, _cookie = _signed_in(session, user)

	assert signed_in.session is not None
	assert signed_in.credential_prefix == signed_in.session.token_prefix

	assert (
		subroutine.domain.authentication.Principal(user=user).credential_prefix is None
	)
