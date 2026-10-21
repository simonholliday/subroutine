"""init says so when it named the first account after a service - `SR#5171`.

Following L-8 of the cold review of 2026-10-17, decided in `#5097`: run without ``--username`` by
the service's account, as the hosting guide ran it, ``init`` named the first account - the
instance's administrator - after the service, which nobody signs in as, and said nothing. **The
system's own account record is what is read**, stood in for here, so each signal is driven
against a person whose login matches it in every other way.
"""

import pathlib
import pwd
import re
import subprocess
import sys
import typing

import pytest
import typer.testing

import subroutine.cli.main
import test_personal_path

#: A fresh home, bound here so pytest finds it as a fixture.
home = test_personal_path.home

#: The check itself, taken before `conftest.py` stands a person in for it in every test.
LOOKS_LIKE_A_SERVICE = subroutine.cli.main._looks_like_a_service

#: What it says, as approved, without the line breaks the terminal's width puts in.
SAID = (
	"The first account, which administers this instance, is named subroutine, after the system "
	"account running this, which looks like a service rather than a person. To administer it as "
	"yourself: subroutine user create <you> --superuser, then subroutine user add <you> --role "
	"admin"
)


def _initialised (
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: pathlib.Path,
	*,
	uid: int,
	shell: str,
	defs: str | None = "UID_MIN\t\t\t 1000\n",
	arguments: typing.Sequence[str] = (),
) -> str:
	"""Run ``init`` as a system account called ``subroutine`` with this id and shell, under this
	``/etc/login.defs`` or none, and return what it printed on one line."""

	record = pwd.struct_passwd(("subroutine", "x", uid, uid, "", "/nonexistent", shell))

	def looked_up (name: str) -> pwd.struct_passwd:
		"""Return the stood-in record for ``subroutine``, and refuse any other name."""

		if name != "subroutine":
			raise KeyError(name)

		return record

	monkeypatch.setattr(pwd, "getpwnam", looked_up)
	monkeypatch.setattr(subroutine.cli.main, "_looks_like_a_service", LOOKS_LIKE_A_SERVICE)
	monkeypatch.setenv("LOGNAME", "subroutine")
	monkeypatch.setenv("USER", "subroutine")
	defined = tmp_path / "login.defs"

	if defs is not None:
		defined.write_text(defs, encoding="utf-8")

	monkeypatch.setattr(subroutine.cli.main, "LOGIN_DEFS", defined)
	subroutine.cli.main._said_unknown_settings = False

	ran = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["init", *arguments])

	assert ran.exit_code == 0, ran.output

	return " ".join(re.sub(r"[│╭╮╰╯─]", " ", ran.output).split())


@pytest.mark.parametrize(
	("uid", "shell", "defs"),
	[
		pytest.param(998, "/usr/sbin/nologin", "UID_MIN 1000\n", id="the guide's service account"),
		pytest.param(122, "/bin/bash", "UID_MIN 1000\n", id="a service with a shell"),
		# Where nothing states the least id, as on macOS: below one, root is that signal's too.
		pytest.param(0, "/bin/sh", None, id="root"),
		pytest.param(200, "/usr/bin/false", None, id="a service where nothing states the least id"),
	],
)
def test_init_says_so_when_a_service_named_the_first_account (
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	uid: int,
	shell: str,
	defs: str | None,
) -> None:
	"""Said after *Ready.*, naming the account and both steps that make the reader what it is."""

	said = _initialised(monkeypatch, tmp_path, uid=uid, shell=shell, defs=defs)

	assert said.startswith("Ready.") and SAID in said, said


@pytest.mark.parametrize(
	("uid", "shell", "defs", "arguments"),
	[
		pytest.param(1000, "/bin/bash", "UID_MIN 1000\n", (), id="a person"),
		pytest.param(501, "/bin/zsh", None, (), id="a person where nothing states the least id"),
		pytest.param(
			998,
			"/usr/sbin/nologin",
			"UID_MIN 1000\n",
			("--username", "laurence"),
			id="a service that named somebody",
		),
	],
)
def test_init_says_nothing_more_for_a_person_or_a_name_given (
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	uid: int,
	shell: str,
	defs: str | None,
	arguments: tuple[str, ...],
) -> None:
	"""A person's own login is not a service, and a name given is the reader's own choice."""

	said = _initialised(monkeypatch, tmp_path, uid=uid, shell=shell, defs=defs, arguments=arguments)

	assert said == 'Ready. Try: subroutine add "something to do"', said


def test_an_account_the_system_cannot_look_up_is_not_guessed_at (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""No record, no signal: nothing is said about an account nobody can describe."""

	def unknown (name: str) -> typing.NoReturn:
		"""Say the system has no such account."""

		raise KeyError(name)

	monkeypatch.setattr(pwd, "getpwnam", unknown)

	assert LOOKS_LIKE_A_SERVICE("subroutine") is False


def test_windows_has_no_account_record_to_ask (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Where there is no password database, nothing is asked of one and nothing is said."""

	def asked (name: str) -> typing.NoReturn:
		"""Fail, since nothing should look the account up."""

		raise AssertionError(f"looked up {name}")

	monkeypatch.setattr(pwd, "getpwnam", asked)
	monkeypatch.setattr(sys, "platform", "win32")

	assert LOOKS_LIKE_A_SERVICE("root") is False


def test_the_suite_stands_a_person_in_for_whoever_runs_it () -> None:
	"""Every other test's ``init`` answers as a person's would, even run as root, as the suite
	may be: this system's own ``root`` is a service by the check, and not by the stand-in."""

	assert LOOKS_LIKE_A_SERVICE("root") is True
	assert subroutine.cli.main._looks_like_a_service("root") is False


def test_the_program_imports_without_a_password_database () -> None:
	"""The command line still loads where ``pwd`` does not exist, as on Windows: the check
	imports it when it runs, not when the module loads."""

	ran = subprocess.run(
		[sys.executable, "-c", "import sys; sys.modules['pwd'] = None; import subroutine.cli.main"],
		capture_output=True, text=True, check=False,
	)

	assert ran.returncode == 0, ran.stderr[-2000:]
