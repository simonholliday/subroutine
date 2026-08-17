"""Credentials do not reach the access log — item `SR#806`.

**Driven through uvicorn's own formatter rather than through a record this file invented.**
The shape of an access record — five arguments, the path third — is not a documented interface,
so a test asserting on a tuple written here would go on passing after an upgrade changed it,
while the server wrote secrets down again. Formatting with `uvicorn.logging.AccessFormatter` is
what makes a version bump fail the build.
"""

import logging

import pytest
import uvicorn.logging

import subroutine.api.app
import subroutine.api.logs
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.sessions

#: A secret shaped like the ones that actually arrive here. Not a real credential, and long
#: enough that a partial redaction would leave something recognisable behind.
SECRET = "sr_lnk_deadbeefcafe.SsSsSsSsSsSsSsSsSsSsSsSsSsSsSsSsSsSs"

#: What uvicorn writes, verbatim from both of its HTTP implementations. Held here so the record
#: below is the one the server makes rather than one shaped to pass.
ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _logged (path: str) -> str:
	"""Return the access line uvicorn would print for this path, with the filter installed."""

	record = logging.LogRecord(
		name=subroutine.api.logs.ACCESS_LOGGER,
		level=logging.INFO,
		pathname=__file__,
		lineno=1,
		msg=ACCESS_FORMAT,
		args=("127.0.0.1:54321", "GET", path, "1.1", 200),
		exc_info=None,
	)

	assert subroutine.api.logs.Redacting().filter(record) is True, (
		"the filter dropped a record, which would lose the access log rather than clean it"
	)

	# `format` rather than `formatMessage`, because that is what a handler calls — it sets
	# `record.message` and the level prefix the access formatter's own template needs.
	return uvicorn.logging.AccessFormatter(use_colors=False).format(record)


def test_a_sign_in_link_does_not_reach_the_access_log () -> None:
	"""**The case `SR#803` turned from theoretical into live.**

	The log line is written on response, so until the confirmation page existed the secret was
	always spent by the time it was written. A confirmation deliberately leaves the link usable —
	so this route can now write down a credential that still works, for as long as half an hour,
	on exactly the path somebody meets when a link arrives that they did not expect.
	"""

	line = _logged(f"/signin?{subroutine.api.sessions.LINK_PARAMETER}={SECRET}")

	assert SECRET not in line, f"the access log carries a live sign-in link: {line}"
	assert subroutine.api.logs.REDACTED in line
	assert "/signin" in line, "the path itself is gone, so the log cannot be read"


@pytest.mark.parametrize("name", sorted(subroutine.api.security.TOKEN_PARAMETERS))
def test_a_token_in_the_wrong_place_does_not_reach_the_access_log (name: str) -> None:
	"""**Parametrised over the thing being measured**, so a sixth name is covered by adding it.

	These are refused, and the refusal tells the caller to *treat that token as compromised* —
	which makes writing it into our own log afterwards the same mistake one layer on. Unlike a
	sign-in link these are long-lived, so the logged value stays useful.
	"""

	line = _logged(f"/v1/tasks?{name}=sr_live_looks_real.SsSsSsSsSsSsSsSs")

	assert "sr_live_looks_real" not in line, f"the access log carries an API token: {line}"
	assert subroutine.api.logs.REDACTED in line


def test_everything_beside_a_secret_survives () -> None:
	"""A log nobody can debug with is a log that gets turned off.

	**The path has to carry a secret *and* ordinary parameters**, and the first version of this
	did not. With no secret present the redaction returns early, so a mutation replacing every
	value in the query passed all twelve tests here — the case was exercising the early exit and
	claiming to have checked the rewrite. Found by falsifying, which is the only thing that
	could have found it.
	"""

	line = _logged(
		f"/v1/tasks?status_category=todo&{subroutine.api.sessions.LINK_PARAMETER}={SECRET}"
		f"&order=-priority_score&limit=50"
	)

	assert SECRET not in line

	for kept in ("status_category=todo", "order=-priority_score", "limit=50"):
		assert kept in line, f"{kept} was removed from a line beside a secret: {line}"


