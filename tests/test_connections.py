"""Tests for the connection roster and for where a token comes from.

docs/design.md §13.7 and §12.3a. Nothing here opens a database or a socket: a roster is read from
two files and an environment, and every refusal in it is a refusal about text.
"""

import os
import pathlib
import stat
import typing

import pytest

import subroutine.config
import subroutine.connections
import subroutine.credentials
import subroutine.errors


@pytest.fixture
def config_home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point the configuration directory at a fresh one, with no token in the environment.

	The token variables are cleared rather than left alone. A developer with
	``SUBROUTINE_TOKEN`` exported would otherwise see resolution tests pass or fail depending
	on their shell, which is the least useful kind of flake.
	"""

	home = tmp_path / "cfg"
	home.mkdir()
	monkeypatch.setenv("XDG_CONFIG_HOME", str(home))

	for name in list(os.environ):
		if name.startswith(subroutine.credentials.DEFAULT_VARIABLE):
			monkeypatch.delenv(name, raising=False)

	return home / "subroutine"


def written (config_home: pathlib.Path, text: str) -> None:
	"""Write a configuration file."""

	config_home.mkdir(parents=True, exist_ok=True)
	(config_home / "config.toml").write_text(text, encoding="utf-8")


def roster (**overrides: typing.Any) -> subroutine.connections.Roster:
	"""Read the roster with settings resolved from whatever is on disk."""

	return subroutine.connections.roster(subroutine.config.load_settings(**overrides))


def test_local_exists_without_being_declared (config_home: pathlib.Path) -> None:
	"""Somebody who has never heard of a connection still has one."""

	found = roster()

	assert found.names == ("local",)
	assert found.default == "local"
	assert found.default_connection().is_local
	assert found.default_connection().label == "Local"

	# The whole of the personal path (docs/design.md §13.5b) is this case, and it is what keeps
	# labels out of that output entirely.
	assert not found.qualifies


def test_local_can_be_renamed_without_being_redeclared (config_home: pathlib.Path) -> None:
	"""``display_name`` changes what is printed and not what is typed."""

	written(config_home, '[connections.local]\ndisplay_name = "Personal"\n')

	found = roster()

	assert found.names == ("local",)
	assert found.require("local").label == "Personal"


def test_a_remote_connection_is_read_with_its_settings (config_home: pathlib.Path) -> None:
	"""Everything §13.7's example configuration says is read back."""

	written(
		config_home,
		"""
		default_connection = "local"

		[connections.work]
		url = "https://tasks.example.com/"
		token_command = "pass show subroutine/work"
		read_only = true
		timeout_seconds = 2.5
		""",
	)

	work = roster().require("work")

	assert work.url == "https://tasks.example.com"
	assert work.token_command == "pass show subroutine/work"
	assert work.read_only
	assert work.timeout_seconds == 2.5
	assert not work.is_local
	assert work.label == "work"


def test_the_default_connection_leads_the_roster (config_home: pathlib.Path) -> None:
	"""Output groups the connection a write would land in first."""

	written(
		config_home,
		"""
		default_connection = "work"

		[connections.work]
		url = "https://tasks.example.com"

		[connections.side]
		url = "https://side.example.com"
		""",
	)

	found = roster()

	assert found.names[0] == "work"
	assert set(found.names) == {"local", "work", "side"}
	assert found.qualifies


def test_a_connection_can_be_turned_off (config_home: pathlib.Path) -> None:
	"""``enabled = false`` removes it without deleting the settings."""

	written(
		config_home,
		"""
		[connections.work]
		url = "https://tasks.example.com"
		enabled = false
		""",
	)

	assert roster().names == ("local",)


def test_turning_local_off_falls_back_to_a_declared_connection (
	config_home: pathlib.Path,
) -> None:
	"""Somebody who disabled their own database has said where their work is."""

	written(
		config_home,
		"""
		[connections.local]
		enabled = false

		[connections.work]
		url = "https://tasks.example.com"
		""",
	)

	found = roster()

	assert found.names == ("work",)
	assert found.default == "work"


