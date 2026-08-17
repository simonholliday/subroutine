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
import sqlalchemy
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
	# A refusal that reads nothing and needs nothing (`#648`). Requiring a credential to be
	# told the method is wrong would mean a client had to authenticate before learning it had
	# asked the wrong question — and a 405 discloses nothing that `/v1/openapi.json` does not.
	"GET /mcp": "refuses the event stream this transport does not have, with Allow: POST",
	# **Added by the framework rather than by this project** (`#927` H-18), and invisible to
	# the walk until it learned to read the built application as well as `ROUTERS`.
	#
	# Public deliberately, and measured rather than assumed: it answers 200 with no credential
	# and publishes all 34 parameter names, which is what `#898` relied on when it decided that
	# refusing an unknown query parameter *before* authenticating discloses nothing. A schema
	# somebody has to authenticate to read is one an agent cannot use to decide how to
	# authenticate.
	#
	# Two entries for one path because Starlette gives its own routes both methods, and naming
	# the pair is honest where collapsing them would hide which one was checked.
	"GET /v1/openapi.json": "the API's own schema, which a client reads before it has a token",
	"HEAD /v1/openapi.json": "the same route; Starlette answers HEAD for it as well",
}


def _registered () -> list[tuple[str, typing.Any]]:
	"""Return every route the application registers, as ``("GET /v1/me", route)``.

	**Two sources, because neither sees the other's routes** (`#927` H-18). ``ROUTERS`` is the
	declared data this project mounts, and it has to be read that way: ``include_router``
	*copies* a route as it mounts it and leaves a private wrapper in ``app.routes`` with no
	``.path`` at all, so a walk over the built application finds nothing for them.

	But FastAPI attaches routes of its own — ``/v1/openapi.json``, and ``/docs``, ``/redoc``
	and ``/docs/oauth2-redirect`` where those are enabled — directly to the application, and
	those are invisible to the ``ROUTERS`` walk. **Eight method-path pairs**, measured, none of
	which required a credential and none of which anything here had to excuse, under a
	docstring in ``api/security.py`` saying this "walks every registered route".

	The blind spot was known and worked around rather than closed: ``PUBLIC_ROUTES`` says the
	app's own files are ordinary routes rather than a ``StaticFiles`` mount *"so that they
	appear in this list at all"*. That reasoning is right and it only ever covered the routes
	this project writes.
	"""

	found: list[tuple[str, typing.Any]] = []
	declared: set[str] = set()

	for prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			described: typing.Any = route

			for method in sorted(described.methods or ()):
				name = f"{method} {prefix}{described.path}"

				declared.add(name)
				found.append((name, described))

	application = api_support.build_app(_no_database())

	for route in application.routes:
		path = getattr(route, "path", None)

		if path is None:
			continue

		for method in sorted(getattr(route, "methods", None) or ()):
			name = f"{method} {path}"

			# The mounted copies come back here too, and they are already accounted for
			# above — from the router objects, where their dependencies can be read.
			if name not in declared:
				found.append((name, route))

	return found


def _no_database () -> typing.Any:
	"""A session factory nothing in this walk will use.

	Building the application is how the framework's own routes become visible, and building
	one needs a factory. Nothing here calls a handler, so it never opens a connection.
	"""

	return sqlalchemy.orm.sessionmaker(bind=sqlalchemy.create_engine("sqlite://"))


def _requires_principal (route: typing.Any) -> bool:
	"""Report whether a route resolves a principal before its handler runs.

	**A route with no ``dependant`` is one that cannot have a dependency at all** — the
	framework's own are plain Starlette routes rather than FastAPI ones — so the honest answer
	for them is *no*, which puts the burden where it belongs: they have to be named in
	:data:`PUBLIC_ROUTES` with a reason, by somebody who has thought about it.
	"""

	dependant = getattr(route, "dependant", None)

	if dependant is None:
		return False

	seen: list[typing.Any] = list(getattr(dependant, "dependencies", []))

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


