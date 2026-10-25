"""A prefix already taken is rolled again, for every kind of credential - `SR#5132`.

**A bullet of L-7 of the cold review of 2026-10-17**: API tokens and calendar feeds re-rolled a
taken prefix, and sign-in links and browser sessions did not, so a clash ended a sign-in in an
integrity error. Made to happen here by handing the generator back a prefix a row already holds.
"""

import dataclasses
import uuid

import pytest
import sqlalchemy.orm

import subroutine.auth
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.domain.users


def _repeating (monkeypatch: pytest.MonkeyPatch, prefix: str, times: int) -> None:
	"""Make the generator hand out ``prefix`` for its next ``times`` calls, then mint as usual."""

	real = subroutine.auth.generate_token
	left = [times]

	def generated (*, kind: str | None = None) -> subroutine.auth.IssuedToken:
		"""Return the taken prefix while any repeats are left."""

		issued = real(kind=kind)

		if left[0] == 0:
			return issued

		left[0] -= 1

		return dataclasses.replace(issued, prefix=prefix)

	monkeypatch.setattr(subroutine.auth, "generate_token", generated)


def test_a_link_and_a_session_are_minted_past_a_taken_prefix (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A sign-in link and a browser session handed a taken prefix each take the next one, as an
	API token does."""

	user = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	first, opening = subroutine.domain.sessions.mint_link(session, user=user)

	_repeating(monkeypatch, first.token_prefix, 1)
	second, again = subroutine.domain.sessions.mint_link(session, user=user)

	assert second.token_prefix != first.token_prefix

	opened, _secret = subroutine.domain.sessions.redeem(session, opening)

	_repeating(monkeypatch, opened.token_prefix, 1)
	reopened, _secret = subroutine.domain.sessions.redeem(session, again)

	assert reopened.token_prefix != opened.token_prefix

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="First"
	)
	session.flush()

	_repeating(monkeypatch, token.token_prefix, 1)
	other, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="Second"
	)

	assert other.token_prefix != token.token_prefix


def test_every_attempt_taken_is_the_refusal_a_token_gives (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Where every attempt is taken, a link is refused as a token would be, naming its table."""

	user = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	first, _secret = subroutine.domain.sessions.mint_link(session, user=user)

	_repeating(monkeypatch, first.token_prefix, subroutine.domain.authentication.PREFIX_ATTEMPTS)

	with pytest.raises(RuntimeError, match="unused login_link prefix in 8 attempts"):
		subroutine.domain.sessions.mint_link(session, user=user)