def test_a_line_with_nothing_to_hide_is_returned_unchanged () -> None:
	"""The common case, byte for byte — no re-encoding, no marker, no work."""

	ordinary = "/v1/tasks?status_category=todo&order=-priority_score&limit=50"

	assert subroutine.api.logs.redacted(ordinary) == ordinary
	assert subroutine.api.logs.REDACTED not in _logged(ordinary)


def test_a_path_with_no_query_is_untouched () -> None:
	"""The overwhelming majority of requests, and the one this must not slow down or rewrite."""

	assert subroutine.api.logs.redacted("/v1/tasks") == "/v1/tasks"
	assert "/healthz" in _logged("/healthz")


def test_the_link_parameter_is_one_the_route_really_declares () -> None:
	"""**The redaction list is checked against the route rather than against itself.**

	Three places have to agree on this name — the query parameter, the confirmation form's field,
	and what is kept out of the log — and only the third is a security control. A constant naming
	a parameter `/signin` had stopped using would go on passing while the secret went on being
	written down, which is `SR#412`'s *reach against write set* shape on a logging filter.
	"""

	declared = {
		field.name
		for path, _methods, route in subroutine.api.routing.mounted(
			subroutine.api.app.ROUTERS
		)
		if path == "/signin"
		for field in route.dependant.query_params
	}

	assert declared, "no /signin route was found, so this is checking nothing"
	assert subroutine.api.sessions.LINK_PARAMETER in declared, (
		f"/signin takes {sorted(declared)} and the redaction protects "
		f"{subroutine.api.sessions.LINK_PARAMETER!r}"
	)


def test_every_secret_parameter_is_one_of_the_two_sources () -> None:
	"""What makes an entry go away: the set is derived, so it cannot grow a name of its own.

	**Folded, and the fold is asserted rather than assumed** (`#946`): comparing against the raw
	union would pass today by luck, because every name in both sources happens to be lower case
	already — so a name added in mixed case would silently stop being redacted.
	"""

	assert subroutine.api.logs.secret_parameters() == frozenset(
		name.lower()
		for name in (
			*subroutine.api.security.TOKEN_PARAMETERS,
			subroutine.api.sessions.LINK_PARAMETER,
		)
	)

	assert all(name == name.lower() for name in subroutine.api.logs.secret_parameters())


@pytest.mark.parametrize("spelling", ["TOKEN", "Token", "ApiKey", "AUTH", "Access_Token", "Link"])
def test_a_capitalised_credential_does_not_reach_the_access_log (spelling: str) -> None:
	"""`#946`, cold review `#927`'s L-13 — a query parameter name is case-sensitive and a
	credential is a credential.

	Whether this server would *honour* ``?TOKEN=`` is a different question from whether the
	value reached a log file, and it did: written out verbatim, on the one line an operator
	keeps for ever, about the one value they would want to have caught.

	**Capitalised by hand rather than by transforming the constants**, so this cannot agree with
	the code by construction.
	"""

	line = _logged(f"/v1/tasks?{spelling}=sr_live_looks_real.SsSsSsSsSsSsSsSs")

	assert "sr_live_looks_real" not in line, (
		f"?{spelling}= put a credential in the access log: {line}"
	)
	assert subroutine.api.logs.REDACTED in line


def test_installing_it_twice_leaves_one_filter () -> None:
	"""``serve`` is one call today, and a second entry point would be the ordinary way to get two."""

	logger = logging.getLogger("subroutine.test.access")
	logger.filters.clear()

	subroutine.api.logs.redact_access_logs(logger)
	subroutine.api.logs.redact_access_logs(logger)

	try:
		assert len(logger.filters) == 1
	finally:
		logger.filters.clear()


def test_it_is_installed_on_the_logger_the_server_uses () -> None:
	"""A filter on a logger nothing writes to is `SR#303`'s inert control wearing a new hat.

	Falsified by pointing ``ACCESS_LOGGER`` at a name of its own: this passes, which is why the
	name is compared against uvicorn's rather than merely used.
	"""

	assert subroutine.api.logs.ACCESS_LOGGER == "uvicorn.access"

	logger = logging.getLogger(subroutine.api.logs.ACCESS_LOGGER)
	had = list(logger.filters)

	try:
		logger.filters.clear()
		subroutine.api.logs.redact_access_logs()

		assert any(
			isinstance(one, subroutine.api.logs.Redacting) for one in logger.filters
		)
	finally:
		logger.filters[:] = had
