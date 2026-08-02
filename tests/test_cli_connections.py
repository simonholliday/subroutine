"""Connections through the CLI, end to end — SPEC.md §13.7, §12.3a and §12.4.

Two installations in two temporary XDG homes, one of them served over a real socket and
reached by the other as a remote connection. That is more machinery than a unit test wants,
and it is the only arrangement that can answer the questions this file exists to ask: does a
merged agenda name a remote task in a form you can type back, does a write land on the right
instance, and does a server going away leave a person able to see their own list.

The parts that need no server — the bind refusal and issuing a token — are here too, because
they are the same command surface.
"""

import contextlib
import datetime
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import typing
import zoneinfo

import httpx
import pytest
import sqlalchemy
import typer.testing

import subroutine.cli.main
import subroutine.credentials

#: The zone every instance in this file is created in, named once because a test that asks
#: what day it is has to ask *this* clock rather than the machine's (`#233`). Deliberately
#: not UTC: a zone that is offset from the runner's is what makes a whole-day expiry mean
#: something to assert.
INSTANCE_ZONE = "Europe/London"


#: How long to wait for a freshly started server before giving up on it.
STARTUP_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG directory at a fresh temporary home, with nothing inherited."""

	root = tmp_path / "here"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	for name in list(os.environ):
		if name.startswith(("SUBROUTINE_TOKEN", "SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", INSTANCE_ZONE)

	return root


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Return a runner for the real CLI, failing loudly on an unexpected exit code."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


def declare (home: pathlib.Path, text: str) -> None:
	"""Append to the configuration file."""

	where = home / "xdg_config_home" / "subroutine"
	where.mkdir(parents=True, exist_ok=True)

	with (where / "config.toml").open("a", encoding="utf-8") as handle:
		handle.write(text)


def free_port () -> int:
	"""Return a port nothing is listening on.

	Bound and released rather than picked from a range: a hard-coded port makes a test that
	fails when somebody happens to be running something, which is the worst kind of failure to
	debug because it is about the machine and not the code.
	"""

	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))

		return int(probe.getsockname()[1])


class Remote(typing.NamedTuple):
	"""Another installation, served over a socket."""

	url: str
	token: str
	home: pathlib.Path


@contextlib.contextmanager
def served (tmp_path: pathlib.Path) -> typing.Iterator[Remote]:
	"""Set up a second installation, put a task in it, and serve it.

	A real subprocess and a real socket. The in-process ASGI transport is enough for
	``tests/test_transport_equivalence.py``, which is comparing two clients; it is not enough
	here, where the questions are about ``subroutine serve`` and about what happens when a
	server is not there.
	"""

	root = tmp_path / "there"
	environment = {
		**os.environ,
		"XDG_CONFIG_HOME": str(root / "config"),
		"XDG_DATA_HOME": str(root / "data"),
		"XDG_STATE_HOME": str(root / "state"),
		"SUBROUTINE_DEFAULT_TIMEZONE": INSTANCE_ZONE,
	}

	for name in list(environment):
		if name.startswith(("SUBROUTINE_TOKEN", "SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			del environment[name]

	def there (*arguments: str) -> str:
		"""Run the CLI against the other installation."""

		done = subprocess.run(
			[sys.executable, "-m", "subroutine", *arguments],
			env=environment,
			capture_output=True,
			text=True,
			check=False,
		)

		assert done.returncode == 0, f"{' '.join(arguments)}\n{done.stdout}\n{done.stderr}"

		return done.stdout

	there("init", "--workspace", "Acme")
	there("add", "Fix the deploy script by friday")

	issued = there("token", "create", "--title", "For the test")
	token = next(
		word for word in issued.split() if word.startswith("sr_")
	)
	port = free_port()

	server = subprocess.Popen(
		[sys.executable, "-m", "subroutine", "serve", "--port", str(port)],
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
	)
	url = f"http://127.0.0.1:{port}"

	try:
		_await(server, url)

		yield Remote(url=url, token=token, home=root)

	finally:
		server.terminate()

		# ``communicate`` rather than ``wait``, because it also *closes* the pipe. Leaving it
		# open leaks a file object, and this suite runs with ``filterwarnings = ["error"]``, so
		# the ResourceWarning arrives as a collection error at teardown — pointing at pytest's
		# internals rather than at the fixture that caused it.
		try:
			server.communicate(timeout=10)

		except subprocess.TimeoutExpired:
			server.kill()
			server.communicate()


def _await (server: subprocess.Popen[str], url: str) -> None:
	"""Wait until the server answers, or say what it printed instead of starting."""

	deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

	while time.monotonic() < deadline:
		if server.poll() is not None:
			pytest.fail(f"the server exited early:\n{server.communicate()[0]}")

		with contextlib.suppress(httpx.HTTPError):
			if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
				return

		time.sleep(0.2)

	pytest.fail(f"the server did not answer within {STARTUP_TIMEOUT_SECONDS:g} seconds")


@pytest.fixture
def two (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> typing.Iterator[Remote]:
	"""One local installation with a task, and one remote one configured as ``work``."""

	run("init", "--workspace", "Personal")
	run("add", "Pay the gas bill")

	with served(tmp_path) as remote:
		declare(home, f'\n[connections.work]\nurl = "{remote.url}"\n')
		subroutine.credentials.store("work", remote.token)

		yield remote


# --- The merged view --------------------------------------------------------------------


def test_the_agenda_merges_both_instances_into_one_day (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The whole point of §13.7: the dentist and the stand-up in one place.

	Merged rather than grouped by connection, because a heading per connection would put a
	person's day in two lists.
	"""

	output = run("today").output

	assert "Pay the gas bill" in output
	assert "Fix the deploy script" in output


