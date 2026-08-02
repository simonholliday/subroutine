"""Slowing down a caller going too fast, and one guessing credentials — SPEC.md §7.7.

Specified since slice 1, and implemented by nothing until `#247`: the ``rate_limited`` code
was in the registry, the ``429`` was in the problem map, and no code path raised either. On a
loopback instance that is invisible and harmless, which is why it survived. On a reachable one
it means **token guessing is unbounded and unlogged** — and ``sha256`` with no pepper (§7.4) is
the right design and does nothing whatever to slow somebody who can ask a million times.

**Two limiters, because they answer different questions.**

*Per token.* A caller with a valid credential, going faster than the instance wants to serve.
Keyed on the token's prefix, which is its public half and already what ``token revoke`` takes.
Generous by default: this is a backstop against a runaway client, not a quota.

*Per address, on failures.* Somebody presenting credentials that do not work. **Keyed on where
the request came from, not on the token prefix**, and that is the whole of whether this works:
a prefix is chosen by the caller, so an attacker varying it gets a fresh bucket every attempt
and is not limited at all. §7.7's "logged with the token prefix only" is about what reaches the
log, not about what the counter is keyed on.

**In memory, in one process, and that is a stated limitation rather than an oversight.** Two
workers would each enforce their own share of the limit. ``serve`` runs one; anything running
this under gunicorn wants a shared store, and there is none.

**Behind a proxy every request carries the proxy's address**, so the failure limiter sees one
client. It is set high enough that ordinary use cannot reach it — the point is to bound
hammering, not to be a lockout — and `X-Forwarded-For` support is `#277`.
"""

import dataclasses
import logging
import threading
import time
import typing

import starlette.requests

import subroutine.auth
import subroutine.config
import subroutine.errors

_logger = logging.getLogger("subroutine.api")

#: How many keys to hold before sweeping the full ones. A bucket that has refilled completely
#: is indistinguishable from one that never existed, so dropping it forgets nothing — and
#: without a sweep the map grows once per distinct address for the lifetime of the process,
#: which is a slow leak somebody would meet as memory rather than as rate limiting.
SWEEP_ABOVE = 4096


@dataclasses.dataclass
class Bucket:
	"""How much of an allowance is left, and when it was last worked out."""

	tokens: float
	at: float


class Limiter:
	"""A token bucket per key, refilling continuously.

	Continuous refill rather than fixed windows, because a window lets a caller spend its
	whole allowance in the last instant of one and again in the first instant of the next —
	twice the intended rate at exactly the moment a limiter is supposed to be holding.
	"""

	def __init__ (self, *, per_minute: int, clock: typing.Callable[[], float] | None = None):
		"""Build a limiter allowing ``per_minute`` requests per key, in a burst of that size."""

		self.per_minute = per_minute
		self.capacity = float(per_minute)
		self._clock = clock or time.monotonic
		self._buckets: dict[str, Bucket] = {}

		# Sync endpoints run in a thread pool, so two requests genuinely do arrive at once.
		# Without this a read-modify-write of the same bucket can lose one of them, which
		# makes the limit approximately rather than actually a limit.
		self._lock = threading.Lock()

	def take (self, key: str) -> float | None:
		"""Spend one request against ``key``, or return how long until one is free.

		``None`` means it was allowed. A number is the ``Retry-After`` value, in seconds and
		rounded up — a caller told to wait 0 seconds retries immediately and is refused again.
		"""

		if self.per_minute <= 0:
			return None

		now = self._clock()

		with self._lock:
			bucket = self._buckets.get(key)

			if bucket is None:
				bucket = Bucket(tokens=self.capacity, at=now)
				self._buckets[key] = bucket

				self._sweep(now)

			bucket.tokens = min(
				self.capacity, bucket.tokens + (now - bucket.at) * self.per_minute / 60.0
			)
			bucket.at = now

			if bucket.tokens >= 1.0:
				bucket.tokens -= 1.0

				return None

			# Time for one whole token, not for a full bucket: the caller wants to make its
			# next request, not to be restored to a fresh allowance.
			needed = (1.0 - bucket.tokens) * 60.0 / self.per_minute

			return max(1.0, needed)

	def _sweep (self, now: float) -> None:
		"""Drop every key whose allowance has fully refilled. Called with the lock held."""

		if len(self._buckets) <= SWEEP_ABOVE:
			return

		self._buckets = {
			key: bucket
			for key, bucket in self._buckets.items()
			if bucket.tokens + (now - bucket.at) * self.per_minute / 60.0 < self.capacity
		}


