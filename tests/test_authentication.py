"""Tests for issuing tokens and resolving them back into a principal.

Run against both backends, because everything here is a database round trip: an indexed
prefix lookup, a timestamp comparison, and a throttled write on the hottest read path in
the application.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.cli.main
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.errors
import subroutine.permissions


def _make_user (
	session: sqlalchemy.orm.Session, **overrides: object
) -> subroutine.db.models.identity.User:
	"""Create a user to own tokens."""

	name = f"user-{uuid.uuid4().hex[:8]}"
	fields: dict[str, object] = {"username": name, "username_normalized": name}
	fields.update(overrides)

	user = subroutine.db.models.identity.User(**fields)
	session.add(user)
	session.flush()

	return user


def _make_workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Create a workspace for a token to be pinned to."""

	workspace = subroutine.db.models.identity.Workspace(
		slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace"
	)
	session.add(workspace)
	session.flush()

	return workspace


def _issue (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	**kwargs: typing.Any,
) -> tuple[subroutine.db.models.identity.ApiToken, str]:
	"""Issue a token and return the row alongside its presentable string."""

	token, issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="Test token", **kwargs
	)

	return token, issued.value.get_secret_value()


def test_a_token_round_trips (session: sqlalchemy.orm.Session) -> None:
	"""Issue, present, and be recognised."""

	user = _make_user(session)
	token, presented = _issue(session, user)

	principal = subroutine.domain.authentication.authenticate(session, presented)

	assert principal.user.id == user.id
	assert principal.token is not None
	assert principal.token.id == token.id


def test_the_secret_is_never_stored (session: sqlalchemy.orm.Session) -> None:
	"""Nothing in the row can be turned back into the credential."""

	user = _make_user(session)
	token, presented = _issue(session, user)
	secret = presented.split("_", 2)[2]

	row = session.execute(
		sqlalchemy.select(subroutine.db.models.identity.ApiToken).where(
			subroutine.db.models.identity.ApiToken.id == token.id
		)
	).one()

	for value in row[0].__dict__.values():
		assert secret not in str(value)

	assert token.token_hash != secret
	assert token.token_prefix in presented