def test_turning_everything_off_is_refused (config_home: pathlib.Path) -> None:
	"""There has to be somewhere to keep things."""

	written(config_home, "[connections.local]\nenabled = false\n")

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		roster()

	assert "turned off" in raised.value.detail


def test_local_cannot_be_given_a_url (config_home: pathlib.Path) -> None:
	"""Accepting this would silently remove the person's own tasks from every listing."""

	written(config_home, '[connections.local]\nurl = "https://tasks.example.com"\n')

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		roster()

	assert "own database" in raised.value.detail
	assert raised.value.errors[0].field == "connections.local.url"


def test_a_remote_connection_needs_a_url (config_home: pathlib.Path) -> None:
	"""A table with no url names an instance nothing can reach."""

	written(config_home, '[connections.work]\nread_only = true\n')

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		roster()

	assert raised.value.errors[0].field == "connections.work.url"


@pytest.mark.parametrize(
	"url", ["tasks.example.com", "ftp://tasks.example.com", "https://", "  "]
)
def test_an_unreachable_url_is_refused_by_shape (config_home: pathlib.Path, url: str) -> None:
	"""A url with no scheme or no host is a mistake worth naming.

	Left alone, ``tasks.example.com`` parses as a *path* and the failure arrives much later
	as something about a relative URL.
	"""

	written(config_home, f'[connections.work]\nurl = "{url}"\n')

	with pytest.raises(subroutine.errors.SubroutineError):
		roster()


def test_a_mistyped_key_is_refused_rather_than_ignored (config_home: pathlib.Path) -> None:
	"""The failure mode of a dropped key is the wrong posture, silently.

	``readonly = true`` would leave an agent able to write to an employer's instance while
	the configuration says, to anybody reading it, that it cannot.
	"""

	written(
		config_home,
		'[connections.work]\nurl = "https://tasks.example.com"\nreadonly = true\n',
	)

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		roster()

	assert raised.value.errors[0].field == "connections.work.readonly"
	assert "read_only" in raised.value.errors[0].message


@pytest.mark.parametrize("name", ["42", "-work", "Work space", "work/acme"])
def test_a_name_that_could_not_be_typed_is_refused (
	config_home: pathlib.Path, name: str
) -> None:
	"""A connection name becomes the first segment of ``work/acme/42``."""

	written(config_home, f'[connections."{name}"]\nurl = "https://x.example.com"\n')

	with pytest.raises(subroutine.errors.SubroutineError):
		roster()


def test_a_default_naming_nothing_is_refused (config_home: pathlib.Path) -> None:
	"""Otherwise the next ``subroutine add`` lands somewhere nobody chose."""

	written(config_home, 'default_connection = "wrok"\n')

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		roster()

	assert raised.value.errors[0].field == "default_connection"


def test_an_unknown_connection_is_a_refusal_naming_the_known_ones (
	config_home: pathlib.Path,
) -> None:
	"""Asking for a connection that is not there says what is."""

	with pytest.raises(subroutine.errors.NotFound) as raised:
		roster().require("work")

	assert raised.value.hint is not None
	assert "local" in raised.value.hint


def test_a_bad_timeout_is_refused (config_home: pathlib.Path) -> None:
	"""A timeout of zero would mean every read fails before it starts."""

	written(
		config_home,
		'[connections.work]\nurl = "https://x.example.com"\ntimeout_seconds = 0\n',
	)

	with pytest.raises(subroutine.errors.SubroutineError):
		roster()


# --- Where the token comes from (§12.3a) ------------------------------------------------


def connection (**fields: typing.Any) -> subroutine.connections.Connection:
	"""Build one connection directly, without a file."""

	fields.setdefault("name", "work")
	fields.setdefault("url", "https://tasks.example.com")

	return subroutine.connections.Connection(**fields)


def test_the_local_connection_legitimately_has_no_token (config_home: pathlib.Path) -> None:
	"""The filesystem permission on the database is the authentication (§12.1a)."""

	resolved = subroutine.credentials.resolve(
		subroutine.connections.Connection(name="local"), default_connection="local"
	)

	assert not resolved.found
	assert resolved.source == "nowhere"


