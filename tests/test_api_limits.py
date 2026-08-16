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

import pathlib
import typing

import pytest
import sqlalchemy.orm
import starlette.requests
import uvicorn

import subroutine.api.app
import subroutine.api.limits
import subroutine.cli.main
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


def test_a_caller_that_waits_exactly_as_long_as_it_was_told_is_served (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The header is an instruction, and obeying it has to be enough.

	``Retry-After`` was computed as a float and then truncated by three separate ``int()``
	calls, so a caller needing 8.6 seconds was told 8 — came back on time, and was refused a
	second time. That is the exact failure the one-second floor was added to prevent, arriving
	through the other end of the same number, under a docstring saying the value was rounded
	up.

	Seven a minute, because 60/7 is not a whole number of seconds: a rate that divides evenly
	makes truncation and rounding agree, which is a fixture that cannot tell them apart. The
	clock is injected so this asserts the arithmetic rather than sleeping through it.
	"""

	world = test_api_tasks._world(session)
	_limited(world, rate_limit_per_minute=7)

	now = [0.0]
	world.application.state.limits.requests = subroutine.api.limits.Limiter(
		per_minute=7, clock=lambda: now[0]
	)

	for _ in range(7):
		assert world.call("GET", "/v1/tasks").status_code == 200

	refused = world.call("GET", "/v1/tasks")

	assert refused.status_code == 429

	now[0] += int(refused.headers["Retry-After"])

	assert world.call("GET", "/v1/tasks").status_code == 200, (
		"a caller that waited as long as it was told is served, not refused again"
	)


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


def test_a_proxied_instance_is_limited_though_its_socket_is_loopback () -> None:
	"""``#286``. The bind says who can open the socket; ``public_url`` says who can reach it.

	A reverse proxy in front of an application on ``127.0.0.1`` is how TLS gets terminated
	everywhere, and it is what ``docs/hosting.md`` recommends — so "loopback means a laptop"
	is false for the deployment this project tells people to build. Asking the socket turned
	the limiter off by default on precisely the instances that needed it.

	Found on 2026-08-02 diagnosing a 502 on Simon's public instance. He was protected only by
	the accident that his proxy runs on a different machine and so could not reach a loopback
	bind at all; co-locate them, which is the commonest arrangement, and the instance is
	public with no limiter and nothing to say so.
	"""

	proxied = subroutine.config.Settings(
		dev_mode=True, public_url="https://subroutine.example.com"
	)

	assert subroutine.api.limits.wanted(proxied, host="127.0.0.1") is True

	# And it is still the *operator's* call, not a rule that cannot be turned off.
	quiet = subroutine.config.Settings(
		dev_mode=True, public_url="https://subroutine.example.com", rate_limit=False
	)

	assert subroutine.api.limits.wanted(quiet, host="127.0.0.1") is False


def test_a_blank_public_url_is_not_a_public_url () -> None:
	"""An empty or whitespace setting is somebody who has not set it, not somebody who has.

	``config.toml`` is hand-edited, so ``public_url = ""`` is a real state — and reading it
	as "this is served publicly" would switch a limiter on for every laptop whose owner left
	the key in the file.
	"""

	for blank in ("", "   "):
		settings = subroutine.config.Settings(dev_mode=True, public_url=blank)

		assert subroutine.api.limits.wanted(settings, host="127.0.0.1") is False


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


# --- Which address a failure is counted against ------------------------------------------


def _arriving (peer: str | None, forwarded: str | None = None) -> starlette.requests.Request:
	"""Return a request from ``peer``, optionally carrying an ``X-Forwarded-For``."""

	headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]

	return starlette.requests.Request(
		{
			"type": "http",
			"method": "GET",
			"path": "/v1/tasks",
			"headers": headers,
			"client": None if peer is None else (peer, 51234),
		}
	)


def test_a_forwarded_header_from_an_untrusted_peer_is_ignored () -> None:
	"""**The one that decides whether any of this is safe** (`#277`, §7.7).

	``X-Forwarded-For`` is written by whoever sends it. Believing it from a peer nobody named
	would let a caller choose its own bucket key and mint a fresh allowance per guess — the
	identical defeat that keying failures on the token prefix would have been, which `#247`
	rejected for the same reason. This is why ``trusted_proxies`` is a list and not a flag.
	"""

	spoofing = _arriving("203.0.113.9", forwarded="10.0.0.1, 10.0.0.2")

	assert subroutine.api.limits._where_from(spoofing, frozenset()) == "203.0.113.9"
	assert (
		subroutine.api.limits._where_from(spoofing, frozenset({"192.168.0.127"}))
		== "203.0.113.9"
	)


def test_a_named_proxy_is_believed_so_callers_get_their_own_allowance () -> None:
	"""What `#277` is actually for: behind NPM every caller shared one bucket.

	Bounded damage — the failure bucket is only ever spent by an authentication that failed,
	so a working credential was never held back — but one client hammering with a stale token
	made *other people's* mistakes answer 429 instead of 401.
	"""

	trusted = frozenset({"192.168.0.127"})

	first = _arriving("192.168.0.127", forwarded="203.0.113.9")
	second = _arriving("192.168.0.127", forwarded="203.0.113.10")

	assert subroutine.api.limits._where_from(first, trusted) == "203.0.113.9"
	assert subroutine.api.limits._where_from(second, trusted) == "203.0.113.10"


def test_a_caller_behind_a_trusted_proxy_cannot_prepend_its_way_out () -> None:
	"""Read from the right, because each hop *appends* the address it received from.

	nginx's ``$proxy_add_x_forwarded_for`` is "whatever arrived, then the peer" — so a caller
	sending ``X-Forwarded-For: fake`` reaches this instance as ``fake, <their real address>``.
	Taking the leftmost entry is the standard way to get this wrong, and it would hand the
	caller its own key back despite the proxy being trusted.
	"""

	trusted = frozenset({"192.168.0.127"})
	sneaky = _arriving("192.168.0.127", forwarded="1.1.1.1, 2.2.2.2, 203.0.113.9")

	assert subroutine.api.limits._where_from(sneaky, trusted) == "203.0.113.9"


def test_chained_proxies_are_skipped_only_where_they_are_named () -> None:
	"""Two hops, both named, so the caller is what is left after skipping them."""

	trusted = frozenset({"192.168.0.127", "10.0.0.8"})
	chained = _arriving("192.168.0.127", forwarded="203.0.113.9, 10.0.0.8")

	assert subroutine.api.limits._where_from(chained, trusted) == "203.0.113.9"

	# And an *unnamed* middle hop is where the chain stops being believed. Returning it rather
	# than the address behind it is the conservative answer: nobody vouched for that claim.
	partial = _arriving("192.168.0.127", forwarded="203.0.113.9, 172.16.0.4")

	assert subroutine.api.limits._where_from(partial, trusted) == "172.16.0.4"


def test_a_trusted_proxy_that_forwards_nothing_falls_back_to_itself () -> None:
	"""Misconfiguration should share one bucket, not stop counting.

	A proxy named in ``trusted_proxies`` but not setting the header is a real setup mistake.
	Counting nothing would turn a typo in ``config.toml`` into a silently disabled limiter,
	which is the failure mode `#286` was about one level up.
	"""

	trusted = frozenset({"192.168.0.127"})

	assert subroutine.api.limits._where_from(_arriving("192.168.0.127"), trusted) == (
		"192.168.0.127"
	)
	assert subroutine.api.limits._where_from(
		_arriving("192.168.0.127", forwarded="  ,  "), trusted
	) == "192.168.0.127"


def test_no_client_at_all_is_counted_rather_than_raising () -> None:
	"""An ASGI transport carrying no client — the test suite is one."""

	assert subroutine.api.limits._where_from(_arriving(None), frozenset()) == "unknown"


def test_a_new_key_is_counted_once_the_map_is_big_enough_to_sweep () -> None:
	"""A key arriving after the sweep threshold is still limited — `#830`.

	**The sweep drops buckets that have fully refilled, and a bucket created for a new key is
	full.** So inserting first and sweeping second removed the key on the line that added it:
	the caller was decremented against an object nothing held, the request was allowed, and
	while the map stayed above ``SWEEP_ABOVE`` *every* request from *every* new key repeated
	the cycle.

	**Driven above the threshold, because that is the only place it can fail.** All sixteen
	cases written before this one run with a handful of buckets, so the limiter they exercise
	is one where ``_sweep`` returns immediately — which is why a control that was off entirely
	for new callers passed a full suite.

	The chaff is *partially drained* deliberately. Buckets that had refilled would be swept
	legitimately, the map would fall back under the threshold, and the case would prove
	nothing.
	"""

	now = 1000.0
	limiter = subroutine.api.limits.Limiter(per_minute=30, clock=lambda: now)

	for which in range(subroutine.api.limits.SWEEP_ABOVE + 1):
		limiter.take(f"chaff-{which}")
		limiter.take(f"chaff-{which}")

	allowed = sum(1 for _ in range(200) if limiter.take("a-new-caller") is None)

	assert allowed == 30, f"the limit is 30 a minute and {allowed} requests got through"


def test_the_sweep_still_forgets_a_bucket_that_has_refilled () -> None:
	"""The other half, so `#830`'s fix cannot be "never sweep".

	The sweep exists because the map otherwise grows once per distinct address for the life of
	the process. A fix that kept every bucket would close the finding and reintroduce the leak
	it was written to prevent, and nothing above would notice.
	"""

	moment = 1000.0
	limiter = subroutine.api.limits.Limiter(per_minute=30, clock=lambda: moment)

	for which in range(subroutine.api.limits.SWEEP_ABOVE + 1):
		limiter.take(f"spent-{which}")

	assert len(limiter._buckets) > subroutine.api.limits.SWEEP_ABOVE

	# A whole minute later every one of those has refilled, so the next new key sweeps them.
	moment += 60.0
	limiter.take("the-one-that-triggers-it")

	assert len(limiter._buckets) == 1, "a refilled bucket should be forgotten"
	assert "the-one-that-triggers-it" in limiter._buckets, "and the new one should survive"


def test_serve_builds_the_application_from_the_address_it_actually_binds (
	monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
	"""`#931`, from `#927` H-4. ``--host`` reached uvicorn and stopped there.

	``serve`` put the flag in a local, handed it to uvicorn and to the TLS refusal, and then
	built the application from **unmutated** settings still saying ``127.0.0.1``. Two things
	read that and both fail open on a wide bind: :class:`Limits` turns the credential-guessing
	limiter off when the socket stays on one machine, and ``/readyz`` decides from it whether a
	driver error — an internal hostname, a database name, a filesystem path — may go to an
	unauthenticated caller.

	``api/app.py`` already argued for reading it from settings *"so an application started by
	gunicorn or by a test gets the same answer as one started by the CLI"*. The CLI was the one
	caller making that untrue.
	"""

	built: dict[str, typing.Any] = {}

	def _capture (*, settings: subroutine.config.Settings) -> object:
		built["settings"] = settings

		return object()

	monkeypatch.setattr(subroutine.api.app, "create_app", _capture)
	monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: built.update(uvicorn=kwargs))
	monkeypatch.setattr(subroutine.cli.main, "_refuse_unusable_storage", lambda settings: None)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_public_bind", lambda *a, **k: None)
	monkeypatch.setattr(subroutine.cli.main, "_database_is_absent", lambda settings: False)

	subroutine.cli.main.serve(host="0.0.0.0", port=8199, log_level="", insecure=True)

	assert built["settings"].host == "0.0.0.0", (
		"the application was built for a different address from the one uvicorn was given"
	)
	assert built["settings"].port == 8199
	assert built["uvicorn"]["host"] == "0.0.0.0"


def test_serve_does_not_let_uvicorn_read_the_forwarded_header (
	monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#931`, from `#927` H-5. Two mechanisms for one job, and the wrong one ran first.

	uvicorn defaults ``proxy_headers`` **on**, with ``forwarded_allow_ips`` falling back to
	``127.0.0.1``, and its middleware rewrites ``scope["client"]`` from ``X-Forwarded-For``
	*before* the application sees the request. So :func:`subroutine.api.limits._where_from` —
	which reads the peer, checks it against ``trusted_proxies`` and only then walks the header
	from the right — was handed a forged address as the peer and returned it as the bucket key.

	**Measured**: 40 failed authentications from one machine, each with a different forged
	header, produced **0** refusals with uvicorn reading it and **10** with it off.
	``docs/hosting.md`` says an empty ``trusted_proxies`` means "the header is ignored
	entirely"; this is what makes that sentence true.
	"""

	seen: dict[str, typing.Any] = {}

	monkeypatch.setattr(subroutine.api.app, "create_app", lambda *, settings: object())
	monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
	monkeypatch.setattr(subroutine.cli.main, "_refuse_unusable_storage", lambda settings: None)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_public_bind", lambda *a, **k: None)
	monkeypatch.setattr(subroutine.cli.main, "_database_is_absent", lambda settings: False)

	subroutine.cli.main.serve(host="", port=0, log_level="", insecure=False)

	assert seen.get("proxy_headers") is False, (
		"uvicorn will rewrite the client address from a header this application parses itself"
	)
