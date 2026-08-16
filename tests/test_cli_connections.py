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
import sqlite3
import subprocess
import sys
import time
import typing
import zoneinfo

import click
import httpx
import pytest
import sqlalchemy
import typer.main
import typer.testing

import subroutine
import subroutine.api.app
import subroutine.cli.main
import subroutine.credentials
import subroutine.domain.profiles
import subroutine.domain.tokens
import subroutine.installations
import subroutine.releases

#: The zone every instance in this file is created in, named once because a test that asks
#: what day it is has to ask *this* clock rather than the machine's (`#233`). Deliberately
#: not UTC: a zone that is offset from the runner's is what makes a whole-day expiry mean
#: something to assert.
INSTANCE_ZONE = "Europe/London"


#: How long to wait for a freshly started server before giving up on it.
STARTUP_TIMEOUT_SECONDS = 20.0

#: How many ports to try before deciding the machine, rather than a race, is the problem.
PORT_ATTEMPTS = 5

#: What a server prints when something else bound the port between choosing it and starting.
#: Matched on the sentence rather than the number, which is errno 98 here and 48 on macOS.
PORT_TAKEN = "address already in use"


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

	def invoke (
		*arguments: str, expect: int = 0, input: str | None = None
	) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments), input=input)

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
	"""Return a port that was free a moment ago.

	Bound and released rather than picked from a range: a hard-coded port makes a test that
	fails when somebody happens to be running something, which is the worst kind of failure to
	debug because it is about the machine and not the code.

	It is free *a moment ago* and not now, and that is not fixable here — a subprocess binds
	its own socket, so this one has to be released before ``serve`` starts, and the suite runs
	across workers all doing the same thing. Whoever hands the port to a server retries;
	whoever wants an address that refuses uses ``refusing`` below, which keeps the socket.
	"""

	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))

		return int(probe.getsockname()[1])


@contextlib.contextmanager
def refusing () -> typing.Iterator[str]:
	"""Yield an address that is refused, and hold it so nothing can start answering there.

	Bound without listening: connecting is refused immediately, which is what a test about an
	unreachable connection wants, and the port cannot be taken while it is held. Choosing one
	and releasing it would leave a test asserting that a connection *cannot* be reached while
	another worker's server was free to bind exactly that port.
	"""

	with socket.socket() as held:
		held.bind(("127.0.0.1", 0))

		yield f"http://127.0.0.1:{held.getsockname()[1]}"


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

	with serving(environment) as url:
		yield Remote(url=url, token=token, home=root)


@contextlib.contextmanager
def serving (environment: dict[str, str]) -> typing.Iterator[str]:
	"""Serve an installation on a port nothing else took, and stop it afterwards.

	Retried rather than prevented: the gap between choosing a port and the server binding it
	belongs to the whole machine, and cannot be closed from here — see ``free_port``. Losing
	that race is a normal event under parallel workers and reads, unhandled, as the server
	having failed to start.
	"""

	for _attempt in range(PORT_ATTEMPTS):
		port = free_port()

		server = subprocess.Popen(
			[sys.executable, "-m", "subroutine", "serve", "--port", str(port)],
			env=environment,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
		)
		url = f"http://127.0.0.1:{port}"

		said = _await(server, url)

		if said is None:
			try:
				yield url

			finally:
				_stop(server)

			return

		if PORT_TAKEN not in said.lower():
			pytest.fail(f"the server exited early:\n{said}")

	pytest.fail(f"{PORT_ATTEMPTS} ports in a row were taken before the server could bind")


def _await (server: subprocess.Popen[str], url: str) -> str | None:
	"""Wait until the server answers, and return what it printed if it gave up instead."""

	deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

	while time.monotonic() < deadline:
		if server.poll() is not None:
			return server.communicate()[0]

		with contextlib.suppress(httpx.HTTPError):
			if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
				return None

		time.sleep(0.2)

	pytest.fail(f"the server did not answer within {STARTUP_TIMEOUT_SECONDS:g} seconds")


def _stop (server: subprocess.Popen[str]) -> None:
	"""Stop a server and close the pipe it was writing to.

	``communicate`` rather than ``wait``, because it also *closes* the pipe. Leaving it open
	leaks a file object, and this suite runs with ``filterwarnings = ["error"]``, so the
	ResourceWarning arrives as a collection error at teardown — pointing at pytest's internals
	rather than at the fixture that caused it.
	"""

	server.terminate()

	try:
		server.communicate(timeout=10)

	except subprocess.TimeoutExpired:
		server.kill()
		server.communicate()


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


