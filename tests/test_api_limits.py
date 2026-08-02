"""Rate limiting — SPEC.md §7.7, item ``#247``.

Specified since slice 1 and implemented by nothing: the ``rate_limited`` code was in the
registry, the ``429`` was in the problem map, and no code path raised either. Invisible on a
loopback instance, which is why it survived a release — and on a reachable one it meant token
guessing was unbounded.

Two of these are the point rather than coverage:

* **A wrong credential is counted against where it came from, not against the prefix it
  presented.** A prefix is the caller's to choose, so keying on it hands an attacker a fresh
  allowance for every guess. This is the test that would fail if somebody "simplified" it.
* **A working credential is never held back by somebody else's failures.** That is what makes
  the address key safe behind a proxy, where every request appears to come from one place.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.api.limits
import subroutine.config
import subroutine.domain.authentication
import test_api_tasks


def _limited (world: test_api_tasks.World, **settings: typing.Any) -> None:
	"""Turn limiting on for one application, as an operator's configuration would."""

	world.application.state.limits = subroutine.api.limits.Limits(
		subroutine.config.Settings(dev_mode=True, rate_limit=True, **settings), host="0.0.0.0"
	)


def test_a_credential_going_too_fast_is_slowed_down (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The per-token bucket, and the ``Retry-After`` a caller is meant to obey."""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_per_minute=3)

	assert [world.call("GET", "/v1/tasks").status_code for _ in range(3)] == [200, 200, 200]

	refused = world.call("GET", "/v1/tasks")

	assert refused.status_code == 429
	assert refused.json()["code"] == "rate_limited"

	# RFC 9110 wants a 429 to say when to come back, and a caller told "0" retries at once
	# and is refused again — so the floor is a second rather than whatever the maths gives.
	assert int(refused.headers["Retry-After"]) >= 1


def test_two_credentials_are_counted_separately (session: sqlalchemy.orm.Session) -> None:
	"""Per *token*, so one runaway client cannot spend everybody's allowance.

	Both tokens belong to the same user here, which is the sharper case: the bucket is keyed
	on the credential rather than on its owner, exactly as `#158` decided for `?actor=me`.
	"""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_per_minute=2)

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="second"
	)
	session.flush()

	other = world._replace(secret=issued.value.get_secret_value())

	assert [world.call("GET", "/v1/tasks").status_code for _ in range(2)] == [200, 200]
	assert world.call("GET", "/v1/tasks").status_code == 429

	# The second credential has spent nothing.
	assert other.call("GET", "/v1/tasks").status_code == 200


def test_guessing_is_limited_however_the_prefix_is_varied (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The one that decides whether any of this works** (§7.7).

	Failed authentications are counted against the *address*, not the token prefix. A prefix
	is the public half of a credential and entirely the caller's to choose — so a limiter keyed
	on it gives somebody guessing a brand-new allowance on every attempt, which is no limiter
	at all. §7.7's "logged with the token prefix only" is about what reaches the log.
	"""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_failures_per_minute=4)

	seen = [
		world.call(
			"GET", "/v1/tasks", headers={"authorization": f"Bearer sr_guess{index}_nope"}
		).status_code
		for index in range(6)
	]

	assert seen[:4] == [401, 401, 401, 401]
	assert seen[4:] == [429, 429], "a fresh prefix must not buy a fresh allowance"


def test_a_working_credential_is_not_held_back_by_somebody_elses_failures (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Which is what makes an address key safe behind a proxy.

	Every request through Nginx Proxy Manager or a load balancer arrives from one address, so
	a limiter that consulted the failure bucket on *every* request would let one attacker lock
	out everybody. The failure bucket is only ever spent by a failure.
	"""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_failures_per_minute=2)

	for _ in range(4):
		world.call("GET", "/v1/tasks", headers={"authorization": "Bearer sr_nope_nope"})

	assert world.call("GET", "/v1/tasks").status_code == 200


def test_a_public_endpoint_is_not_counted (session: sqlalchemy.orm.Session) -> None:
	"""No exempt-path list, because the limiter lives in the authentication dependency.

	A health check a load balancer polls every second declares no principal, so it is never
	reached — and nobody has to maintain a list that can disagree with the routes, which is
	the arrangement `tests/test_api_authentication.py` already rejects for authentication.
	"""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_per_minute=1)

	assert [world.call("GET", "/healthz").status_code for _ in range(5)] == [200] * 5


@pytest.mark.parametrize(
	("host", "expected"),
	[("127.0.0.1", False), ("localhost", False), ("::1", False), ("0.0.0.0", True), ("10.0.0.4", True)],
)
def test_limiting_is_off_on_loopback_and_on_otherwise (host: str, expected: bool) -> None:
	"""§7.7's default, and the reason the whole test suite is unaffected by this feature.

	A limiter is about callers reaching an instance over a network. On a laptop the only
	caller is the person who owns the machine, and counting their requests would be ceremony
	in service of nothing.
	"""

	settings = subroutine.config.Settings(dev_mode=True)

	assert subroutine.api.limits.wanted(settings, host=host) is expected


@pytest.mark.parametrize("chosen", [True, False])
def test_an_explicit_setting_is_obeyed_either_way (chosen: bool) -> None:
	"""Somebody testing a limiter on loopback, or turning one off on a public bind, said so."""

	settings = subroutine.config.Settings(dev_mode=True, rate_limit=chosen)

	assert subroutine.api.limits.wanted(settings, host="127.0.0.1") is chosen
	assert subroutine.api.limits.wanted(settings, host="0.0.0.0") is chosen


def test_an_allowance_refills_over_time () -> None:
	"""Continuously, not in windows.

	A fixed window lets a caller spend everything in its last instant and again in the first
	instant of the next — twice the intended rate at exactly the moment a limiter is supposed
	to be holding. The clock is injected so this asserts the arithmetic rather than sleeping.
	"""

	now = [1000.0]
	limiter = subroutine.api.limits.Limiter(per_minute=60, clock=lambda: now[0])

	for _ in range(60):
		assert limiter.take("k") is None

	assert limiter.take("k") is not None

	# One second at sixty a minute is exactly one more request, and no more than one.
	now[0] += 1.0

	assert limiter.take("k") is None
	assert limiter.take("k") is not None