def test_a_wrong_secret_is_refused_like_an_unknown_one (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Guessing half a credential must not be detectable."""

	user = _make_user(session)
	token, presented = _issue(session, user)

	tampered = presented[:-1] + ("a" if presented[-1] != "a" else "b")

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as wrong:
		subroutine.domain.authentication.authenticate(session, tampered)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as absent:
		subroutine.domain.authentication.authenticate(session, "sr_deadbeef_nonesuch")

	assert wrong.value.failure is subroutine.domain.authentication.AuthenticationFailure.UNKNOWN
	assert absent.value.failure is wrong.value.failure
	assert token.token_prefix == wrong.value.prefix


def test_a_malformed_token_never_reaches_the_database (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Shape is checked first, and the failure names no prefix because none was readable."""

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as error:
		subroutine.domain.authentication.authenticate(session, "not-a-token")

	assert error.value.failure is subroutine.domain.authentication.AuthenticationFailure.MALFORMED
	assert error.value.prefix is None


def test_revocation_takes_effect_immediately (session: sqlalchemy.orm.Session) -> None:
	"""No grace period, no cache to wait out."""

	user = _make_user(session)
	token, presented = _issue(session, user)

	subroutine.domain.authentication.authenticate(session, presented)
	subroutine.domain.authentication.revoke_token(token)
	session.flush()

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as error:
		subroutine.domain.authentication.authenticate(session, presented)

	assert error.value.failure is subroutine.domain.authentication.AuthenticationFailure.REVOKED


def test_revoking_twice_keeps_the_first_time (session: sqlalchemy.orm.Session) -> None:
	"""When a credential stopped being trusted is worth not overwriting."""

	user = _make_user(session)
	token, _ = _issue(session, user)

	first = subroutine.db.types.utcnow() - datetime.timedelta(hours=1)
	subroutine.domain.authentication.revoke_token(token, at=first)
	subroutine.domain.authentication.revoke_token(token)

	assert token.revoked_at == first


def test_an_expired_token_is_refused_and_a_future_one_is_not (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Expiry is compared against the moment of the request."""

	user = _make_user(session)
	now = subroutine.db.types.utcnow()

	_, live = _issue(session, user, expires_at=now + datetime.timedelta(days=1))
	_, stale = _issue(session, user, expires_at=now - datetime.timedelta(seconds=1))

	assert subroutine.domain.authentication.authenticate(session, live, now=now) is not None

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as error:
		subroutine.domain.authentication.authenticate(session, stale, now=now)

	assert error.value.failure is subroutine.domain.authentication.AuthenticationFailure.EXPIRED


def test_a_deactivated_or_deleted_owner_takes_their_tokens_with_them (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Disabling an account must not leave its agents still working."""

	deactivated = _make_user(session, is_active=False)
	_, presented = _issue(session, deactivated)

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as inactive:
		subroutine.domain.authentication.authenticate(session, presented)

	assert (
		inactive.value.failure
		is subroutine.domain.authentication.AuthenticationFailure.USER_INACTIVE
	)

	deleted = _make_user(session)
	_, other = _issue(session, deleted)
	deleted.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as gone:
		subroutine.domain.authentication.authenticate(session, other)

	assert gone.value.failure is subroutine.domain.authentication.AuthenticationFailure.USER_INACTIVE


def test_last_used_is_recorded_but_not_on_every_request (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A read path must not become a write path once per request (SPEC.md §7.4)."""

	user = _make_user(session)
	token, presented = _issue(session, user)
	start = subroutine.db.types.utcnow()

	assert token.last_used_at is None

	subroutine.domain.authentication.authenticate(session, presented, now=start)
	assert token.last_used_at == start

	moments_later = start + datetime.timedelta(seconds=30)
	subroutine.domain.authentication.authenticate(session, presented, now=moments_later)
	assert token.last_used_at == start, "written again inside the throttle interval"

	after = start + subroutine.domain.authentication.LAST_USED_INTERVAL
	subroutine.domain.authentication.authenticate(session, presented, now=after)
	assert token.last_used_at == after


def test_an_unknown_scope_is_refused_with_the_valid_ones_listed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A typo becomes an error, not a silently inert restriction.

	**`ValidationError`, not `ValueError`** (`#209`). This asserted the latter, which is what
	let the refusal exist for months while reaching nobody: a `ValueError` out of a service is
	a 500 over HTTP and a traceback on the CLI, and both surfaces can send a mistyped scope.
	The test pinned that the refusal *happened*, never that anyone could read it.
	"""

	user = _make_user(session)

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.authentication.issue_token(
			session, user=user, title="Broken", scopes=["task:reed"]
		)

	assert error.value.errors[0].field == "scopes"
	assert "task:reed" in error.value.errors[0].message
	assert subroutine.permissions.TASK_READ in (error.value.errors[0].hint or "")


def test_a_scoped_token_reports_its_narrowing (session: sqlalchemy.orm.Session) -> None:
	"""What the token restricts is readable; intersecting it with a role is §7.3's job."""

	user = _make_user(session)
	workspace = _make_workspace(session)
	project = str(subroutine.db.types.new_uuid())

	_, presented = _issue(
		session,
		user,
		scopes=[subroutine.permissions.TASK_READ],
		project_scope=[project],
		workspace_id=workspace.id,
	)

	principal = subroutine.domain.authentication.authenticate(session, presented)

	assert principal.scopes == [subroutine.permissions.TASK_READ]
	assert principal.project_scope == [project]
	assert principal.pinned_workspace_id == workspace.id


def test_an_unscoped_token_narrows_nothing (session: sqlalchemy.orm.Session) -> None:
	"""The empty-scope sentinel: no narrowing, not no permission (SPEC.md §7.3)."""

	user = _make_user(session)
	_, presented = _issue(session, user)

	principal = subroutine.domain.authentication.authenticate(session, presented)

	assert principal.scopes == []
	assert principal.project_scope is None
	assert principal.pinned_workspace_id is None


def test_superuser_status_comes_from_the_user_not_the_token (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Superusers bypass roles; their tokens still bypass nothing (SPEC.md §7.3)."""

	user = _make_user(session, is_superuser=True)
	_, presented = _issue(session, user, scopes=[subroutine.permissions.TASK_READ])

	principal = subroutine.domain.authentication.authenticate(session, presented)

	assert principal.is_superuser
	assert principal.scopes == [subroutine.permissions.TASK_READ]


def test_tokens_do_not_collide (session: sqlalchemy.orm.Session) -> None:
	"""Prefixes are unique, and the issuer re-rolls rather than failing if one is taken."""

	user = _make_user(session)
	prefixes = {_issue(session, user)[0].token_prefix for _ in range(25)}

	assert len(prefixes) == 25


def test_a_project_scope_is_canonicalised_at_issue (session: sqlalchemy.orm.Session) -> None:
	"""The check compares against the lowercase form, so an upper-cased id must not deny."""

	user = _make_user(session)
	identifier = subroutine.db.types.new_uuid()

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="Scoped", project_scope=[str(identifier).upper()]
	)

	assert token.project_scope == [str(identifier)]


def test_a_malformed_project_scope_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""Silently producing a token denied on every project helps nobody."""

	user = _make_user(session)

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.authentication.issue_token(
			session, user=user, title="Broken", project_scope=["SR", "not-a-uuid"]
		)

	assert error.value.errors[0].field == "project_scope"
	assert "SR" in error.value.detail, "and it quotes what was actually sent"


def test_an_empty_project_scope_is_refused_rather_than_guessed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One reading widens the token to everything, the other denies it everything."""

	user = _make_user(session)

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.authentication.issue_token(
			session, user=user, title="Ambiguous", project_scope=[]
		)

	assert "ambiguous" in error.value.detail.lower()
	assert error.value.errors[0].field == "project_scope"


def test_a_credentials_project_scope_is_listed_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``#203``, from the release-candidate review. Same output, same argument, applied once.

	``token list`` resolves the workspace pin to a slug, with a comment giving the reason — "a
	UUID in a listing is something to go and look up, which is the opposite of what a listing
	is for". The project scope printed on the very next line was raw ids.

	An id that no longer resolves keeps its raw form rather than being dropped: a listing whose
	job is "what can this credential reach" must never report a *narrower* reach than the
	credential has, and an id nobody can look up is still the truth about it.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	session.flush()

	gone = uuid.uuid4()
	token, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=setup.user,
		title="scoped",
		project_scope=[str(setup.inbox.id), str(gone)],
	)
	session.flush()

	named = subroutine.cli.main._scope_of(
		session, subroutine.domain.authentication.Principal(user=setup.user), token
	)

	assert named == [setup.inbox.key, str(gone)]
	assert f"projects {setup.inbox.key}" in subroutine.cli.main._credential_reach(
		token, None, named
	)