def test_add_says_which_instance_the_new_item_landed_on (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``#279``, reported by a Claude Code agent whose own bug report went to the wrong one.

	``add`` confirmed with the title alone, so a capture routed somewhere unexpected — by a
	``use`` context, by a ``.subroutine`` marker, or by ``-c`` — was indistinguishable from
	one that went where the caller meant. That is the ``#273`` hazard's missing safety net:
	nothing was wrong with *where* it went, only with nobody being told.

	The creating command is the one where this costs most. Acting on the wrong item is at
	least visible in the listing you took the number from; creating one in the wrong place
	leaves a row in an instance nobody thinks to look at.
	"""

	landed = run("-c", "work", "add", "Renew the certificate").output

	assert "Renew the certificate" in landed
	assert "work/" in landed, f"add must name the instance it wrote to: {landed!r}"

	# And what it printed is an address, so it resolves — the same property the row test
	# above asserts for a listing. A confirmation naming a place you cannot type back at is
	# only half an answer.
	address = next(
		word for word in landed.split() if word.startswith("work/")
	).replace("#", "")

	assert "Renew the certificate" in run("show", address).output


def test_advice_printed_under_a_flag_still_works_once_the_flag_is_gone (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``#280``, and the sharpest of the six the agent filed.

	A tip is read *after* the command that printed it has finished, so it is a statement
	about the next invocation — and ``-c``/``-w`` have expired by then. The agenda's closing
	tip is ``subroutine done``, so a bare number justified by a flag is somebody's item
	completed on another instance.

	What makes this one hard to see is that it is the **qualified** invocation that was
	wrong and the bare one that was right, which is backwards from any guess.

	**The flags have to name the place the item is actually in**, or this cannot fail. A
	first attempt used ``-c work -w personal``, which put the context on one connection and
	the agenda's first row on the other — so the address was qualified for the ordinary
	reason and the test passed against the very defect it was written for.
	"""

	tip = next(
		line
		for line in run("-c", "work", "-w", "acme", "today").output.splitlines()
		if "subroutine done" in line
	)
	typed = tip.split("subroutine done")[1].split()[0]

	assert "/" in typed, f"a tip printed under a flag has to carry its address: {tip!r}"

	# The property itself, rather than the shape of the string: typed into a shell that
	# carries no flags, it reaches the item the tip was about. `run` fails on a non-zero
	# exit, so a tip naming something unreachable fails here.
	run("done", typed)


def test_a_listing_under_a_flag_says_the_context_came_from_the_flag (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``#281``. The footer was true of the rows above it and false of the next command.

	It was read exactly that way — as evidence that the stored context had changed — and
	the reader had to run a second, bare listing to find out it had not.
	"""

	assert "from the command line" in run("-c", "work", "list").output

	# And the ordinary case is untouched: with the context settled by something that will
	# still be true next time, there is no provenance to explain.
	assert "from the command line" not in run("list").output


def test_connections_marks_the_one_being_written_to_not_only_the_fallback (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``#278``. Two meanings of "default" coexisted and the listing named only one.

	``roster.default`` is the fallback — where a write goes when *nothing* has chosen — and
	the current context is where it actually goes. An agent read the column, concluded local
	was the answer, and told Simon so; a bare ``add`` then filed to the other instance.
	"""

	run("use", "work/acme")

	output = run("connections").output
	marked = next(line for line in output.splitlines() if "in use" in line)

	assert marked.startswith("work"), f"the context's connection is the one in use: {output!r}"

	# The column can say which; only prose can say why, and why is the whole question for
	# somebody who has just watched a write land somewhere unexpected.
	assert "Writing to work/" in output
	assert "subroutine use" in output


def test_connections_says_nothing_extra_when_both_answers_agree (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The ordinary case, which must not grow an explanation nobody needs.

	With no ``use`` set the context falls back to the default, so the two questions have one
	answer and there is nothing to disentangle.
	"""

	output = run("connections").output

	assert "in use, default" in output
	assert "Writing to" not in output


def test_doc_create_confirms_with_an_address_that_can_be_typed_back (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``#289``. It passed a workspace *id* where ``Located.workspace`` wants a slug.

	``refs.format_address`` composes ``connection/workspace/ref`` from it, so the
	confirmation read ``local/019fad98-4313-7e36-b972-f7decf66f8ae/#288`` — an address that
	resolves to nothing and that nobody would type. Every other caller of ``_acted`` passes
	a slug.

	**This needs a qualifying world or it cannot fail.** With one connection and one
	workspace ``_acted`` returns the title alone and the id is never rendered, which is why
	the defect survived: the single-instance path does not simplify the multi-instance one,
	it skips the code. `#273` and `#276` were invisible for the same reason.
	"""

	written = run("doc", "create", "A conclusion", "--body", "Something concluded.").output
	address = next(word for word in written.split() if "/#" in word)

	assert "-" not in address, f"an id where a slug belongs: {written!r}"

	# The property rather than the shape: what it printed reaches the document.
	assert "A conclusion" in run("show", address.replace("#", "")).output


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

	with refusing() as nowhere:
		declare(home, f'\n[connections.work]\nurl = "{nowhere}"\n')
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

	with refusing() as nowhere:
		declare(home, f'\n[connections.work]\nurl = "{nowhere}"\n')
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


# --- Adding a connection ----------------------------------------------------------------


def _configured (home: pathlib.Path) -> str:
	"""Return the configuration file's text, or nothing when there is none."""

	path = home / "xdg_config_home" / "subroutine" / "config.toml"

	return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_a_connection_is_added_by_command_and_the_next_listing_spans_both (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The whole of `#261`: reaching a server is a command, not a file somebody edits.

	Driven end to end rather than by reading what was written, because the thing being claimed
	is that the machine now works — a file with the right keys in it is the *evidence* for
	that, and this project has been wrong about what that evidence proves before.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Pay the gas bill")

	with served(tmp_path) as remote:
		added = run(
			"connections", "add", "work", "--url", remote.url, input=f"{remote.token}\n"
		)

		assert "Reached" in added.output
		assert "Added work" in added.output

		listed = run("list").output

		assert "Fix the deploy script" in listed, "the remote's work"
		assert "Pay the gas bill" in listed, "and this machine's own"


def test_the_token_goes_where_nothing_but_this_reads_it (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.3a's split, which is the reason there are two files rather than one.

	A configuration file is one a person can commit, sync between machines and paste into a
	support thread. The moment this command writes a token into it that stops being true, and
	nothing would report it — the connection would work perfectly.
	"""

	run("init", "--workspace", "Personal")

	with served(tmp_path) as remote:
		run("connections", "add", "work", "--url", remote.url, input=f"{remote.token}\n")

		assert remote.token not in _configured(home)
		assert remote.token in subroutine.credentials.credentials_file_path().read_text(
			encoding="utf-8"
		)


def test_a_token_is_never_asked_for_on_the_command_line () -> None:
	"""§12.3a: an argument lands in shell history and in the process list.

	Asserted against the command's own parameters rather than by reading the source, so that
	adding one for convenience fails the build. The prompt exists precisely because this is
	forbidden, and the two are easy to conflate when somebody is scripting.
	"""

	# **Typed loosely, with the reason written down**, exactly as ``tests/test_cli_help.py``
	# has to be. Typer vendors its own click shim, so what ``get_command`` returns is a
	# ``typer._click.core.Command`` — not a ``click.Command``, and with no exported name to
	# claim. The walk below uses only the methods both kinds carry.
	command: typing.Any = typer.main.get_command(subroutine.cli.main.app)
	group: typing.Any = command.get_command(
		click.Context(command, info_name="subroutine"), "connections"
	)
	add: typing.Any = group.get_command(
		click.Context(group, info_name="connections"), "add"
	)

	for parameter in add.params:
		assert "token" not in parameter.name or parameter.name in (
			"token_env",
			"token_command",
		), f"--{parameter.name.replace('_', '-')} would put a credential in shell history"


def test_nothing_is_written_when_the_instance_cannot_be_reached (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Checked before it is recorded, so a typo is refused where somebody can fix it.

	The alternative is a connection that parses, is written, and fails on the first listing —
	at which point the failure is one line among the results of every other connection, and
	the person has moved on to something else.
	"""

	run("init", "--workspace", "Personal")

	with refusing() as nowhere:
		refused = run(
			"connections",
			"add",
			"work",
			"--url",
			nowhere,
			input="sr_whatever\n",
			expect=1,
		)

	assert "could not be reached" in refused.output
	assert "connections.work" not in _configured(home)
	assert not subroutine.credentials.credentials_file_path().is_file()


def test_a_credential_the_instance_refuses_leaves_nothing_behind (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The address being right is half of it, and the other half is the token that was pasted."""

	run("init", "--workspace", "Personal")

	with served(tmp_path) as remote:
		refused = run(
			"connections",
			"add",
			"work",
			"--url",
			remote.url,
			input="sr_notthetokenyouwerelookingfor\n",
			expect=1,
		)

		assert "not accepted" in refused.output
		assert "connections.work" not in _configured(home)
		assert not subroutine.credentials.credentials_file_path().is_file()


def test_the_same_instance_under_a_second_name_is_refused_before_it_lands (
	two: Remote,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The sharpest case of "refused where it can be fixed", and it was reachable in one command.

	Two names for one instance make every merged read refuse outright, and the refusal can only
	tell somebody to go and edit a file — which is the friction this command exists to remove.
	So the check happens here, where the remedy is a different word, and the machine is left
	able to list its own work.
	"""

	refused = run(
		"connections", "add", "acme", "--url", two.url, input=f"{two.token}\n", expect=1
	)

	assert "same instance" in refused.output
	assert "work" in refused.output
	assert "connections.acme" not in _configured(home)

	assert "Fix the deploy script" in run("list").output, "and the machine still works"


def test_a_connection_that_is_turned_off_still_holds_its_name (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The roster drops a disabled connection and the file still declares it.

	Asking the roster would report the name as free and append a second table under it, after
	which the file means whichever of the two TOML kept — and this command's whole promise is
	that nobody has to open that file to find out.
	"""

	run("init", "--workspace", "Personal")
	declare(home, '\n[connections.work]\nurl = "https://tasks.example.com"\nenabled = false\n')

	refused = run(
		"connections", "add", "work", "--url", "https://elsewhere.example", expect=1
	)

	assert "already a connection called 'work'" in refused.output
	assert _configured(home).count("[connections.work]") == 1


def test_a_machine_with_no_list_of_its_own_starts_filing_to_the_connection (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The machine this command was written for: a second laptop, reaching work.

	Leaving the default pointed at a database nobody has created would make the very next
	``add`` fail, on a machine where the person has just said where their work is.
	"""

	with served(tmp_path) as remote:
		added = run(
			"connections", "add", "work", "--url", remote.url, input=f"{remote.token}\n"
		)

		assert "no list of its own" in added.output

		run("add", "Filed from the laptop")

		assert "Filed from the laptop" in run("list").output


def test_a_machine_that_has_its_own_list_keeps_writing_to_it (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Moving somebody's writes off their own to-do list is their call, not this command's."""

	run("init", "--workspace", "Personal")

	with served(tmp_path) as remote:
		added = run(
			"connections", "add", "work", "--url", remote.url, input=f"{remote.token}\n"
		)

		assert "no list of its own" not in added.output
		assert "default_connection" not in _configured(home)


@pytest.mark.parametrize(
	"name,said",
	[("local", "own database"), ("2026", "starts with a letter"), ("a b", "starts with a letter")],
)
def test_a_name_that_could_not_be_an_address_is_refused (
	name: str,
	said: str,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A connection name is the first segment of every address its items print as."""

	run("init", "--workspace", "Personal")

	refused = run(
		"connections", "add", name, "--url", "https://tasks.example.com", expect=1
	)

	assert said in refused.output
	assert "connections." not in _configured(home)


def test_a_name_typed_in_capitals_is_the_name_in_lower_case (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Every lookup already lower-cases, so refusing it here would be the one place that did not.

	``Roster.find`` has always resolved ``Work`` to ``work``, which means a capital was already
	accepted everywhere a connection is *named* — by the flag, by an address, by ``use``. What
	is stored is one form, so that an address is predictable, which is what a project key does
	for the same reason.
	"""

	with served(tmp_path) as remote:
		run("connections", "add", "Work", "--url", remote.url, input=f"{remote.token}\n")

		assert "[connections.work]" in _configured(home)
		assert "work" in run("connections").output


def test_the_listing_is_still_what_a_bare_connections_prints (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Making room for ``add`` must not rename the command people already have.

	``subroutine connections`` is in other people's notes and is what several refusals offer,
	so the group's bare invocation stays the listing rather than becoming ``connections list``.
	"""

	assert "work" in run("connections").output
	assert "add" in run("connections", "--help").output


# --- Serving, and issuing a credential -------------------------------------------------


def test_an_address_called_unreachable_stays_unreachable_while_it_is_held () -> None:
	"""Both halves, because a test about a connection being down needs both.

	Choosing a port and letting it go proves it was free a moment ago, which is a different
	claim from *nothing will answer there* — and under parallel workers the difference is
	another server in this same file binding it and answering the request that was supposed
	to fail.
	"""

	with refusing() as nowhere:
		port = int(nowhere.rsplit(":", 1)[1])

		with pytest.raises(ConnectionRefusedError), socket.socket() as caller:
			caller.settimeout(5.0)
			caller.connect(("127.0.0.1", port))

		with pytest.raises(OSError), socket.socket() as rival:
			rival.bind(("127.0.0.1", port))


def test_a_port_taken_before_the_server_binds_it_is_tried_again (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Losing the race for a port is a normal event, not a failure to start.

	This file starts thirty-odd servers and the suite runs across workers, so two of them
	choosing the same free port and one of them binding it first is only a matter of time —
	and it arrives as ``the server exited early``, which reads like a broken build. Held here
	by a listener on the port the fixture is handed, which is what losing that race looks like
	from inside.
	"""

	with socket.socket() as held:
		held.bind(("127.0.0.1", 0))
		held.listen(1)

		taken = int(held.getsockname()[1])
		unlucky = [taken]
		choose = free_port

		def once () -> int:
			"""Hand out the port somebody else is on, then real ones."""

			return unlucky.pop() if unlucky else choose()

		monkeypatch.setattr(sys.modules[__name__], "free_port", once)

		with served(tmp_path) as remote:
			assert not unlucky, "the fixture was handed the taken port"
			assert remote.url != f"http://127.0.0.1:{taken}", "and did not serve on it"
			assert httpx.get(f"{remote.url}/healthz", timeout=5.0).status_code == 200


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


def test_serving_bounds_how_long_a_stopping_server_waits (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#567`. Uvicorn waits for ever by default, so one stuck request means no restart.

	Met twice on 2026-08-07 with `/mcp` wedged by `#565`: `systemctl restart` hung, then
	`systemctl stop` hung, and the service went away only when systemd's 90-second
	`TimeoutStopSec` killed it — on the one occasion an operator is least able to wait.

	`#565` removed today's reason for a request to block for ever and cannot remove every
	future one. What can be guaranteed is that the thing can be restarted.
	"""

	run("init")

	passed: dict[str, typing.Any] = {}

	def instead_of_listening (app: typing.Any, **given: typing.Any) -> None:
		passed.update(given)

	monkeypatch.setattr("uvicorn.run", instead_of_listening)

	run("serve")

	assert passed, "serve did not reach uvicorn at all, so this asserts nothing"

	waiting = passed.get("timeout_graceful_shutdown")

	assert waiting == subroutine.cli.main.SHUTDOWN_GRACE_SECONDS

	assert waiting is not None and 0 < waiting <= 30, (
		"an unbounded or very long graceful shutdown is systemd's SIGKILL timeout wearing "
		"another name, and the operator did not choose that one either"
	)


def test_starting_a_server_says_every_transport_it_just_started (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#780`, and Simon's question is the test: *is that instance also running MCP?*

	It is, on every ``serve``, and the line printed named only the agent guide. An operator
	forms a belief about what they have just started and nothing corrects it — `#515`
	inverted, where a plugin reported success with its server dead.

	**Driven rather than asked.** ``api.app.serving()`` is a pure function and testing it
	alone would prove only that it can answer; what shipped broken here was the wire between
	a correct answer and the thing that prints it, six times over during the browser arc.
	So this runs the real command, with only uvicorn replaced.
	"""

	run("init")

	monkeypatch.setattr("uvicorn.run", lambda app, **given: None)

	printed = run("serve").output

	assert "Serving on http://127.0.0.1:" in printed

	for surface in subroutine.api.app.serving():
		assert surface.path in printed, f"{surface.path} was started and not mentioned"

	assert "/v1/docs/agent" in printed, "the guide is still the thing to read next"
	assert "nothing installed" in printed, "and MCP's line says what a caller needs"

	# `#538`: two MCP paths reach an instance, and this is the one that needs nothing at the
	# caller's end. Announcing the other would send an operator to install a package.
	assert "subroutine mcp" not in printed, "the stdio server is not what just started"

	# **No second address invented where there is none.** A laptop bound to loopback is reached
	# at loopback, and a line repeating the one above it is noise on the commonest case.
	assert "Reached at" not in printed, printed


def test_a_proxied_instance_names_the_address_that_reaches_it (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#793`, from review `#789`. The line told an agent to use an address reaching nobody.

	`serve` says, of `/mcp`, *an agent needs the address above and a token* — and on the
	deployment this project actually runs, *above* was `http://127.0.0.1:8471` behind a proxy.
	The operator has already said what the real one is: `public_url` is what
	`_refuse_public_bind` reads four lines earlier to decide whether a public bind is safe, and
	this is the same fact serving the same reader.
	"""

	run("init")
	declare(home, '\npublic_url = "https://tasks.example.com"\n')

	monkeypatch.setattr("uvicorn.run", lambda app, **given: None)

	printed = run("serve", "--host", "0.0.0.0").output

	assert "Reached at https://tasks.example.com" in printed, printed
	assert "Serving on http://0.0.0.0:" in printed, "the socket it bound is still worth saying"

	# The order matters: *the address above* has to be the reachable one by the time the
	# surfaces are listed, or the sentence points at the wrong line.
	assert printed.index("Reached at") < printed.index("/mcp"), printed


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.1.2.3", "::1", "[::1]"])
def test_a_loopback_bind_is_recognised_however_it_is_written (host: str) -> None:
	"""Including ``localhost``, which is the case the check exists to allow.

	``ipaddress`` cannot parse a name, so refusing to serve on ``localhost`` because it is not
	spelled ``127.0.0.1`` would be the check failing on its own best case.
	"""

	assert subroutine.config.is_loopback(host)


def test_a_wildcard_bind_is_not_loopback_even_though_it_includes_it () -> None:
	"""It accepts a connection from anywhere the machine has an address, which is the point."""

	assert not subroutine.config.is_loopback("0.0.0.0")
	assert not subroutine.config.is_loopback("::")
	assert not subroutine.config.is_loopback("192.168.0.5")


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
	assert subroutine.domain.tokens.SERVICE_ACCOUNT_ROLE in result.output


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


def test_one_command_sets_an_agent_up_and_the_line_it_prints_works (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#339`, and the whole of `#338` in one test: the printed line is the deliverable.

	Not "a token was issued" — that was already true. The claim is that following this
	command's output produces a shell that acts as the agent and is bounded to one project,
	which is what `#346` measured was *not* happening.
	"""

	run("init", "--username", "si", "--workspace", "Projects")
	run("project", "create", "web", "Website")
	run("project", "create", "ops", "Operations")
	run("add", "Fix the header +web")
	run("add", "Rotate the certificates +ops")

	made = run(
		"agent", "create", "claude", "--project", "web", "--scope", "task:read"
	).output

	assert "Created service account claude" in made

	line = re.search(r"(SUBROUTINE_TOKEN_[A-Z0-9_]+)=(sr_\S+)", made)

	assert line is not None, "the environment line is the deliverable, not a nicety"

	assert "claude (agent)" in made, "checked by presenting it, not by describing it"
	assert "only within web" in made
	assert "its shell acts as si" in made, "and it says what is not yet bounded"

	# **Follow the instruction and see what happens** — the only version of this check worth
	# anything. Both halves of the claim: the shell is the agent, and it is bounded.
	monkeypatch.setenv(line.group(1), line.group(2))

	assert "claude (agent)" in run("whoami").output
	assert "si (person)" not in run("whoami").output

	listed = run("list").output

	assert "Fix the header" in listed
	assert "Rotate the certificates" not in listed, "the other project is out of reach"


def test_setting_an_agent_up_reaches_a_served_instance (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The case it exists for: the machine an agent runs on holds no database of its own."""

	made = run("-c", "work", "agent", "create", "claude").output

	assert "Created service account claude" in made
	assert "SUBROUTINE_TOKEN_WORK=" in made, "named for the connection it was minted against"
	assert "claude (agent)" in made

	assert "claude" in run("-c", "work", "token", "list").output
	assert "claude" not in run("-c", "local", "token", "list").output


def test_credentials_can_be_administered_on_a_machine_that_holds_no_database (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#348`, end to end over a real socket: the case both of our machines are in.

	The three commands that set an agent up opened a local database, because §12.4 requires
	the administrative commands to work when the service will not start. Where the work lives
	on a *served* instance there is no local database to open, so they refused by name — on
	the machine somebody is setting the agent up on.
	"""

	issued = run("-c", "work", "token", "create", "--title", "Minted from here").output
	prefix = re.search(r"sr_([0-9a-f]{8})_", issued)

	assert prefix is not None, "a credential was issued against the remote instance"

	listed = run("-c", "work", "token", "list").output

	assert "Minted from here" in listed
	assert prefix.group(1) in listed

	revoked = run("-c", "work", "token", "revoke", prefix.group(1)).output

	assert "stops working immediately" in revoked

	# And the local instance is untouched by any of it, which is what makes `-c` meaningful.
	assert "Minted from here" not in run("token", "list").output


def test_administering_credentials_goes_where_writes_go (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""No flag decides the route — the connection does, the same one `add` would write to.

	A flag would have left the bare command still failing on every machine whose work is on a
	served instance, which is the complaint this fixes.
	"""

	# The whole address, because `use` names a place to work rather than a server — the remote
	# instance's workspace is `acme`, and a bare connection name leaves the other half unsaid.
	run("use", "work/acme")
	run("token", "create", "--title", "Minted after use")

	assert "Minted after use" in run("token", "list").output
	assert "Minted after use" not in run("-c", "local", "token", "list").output


def test_a_credential_can_be_restricted_to_one_project_from_the_command_line (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#216`: the narrowest credential the CLI could issue was wider than the API's.

	This is the only *enforcement* in the agent-identity milestone. Everything else records
	who did something; this is what stops an agent set up for one project touching another —
	and until now the surface people actually use to set an agent up could not ask for it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")
	run("project", "create", "ops", "Operations")
	run("add", "Fix the header +web")
	run("add", "Rotate the certificates +ops")

	issued = run("token", "create", "--service-account", "web", "--project", "web")

	assert "Restricted to web and anything filed underneath" in issued.output, (
		"the subtree is the part nobody would guess from what they typed"
	)

	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.output.split() if word.startswith("sr_"))
	)

	listed = run("list").output

	assert "Fix the header" in listed
	assert "Rotate the certificates" not in listed, "the other project is not reachable"

	# Not merely absent from a listing: the project itself does not resolve, which is what
	# §7.3a means by a restriction hiding rather than forbidding.
	assert "no project 'ops'" in run("list", "--project", "ops").output


def test_a_project_restriction_reaches_everything_under_it (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A restriction that stopped at one level would be useless on any real tree (§7.3).

	**The sibling is what makes this test able to fail.** Asserting only that the child's work
	is visible passes just as well against a credential restricted to nothing at all — which
	is exactly what it did when the restriction was falsified, while the other three tests
	caught it. A subtree claim needs something outside the subtree.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "SR", "Subroutine")
	run("project", "create", "web", "Web UI", "--parent", "SR")
	run("project", "create", "ops", "Operations")
	run("add", "Build the login page +web")
	run("add", "Rotate the certificates +ops")

	issued = run("token", "create", "--service-account", "core", "--project", "SR")

	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.output.split() if word.startswith("sr_"))
	)

	listed = run("list").output

	assert "Build the login page" in listed, "a sub-project comes with its parent"
	assert "Rotate the certificates" not in listed, "and a sibling of the parent does not"


def test_a_restricted_credential_reads_back_the_key_that_was_typed (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`project_scope` stores ids, and a person who typed `WEB` is owed `WEB` back (`#203`)."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")

	issued = run("token", "create", "--service-account", "web", "--project", "web")

	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.output.split() if word.startswith("sr_"))
	)

	assert "Narrowed to projects web" in run("whoami").output

	scoped = json.loads(run("whoami", "--json").output)[0]["credential"]

	# The id is still reported, because it is what is stored and what the API takes.
	assert scoped["project_scope_keys"] == ["web"]
	assert scoped["project_scope"] != ["web"]


def test_a_project_named_in_two_workspaces_is_refused_rather_than_picked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A key is unique per workspace, not per instance (§5.2).

	Guessing would hand an agent authority over the wrong tree, and nothing downstream could
	notice — the credential works, against the wrong project.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")
	run("workspace", "create", "acme", "Acme")
	run("-w", "acme", "project", "create", "web", "Acme website")

	refused = run("token", "create", "--username", "si", "--project", "web", expect=1)

	assert "More than one workspace" in refused.output
	assert "acme, personal" in refused.output
	assert "--workspace" in refused.output, "and it says how to settle it"

	# Named, it resolves — and the two are different projects, so this is the whole fix.
	assert "Restricted to web" in run(
		"token", "create", "--username", "si", "--workspace", "acme", "--project", "web"
	).output


def test_a_mistyped_project_is_refused_before_a_credential_exists (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The API takes ids it cannot check; a key typed by a person can be checked, so it is.

	A credential naming a project that does not exist is refused everywhere it is presented,
	for a reason nobody can see — and the secret has already been shown once and cannot be
	recovered, so minting it and finding out later is the expensive order.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	refused = run("token", "create", "--username", "si", "--project", "nope", expect=1)

	assert "no project 'nope'" in refused.output
	assert "sr_" not in refused.output, "nothing was minted"
	assert "nope" not in run("token", "list").output


def test_whoami_names_the_account_and_where_its_authority_comes_from (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The plain case: one person, one installation, no credential in sight (`#336`).

	Local mode has no token by design (§12.1a), so "via the local database" is the honest
	answer rather than a missing field — and it is the sentence that tells somebody reading
	this on a machine where an agent also works that *they* are the one asking.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	answer = run("whoami").output

	assert "si (person)" in answer
	assert "via the local database" in answer
	assert "personal" in answer, "and where that authority reaches"


def test_whoami_tells_two_principals_on_one_machine_apart (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""**The question `#338` rests on**: which of the machine's credentials is this command.

	An agent and its operator share a machine, a shell and a connection, and differ only in
	which credential the process holds (`#337`). Until `#336` nothing could be asked — the
	nuc14 agent inferred its own identity by watching a token's ``last_used_at`` move, which
	is ingenious and is a statement about what was missing.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	issued = run("token", "create", "--service-account", "claude", "--title", "the agent")
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))

	assert "si (person)" in run("whoami").output

	monkeypatch.setenv("SUBROUTINE_TOKEN", secret)

	agent = run("whoami").output

	assert "claude (agent)" in agent
	assert "the agent" in agent, "named by the title, which is what `token list` shows"
	assert "si (person)" not in agent


def test_whoami_says_what_a_narrowed_credential_is_limited_to (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""What a credential *withholds* is the part worth printing (§13.1).

	An agent should not have to discover its own authority by being refused things, and the
	permissions are stated on the row where the credential narrowed them rather than on every
	row — an unnarrowed owner would otherwise be handed twenty keys they already hold.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	issued = run(
		"token",
		"create",
		"--service-account",
		"claude",
		"--workspace",
		"personal",
		"--scope",
		"task:read",
	)

	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.output.split() if word.startswith("sr_"))
	)

	answer = run("whoami").output

	assert "Narrowed to" in answer
	assert "workspace 'personal'" in answer
	assert "scopes task:read" in answer
	assert "may: task:read" in answer


def test_whoami_hands_a_script_the_permissions_it_has_to_act_on (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The scripted path reports the whole answer, including what the printed one condenses."""

	run("init", "--username", "si", "--workspace", "Personal")

	issued = run("token", "create", "--service-account", "claude", "--scope", "task:read")

	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.output.split() if word.startswith("sr_"))
	)

	answer = json.loads(run("whoami", "--json").output)

	assert [entry["user"]["username"] for entry in answer] == ["claude"]
	assert answer[0]["credential"]["scopes"] == ["task:read"]
	assert answer[0]["credential"]["narrows"] is True
	assert answer[0]["workspaces"][0]["permissions"] == ["task:read"]

	# The secret is never in it, in any form.
	assert "sr_" not in run("whoami", "--json").output


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

	environment = {
		**os.environ,
		"XDG_CONFIG_HOME": str(home / "xdg_config_home"),
		"XDG_DATA_HOME": str(home / "xdg_data_home"),
		"XDG_STATE_HOME": str(home / "xdg_state_home"),
	}

	with serving(environment) as url:
		headers = {"authorization": f"Bearer {token}"}

		created = httpx.post(
			f"{url}/v1/tasks", json={"text": "written by the agent"}, headers=headers
		)

		assert created.status_code == 201, created.text
		assert created.json()["title"] == "written by the agent"

		# And it can read back what it wrote, which needs the read half of the role too.
		assert httpx.get(f"{url}/v1/tasks", headers=headers).status_code == 200


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
	run("user", "create", "thomas", "--name", "Thomas Anderson")
	run("user", "add", "thomas", "--role", "member")

	issued = run("token", "create", "--username", "thomas", "--title", "Thomas's laptop").output

	assert re.search(r"sr_[0-9a-f]{8}_", issued), "the documented command issues a credential"
	assert "service account" not in issued.lower(), "and does not report making one"

	# Thomas is still a person. The flag that would have said otherwise now refuses.
	refused = run("token", "create", "--service-account", "thomas", expect=1).output

	assert "not a machine identity" in refused
	assert "--username thomas" in refused, "and names the flag that does what they meant"

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
	run("user", "create", "thomas", "--name", "Thomas Anderson")
	run("user", "add", "thomas", "--role", "member")

	_deactivate(home, "thomas")

	stopped = run("token", "create", "--username", "thomas", expect=1).output

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


def test_a_listing_says_which_place_a_bare_number_means (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#271`. Simon, reading a two-connection listing: "I could mark the wrong item complete."

	He was right about the risk and wrong about the mechanism, and the mechanism is worth
	keeping: `_locate` never guesses. What was missing is that the listing never *said* which
	place a bare number meant — the only signal was which rows happened to print bare, which
	is the shortest-address rule read backwards.

	`subroutine use` answers it exactly and is one command away, which is the wrong place: the
	risk is at the moment of reading a list and typing a number off it.
	"""

	run("use", "work/acme")

	listed = run("ls").output

	assert "A bare number means work/acme" in listed


def test_one_connection_alone_says_nothing_about_context (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the bound on it, which is the whole of the argument against a banner.

	§13.7's case for leaving the context unsaid is that forgetting it cannot cost you a missed
	item, because reads span everything reachable. That holds, and is why this appears only
	when there is more than one place to be in — somebody who has never heard of a connection
	sees exactly what §13.5b says they see.
	"""

	run("init")

	assert "A bare number means" not in run("ls").output


def test_a_marker_records_its_connection_even_when_there_is_only_one (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#273`. The marker's completeness must not depend on the day it was written.

	`use --here` wrote the connection only when a second one already existed. So every marker
	written on a one-connection machine became ambiguous the moment a second was configured —
	silently, and for exactly the caller §13.7a says cannot be asked. An agent working in this
	repository had two writes redirected to a different instance within an hour of that
	happening.
	"""

	run("init")

	written = tmp_path / "checkout"
	written.mkdir()

	# **`monkeypatch.chdir`, not `os.chdir`** — it restores afterwards, and a test that leaves
	# the process somewhere else takes the rest of the suite with it.
	monkeypatch.chdir(written)
	run("use", "--here")

	marker = (written / ".subroutine").read_text(encoding="utf-8")

	# The whole point: one connection here, and it is still named. Written with the `two`
	# fixture this passed without testing anything the title claims.
	assert "connection =" in marker


def test_db_migrate_refuses_a_database_that_is_not_there (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#394`. It created one, migrated it to head, and reported a schema as though all was well.

	The empty instance then sat at the default path and shadowed the real one for every command
	run without the right configuration — on the machine this was found on it absorbed four
	backups, each reporting a plausible size and a correct schema.

	**§12.4's blunt-tool licence does not stretch to this.** `db migrate` is deliberately
	without confirmation, a backup or a version report, so that recovery works when everything
	else refuses. That is about skipping ceremony; there is nothing to recover from a database
	that does not exist. The three commands beside it — `db upgrade`, `db backup` and
	`db current` — all refused properly, which is what made this one's silence read as house
	style rather than as a gap.

	It was `db upgrade` when `#394` was filed; `#509` renamed it, and the name it gave up now
	belongs to the safe procedure — so this docstring names the blunt one and nothing else.
	"""

	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["db", "migrate"])

	assert result.exit_code != 0, result.output
	assert "There is no database" in result.output
	assert result.exception is None or isinstance(result.exception, SystemExit), (
		f"a refusal, not a crash: {result.exception!r}"
	)

	# The whole point: nothing was created on the way to refusing.
	assert not list((tmp_path / "data").rglob("*.db")), (
		"db migrate built a database while declining to migrate one"
	)


def test_db_migrate_still_migrates_a_database_that_exists_unstamped (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The property `#394`'s fix must not take away, and the reason it checks the *file*.

	A restored dump or an old instance has no `alembic_version` row, and migrating it is
	exactly what §12.4 keeps this command blunt for. The distinction is whether the database is
	there, never whether it has been stamped.
	"""

	data = tmp_path / "data" / "subroutine"
	data.mkdir(parents=True)
	sqlite3.connect(data / "subroutine.db").close()

	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["db", "migrate"])

	assert result.exit_code == 0, result.output
	assert "Schema is at" in result.output


def test_whoami_says_which_versions_are_in_play (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Item ``#381``: the numbers that decide whether a feature exists here yet.

	**A footer rather than a warning**, and that is the point. Somebody debugging a tool that
	behaves oddly has no reason to suspect a version at all until they can see one, so this
	line has to be part of the ordinary answer rather than something that appears when the
	program has already worked out that it is in trouble.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	answer = run("whoami").output

	assert f"Program {subroutine.__version__}" in answer
	assert f"instance {subroutine.__version__}" in answer
	assert "schema " in answer, "the migration this database is actually at"

	# One process reaching its own database cannot disagree with itself, so the advice half
	# stays silent — the rule every listing here keeps about what is true of every row.
	assert "disagree" not in answer


def test_whoami_reports_the_plugin_that_started_it (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	"""A session launched by a plugin has a third version, and it is the one nothing reported.

	The failure this closes was met twice on 2026-08-03 (`#380`, `#393`): an editor's cached
	copy of the plugin predating the feature it had been installed for, reporting success on
	install and changing nothing a session could see.

	**The fixture used to be ``9.9.9``, which is the opposite of what the docstring says**
	(`#417`). A plugin *ahead* of the program is the state decision `#396` requires, so this
	asserted the warning in the one case that is not a fault — and passed, because the old
	clause fired on any difference at all. Now it is behind, which is what `#380` and `#393`
	actually were.

	``program`` is pinned because a checkout reports a development version, which
	:func:`subroutine.installations.ordered` declines to compare — so on this machine the
	clause could never fire and the assertion would be untestable rather than merely wrong.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	monkeypatch.setattr(subroutine.installations, "program", lambda: "1.0.0")

	manifest = tmp_path / ".claude-plugin" / "plugin.json"
	manifest.parent.mkdir(parents=True)
	manifest.write_text(json.dumps({"version": "0.9.0"}), encoding="utf-8")
	monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

	answer = run("whoami").output

	assert "Plugin 0.9.0" in answer
	assert "The plugin is older than the program" in answer


def test_whoami_says_nothing_about_a_plugin_that_is_merely_newer (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
) -> None:
	"""Item ``#417``, end to end on the surface a person reads.

	The manifest's version is a cache key and has to move on any change under ``plugins/``, so
	it leads between releases by design. Warning about it put a line on every ``whoami`` in the
	healthy state — and the skill tells an agent to act on that line.

	The versions are still all printed; what is withheld is a claim that something is wrong.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	monkeypatch.setattr(subroutine.installations, "program", lambda: "1.0.0")

	manifest = tmp_path / ".claude-plugin" / "plugin.json"
	manifest.parent.mkdir(parents=True)
	manifest.write_text(json.dumps({"version": "1.1.0"}), encoding="utf-8")
	monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

	answer = run("whoami").output

	assert "Plugin 1.1.0, program 1.0.0" in answer
	assert "older than" not in answer
	assert "disagree" not in answer


def test_whoami_json_carries_the_versions_of_the_process_that_asked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``--json`` is what an agent reads, and it needs both sides of the comparison.

	``instance_version`` arrives inside the response; the program's and the plugin's are
	properties of the process that made the call and exist nowhere in it. A reader handed one
	side of a comparison has been handed nothing.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	answered = json.loads(run("whoami", "--json").output)[0]

	assert answered["program_version"] == subroutine.__version__
	assert answered["plugin_version"] is None, "no plugin started a command line"
	assert answered["instance_version"] == subroutine.__version__
	assert answered["schema_revision"]


def test_a_worker_profile_bounds_an_agent_to_one_project (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#372``. The credential is described by presenting it, not by what was asked for.

	`agent create`'s closing line is read back from the instance, so this asserts what the
	instance decided rather than what the command intended — the two differ exactly where the
	interesting failures are.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")

	made = run("agent", "create", "claude", "--profile", "worker", "--project", "web").output

	assert "only within web" in made
	assert "writing only in" not in made, "a worker writes everywhere it reaches"


def test_a_collaborator_reads_a_tree_and_writes_one_part_of_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Decision ``#370``'s whole purpose, and `#403` is why the second half is asserted.

	The reach was reported and the write set was not, so a collaborator read back exactly like
	a worker with two projects — the distinction the credential exists for, invisible on the
	one line somebody reads immediately after minting it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")
	run("project", "create", "api", "Public API")

	made = run(
		"agent", "create", "sam",
		"--profile", "collaborator",
		"--project", "web", "--project", "api",
		"--write", "api",
	).output

	assert "only within web, api" in made
	assert "writing only in api" in made


def test_an_observer_can_read_and_is_refused_a_write (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The profile is checked by *using* the credential, which is the only version worth having.

	A test asserting that `observer` sets four scopes would pass on a profile whose scopes
	named verbs nothing checks. This presents the credential and asks the instance.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something already here")

	issued = run("token", "create", "--service-account", "watcher", "--profile", "observer")
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))
	monkeypatch.setenv("SUBROUTINE_TOKEN_LOCAL", secret)

	assert "Something already here" in run("list").output

	refused = run("add", "This must not be filed", expect=1).output

	assert "task:write" in refused
	assert "scoped to a narrower set" in refused


def test_a_profile_that_means_two_things_is_refused_rather_than_resolved (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``--profile observer --write WEB`` is not a narrower observer — item ``#372``.

	**The feature is this refusal.** Either half is a reasonable thing to want and the
	combination is not one intention, so a program that quietly picked one would hand somebody
	a credential that does not do what they just said and tell them it had worked.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "web", "Website")

	refused = run(
		"agent", "create", "nosy", "--profile", "observer", "--write", "web", expect=1
	).output

	assert "does not go with the 'observer' profile" in refused
	assert "collaborator" in refused, "and it names the profile that does mean this"
	assert "sr_" not in refused, "nothing was minted"


def test_an_unknown_profile_lists_the_ones_that_exist (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Four items is a short enough list to print rather than to send somebody looking for."""

	run("init", "--username", "si", "--workspace", "Personal")

	refused = run("agent", "create", "x", "--profile", "supervisor", expect=1).output

	for profile in subroutine.domain.profiles.CATALOGUE:
		assert profile.key in refused


def test_whoami_names_a_credentials_write_set (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Item ``#403``. A credential narrowed only this way used to say "Narrowed to ."

	`narrows` counts the write scope, so the sentence was printed; `narrowing()` had no clause
	to put in it. A sentence asserting a boundary and naming none is worse than no sentence at
	all — it tells the reader there is something to know and refuses to say what.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("project", "create", "api", "Public API")

	issued = run(
		"token", "create", "--service-account", "narrow", "--title", "probe", "--write", "api"
	)
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))
	monkeypatch.setenv("SUBROUTINE_TOKEN_LOCAL", secret)

	answer = run("whoami").output

	assert "Narrowed to writing in api." in answer


def test_upgrade_check_asks_nothing_of_the_database (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Item ``#321``. ``--check`` reports and touches nothing, including on a bare machine.

	Asserted on a machine with **no instance at all**, because that is where somebody deciding
	whether to install stands. The ordinary path refuses there — ``upgrade`` needs a database
	to migrate — and a check that inherited that refusal would be unreachable exactly when it
	is most useful.

	The fetch is replaced, because a test that reached the network would be measuring GitHub.
	"""

	def answer (url: str = "", **_kwargs: typing.Any) -> list[subroutine.releases.Release]:
		"""Stand in for the published record."""

		return [subroutine.releases.Release(version="9.9.9", schema="abc", date="2026-09-01")]

	monkeypatch.setattr(subroutine.releases, "published", answer)

	said = run("db", "upgrade", "--check").output

	assert "9.9.9" in said
	assert subroutine.__version__ in said


def test_upgrade_check_says_when_it_could_not_ask (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A machine with no route out is the ordinary case for a server, not an exception.

	It has to read as "the check could not be made" rather than as "you are up to date" — the
	second is the answer that would let somebody skip a migration they needed.
	"""

	def refuse (url: str = "", **_kwargs: typing.Any) -> list[subroutine.releases.Release]:
		"""Fail the way an unreachable host does."""

		raise subroutine.errors.ServiceUnavailable(
			"Could not read the list of releases.", hint="Check the machine's network."
		)

	monkeypatch.setattr(subroutine.releases, "published", refuse)

	refused = run("db", "upgrade", "--check", expect=1).output

	assert "Could not read the list of releases" in refused
	assert "up to date" not in refused


def test_upgrade_without_check_reaches_no_network (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""§12.4a, asserted rather than promised: nothing phones home uninvited.

	The rule is that it must be possible to run an instance that never makes an outbound
	request, and the way this would break is somebody adding a courtesy check to the ordinary
	path. So the fetch is replaced with something that fails the test if it is called at all.
	"""

	def forbidden (url: str = "", **_kwargs: typing.Any) -> list[subroutine.releases.Release]:
		"""Fail loudly rather than answering."""

		raise AssertionError("upgrade asked the network without being told to")

	monkeypatch.setattr(subroutine.releases, "published", forbidden)

	run("init", "--username", "si", "--workspace", "Personal")

	assert "Nothing to do" in run("db", "upgrade").output


def test_a_database_command_on_a_connection_only_machine_is_not_told_to_run_init (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#328`: the advice `#264` and `#267` exist to prevent, arriving by a different door.

	A machine whose work is on a served instance holds no database of its own — the ordinary
	arrangement, and the one `docs/connecting.md` now documents. Told to run `init`, somebody
	follows the advice and ends up with a second, empty instance beside a connection that
	works, and nothing reports that they have two.

	**The message already knew how to tell two faults apart and consulted only one fact.** It
	asked where `database_url` came from and never asked whether any connection was configured,
	though the roster sits one module away.
	"""

	declare(home, '\ndefault_connection = "work"\n\n[connections.work]\nurl = "http://127.0.0.1:1"\n')

	refused = run("db", "backup", expect=1).output

	assert "no instance of its own" in refused
	assert "it reaches work" in refused, "name the instance this machine does have"
	assert "run it where that instance lives" in refused

	assert "set an instance up here" not in refused, "the advice `#328` is about"
	assert "would set up a second, empty one here" in refused, (
		"say why the obvious remedy is wrong, rather than only withholding it"
	)


def test_a_machine_with_no_connections_is_still_told_to_run_init (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The case `#328` must not take away: nothing configured at all, so `init` is right.

	This is the half that makes the other half safe. A fix that stopped recommending `init`
	everywhere would leave somebody setting up their first instance with no way in — and it
	would look like an improvement, because the message it replaced was the wrong one *in the
	other situation*.
	"""

	refused = run("db", "backup", expect=1).output

	assert "Run 'subroutine init' to set an instance up here" in refused
	assert "no instance of its own" not in refused


def test_naming_a_database_that_is_absent_is_unchanged_by_having_a_connection (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#328` widens the default branch only, and this is what says so.

	Writing `database_url` states an intention to keep an instance here, so a connection beside
	it says nothing about that fault: the database somebody named is missing, and that is what
	they need to hear. Widening both branches would have been the easy over-reach.
	"""

	declare(
		home,
		'\ndatabase_url = "sqlite:////nonexistent/chosen.db"\n'
		'\n[connections.work]\nurl = "http://127.0.0.1:1"\n',
	)

	refused = run("db", "backup", expect=1).output

	assert "Run 'subroutine init' first." in refused
	assert "no instance of its own" not in refused


def test_a_configuration_the_roster_cannot_read_still_refuses_rather_than_crashing (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""A refusal is the wrong place to raise from, so the new sentence fails soft.

	Reading the connections is new work done *inside* an error path. If the roster refuses —
	a malformed `[connections.x]` table is an ordinary mistake — the person gets the advice
	they got before this existed, rather than a traceback in place of a clear message.
	"""

	declare(home, '\n[connections.work]\nurl = 17\n')

	result = run("db", "backup", expect=1)

	# **Named directly rather than inferred from the output.** `CliRunner` captures what a
	# command raised instead of letting Typer render it, so a crash leaves `output` empty —
	# which every content assertion below would also catch, but as "the sentence is missing"
	# rather than as "it blew up". Falsified both ways by removing the guard.
	assert result.exception is None or isinstance(result.exception, SystemExit), (
		f"a refusal, not a crash: {result.exception!r}"
	)

	assert "There is no database at" in result.output
	assert "Run 'subroutine init' to set an instance up here" in result.output


def test_a_listing_is_not_refused_for_a_duplicate_it_reports_separately (
	two: Remote, home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#327`: the guard against double-counting refused reads that count nothing.

	`list` groups by connection with a heading each, so one instance configured under two
	names is *shown* twice — which is what the file says — rather than counted twice. Refusing
	it is what made `#288`'s migration verify after cutover instead of before: the one time two
	connections legitimately name one instance is while you are copying between them.
	"""

	declare(home, f'\n[connections.acme]\nurl = "{two.url}"\n')
	subroutine.credentials.store("acme", two.token)

	listed = run("list").output

	assert "work" in listed and "acme" in listed, "both headings, so the two can be compared"
	assert "same instance" not in listed


def test_whoami_answers_on_a_machine_whose_connections_collide (
	two: Remote, home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The sharpest case, because of which command it is.

	`whoami` prints a line per connection and combines nothing, so it cannot double-count. It
	is also the command somebody runs to work out what their machine is talking to — so
	refusing it answered "your configuration is ambiguous" by way of the one question that
	would have shown them the ambiguity.
	"""

	declare(home, f'\n[connections.acme]\nurl = "{two.url}"\n')
	subroutine.credentials.store("acme", two.token)

	said = run("whoami").output

	assert "work" in said and "acme" in said
	assert "same instance" not in said


def test_a_merged_agenda_still_refuses_when_two_connections_are_one_instance (
	two: Remote, home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The half `#327` must not take away, stated beside the halves it changes.

	`today` merges across connections into one set of buckets by design (§13.7), so a
	duplicate genuinely is counted twice there. Keeping this is also what keeps `#337`'s
	conclusion true: identity belongs in the environment rather than in a connection, because
	one connection per identity would leave the operator's own agenda refusing.
	"""

	declare(home, f'\n[connections.acme]\nurl = "{two.url}"\n')
	subroutine.credentials.store("acme", two.token)

	refused = run("today", expect=1).output

	assert "same instance" in refused
	assert "work" in refused and "acme" in refused


def test_a_listing_can_be_narrowed_to_one_connection (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#272`. "Show me only what is on the work instance" had no spelling at all.

	`-c` moves the *write* context and deliberately does not narrow a read (§13.7), because
	forgetting which context you are in must never cost you a missed item. That is right, and
	it left the other question — asked while checking one server, or driving one instance
	during a migration — answerable only by disabling the others in `config.toml` and putting
	them back afterwards.

	A filter, spelled after the command like `--project`, changing nothing durable.
	"""

	both = run("list").output

	assert "Fix the deploy script" in both and "Pay the gas bill" in both

	narrowed = run("list", "--connection", "work").output

	assert "Fix the deploy script" in narrowed
	assert "Pay the gas bill" not in narrowed, "the local connection's task is filtered out"

	# Nothing durable changed: the next bare listing is the whole thing again.
	assert "Pay the gas bill" in run("list").output


def test_narrowing_to_one_connection_does_not_shorten_the_addresses (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#280`'s rule, met by a filter rather than by a context flag.

	An address is printed as the shortest form that *resolves*, and with one connection in
	view the shortest form drops its name. But the flag is gone by the command somebody types
	next, so a row printed bare here would be an invitation to act on the wrong instance —
	which is the entire hazard qualifying an address exists to remove.

	Found by running it rather than by reasoning: the first version of this narrowed the
	reached set, `qualifies_connection` counts that set, and the addresses quietly shortened.
	"""

	narrowed = [
		line for line in run("list", "--connection", "work").output.splitlines()
		if "Fix the deploy script" in line
	]

	assert narrowed, "the row is there to be addressed"
	assert "work/" in narrowed[0], (
		f"the address must still name the connection: {narrowed[0]!r}"
	)


def test_narrowing_to_a_connection_that_is_not_configured_names_the_ones_that_are (
	two: Remote, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""A filter naming nothing is a typo, and the remedy is the list of real names."""

	refused = run("list", "--connection", "wrok", expect=1).output

	assert "wrok" in refused
	assert "work" in refused and "local" in refused


def test_a_marker_finds_its_workspace_on_a_connection_called_something_else_here (
	two: Remote,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#556`, building `#330`'s decision. A connection name is each machine's private alias.

	Two machines mounting one filesystem read one `.subroutine`, so the nickname has to agree —
	and when it does not, the *whole* marker stopped directing rather than just the connection:
	`context.resolve` drops the workspace with it, because a slug means nothing on an instance
	that has never heard of it. The id does, and has been in every marker since `#317`.

	Written by the program and then edited, rather than hand-built, so the shape under test is
	the one `use --here` actually produces.
	"""

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	monkeypatch.chdir(checkout)

	run("-c", "work", "use", "--here")

	written = (checkout / subroutine.directory.FILE_NAME).read_text(encoding="utf-8")

	assert "workspace_id" in written, "the marker carries the durable half"

	(checkout / subroutine.directory.FILE_NAME).write_text(
		written.replace('connection = "work"', 'connection = "their-name-for-it"'),
		encoding="utf-8",
	)

	added = run("add", "Filed by id")

	assert "their-name-for-it" in added.output, "say the file names something we do not have"
	assert "its workspace is on 'work'" in added.output, "and say where it was found"
	assert "Using 'local' instead" not in added.output, "which is what used to happen"

	# The whole point: it landed on the instance the marker meant.
	assert "Filed by id" in run("list", "--connection", "work").output
	assert "Filed by id" not in run("list", "--connection", "local").output


def test_a_marker_whose_workspace_is_nowhere_still_does_not_file_by_project_key (
	two: Remote,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#414` must survive `#556`, and this is what says the widening is narrow.

	`#414` found a marker whose connection was dropped matching its project by **key** on
	whichever instance answered, and filing work into a same-named project somewhere else. The
	fix was `Marker.speaks_for`, which compares the name the marker wrote.

	`#556` widens that gate — but only on a `workspace_id` match, which is a claim a key cannot
	make. So a marker whose id is held by nothing must be refused exactly as before, *even
	though* a project of its key exists right here. Falsified by widening the gate to any
	dropped marker: the task is then filed in `SR`.
	"""

	run("project", "create", "sr", "Subroutine")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	monkeypatch.chdir(checkout)

	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "gone"\n'
		'workspace_id = "019f0000-0000-7000-8000-000000000000"\n'
		'project = "sr"\n',
		encoding="utf-8",
	)

	added = run("add", "Filed where this connection says")

	assert "in sr" not in added.output, "no instance holds that workspace, so nothing is honoured"
	assert "Using 'local' instead" in added.output


def test_a_marker_whose_workspace_is_on_two_connections_falls_back_rather_than_guessing (
	two: Remote,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#327` made two connections to one instance readable, so this is a state somebody has.

	Both hold the id, so the question has two answers and the point of resolving by id is that
	it has one. Falling back is today's behaviour and says so out loud, which is the whole of
	what `#330` decided here: it never guesses.
	"""

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	monkeypatch.chdir(checkout)

	# The marker is written before the second alias exists, because `use` is a write and a
	# duplicate instance is refused there — `#327` left that check on the merged path.
	run("-c", "work", "use", "--here")

	written = (checkout / subroutine.directory.FILE_NAME).read_text(encoding="utf-8")

	(checkout / subroutine.directory.FILE_NAME).write_text(
		written.replace('connection = "work"', 'connection = "their-name-for-it"'),
		encoding="utf-8",
	)

	declare(home, f'\n[connections.acme]\nurl = "{two.url}"\n')
	subroutine.credentials.store("acme", two.token)

	said = run("list").output

	assert "Using" in said and "instead" in said, "today's behaviour, unchanged"
	assert "its workspace is on" not in said, "two answers is not an answer"


def test_whoami_lists_permissions_for_a_role_that_does_not_hold_everything (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#717` — an unnarrowed credential learned *less* about itself than a narrowed one.

	The list used to appear only when the credential was narrowed, on the reasoning that an
	unnarrowed owner would be handed twenty keys it already holds. True, and about the wrong
	case: an owner holding everything is where a list says nothing, and a contributor holding
	six of seventeen is where it says the most. So a plain contributor credential got the word
	*Contributor* and no statement of what that meant.

	Driven with a service account holding a role and **no scopes, no project scope and no
	workspace pin** — the shape that was silent.
	"""

	run("init")
	run("user", "create", "colleague")
	run("user", "add", "colleague", "--workspace", "personal", "--role", "member")

	# **No `--workspace` and no `--scope`**, which is the whole point: those pin and narrow the
	# credential, and a narrowed one was already being served. This is the shape that was not.
	issued = run("token", "create", "--username", "colleague", "--title", "Probe")
	secret = next(word for word in issued.output.split() if word.startswith("sr_"))

	monkeypatch.setenv("SUBROUTINE_TOKEN", secret)

	answer = run("whoami").output

	assert "Narrowed to" not in answer, "the fixture narrowed the credential, so it proves nothing"
	assert "may: " in answer, "a contributor was told its role and not what the role may do"
	assert "task:read" in answer


def test_a_token_crossing_the_network_in_the_clear_is_said_on_every_surface (
	two: Remote, home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`serve` refuses to *be* the other end of this and the client said nothing.

	An instance declines to listen beyond its own machine without TLS, in as many words —
	"bearer tokens sent over plain HTTP are compromised tokens". A client pointed at exactly
	that address stored the token, sent it on every request and mentioned it nowhere:
	not when the connection was added, not in the listing, not in `doctor`. The README states
	the rule twice.

	Driven on all three, because the finding is that three surfaces were silent rather than
	that one was. Said rather than refused: the server is somebody else's and the reader may
	have no say over it, so what helps is knowing.
	"""

	assert two.url.startswith("http://127.0.0.1"), "the fixture serves over loopback"

	# Loopback is not in the clear — nothing leaves the machine — so the ordinary fixture must
	# say nothing at all, or the warning is one nobody would read twice.
	assert "plain http" not in run("connections").output
	assert "plain http" not in run("doctor").output

	declare(home, '\n[connections.remote]\nurl = "http://tasks.example.com"\n')
	subroutine.credentials.store("remote", "sr_not_a_real_token")

	listed = run("connections").output

	assert "plain http" in listed
	assert "remote" in listed

	# `doctor` exits 1 when something wants acting on, which this does.
	assert "plain http" in run("doctor", expect=1).output


def test_adding_a_connection_over_plain_http_says_so (
	tmp_path: pathlib.Path,
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The moment somebody is choosing, which is the one moment they can ask for an https one."""

	run("init", "--workspace", "Personal")

	with served(tmp_path) as remote:
		added = run(
			"connections",
			"add",
			"work",
			"--url",
			remote.url.replace("127.0.0.1", "localhost"),
			input=f"{remote.token}\n",
		)

		assert "plain http" not in added.output, "localhost is loopback and leaves the machine"


def test_a_credentials_file_anybody_can_read_is_said_by_an_ordinary_command (
	two: Remote, home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""The warning existed, promised every command that reads a token, and reached one.

	`credentials.permission_warning`'s own docstring said it was reported by ``subroutine
	connections`` *and by any command that actually reads a token from the file*. There was
	one caller, and §1.4 hides that command from ``--help`` until a second connection exists —
	so the person most likely to have a loose file, and least likely to go looking for it, was
	told by nothing they would ever run.

	Driven through ``list``, which is what somebody actually types.
	"""

	where = subroutine.credentials.credentials_file_path()

	assert where.is_file(), "the fixture stored a token, so there is a file to loosen"

	where.chmod(0o644)

	listed = run("list")

	assert str(where) in listed.output, "the file is named, because the remedy is on it"
	assert "chmod" in listed.output, "and the command that fixes it"

	where.chmod(0o600)

	assert str(where) not in run("list").output, "and nothing is said when it is not loose"


def test_a_timezone_this_machine_cannot_use_is_refused_by_add_as_it_is_by_today (
	home: pathlib.Path,
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""``today`` resolved the day here and ``add`` let each instance decide.

	``today``'s own comment states the rule in as many words — *"resolved once, here, in this
	machine's zone, because each instance would otherwise apply its own notion of the caller's
	timezone, and a person whose work profile says America/New_York and whose personal one
	says Europe/London would get two different days merged into one list"*. ``add`` passed no
	timezone at all, so the argument held for reading and not for writing.

	**Asserted through the misconfiguration rather than through a date**, deliberately: a test
	comparing days depends on what time it is run, which this suite has been bitten by, and a
	setting neither command can use is true at every hour. Before this, ``today`` refused it
	and ``add`` filed happily — which is the same divergence stated in a way a clock cannot
	move.
	"""

	run("init", "--workspace", "Personal")
	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Nowhere/Atall")

	refused = run("today", expect=1)

	assert "Nowhere/Atall" in refused.output, "today has always refused it"

	filed = run("add", "Pay the rent by friday", expect=1)

	assert "Nowhere/Atall" in filed.output, (
		"add read the zone from nowhere, so a setting today refuses was invisible to it"
	)