def test_a_per_connection_variable_wins (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Rule 1, and the name is the connection's with non-alphanumerics as underscores."""

	monkeypatch.setenv("SUBROUTINE_TOKEN_MY_WORK", "sr_from_environment")

	resolved = subroutine.credentials.resolve(connection(name="my-work"), default_connection="local")

	assert resolved.token == "sr_from_environment"
	assert resolved.source == "SUBROUTINE_TOKEN_MY_WORK"


def test_the_bare_variable_applies_to_the_default_connection_only (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``SUBROUTINE_TOKEN`` is for the common case of one instance and one secret.

	Applying it to *every* connection would send a personal token to an employer's server,
	which is a credential leaking across a trust boundary rather than a convenience.
	"""

	monkeypatch.setenv(subroutine.credentials.DEFAULT_VARIABLE, "sr_default")

	assert subroutine.credentials.resolve(
		connection(name="work"), default_connection="work"
	).token == "sr_default"

	assert not subroutine.credentials.resolve(
		connection(name="work"), default_connection="local"
	).found


def test_token_env_names_a_variable_explicitly (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Rule 2."""

	monkeypatch.setenv("WORK_TASKS_TOKEN", "sr_named")

	resolved = subroutine.credentials.resolve(connection(token_env="WORK_TASKS_TOKEN"), default_connection="local")

	assert resolved.token == "sr_named"
	assert "token_env" in resolved.source


def test_token_env_naming_an_unset_variable_is_reported (config_home: pathlib.Path) -> None:
	"""Falling through to the file would hide a mistake the person already declared.

	They have said where the token is. Reading a different one instead means the connection
	either works with the wrong credential or fails with a 401 that names nothing.
	"""

	with pytest.raises(subroutine.errors.Unauthenticated) as raised:
		subroutine.credentials.resolve(connection(token_env="NOT_SET_ANYWHERE"), default_connection="local")

	assert "NOT_SET_ANYWHERE" in raised.value.detail


def test_token_command_takes_the_first_line (config_home: pathlib.Path) -> None:
	"""Rule 3, and ``pass show`` prints the secret first and anything at all after it."""

	resolved = subroutine.credentials.resolve(
		connection(token_command="printf 'sr_piped\\nnotes about it\\n'"),
		default_connection="local",
	)

	assert resolved.token == "sr_piped"
	assert "token_command" in resolved.source


def test_a_failing_token_command_reports_its_own_message (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Its stderr says what to do; "the command failed" does not.

	Its *stdout* is deliberately not shown, because that is where the secret would be on a
	successful run. The secret is passed in through the environment so that it appears
	nowhere in the command line either — otherwise the assertion below would be testing the
	test rather than the code.
	"""

	monkeypatch.setenv("HELPER_OUTPUT", "sr_secret")

	with pytest.raises(subroutine.errors.Unauthenticated) as raised:
		subroutine.credentials.resolve(
			connection(
				token_command="sh -c 'echo $HELPER_OUTPUT; echo no key found >&2; exit 2'"
			),
			default_connection="local",
		)

	assert "no key found" in raised.value.detail
	assert "sr_secret" not in raised.value.detail


def test_a_missing_token_command_says_it_is_not_installed (config_home: pathlib.Path) -> None:
	"""The commonest failure, on a machine where the helper was never set up."""

	with pytest.raises(subroutine.errors.Unauthenticated) as raised:
		subroutine.credentials.resolve(
			connection(token_command="subroutine-no-such-credential-helper"),
			default_connection="local",
		)

	assert "not installed" in raised.value.detail


def test_a_silent_token_command_is_refused (config_home: pathlib.Path) -> None:
	"""Succeeding while printing nothing would present as an empty bearer token."""

	with pytest.raises(subroutine.errors.Unauthenticated) as raised:
		subroutine.credentials.resolve(connection(token_command="true"), default_connection="local")

	assert "printed nothing" in raised.value.detail


def test_the_credentials_file_is_the_last_resort (config_home: pathlib.Path) -> None:
	"""Rule 4, which is where ``token create`` puts one."""

	subroutine.credentials.store("work", "sr_stored")

	resolved = subroutine.credentials.resolve(connection(), default_connection="local")

	assert resolved.token == "sr_stored"
	assert resolved.source.endswith("credentials.toml")


def test_storing_a_token_keeps_the_others (config_home: pathlib.Path) -> None:
	"""Adding a second connection's token must not lose the first."""

	subroutine.credentials.store("work", "sr_one")
	path = subroutine.credentials.store("side", "sr_two")

	assert subroutine.credentials.read_file() == {
		"work": subroutine.credentials.Stored(token="sr_one"),
		"side": subroutine.credentials.Stored(token="sr_two"),
	}
	assert path == subroutine.credentials.credentials_file_path()


def test_the_credentials_file_is_private (config_home: pathlib.Path) -> None:
	"""0600, and asserted rather than assumed."""

	path = subroutine.credentials.store("work", "sr_one")
	mode = stat.S_IMODE(path.stat().st_mode)

	assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
	assert subroutine.credentials.permission_warning() is None


def test_a_world_readable_credentials_file_is_warned_about (
	config_home: pathlib.Path,
) -> None:
	"""``ssh`` refuses outright; this warns, because these are tasks and not a private key."""

	path = subroutine.credentials.store("work", "sr_one")
	path.chmod(0o644)

	warning = subroutine.credentials.permission_warning()

	assert warning is not None
	assert "chmod 600" in warning
	assert "sr_one" not in warning


def test_a_token_is_never_written_to_the_config_file (config_home: pathlib.Path) -> None:
	"""§12.3a's whole point: one file you can commit, one that never leaves the machine."""

	subroutine.credentials.store("work", "sr_one")
	written(config_home, '[connections.work]\nurl = "https://tasks.example.com"\n')

	assert "sr_one" not in (config_home / "config.toml").read_text(encoding="utf-8")
	assert "sr_one" in (config_home / "credentials.toml").read_text(encoding="utf-8")


def test_an_unparseable_credentials_file_is_reported (config_home: pathlib.Path) -> None:
	"""Treating it as empty would present as a 401 against every remote at once."""

	config_home.mkdir(parents=True, exist_ok=True)
	(config_home / "credentials.toml").write_text("[work\ntoken =\n", encoding="utf-8")

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		subroutine.credentials.read_file()

	assert "credentials.toml" in raised.value.detail


# --- Which of a connection's two tokens answers (`#1449`) --------------------------------
#
# The person and the agent at one machine are the same account, in the same directory,
# reading the same two files. The only thing that differs is the environment each process was
# started in, so that is what step 4 asks. Everything above it is untouched, which is what
# makes removing the second token a complete undo.


def test_the_agents_token_answers_where_the_marker_is_set (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The whole of `#1449`: one machine, one file, two principals."""

	subroutine.credentials.store("work", "sr_person")
	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.setenv(subroutine.connections.DEFAULT_AGENT_WHEN, "1")

	resolved = subroutine.credentials.resolve(connection(), default_connection="local")

	assert resolved.token == "sr_agent"
	assert subroutine.connections.DEFAULT_AGENT_WHEN in resolved.source
	assert "agent" in resolved.source


def test_the_persons_token_answers_where_it_is_not (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The other half, and the pair is the point — either alone proves nothing."""

	subroutine.credentials.store("work", "sr_person")
	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.delenv(subroutine.connections.DEFAULT_AGENT_WHEN, raising=False)

	resolved = subroutine.credentials.resolve(connection(), default_connection="local")

	assert resolved.token == "sr_person"
	assert "agent" not in resolved.source


def test_an_agents_token_is_never_offered_to_the_person (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A machine holding only an agent's credential gives the person nothing, not the agent's.

	**"nowhere" rather than the file**, deliberately: there is no token *for them*, and naming
	the file would send somebody to read a line that is not about them.
	"""

	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.delenv(subroutine.connections.DEFAULT_AGENT_WHEN, raising=False)

	resolved = subroutine.credentials.resolve(connection(), default_connection="local")

	assert not resolved.found
	assert resolved.source == "nowhere"


def test_a_connection_can_name_a_different_editors_variable (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``CLAUDECODE`` is a default rather than a constant, because it is somebody else's name.

	`#1434`: a claim about another program is a promise no test of ours can keep. What is
	testable is that the variable is ours to change, which is what keeps this from rotting into
	a hard-coded dependency on one vendor.
	"""

	subroutine.credentials.store("work", "sr_person")
	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.setenv(subroutine.connections.DEFAULT_AGENT_WHEN, "1")
	monkeypatch.setenv("SOME_OTHER_EDITOR", "1")

	named = connection(agent_when="SOME_OTHER_EDITOR")

	assert subroutine.credentials.resolve(named, default_connection="local").token == "sr_agent"

	monkeypatch.delenv("SOME_OTHER_EDITOR")

	# The default is *replaced*, not added to — otherwise naming one editor would silently
	# leave every other vendor's name live as well.
	assert subroutine.credentials.resolve(named, default_connection="local").token == "sr_person"


def test_a_variable_set_by_hand_still_beats_both (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Step 4 sits below every explicit source, which is what keeps `#1455` true.

	Somebody who has said where the token is has said it, and a condition inferred from the
	process must not overrule them. It is also what makes the mechanism reversible: unset the
	variable and today's behaviour is back, exactly.
	"""

	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.setenv(subroutine.connections.DEFAULT_AGENT_WHEN, "1")
	monkeypatch.setenv("SUBROUTINE_TOKEN_WORK", "sr_by_hand")

	assert (
		subroutine.credentials.resolve(connection(), default_connection="local").token
		== "sr_by_hand"
	)


def test_a_credential_helper_still_beats_both (
	config_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``token_env`` is rule 2 and stays there — the same reasoning one source along."""

	subroutine.credentials.store("work", "sr_agent", agent=True)

	monkeypatch.setenv(subroutine.connections.DEFAULT_AGENT_WHEN, "1")
	monkeypatch.setenv("WORK_TASKS_TOKEN", "sr_named")

	resolved = subroutine.credentials.resolve(
		connection(token_env="WORK_TASKS_TOKEN"), default_connection="local"
	)

	assert resolved.token == "sr_named"


def test_storing_one_token_leaves_the_other_alone (config_home: pathlib.Path) -> None:
	"""Setting an agent up on a machine somebody works on must not take their own list away.

	That is the objection ``token create --store`` was written around — a narrow credential
	written under the local connection silently narrowing the operator's own CLI — and the
	second slot is what answers it rather than an argument.
	"""

	subroutine.credentials.store("work", "sr_person")
	subroutine.credentials.store("work", "sr_agent", agent=True)

	assert subroutine.credentials.read_file()["work"] == subroutine.credentials.Stored(
		token="sr_person", agent_token="sr_agent"
	)

	subroutine.credentials.store("work", "sr_person_again")

	assert subroutine.credentials.read_file()["work"] == subroutine.credentials.Stored(
		token="sr_person_again", agent_token="sr_agent"
	)


def test_a_file_written_before_any_of_this_still_reads (config_home: pathlib.Path) -> None:
	"""Every credentials.toml in existence has one key per table, and must go on working."""

	config_home.mkdir(parents=True, exist_ok=True)
	(config_home / "credentials.toml").write_text(
		'[work]\ntoken = "sr_old"\n', encoding="utf-8"
	)

	assert subroutine.credentials.read_file() == {
		"work": subroutine.credentials.Stored(token="sr_old")
	}


def test_neither_token_is_in_a_repr (config_home: pathlib.Path) -> None:
	"""A live local in ``_from_file`` reaches any traceback-with-locals renderer.

	:class:`Resolved` was given a safe ``__repr__`` for exactly this and the second type would
	have reintroduced it — a secret that is never written down anywhere except in a crash.
	"""

	shown = repr(subroutine.credentials.Stored(token="sr_person", agent_token="sr_agent"))

	assert "sr_person" not in shown
	assert "sr_agent" not in shown
	assert "<set>" in shown
