"""Who the API thinks is calling, and what it tells them they may do.

The first test in this file is the important one. Everything else here checks that a
credential is accepted or refused correctly; that one checks that the check *happens* —
which is the failure the slice-2 review found, where `authorize()` existed, four documents
said it ran, and nothing called it.
"""

import datetime
import typing
import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.api.security
import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions

#: Routes that answer without a credential, each with the reason it is allowed to.
#: Anything not listed here must require a principal, and the test below fails the build
#: if it does not — the list is the point, not the exemptions.
PUBLIC_ROUTES: dict[str, str] = {
	"GET /healthz": "liveness, probed before any credential exists",
	"GET /readyz": "readiness, probed by an orchestrator with no account",
	# The one route somebody reaches *in order to* get a credential, so it cannot require
	# one (`#248`). It carries §7.7's limiter itself rather than inheriting it from the
	# principal dependency, which is the cost decision `#364` predicted a login endpoint
	# would inherit — see `test_signing_in_is_rate_limited_although_it_has_no_principal`.
	"GET /signin": "exchanges a sign-in link for a session, so it has no credential yet",
	# The browser app (`#597`). The page has to load before anybody can sign in, and what it
	# then asks for needs the cookie like everything else. Nothing here is workspace-scoped,
	# personal or read from the database — the same bytes for every caller, signed in or not.
	#
	# They are ordinary routes rather than a `StaticFiles` mount **so that they appear in this
	# list at all**: a mount is attached to the application rather than to a router, and the
	# walk above reads `ROUTERS`. Two entries somebody had to write are worth more than a
	# mount this test could never have seen.
	"GET /": "the browser app's page, which loads before there is a session to load it with",
	"GET /app/{name}": "the browser app's own files, the same bytes for every caller",
}


def _registered () -> list[tuple[str, typing.Any]]:
	"""Return every route the application registers, as ``("GET /v1/me", route)``."""

	found: list[tuple[str, typing.Any]] = []

	for prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			described: typing.Any = route

			for method in sorted(described.methods or ()):
				found.append((f"{method} {prefix}{described.path}", described))

	return found


def _requires_principal (route: typing.Any) -> bool:
	"""Report whether a route resolves a principal before its handler runs."""

	seen: list[typing.Any] = list(getattr(route.dependant, "dependencies", []))

	while seen:
		dependency = seen.pop()

		if dependency.call is subroutine.api.security.principal:
			return True

		seen.extend(dependency.dependencies)

	return False


def test_every_route_either_requires_a_credential_or_is_listed_as_public () -> None:
	"""No endpoint is protected by nobody having noticed it is not.

	Authentication is a dependency declared on the route, which means forgetting it is
	silent: the endpoint works, for everyone. This walks what the application actually
	registers and requires each route to be one or the other, deliberately, in writing.
	"""

	unprotected = [
		name
		for name, route in _registered()
		if name not in PUBLIC_ROUTES and not _requires_principal(route)
	]

	assert not unprotected, (
		f"These routes need no credential and are not listed as public: "
		f"{', '.join(unprotected)}. Add the principal dependency, or add them to "
		f"PUBLIC_ROUTES with the reason."
	)


def test_the_public_list_names_only_routes_that_exist () -> None:
	"""An exemption for a route that has been renamed is an exemption nobody notices."""

	registered = {name for name, _ in _registered()}

	assert set(PUBLIC_ROUTES) <= registered, (
		f"PUBLIC_ROUTES names routes that no longer exist: "
		f"{', '.join(sorted(set(PUBLIC_ROUTES) - registered))}."
	)


class Setup(typing.NamedTuple):
	"""A user, their workspace and an application talking to the test's transaction."""

	application: fastapi.FastAPI
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace


@pytest.fixture
def setup (session: sqlalchemy.orm.Session) -> Setup:
	"""Create a workspace with an owner, and an application to reach it through."""

	user = subroutine.domain.users.create(
		session, username=f"caller-{uuid.uuid4().hex[:8]}", display_name="The Caller"
	)
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=user
	)

	return Setup(
		application=api_support.build_app(api_support.factory_for(session)),
		user=user,
		workspace=workspace,
	)