def test_every_credential_kind_can_be_refused_by_name () -> None:
	"""`SR#916`. A kind this program mints and cannot name is the one that fails silently.

	**`auth.CREDENTIAL_KINDS` was read by nothing** until the calendar feed became the fourth
	entry. It called itself *"every kind this program mints, for the refusals that have to
	name them"* and no refusal read it: `api/security._ELSEWHERE` was a second hand-written
	list, and the calendar's own refusal was a third branch written out above both. Three
	statements of one fact, in the module where being wrong means a credential is accepted
	somewhere it should not be.

	**The direction that matters is minting-without-a-refusal**, not the reverse. A kind with
	an entry it does not need is a paragraph nobody reads; a kind with no entry is refused as
	*mistyped*, which sends its holder looking for a typo in a string they pasted correctly.

	Driven through the real function rather than compared as sets, because a registry read by
	a loop that no longer runs would satisfy any comparison of the two lists.
	"""

	assert subroutine.auth.CREDENTIAL_KINDS, "no kinds were found, so this checks nothing"

	for kind in subroutine.auth.CREDENTIAL_KINDS:
		minted = subroutine.auth.generate_token(kind=kind)

		with pytest.raises(subroutine.errors.Unauthenticated) as refused:
			subroutine.api.security._refuse_a_credential_of_another_kind(
				minted.value.get_secret_value()
			)

		assert refused.value.hint, f"the refusal for {kind!r} says what it is and not what to do"

	# **The other half, which is what stops the registry becoming a graveyard.** An entry for
	# a kind nothing mints is a decision recorded about code that has gone, and it reads
	# exactly like a considered one (`SR#405`).
	stale = sorted(
		set(subroutine.api.security._ELSEWHERE) - set(subroutine.auth.CREDENTIAL_KINDS)
	)

	assert not stale, f"_ELSEWHERE names {stale}, which this program does not mint."


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

	This is the sentinel that reads backwards (docs/design.md §7.3). An agent taking the empty
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
	"""A workspace owner is not an administrator of the installation (docs/design.md §7.1)."""

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

	docs/design.md §7.4: a query string reaches access logs, browser history and referrer headers.
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
	"path", ["/v1/me", "/v1/tasks", "/v1/workspaces"]
)
def test_a_credential_in_the_url_is_called_out_however_else_the_request_is_answered (
	session: sqlalchemy.orm.Session, setup: Setup, path: str
) -> None:
	"""`#899`. The warning was reachable down one path in four, and nothing said so.

	Two things hid it, and the test above met neither. It drives ``/v1/me`` **without** a
	header — and ``/v1/me`` was, until `#676`, the one GET route in the application with no
	unknown-parameter refusal, so it was also the one route where the 401 was reached at all.
	Measured on the served instance before the fix, three of these four combinations were
	silent about a secret sitting in a URL:

	- authenticated, on any route: `200`, no mention of it.
	- unauthenticated, on a route refusing unknown parameters: ``unknown_field``, which reads
	  as a typo — so the caller fixes the spelling and never revokes anything.

	Parametrised over paths **because the defect was one route behaving unlike the rest**, and
	a single-path test is what let it hide. A valid header is sent deliberately: by the time
	this fires the secret is in a log whatever else was true of the request.
	"""

	_, secret = _token(session, setup.user)
	_, other = _token(session, setup.user)

	answered = api_support.call(
		setup.application,
		"GET",
		f"{path}?token={secret}",
		headers={"authorization": f"Bearer {other}"},
	)

	assert answered.status_code == 401, (
		f"{path} answered {answered.status_code} to a request carrying a credential in "
		f"its URL"
	)

	body = answered.json()

	assert body["code"] == "unauthenticated"
	assert "compromised" in body["hint"], "the one sentence worth saying was not said"
	assert secret not in answered.text, "the refusal must not repeat the credential"
	assert other not in answered.text


@pytest.mark.parametrize("spelling", ["TOKEN", "Token", "ApiKey", "AUTH", "Access_Token"])
def test_a_credential_in_the_url_is_called_out_however_it_was_capitalised (
	session: sqlalchemy.orm.Session, setup: Setup, spelling: str
) -> None:
	"""`#946`, cold review `#927`'s L-13 — `#899` again, in a case nobody thought of.

	**A query parameter name is case-sensitive in HTTP, and that is not the point.** Whether
	this server would *honour* ``TOKEN`` is a different question from whether a credential
	reached a URL, and it did: before this, ``?TOKEN=`` fell past the refusal and was answered
	``unknown_field`` — the typo report `#899` exists to stop somebody reading — while the live
	value went into the access log verbatim.

	**Driven rather than asserted against the register**, and capitalised by hand rather than by
	upper-casing the constants: a test that derives its cases the same way the code derives its
	comparison agrees with the code by construction, which is the shape this project keeps
	finding. ``Access_Token`` is here because it is the one name where the fold and a simple
	``.title()`` disagree.
	"""

	_, secret = _token(session, setup.user)

	answered = api_support.call(
		setup.application, "GET", f"/v1/tasks?{spelling}={secret}"
	)

	assert answered.status_code == 401, (
		f"?{spelling}= was answered {answered.status_code}, so a credential in a URL was "
		f"treated as a misspelled parameter"
	)

	body = answered.json()

	assert body["code"] == "unauthenticated"
	assert "compromised" in body["hint"], "the one sentence worth saying was not said"
	assert secret not in answered.text


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
	"""Every reason reads identically from outside, on purpose (docs/design.md §7.4)."""

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
