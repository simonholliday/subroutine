"""Credentials over HTTP — item ``#208``.

The gap this closes is the one ``#196`` was, one surface along: ``POST /v1/users`` could add
Thomas and nothing over HTTP could give them a way in, so an administrator without a shell on
the server created accounts that could never be used.

**What is worth testing here is not "does it issue one".** It is the three refusals — a
credential may never be wider than the one that asked for it, may not be minted for an account
that could not use it, and may not be revoked by somebody it has nothing to do with — because
each of those is a check that would look exactly the same if it were not running.
"""

import typing
import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions


class World(typing.NamedTuple):
	"""An instance with a superuser, an ordinary member, and a token for each."""

	application: fastapi.FastAPI
	session: sqlalchemy.orm.Session
	founder: subroutine.db.models.identity.User
	member: subroutine.db.models.identity.User
	secret: str
	member_secret: str

	def call (self, method: str, path: str, *, as_member: bool = False, **kwargs: typing.Any) -> typing.Any:
		"""Make a request as the founder, or as the ordinary member."""

		token = self.member_secret if as_member else self.secret
		headers = {"authorization": f"Bearer {token}", **kwargs.pop("headers", {})}

		return api_support.call(self.application, method, path, headers=headers, **kwargs)


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""Set up an instance whose two accounts hold different authority."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	member = subroutine.domain.users.create(session, username=f"thomas-{uuid.uuid4().hex[:8]}")

	subroutine.domain.workspaces.add_member(
		session, setup.workspace, member, role_key="member", actor=None
	)

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="founder"
	)
	_member_row, member_issued = subroutine.domain.authentication.issue_token(
		session, user=member, title="member"
	)
	session.flush()

	return World(
		application=api_support.build_app(api_support.factory_for(session)),
		session=session,
		founder=setup.user,
		member=member,
		secret=issued.value.get_secret_value(),
		member_secret=member_issued.value.get_secret_value(),
	)


def test_a_credential_is_issued_once_and_never_readable_again (world: World) -> None:
	"""The secret is in the create response and in nothing else, ever.

	Only a hash is stored (§7.4), so this is not a policy that could be relaxed — there is
	nothing left to return.
	"""

	response = world.call("POST", "/v1/tokens", json={"title": "My laptop"})

	assert response.status_code == 201

	created = response.json()

	assert created["token"].startswith("sr_")
	assert created["prefix"] in created["token"]
	assert created["usable"] is True
	assert created["narrows"] is False, "an unscoped credential says so"

	listed = world.call("GET", "/v1/tokens").json()["items"]
	mine = next(row for row in listed if row["prefix"] == created["prefix"])

	assert "token" not in mine, "the secret is never in a listing"
	assert mine["title"] == "My laptop"


def test_a_credential_may_not_be_wider_than_the_one_that_asked_for_it (world: World) -> None:
	"""§7.4's whole least-privilege story, and the endpoint is where it is now reachable.

	A narrow token that can mint an unrestricted one is not a restriction, it is a formality —
	and the refusal it had just met would be one command away from irrelevant.
	"""

	narrow = world.call(
		"POST",
		"/v1/tokens",
		json={"title": "read only", "scopes": [subroutine.permissions.TASK_READ]},
	).json()["token"]

	headers = {"authorization": f"Bearer {narrow}"}
	widened = api_support.call(
		world.application,
		"POST",
		"/v1/tokens",
		headers=headers,
		json={"title": "wider", "scopes": [subroutine.permissions.TASK_WRITE]},
	)

	assert widened.status_code == 403
	assert widened.json()["errors"][0]["field"] == "scopes"

	# And "no scopes at all" is the widest of the lot, so it is refused the same way.
	unrestricted = api_support.call(
		world.application, "POST", "/v1/tokens", headers=headers, json={"title": "unscoped"}
	)

	assert unrestricted.status_code == 403