def _token (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	**kwargs: typing.Any,
) -> tuple[subroutine.db.models.identity.ApiToken, str]:
	"""Issue a token and return it with its readable secret."""

	kwargs.setdefault("title", "Test token")
	token, issued = subroutine.domain.authentication.issue_token(session, user=user, **kwargs)
	session.flush()

	return token, issued.value.get_secret_value()


def _me (application: fastapi.FastAPI, secret: str | None = None) -> typing.Any:
	"""Call ``/v1/me``, with a bearer token if one is given."""

	headers = {} if secret is None else {"authorization": f"Bearer {secret}"}

	return api_support.call(application, "GET", "/v1/me", headers=headers)


def test_a_valid_token_identifies_its_owner (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""``/v1/me`` reports the account, the workspace and the role in one round trip."""

	_, secret = _token(session, setup.user)
	response = _me(setup.application, secret)

	assert response.status_code == 200

	body = response.json()

	assert body["user"]["username"] == setup.user.username
	assert body["user"]["display_name"] == "The Caller"
	assert body["credential"]["kind"] == "api_token"
	assert [workspace["slug"] for workspace in body["workspaces"]] == [setup.workspace.slug]
	assert body["workspaces"][0]["role"] == "Owner"


def test_the_response_never_carries_the_secret (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The credential is described by its public prefix and nothing else."""

	token, secret = _token(session, setup.user)
	response = _me(setup.application, secret)

	assert secret not in response.text
	assert token.token_hash not in response.text
	assert response.json()["credential"]["prefix"] == token.token_prefix


def test_an_unnarrowed_token_reports_the_whole_role (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""``scopes: []`` means no narrowing, and the permissions prove it.

	This is the sentinel that reads backwards (SPEC.md §7.3). An agent taking the empty
	list as "no permissions" would conclude it could do nothing; the ``narrows`` flag and
	the permission list are both there so it does not have to interpret anything.
	"""

	_, secret = _token(session, setup.user)
	body = _me(setup.application, secret).json()

	assert body["credential"]["scopes"] == []
	assert body["credential"]["narrows"] is False
	assert body["workspaces"][0]["narrowed_by_credential"] is False
	assert subroutine.permissions.TASK_WRITE in body["workspaces"][0]["permissions"]


def test_a_scoped_token_reports_only_what_it_can_actually_do (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The permissions reported are the intersection, not the role.

	An owner scoped to ``task:read`` may read tasks and nothing else, and this is where it
	finds that out — rather than by trying to write one and being refused.
	"""

	_, secret = _token(session, setup.user, scopes=[subroutine.permissions.TASK_READ])
	body = _me(setup.application, secret).json()

	assert body["credential"]["scopes"] == [subroutine.permissions.TASK_READ]
	assert body["credential"]["narrows"] is True
	assert body["workspaces"][0]["permissions"] == [subroutine.permissions.TASK_READ]
	assert body["workspaces"][0]["role"] == "Owner", "the role is unchanged; the token narrows"
	assert body["workspaces"][0]["narrowed_by_credential"] is True


def test_instance_permissions_belong_to_superusers_only (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""A workspace owner is not an administrator of the installation (SPEC.md §7.1)."""

	_, secret = _token(session, setup.user)

	assert _me(setup.application, secret).json()["instance_permissions"] == []

	setup.user.is_superuser = True
	session.flush()

	granted = _me(setup.application, secret).json()["instance_permissions"]

	assert set(granted) == set(subroutine.permissions.INSTANCE_LEVEL)


def test_a_pinned_token_sees_only_its_workspace (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Pinning narrows what the caller can even see, not merely what it may write."""

	second = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=setup.user
	)
	session.flush()

	_, unpinned = _token(session, setup.user)
	_, pinned = _token(session, setup.user, workspace_id=second.id)

	assert len(_me(setup.application, unpinned).json()["workspaces"]) == 2

	body = _me(setup.application, pinned).json()

	assert [workspace["id"] for workspace in body["workspaces"]] == [str(second.id)]
	assert body["credential"]["workspace_id"] == str(second.id)
	assert body["credential"]["narrows"] is True


def test_using_a_token_records_that_it_was_used (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""``last_used_at`` is written, so an unused credential can be found and revoked."""

	token, secret = _token(session, setup.user)

	assert token.last_used_at is None

	assert _me(setup.application, secret).status_code == 200

	session.expire(token)

	assert token.last_used_at is not None


def test_a_request_with_no_credential_is_refused_and_told_how (
	setup: Setup,
) -> None:
	"""401, with the header RFC 9110 requires and a hint that names the next step."""

	response = _me(setup.application)

	assert response.status_code == 401
	assert response.headers["www-authenticate"] == "Bearer"

	body = response.json()

	assert body["code"] == "unauthenticated"
	assert "Authorization: Bearer" in body["hint"]
	assert "subroutine token create" in body["hint"]


def test_a_token_in_the_query_string_is_refused_and_called_out (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Tokens are never read from a URL, and the refusal says why rather than puzzling.

	SPEC.md §7.4: a query string reaches access logs, browser history and referrer headers.
	Ignoring the parameter silently would leave the caller staring at a 401 while looking
	straight at a credential it can see in the URL.
	"""

	_, secret = _token(session, setup.user)
	response = api_support.call(setup.application, "GET", f"/v1/me?token={secret}")

	assert response.status_code == 401

	hint = response.json()["hint"]

	assert "query parameter" in hint
	assert "compromised" in hint
	assert secret not in response.text, "the refusal must not repeat the credential"


@pytest.mark.parametrize(
	"header",
	["Basic dXNlcjpwYXNz", "Token sr_deadbeef_secret", "sr_deadbeef_secret"],
)
def test_another_authentication_scheme_is_refused (setup: Setup, header: str) -> None:
	"""Only Bearer is accepted, and the refusal names the scheme that is."""

	response = api_support.call(
		setup.application, "GET", "/v1/me", headers={"authorization": header}
	)

	assert response.status_code == 401
	assert "Bearer" in response.json()["hint"]


def test_a_calendar_credential_cannot_be_used_as_a_bearer_token (setup: Setup) -> None:
	"""A feed credential is a different kind of thing and is refused as one.

	The feeds themselves are §20 and are not built. This holds the promise made about them
	now, while it costs one branch, rather than discovering later that the token grammar
	happened to admit one.
	"""

	response = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		headers={"authorization": "Bearer sr_cal_0123456789abcdef"},
	)

	assert response.status_code == 401
	assert "calendar" in response.json()["detail"].lower()


@pytest.mark.parametrize(
	"presented",
	["", "nonsense", "sr_short_x", "sr_XYZNOTHEX_secret", "sr_deadbeef_wrongsecret"],
)
def test_an_unusable_token_is_refused (setup: Setup, presented: str) -> None:
	"""Every reason reads identically from outside, on purpose (SPEC.md §7.4)."""

	response = api_support.call(
		setup.application, "GET", "/v1/me", headers={"authorization": f"Bearer {presented}"}
	)

	assert response.status_code == 401


def test_a_revoked_token_stops_working (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Revocation takes effect on the very next request."""

	token, secret = _token(session, setup.user)

	assert _me(setup.application, secret).status_code == 200

	subroutine.domain.authentication.revoke_token(token)
	session.flush()

	assert _me(setup.application, secret).status_code == 401


def test_an_expired_token_stops_working (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""An expiry in the past is a refusal, never a warning."""

	past = subroutine.db.types.utcnow() - datetime.timedelta(minutes=1)
	_, secret = _token(session, setup.user, expires_at=past)

	assert _me(setup.application, secret).status_code == 401


def test_a_deactivated_account_stops_working (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Deactivating a user refuses their tokens too, without revoking each one."""

	_, secret = _token(session, setup.user)

	setup.user.is_active = False
	session.flush()

	assert _me(setup.application, secret).status_code == 401


def test_the_refusal_never_says_which_check_failed (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""An unknown token and a revoked one are indistinguishable from outside.

	Distinguishing them would tell an attacker when it had guessed half a credential.
	"""

	token, secret = _token(session, setup.user)
	subroutine.domain.authentication.revoke_token(token)
	session.flush()

	revoked = _me(setup.application, secret).json()
	unknown = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		headers={"authorization": "Bearer sr_deadbeef_nosuchsecret"},
	).json()

	assert revoked["detail"] == unknown["detail"]
	assert revoked["code"] == unknown["code"]
