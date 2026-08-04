"""Whether this machine's installation is coherent — item ``#407``.

**The property under test is that it survives what it is examining.** A diagnostic that raises
on a broken machine is worse than none, because it is reached precisely when something is
already wrong — so most of this file is about the failures: a connection that will not open, a
credential that is refused, a configuration that will not parse, a backup directory that is not
there. Each has to become a line, and the lines after it have to still be printed.

The other half is that it reports *what is*, not what was configured. `#407` exists because
four separate confusions in one day came from a command acting on a different database from
the one the operator meant, and every one of them looked like success.
"""

import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.diagnosis
import subroutine.errors
import subroutine.installations


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG root at a fresh directory, the way a real installation is isolated."""

	root = tmp_path / "home"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	return root


def _areas (findings: typing.Sequence[subroutine.diagnosis.Finding]) -> list[str]:
	"""Return what was looked at, in the order somebody reads it."""

	return [finding.area for finding in findings]


def _named (
	findings: typing.Sequence[subroutine.diagnosis.Finding], area: str
) -> subroutine.diagnosis.Finding:
	"""Return one finding by its label, failing loudly when it is not there."""

	for finding in findings:
		if finding.area == area:
			return finding

	raise AssertionError(f"nothing reported {area!r}; got {_areas(findings)}")


class TestWhatItLooksAt:
	"""The shape of the report, on a machine where nothing is wrong."""

	def test_it_says_what_is_running_and_where_from (self, home: pathlib.Path) -> None:
		"""Two halves of one confusion: two installs on a machine report two numbers.

		The version says which build; the path says which install just answered. `#381` was
		found because an editable install reports the version it was made at, so the number
		alone cannot tell somebody which copy they are talking to.
		"""

		found = subroutine.diagnosis.examine(subroutine.config.Settings())
		program = _named(found, "program")

		assert subroutine.__version__ in program.detail
		assert " at " in program.detail

	def test_it_says_which_configuration_is_in_force (self, home: pathlib.Path) -> None:
		"""**The line the whole command exists for.**

		A command run without the service's environment acts on a different database and does
		not look like it — `#376` was a server against a database nobody meant, `#395` a backup
		of an empty one, and both reported success. Printing the three roots is what makes
		every line below them mean something.
		"""

		found = subroutine.diagnosis.examine(subroutine.config.Settings())

		for area in ("config", "data", "state"):
			assert str(home) in _named(found, area).detail

	def test_it_reports_no_plugin_by_saying_nothing (self, home: pathlib.Path) -> None:
		"""A command line is not a plugin's child process, and that is the ordinary case.

		Saying "no plugin" every time would put a line about a concept most readers have not
		got at the top of the one command they run when something is already wrong.
		"""

		found = subroutine.diagnosis.examine(subroutine.config.Settings())

		assert "plugin" not in _areas(found)

	def test_it_reports_the_plugin_that_started_it (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""And says so when it can, which is a session launched by one."""

		manifest = tmp_path / "cached" / ".claude-plugin"
		manifest.mkdir(parents=True)
		(manifest / "plugin.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
		monkeypatch.setenv(
			subroutine.installations.PLUGIN_ROOT, str(tmp_path / "cached")
		)

		found = subroutine.diagnosis.examine(subroutine.config.Settings())

		assert _named(found, "plugin").detail == "9.9.9"


class TestWhenThingsAreBroken:
	"""The half that decides the design: it has to survive what it is diagnosing."""

	def test_a_connection_that_will_not_open_becomes_a_line (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""Not a traceback, and not the end of the report.

		This is the whole rule. A connection is a socket, a credential and somebody else's
		uptime, and the one command somebody runs when things are wrong must not be the thing
		that breaks — so *every* exception becomes a finding, not only the ones this codebase
		knows how to translate.
		"""

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Fail the way an unreachable host does, with somebody else's exception."""

			raise RuntimeError("connection refused")

		monkeypatch.setattr(subroutine.clients.opening, "for_connection", refuse)

		found = subroutine.diagnosis.examine(subroutine.config.Settings())
		local = _named(found, "local")

		assert not local.ok
		assert "RuntimeError" in local.detail, "an untranslated failure is named by its type"

		# And the report goes on. A diagnostic that stops at the first problem is how
		# somebody fixes one thing and meets the next tomorrow.
		assert "backups" in _areas(found)

	def test_a_refusal_this_program_wrote_is_shown_as_written (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""One that has already been worded for a person is not decorated with its type."""

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Fail the way this program does."""

			raise subroutine.errors.NotFound("There is no instance here yet.")

		monkeypatch.setattr(subroutine.clients.opening, "for_connection", refuse)

		local = _named(subroutine.diagnosis.examine(subroutine.config.Settings()), "local")

		assert local.detail == "There is no instance here yet."
		assert "NotFound" not in local.detail

	def test_a_configuration_that_will_not_parse_becomes_a_line (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""The roster is read from a file somebody edits, so it is a thing that fails."""

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Fail the way an unparseable connections table does."""

			raise subroutine.errors.ValidationError("connections.work has no url.")

		monkeypatch.setattr(subroutine.connections, "roster", refuse)

		found = subroutine.diagnosis.examine(subroutine.config.Settings())

		assert not _named(found, "connections").ok
		assert "backups" in _areas(found), "and the rest is still reported"

	def test_a_connection_switched_off_is_simply_not_listed (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""`[connections.local] enabled = false` is a decision, and it is not a fault.

		Pinned because the first version of this file had a branch reporting such a connection
		as "disabled in config.toml" — and that branch could never run, because
		``connections.roster`` returns only the live ones. A check that cannot fire is the
		shape this codebase spends most of its time finding, so the real behaviour is asserted
		instead: it is absent, exactly as ``subroutine connections`` shows it.

		It matters because this machine's own configuration does exactly that, and a health
		command that went red on a deliberate setting would be switched off within a week.
		"""

		where = home / "xdg_config_home" / "subroutine"
		where.mkdir(parents=True, exist_ok=True)
		(where / "config.toml").write_text(
			'[connections.local]\nenabled = false\n\n'
			'[connections.work]\nurl = "https://example.invalid"\n',
			encoding="utf-8",
		)

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Stand in for the unreachable instance, so this is about the roster."""

			raise RuntimeError("connection refused")

		monkeypatch.setattr(subroutine.clients.opening, "for_connection", refuse)

		found = subroutine.diagnosis.examine(subroutine.config.load_settings())

		assert "local" not in _areas(found)
		assert "work" in _areas(found)

	def test_a_backup_directory_that_is_not_there_is_not_a_failure (
		self, home: pathlib.Path, tmp_path: pathlib.Path
	) -> None:
		"""Nobody has taken one yet, which is a state every new installation is in."""

		settings = subroutine.config.Settings(backup_directory=str(tmp_path / "nowhere"))
		backups = _named(subroutine.diagnosis.examine(settings), "backups")

		assert backups.ok
		assert backups.unknown


class TestTheVerdict:
	"""What the closing line says, and what the exit code follows."""

	def test_nothing_wrong_says_so (self) -> None:
		"""And an unknown is not wrong. A command line has no plugin and is not unhealthy."""

		findings = [
			subroutine.diagnosis.Finding(area="program", detail="1.0.0"),
			subroutine.diagnosis.Finding(area="local", detail="disabled", unknown=True),
		]

		assert subroutine.diagnosis.verdict(findings) == "Nothing here needs attention."

	def test_a_failure_is_counted_against_the_whole (self) -> None:
		"""Counted rather than listed: the lines above already say which."""

		findings = [
			subroutine.diagnosis.Finding(area="program", detail="1.0.0"),
			subroutine.diagnosis.Finding(area="work", detail="refused", ok=False),
		]

		assert subroutine.diagnosis.verdict(findings) == "1 of 2 need attention."


class TestTheCommand:
	"""Driving it, because the exit code is half of what it is for."""

	def test_it_exits_zero_on_a_healthy_machine (self, home: pathlib.Path) -> None:
		"""So it can be the last line of an update script."""

		runner = typer.testing.CliRunner()
		runner.invoke(subroutine.cli.main.app, ["init"])
		done = runner.invoke(subroutine.cli.main.app, ["doctor"])

		assert done.exit_code == 0, done.output
		assert "Nothing here needs attention." in done.output

	def test_it_exits_non_zero_when_something_needs_attention (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""**The half that makes it usable unattended.**

		A report a person reads and forgets is a runbook with extra steps. The exit code is
		what lets it end a script, and it is the thing a wrapper would silently get wrong.
		"""

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Fail the way an unreachable host does."""

			raise RuntimeError("connection refused")

		monkeypatch.setattr(subroutine.clients.opening, "for_connection", refuse)

		runner = typer.testing.CliRunner()
		done = runner.invoke(subroutine.cli.main.app, ["doctor"])

		assert done.exit_code == 1, done.output
		assert "needs attention" in done.output

	def test_the_failing_line_says_so_in_words_as_well_as_colour (
		self, home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""Decision ``#102``: no information exists only in a colour.

		Piped into a log, or read by somebody who cannot distinguish red, the line has to
		carry the same answer — and this output is *meant* to be piped, since it is the last
		line of an update script.
		"""

		def refuse (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
			"""Fail the way an unreachable host does."""

			raise RuntimeError("connection refused")

		monkeypatch.setattr(subroutine.clients.opening, "for_connection", refuse)

		runner = typer.testing.CliRunner()
		done = runner.invoke(subroutine.cli.main.app, ["doctor"])

		assert "— needs attention" in done.output, "the word, not only the colour"