def test_issuing_for_somebody_else_needs_the_authority_to_create_them (world: World) -> None:
	"""An account plus a credential for it is one act in two steps, so it is one authority.

	The ordinary member holds no instance permission, so this is the check that stops anybody
	who can authenticate from minting a credential in a colleague's name.
	"""

	refused = world.call(
		"POST",
		"/v1/tokens",
		as_member=True,
		json={"title": "not mine", "username": world.founder.username},
	)

	assert refused.status_code == 403

	allowed = world.call(
		"POST", "/v1/tokens", json={"title": "Thomas's laptop", "username": world.member.username}
	)

	assert allowed.status_code == 201
	assert allowed.json()["username"] == world.member.username


def test_no_credential_is_issued_for_an_account_that_could_not_use_it (world: World) -> None:
	"""`#207`'s rule, on the other surface. A token whose owner is inactive is refused at
	authentication, so issuing one produces a credential that is dead on arrival."""

	world.member.is_active = False
	world.session.flush()

	refused = world.call(
		"POST", "/v1/tokens", json={"title": "dead on arrival", "username": world.member.username}
	)

	assert refused.status_code == 404
	assert refused.json()["errors"][0]["field"] == "username"

	missing = world.call("POST", "/v1/tokens", json={"title": "x", "username": "nobody"})

	assert missing.status_code == 404


def test_revoking_stops_it_working_on_the_next_request (world: World) -> None:
	"""Immediate, because ``revoked_at`` is read on every request rather than cached.

	Asserted by *using* the credential either side of the revocation rather than by reading a
	column — the column has been right since M1, and what `#156` found missing was anything
	that set it.
	"""

	created = world.call("POST", "/v1/tokens", json={"title": "leaked"}).json()
	headers = {"authorization": f"Bearer {created['token']}"}

	assert api_support.call(world.application, "GET", "/v1/me", headers=headers).status_code == 200

	revoked = world.call("DELETE", f"/v1/tokens/{created['prefix']}")

	assert revoked.status_code == 200
	assert revoked.json()["usable"] is False
	assert revoked.json()["revoked_at"] is not None

	assert api_support.call(world.application, "GET", "/v1/me", headers=headers).status_code == 401

	# Idempotent, and it keeps the first instant: when a credential stopped being trusted is a
	# fact worth not overwriting.
	again = world.call("DELETE", f"/v1/tokens/{created['prefix']}")

	assert again.status_code == 200
	assert again.json()["revoked_at"] == revoked.json()["revoked_at"]


def test_a_credential_that_is_nothing_to_do_with_you_is_not_there (world: World) -> None:
	"""Absent rather than forbidden, so this endpoint discloses nothing a listing would not.

	The member can see their own and the ones they issued. The founder's is neither, and
	answering 403 about it would confirm a prefix somebody had guessed.
	"""

	theirs = world.call("POST", "/v1/tokens", json={"title": "the founder's"}).json()
	listed = world.call("GET", "/v1/tokens", as_member=True).json()["items"]

	assert theirs["prefix"] not in [row["prefix"] for row in listed]

	refused = world.call("DELETE", f"/v1/tokens/{theirs['prefix']}", as_member=True)

	assert refused.status_code == 404


def test_an_expiry_is_read_as_the_whole_day_it_names (world: World) -> None:
	"""The same grammar the CLI takes, from the one definition both now share (`#208`).

	A credential that stopped at midnight starting the day somebody named is the kind of
	surprise that arrives at the worst moment, so the day runs to its end.
	"""

	created = world.call(
		"POST", "/v1/tokens", json={"title": "this month", "expires": "2026-09-01"}
	).json()

	assert created["expires_at"].startswith("2026-09-01T23:59:59")

	relative = world.call(
		"POST", "/v1/tokens", json={"title": "thirty days", "expires": "now+30d"}
	).json()

	assert relative["expires_at"] is not None

	nonsense = world.call("POST", "/v1/tokens", json={"title": "x", "expires": "soonish"})

	assert nonsense.status_code == 422


def test_an_empty_project_scope_is_refused_rather_than_guessed_at (world: World) -> None:
	"""One reading widens the credential to every project and the other denies it every one.

	Picking either on the caller's behalf gets a security control wrong in silence, which is
	the argument `#201` settled for the predicate that reads this column.
	"""

	refused = world.call(
		"POST", "/v1/tokens", json={"title": "scoped to nothing", "project_scope": []}
	)

	assert refused.status_code == 422