def test_a_remote_row_prints_an_address_that_can_be_typed_back (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""What is printed is what can be typed back, per row rather than in a footer."""

	line = next(
		text for text in run("today").output.splitlines() if "Fix the deploy script" in text
	)
	address = line.split()[0]

	assert address.startswith("work/"), f"a remote row names its connection: {line!r}"

	# And the thing it printed resolves — without the sigil, which a shell would eat.
	typed = address.replace("#", "")

	assert "Done: work/" in run("done", typed).output


def test_the_listing_groups_by_connection_and_merges_on_request (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""A flat list of open tasks has no ordering a person already holds, so the connection is
	the only structure there is — until they ask for one list."""

	grouped = run("ls").output

	assert "Local" in grouped
	assert "work" in grouped

	merged = run("ls", "--merged").output

	assert "Pay the gas bill" in merged
	assert "Fix the deploy script" in merged
	assert "Local\n" not in merged, "no group headings once it is one list"


def test_a_write_lands_on_the_connection_the_address_named (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Reads fan out; writes never do."""

	run("done", "work/acme/1")

	after = run("ls").output

	assert "Fix the deploy script" not in after, "the remote task was completed"
	assert "Pay the gas bill" in after, "and the local one was not touched"


def test_use_changes_what_a_bare_number_means_and_not_what_can_be_seen (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The load-bearing rule of §13.7.

	Because nothing is ever hidden, forgetting your context cannot cost you a missed item —
	which is what makes a switchable context usable without a banner on every response.
	"""

	run("use", "work/acme")

	assert "Now working in work/acme" in run("use", "work/acme").output

	still_visible = run("ls").output

	assert "Pay the gas bill" in still_visible, "reads still span everything reachable"

	# And a bare number now means the remote one.
	assert "work/acme/#1" in run("done", "1").output


def test_use_reports_where_the_context_came_from (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Provenance, which is the half that earns its keep."""

	run("use", "work/acme")

	assert "from 'subroutine use'" in run("use").output

	run("use", "--reset")

	assert "work/acme" not in run("use").output


def test_an_unreachable_connection_is_named_and_skipped (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the command still exits 0 with the results it has.

	An agenda that refuses to print because one of three servers is down is worse than an
	agenda with a line saying which one.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Pay the gas bill")

	declare(home, f'\n[connections.work]\nurl = "http://127.0.0.1:{free_port()}"\n')
	subroutine.credentials.store("work", "sr_not_a_real_token")

	result = run("today")

	assert "work" in result.output, "the connection that failed is named"
	assert "Pay the gas bill" in result.output, "and the rest of the list still prints"


def test_when_nothing_can_be_reached_the_reason_is_still_printed (
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#127``. The commonest failure there is: the only connection is the broken one.

	Every failure already carries a sentence somebody can act on, and this path threw all of
	them away for "No connection could be reached" plus an instruction to go and check
	configuration that is perfectly fine — a message naming a cause which is not the cause.

	It hid behind the *partial* case reading well: one connection down out of three is named
	properly, because that report happens after the point this returns from.

	The break is a dropped table rather than a schema behind the code, deliberately. A stale
	schema has its own explanation now, and a test that could be satisfied by it would stop
	covering this the moment that landed.
	"""

	run("init", "--workspace", "Personal")

	database = home / "xdg_data_home" / "subroutine" / "subroutine.db"
	engine = sqlalchemy.create_engine(f"sqlite:///{database}")

	try:
		with engine.begin() as connection:
			connection.exec_driver_sql("DROP TABLE workspace")

	finally:
		engine.dispose()

	result = run("today", expect=1)

	assert "workspace" in result.output, f"the reason was not printed:\n{result.output}"
	assert "to see what is configured" not in result.output, (
		"the generic hint is for having no connections, not for having a broken one"
	)

	# **And not "could not be reached"**, which asserts a cause as confidently as the hint did.
	# This connection was reached; it is unusable. The original wording is still right for the
	# case it was written for — having nothing to ask in the first place.
	assert "Nothing could be read." in result.output
	assert "could not be reached" not in result.output


def test_strict_makes_an_unreachable_connection_fatal_and_says_so_plainly (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""For a script that would rather stop than act on a partial view.

	The refusal has to be a sentence. It arrived as a traceback the first time this was run by
	hand, because the failure escaped past the handler that turns one into the other.
	"""

	run("init", "--workspace", "Personal")
	declare(home, f'\n[connections.work]\nurl = "http://127.0.0.1:{free_port()}"\n')
	subroutine.credentials.store("work", "sr_not_a_real_token")

	result = run("today", "--strict", expect=1)

	assert "could not be reached" in result.output
	assert "Traceback" not in result.output


def test_the_same_instance_configured_twice_is_refused_by_name (
	two: Remote,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Otherwise every task on it is counted, printed and offered for completion twice.

	Named rather than deduplicated: silently dropping one would leave a person with a
	connection that does nothing and no way to find out why.
	"""

	declare(home, f'\n[connections.acme]\nurl = "{two.url}"\n')
	subroutine.credentials.store("acme", two.token)

	result = run("today", expect=1)

	assert "same instance" in result.output
	assert "work" in result.output and "acme" in result.output


def test_the_scripted_path_carries_the_address_and_says_what_it_missed (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""A script merging two connections needs the thing it can type back."""

	agenda = json.loads(run("today", "--json").output)
	rows = [*agenda["overdue"], *agenda["today"], *agenda["upcoming"], *agenda["unscheduled"]]
	remote = next(row for row in rows if row["title"].startswith("Fix the deploy"))

	assert remote["connection"] == "work"
	assert remote["address"].startswith("work/")
	assert agenda["unreachable"] == []


def test_connections_reports_where_each_token_came_from_without_printing_one (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Which of §12.3a's four places supplied it is the useful part."""

	output = run("connections").output

	assert "credentials.toml" in output, "and where it read the token from"
	assert two.token not in output, "but never the token itself"
	assert "default" in output


# --- Serving, and issuing a credential -------------------------------------------------


def test_serve_refuses_a_non_loopback_bind_without_tls (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""SPEC.md §12.4. Binding beyond this machine is where bearer tokens start crossing a
	network, and a note in the documentation puts the warning where it will not be read."""

	run("init")

	result = run("serve", "--host", "0.0.0.0", expect=1)

	assert "Refusing to listen on 0.0.0.0" in result.output
	assert "compromised tokens" in result.output
	assert "--insecure" in result.output, "and it says both ways out"
	assert "public_url" in result.output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.1.2.3", "::1", "[::1]"])
def test_a_loopback_bind_is_recognised_however_it_is_written (host: str) -> None:
	"""Including ``localhost``, which is the case the check exists to allow.

	``ipaddress`` cannot parse a name, so refusing to serve on ``localhost`` because it is not
	spelled ``127.0.0.1`` would be the check failing on its own best case.
	"""

	assert subroutine.cli.main.is_loopback(host)


def test_a_wildcard_bind_is_not_loopback_even_though_it_includes_it () -> None:
	"""It accepts a connection from anywhere the machine has an address, which is the point."""

	assert not subroutine.cli.main.is_loopback("0.0.0.0")
	assert not subroutine.cli.main.is_loopback("::")
	assert not subroutine.cli.main.is_loopback("192.168.0.5")


def test_public_url_over_https_is_what_makes_a_public_bind_acceptable (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""The correct production setup: a TLS-terminating proxy in front."""

	run("init")
	declare(home, '\npublic_url = "http://tasks.example.com"\n')

	plain = run("serve", "--host", "0.0.0.0", expect=1)

	assert "not an https:// address" in plain.output, "and it says why this one did not count"


def test_a_token_is_printed_once_and_not_stored_by_default (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""Writing a narrow token into ``credentials.toml`` would silently narrow the local CLI."""

	run("init")

	result = run("token", "create", "--title", "My laptop")
	secret = next(word for word in result.output.split() if word.startswith("sr_"))

	assert "only time it is shown" in result.output
	assert subroutine.credentials.read_file() == {}
	assert secret not in (
		home / "xdg_config_home" / "subroutine" / "config.toml"
	).read_text(encoding="utf-8")


def test_a_token_can_be_stored_against_a_connection_on_request (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``--store`` is the opt-in, and it writes to the private file rather than the public one."""

	run("init")

	result = run("token", "create", "--store", "work")
	secret = next(word for word in result.output.split() if word.startswith("sr_"))

	assert subroutine.credentials.read_file() == {"work": secret}
	assert subroutine.credentials.permission_warning() is None


def test_a_service_account_is_created_with_a_role_it_can_work_with (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""An account with no role authenticates and can do nothing, which reads as a broken token."""

	run("init")

	result = run("token", "create", "--service-account", "claude")

	assert "Created service account claude" in result.output
	assert subroutine.cli.main.SERVICE_ACCOUNT_ROLE in result.output


def test_creating_a_service_account_does_not_break_the_local_to_do_list (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Setting up an agent must not cost you your own list.

	Local mode picks the sole account when there is one (§12.1a). Service accounts were counted
	until 2026-07-30, so the command §12.3a exists for immediately broke ``subroutine add``
	with "this database has more than one account". A machine identity was never a candidate
	for the answer to *whose* to-do list this is.
	"""

	run("init")
	run("add", "Pay the gas bill")
	run("token", "create", "--service-account", "claude")

	assert "Added: Buy milk" in run("add", "Buy milk").output
	assert "Pay the gas bill" in run("ls").output


def test_issuing_a_second_token_for_one_agent_reuses_the_account (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	""""That name is taken" would be a strange thing to say about the account you asked for."""

	run("init")
	run("token", "create", "--service-account", "claude")

	again = run("token", "create", "--service-account", "claude")

	assert "Created service account" not in again.output


def test_a_scoped_token_cannot_mint_itself_a_wider_one (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The privilege escalation this file exists to prevent recurring.

	`token create` resolved the operator with no token at all, so an agent holding a credential
	scoped to `task:read` could not add a task and *could* mint itself an unrestricted one — it
	was authorised as the sole human, which after `init` is a superuser. Two commands defeated
	§7.4's whole least-privilege model, and §12.1a's promise that "the check runs in local mode
	exactly as it runs over HTTP" was false for the one command that hands out authority.
	"""

	run("init")

	issued = run("token", "create", "--service-account", "claude", "--scope", "task:read")
	narrow = next(word for word in issued.output.split() if word.startswith("sr_"))

	monkeypatch.setenv("SUBROUTINE_TOKEN", narrow)

	# The baseline: this token cannot write, and says so usefully.
	refused = run("add", "agent writing", expect=1)

	assert "task:write" in refused.output

	# And it cannot route around that by issuing itself a better one.
	escalation = run("token", "create", "--title", "escalated", expect=1)

	assert "cannot grant more" in escalation.output
	assert "task:read" in escalation.output, "and it says what the presented token allows"
	assert not re.search(r"sr_[0-9a-f]{8}_", escalation.output), "no credential was printed"

	# The legitimate case still works: same scopes, or fewer.
	narrower = run("token", "create", "--title", "fine", "--scope", "task:read")

	assert re.search(r"sr_[0-9a-f]{8}_", narrower.output)


def test_a_service_account_token_actually_works_over_http (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The role a service account is given has to grant what the CLI claims it does.

	This was asserted by string-matching the word "contributor" in the command's own output —
	a true assertion that proved nothing. An account with no usable membership authenticates
	and can do nothing, which reads as a broken token rather than a missing role.
	"""

	run("init", "--workspace", "Personal")

	issued = run("token", "create", "--service-account", "claude")
	token = next(word for word in issued.output.split() if word.startswith("sr_"))
	port = free_port()

	server = subprocess.Popen(
		[sys.executable, "-m", "subroutine", "serve", "--port", str(port)],
		env={
			**os.environ,
			"XDG_CONFIG_HOME": str(home / "xdg_config_home"),
			"XDG_DATA_HOME": str(home / "xdg_data_home"),
			"XDG_STATE_HOME": str(home / "xdg_state_home"),
		},
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
	)
	url = f"http://127.0.0.1:{port}"

	try:
		_await(server, url)
		headers = {"authorization": f"Bearer {token}"}

		created = httpx.post(
			f"{url}/v1/tasks", json={"text": "written by the agent"}, headers=headers
		)

		assert created.status_code == 201, created.text
		assert created.json()["title"] == "written by the agent"

		# And it can read back what it wrote, which needs the read half of the role too.
		assert httpx.get(f"{url}/v1/tasks", headers=headers).status_code == 200

	finally:
		server.terminate()

		try:
			server.communicate(timeout=10)

		except subprocess.TimeoutExpired:
			server.kill()
			server.communicate()


def test_a_token_is_never_issued_against_an_unusable_credentials_file (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`--store` committed the token before it could store or print it.

	With a malformed `credentials.toml`, the command died with a traceback after `commit()` —
	leaving a live, unrevoked credential whose secret was never displayed and cannot be
	recovered, because only a hash is kept (§7.4). Every retry made another orphan.
	"""

	run("init")

	broken = home / "xdg_config_home" / "subroutine" / "credentials.toml"
	broken.parent.mkdir(parents=True, exist_ok=True)
	broken.write_text("[work\ntoken =\n", encoding="utf-8")

	result = run("token", "create", "--store", "work", expect=1)

	assert "not valid TOML" in result.output
	assert not re.search(r"sr_[0-9a-f]{8}_", result.output), "nothing was printed"

	# The refusal happened before anything was issued.
	assert _tokens_in(home) == 0, "a credential was minted and stranded"


def test_a_credential_for_a_person_and_one_for_a_machine_are_different_commands (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#207`. One flag answered two questions and got each of them wrong at an edge.

	``--service-account`` meant both "who is this for" and "make a machine identity if there
	is none", so naming a *person* issued that person's credential and said nothing — under a
	flag whose stated subject is machines. `#196` had no honest command to document because of
	it: both published pages told the operator to type ``--username``, which did not exist.

	Two flags, each answering one question and each true of what it does.
	"""

	run("init")
	run("user", "create", "ana", "--name", "Ana Ruiz")
	run("user", "add", "ana", "--role", "member")

	issued = run("token", "create", "--username", "ana", "--title", "Ana's laptop").output

	assert re.search(r"sr_[0-9a-f]{8}_", issued), "the documented command issues a credential"
	assert "service account" not in issued.lower(), "and does not report making one"

	# Ana is still a person. The flag that would have said otherwise now refuses.
	refused = run("token", "create", "--service-account", "ana", expect=1).output

	assert "not a machine identity" in refused
	assert "--username ana" in refused, "and names the flag that does what they meant"

	# The machine half is untouched: it still creates, and still reuses on a second call.
	created = run("token", "create", "--service-account", "claude").output

	assert "Created service account claude" in created

	again = run("token", "create", "--service-account", "claude").output

	assert "Created service account" not in again, "a second token for one agent is ordinary"


def test_a_credential_is_never_issued_for_an_account_that_could_not_use_it (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#207`, the other half. It minted tokens that were dead on arrival.

	``authenticate`` refuses a token whose owner is inactive, so issuing one for a deactivated
	account produced a credential that was accepted, printed, possibly stored — and then
	refused the first time anybody used it, with a message about the account rather than about
	the command that made it.

	Absent and deactivated get different sentences, because they have different remedies and
	the wrong one sends somebody at a name that is already taken.
	"""

	run("init")
	run("user", "create", "ana", "--name", "Ana Ruiz")
	run("user", "add", "ana", "--role", "member")

	_deactivate(home, "ana")

	stopped = run("token", "create", "--username", "ana", expect=1).output

	assert "deactivated" in stopped
	assert "Reactivate" in stopped
	assert not re.search(r"sr_[0-9a-f]{8}_", stopped), "and nothing was issued"
	assert _tokens_in(home) == 0

	missing = run("token", "create", "--username", "bob", expect=1).output

	assert "no account called 'bob'" in missing
	assert "user create bob" in missing, "which is the remedy for this one and not the other"


def _deactivate (home: pathlib.Path, username: str) -> None:
	"""Switch an account off, the way an administrator eventually will."""

	engine = sqlalchemy.create_engine(
		f"sqlite:///{home / 'xdg_data_home' / 'subroutine' / 'subroutine.db'}"
	)

	try:
		with engine.begin() as connection:
			connection.execute(
				sqlalchemy.text("update user set is_active = 0 where username = :name"),
				{"name": username},
			)

	finally:
		engine.dispose()


def test_an_administrative_refusal_names_the_file_the_bad_credential_is_in (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#199`. `#175`'s fix reached the item commands and not the ones beside them.

	`domain.local.principal` takes a `token_source` so a refusal can name where the credential
	came from; `clients/local.py` passed it and `cli/main._operator` — which serves `token
	create`, `token list` and `token revoke` — did not. So an unusable token in
	`credentials.toml` produced "the token supplied could not be used" and a remedy that goes in
	a circle: issuing another does not remove the one in the file refusing every command.

	Asserted **against the ordinary command as well**, because the defect was invisible from
	either side alone — each message reads perfectly well until they are put side by side, and
	the pair is what says the two paths agree.
	"""

	run("init")

	stored = home / "xdg_config_home" / "subroutine" / "credentials.toml"
	stored.parent.mkdir(parents=True, exist_ok=True)
	stored.write_text('[local]\ntoken = "sr_deadbeef_nonsense"\n', encoding="utf-8")

	for command in (("list",), ("token", "list")):
		output = run(*command, expect=1).output

		assert "credentials.toml" in output, f"'{' '.join(command)}' does not say where it read it"
		assert "Remove it from" in output, f"'{' '.join(command)}' does not say what to do"


def _tokens_in (home: pathlib.Path) -> int:
	"""Count the credentials in the installation under this temporary home."""

	path = home / "xdg_data_home" / "subroutine" / "subroutine.db"

	if not path.exists():
		return 0

	engine = sqlalchemy.create_engine(f"sqlite:///{path}")

	try:
		with engine.connect() as connection:
			return int(
				connection.execute(
					sqlalchemy.text("select count(*) from api_token")
				).scalar_one()
			)

	finally:
		engine.dispose()


def test_a_credential_can_be_listed_and_revoked (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#156`. `revoked_at` was read on every request since M1 and nothing could set it.

	So an instance could issue credentials and never take one back — survivable for a single
	user on their own machine, and the whole safety model for the case Simon described: a
	month's freelancing on somebody else's instance, where access has to end.
	"""

	run("init")

	issued = run("token", "create", "--title", "Acme")
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))
	prefix = secret.split("_")[1]

	listed = run("token", "list")

	assert prefix in listed.output
	assert "Acme" in listed.output
	assert "no expiry" in listed.output

	# It works, and then it does not — immediately, because the column is read on every
	# request rather than cached at issue.
	monkeypatch.setenv("SUBROUTINE_TOKEN", secret)

	assert run("add", "while it works").exit_code == 0

	monkeypatch.delenv("SUBROUTINE_TOKEN")
	run("token", "revoke", prefix)

	monkeypatch.setenv("SUBROUTINE_TOKEN", secret)
	refused = run("add", "after it is revoked", expect=1)

	assert "not accepted" in refused.output


def test_revoking_twice_reports_when_it_actually_stopped (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The second caller wants the credential dead, which it is.

	Rewriting the instant would lose when it stopped — which is the fact somebody re-running
	this under pressure is trying to establish.
	"""

	run("init")

	issued = run("token", "create", "--title", "Acme")
	prefix = next(word for word in issued.output.split() if word.startswith("sr_")).split("_")[1]

	run("token", "revoke", prefix)
	again = run("token", "revoke", prefix)

	assert "already revoked" in again.output


def test_a_token_can_be_given_an_expiry_and_stops_working_after_it (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#156`: `expires_at` was settable by `issue_token` and by no command.

	Enforced at `authentication.py`'s expiry check, which has also been there since M1 — so
	this is the other half of the same gap, and the one that ends access without anybody
	having to remember on the day.
	"""

	run("init")

	dead = run("token", "create", "--title", "last month", "--expires", "2020-01-01")
	stale = next(word for word in dead.output.split() if word.startswith("sr_"))

	assert "expired" in run("token", "list").output

	monkeypatch.setenv("SUBROUTINE_TOKEN", stale)

	assert "not accepted" in run("add", "too late", expect=1).output


def test_an_expiry_names_a_whole_day_and_the_token_lives_through_it (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""§6.5's reading of a deadline, applied to a credential.

	A token that stopped at the midnight *starting* the day somebody named would be a
	surprise arriving at the worst possible moment — most of a day earlier than they meant.
	"""

	run("init")

	# **The instance's today, not the machine's** (`#233`). The runner is UTC and the instance
	# is an hour ahead of it, so between 23:00 and midnight `date.today()` names a day that
	# ended here an hour ago — and the token issued "through today" is already expired when the
	# listing is read. One hour a night, every night the offset is not zero.
	today = datetime.datetime.now(zoneinfo.ZoneInfo(INSTANCE_ZONE)).date().isoformat()
	issued = run("token", "create", "--title", "through today", "--expires", today)
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))

	assert f"until {today}" in run("token", "list").output

	monkeypatch.setenv("SUBROUTINE_TOKEN", secret)

	assert run("add", "still valid on the named day").exit_code == 0


def test_a_whole_token_is_refused_where_a_prefix_belongs (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""It would work, which is exactly why it must not.

	The prefix is right there in the string, so accepting it costs nothing to implement and
	puts a live credential into shell history and into `ps` output for every process on the
	machine — the one thing §7.4 never lets a secret do.
	"""

	run("init")

	issued = run("token", "create", "--title", "Acme")
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))

	refused = run("token", "revoke", secret, expect=1)

	assert "whole token" in refused.output
	assert secret.split("_")[1] in refused.output, "it says which part to use instead"

	# And the forgiving half: the scheme-prefixed spelling of the *prefix* is taken, because
	# a ref accepts `42` and `#42` for the same reason.
	assert "Revoked" in run("token", "revoke", "_".join(secret.split("_")[:2])).output


def test_an_empty_connection_keeps_its_heading (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#269`. A reachable instance with nothing in it must not read as a broken one.

	Simon wired up a connection, saw it in `subroutine connections`, ran `subroutine list`, and
	saw only `Local` — because the new instance had no items yet and `_grouped` skipped it. He
	went back to check two configuration files and a token, all of which were correct. The
	absence carried a meaning it had not earned.

	The failure line only appears for a connection that *errored*, so before this there was
	nothing anywhere in the output separating "reachable and empty" from "not reachable".
	"""

	run("done", "work/acme/1")

	listed = run("ls").output

	assert "work" in listed, "the connection is still named"
	assert "Nothing here." in listed
	assert "Pay the gas bill" in listed, "and the local side is unaffected"


def test_one_connection_alone_still_prints_no_heading (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And §13.5b's transcript is untouched, which is what bounds the fix above.

	With a single connection there is no group at all — printing "Local" and "Nothing here." to
	somebody setting up a to-do list would be the whole §1.4 rule broken to solve a problem
	they do not have. Without this the change above would have gone unnoticed until it reached
	the four-command test.
	"""

	run("init")

	assert "Nothing here." not in run("ls").output
	assert "Local" not in run("ls").output


def test_use_says_when_the_name_given_is_a_connection (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#270`. `use` takes `workspace` or `connection/workspace`, and a bare connection name is
	the obvious near-miss — the roster had just printed it.

	It looked for a *workspace* called `work` on the current connection, did not find one, and
	reported about somewhere else entirely. The completion is exact rather than a shape,
	because every connection's workspaces are already loaded when the world opens.
	"""

	refused = run("use", "work", expect=1)

	assert "is a connection, not a workspace" in refused.output
	assert "subroutine use work/acme" in refused.output