def wanted (settings: subroutine.config.Settings, *, host: str) -> bool:
	"""Report whether this instance should be rate limiting at all.

	``rate_limit`` unset means **on unless the bind is loopback** (§7.7). A limiter is about
	callers reaching this over a network, and on a laptop the only caller is the person who
	owns the machine — counting their requests would be ceremony in service of nothing.

	Set explicitly, it is obeyed either way: an operator who wants one on loopback is usually
	testing the limiter, and one who wants it off on a public bind has said so out loud.
	"""

	if settings.rate_limit is not None:
		return settings.rate_limit

	return not subroutine.config.is_loopback(host)


class Limits:
	"""Both of §7.7's limiters, and the decision to run them at all.

	Held on the application rather than made per request, because a token bucket that is
	rebuilt on every call counts nothing. Built once in :func:`subroutine.api.app.create_app`
	and asked by the authentication dependency.
	"""

	def __init__ (self, settings: subroutine.config.Settings, *, host: str) -> None:
		"""Build the limiters this instance wants, or none at all."""

		self.on = wanted(settings, host=host)
		self.requests = Limiter(per_minute=settings.rate_limit_per_minute)
		self.failures = Limiter(per_minute=settings.rate_limit_failures_per_minute)

	def count_a_request (self, prefix: str) -> None:
		"""Spend one request against a working credential, refusing when it is going too fast."""

		if not self.on:
			return

		waiting = self.requests.take(prefix)

		if waiting is None:
			return

		raise subroutine.errors.RateLimited(
			"This credential is making requests faster than this instance serves them.",
			hint=f"Wait {int(waiting)} seconds and try again.",
			extensions={"retry_after": int(waiting)},
		)

	def count_a_failure (self, request: starlette.requests.Request) -> None:
		"""Spend one failed authentication against where it came from.

		**Keyed on the address, and logged with the token prefix only** (§7.7). The prefix is
		the caller's to choose, so keying the counter on it would hand an attacker a fresh
		allowance for every guess; the prefix is still what reaches the log, because it is the
		half a person can act on — ``token revoke`` takes it — and the secret must not be
		written down anywhere, ever.
		"""

		if not self.on:
			return

		waiting = self.failures.take(_where_from(request))

		if waiting is None:
			return

		_logger.warning(
			"Rate limiting failed authentications from %s (token prefix %s)",
			_where_from(request),
			_prefix_of(request) or "none",
		)

		raise subroutine.errors.RateLimited(
			"Too many credentials that did not work.",
			hint=f"Wait {int(waiting)} seconds and try again.",
			extensions={"retry_after": int(waiting)},
		)


def _where_from (request: starlette.requests.Request) -> str:
	"""Return the address a request came from, or a stand-in when there is none.

	``request.client`` is ``None`` under an ASGI transport that carries no client — the test
	suite is one — and a limiter that raised there would fail a request rather than count it.
	"""

	return "unknown" if request.client is None else request.client.host


def _prefix_of (request: starlette.requests.Request) -> str | None:
	"""Return the public half of whatever token was presented, for the log and nothing else.

	Never the secret. This is read from a credential that has already been *rejected*, so it
	is as likely to be somebody's guess as anybody's token — which is exactly why the log
	entry is worth having and why it must carry no more than this.
	"""

	header = request.headers.get("authorization", "")
	_scheme, _, credential = header.partition(" ")
	parts = credential.strip().split("_")

	return parts[1] if len(parts) >= 3 and parts[0] == subroutine.auth.TOKEN_SCHEME else None
