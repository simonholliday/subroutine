"""Tests for path resolution, settings precedence and the SQLite storage probe."""

import os
import pathlib
import tomllib

import pytest

import subroutine.config


def test_xdg_paths_are_honoured (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Configuration and data directories follow the XDG environment variables."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

	assert subroutine.config.config_home() == tmp_path / "cfg" / "subroutine"
	assert subroutine.config.data_home() == tmp_path / "data" / "subroutine"
	assert subroutine.config.config_file_path().name == "config.toml"


def test_default_database_lives_under_data_home (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The database defaults to durable local storage, not the working directory."""

	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

	path = subroutine.config.default_database_path()

	assert path == tmp_path / "data" / "subroutine" / "subroutine.db"
	assert subroutine.config.default_database_url() == f"sqlite:///{path}"


def test_environment_beats_config_file (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""An environment variable overrides the same key in the configuration file."""

	config_dir = tmp_path / "cfg" / "subroutine"
	config_dir.mkdir(parents=True)
	(config_dir / "config.toml").write_text('port = 9000\nlog_level = "DEBUG"\n', encoding="utf-8")

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
	monkeypatch.setenv("SUBROUTINE_PORT", "7000")

	settings = subroutine.config.load_settings()

	assert settings.port == 7000
	assert settings.log_level == "DEBUG"

	sources = subroutine.config.setting_sources(settings)

	assert sources["port"] == "environment"
	assert sources["log_level"].endswith("config.toml")
	assert sources["host"] == "default"


def test_explicit_arguments_beat_everything (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A value passed directly wins over the environment."""

	monkeypatch.setenv("SUBROUTINE_PORT", "7000")

	assert subroutine.config.load_settings(port=1234).port == 1234


def test_secret_key_is_required_outside_development (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Starting without a signing key fails loudly rather than inventing one."""

	monkeypatch.delenv("SUBROUTINE_SECRET_KEY", raising=False)

	settings = subroutine.config.load_settings(secret_key=None, dev_mode=False)

	with pytest.raises(RuntimeError, match="No secret_key is configured"):
		settings.require_secret_key()

	assert subroutine.config.load_settings(secret_key=None, dev_mode=True).require_secret_key()
	assert subroutine.config.load_settings(secret_key="abc").require_secret_key() == "abc"


def test_sqlite_path_is_extracted (tmp_path: pathlib.Path) -> None:
	"""The SQLite file path is recoverable from the URL, and absent for other backends."""

	sqlite_settings = subroutine.config.load_settings(database_url=f"sqlite:///{tmp_path}/x.db")

	assert sqlite_settings.is_sqlite
	assert sqlite_settings.sqlite_path == tmp_path / "x.db"

	postgres_settings = subroutine.config.load_settings(
		database_url="postgresql+psycopg://localhost/subroutine"
	)

	assert not postgres_settings.is_sqlite
	assert postgres_settings.sqlite_path is None


def test_sqlite_probe_succeeds_on_local_disk (tmp_path: pathlib.Path) -> None:
	"""The storage probe passes on ordinary local storage and leaves nothing behind."""

	assert subroutine.config.probe_sqlite_locking(tmp_path) is None
	assert list(tmp_path.iterdir()) == []


def test_sqlite_probe_reports_a_usable_error (tmp_path: pathlib.Path) -> None:
	"""A failing probe explains the problem in terms a user can act on."""

	unwritable = tmp_path / "readonly"
	unwritable.mkdir()
	unwritable.chmod(0o500)

	try:
		message = subroutine.config.probe_sqlite_locking(unwritable)

		# A read-only directory is not a network filesystem, but it exercises the same
		# reporting path: the user gets a sentence naming the directory and a remedy.
		assert message is not None
		assert str(unwritable) in message
		assert "PostgreSQL" in message

	finally:
		unwritable.chmod(0o700)


def test_filesystem_type_is_reported_for_a_real_path (tmp_path: pathlib.Path) -> None:
	"""The filesystem type of an existing path can be identified on Linux."""

	fs_type = subroutine.config.filesystem_type(tmp_path)

	assert fs_type is None or isinstance(fs_type, str)


def test_network_filesystem_detection_walks_to_an_existing_parent (
	tmp_path: pathlib.Path,
) -> None:
	"""A database path that does not exist yet is still checkable via its parent."""

	assert not subroutine.config.is_network_filesystem(tmp_path / "nested" / "not-yet.db")


def test_system_timezone_is_an_iana_name () -> None:
	"""A timezone name is always produced, defaulting to UTC when unknown."""

	name = subroutine.config.system_timezone()

	assert name
	assert " " not in name


def test_storing_a_setting_twice_leaves_the_file_parseable (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Appending blindly used to produce a duplicate key, which TOML refuses to parse."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	subroutine.config.store_setting("secret_key", "first")
	subroutine.config.store_setting("secret_key", "second")

	path = subroutine.config.config_file_path()
	parsed = tomllib.loads(path.read_text(encoding="utf-8"))

	assert parsed["secret_key"] == "second"


def test_a_setting_lands_above_any_table_header (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Everything the program reads is a top-level key.

	Text appended after a ``[table]`` header belongs to that table, so a key written there
	is invisible to Settings — and the *next* run, seeing it still unset, would append
	another and make the file unparseable.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("# a comment\nport = 9000\n\n[server]\nhost = \"0.0.0.0\"\n", encoding="utf-8")

	subroutine.config.store_setting("secret_key", "written")

	text = path.read_text(encoding="utf-8")
	parsed = tomllib.loads(text)

	assert parsed["secret_key"] == "written", "the key must be readable at the top level"
	assert parsed["server"] == {"host": "0.0.0.0"}, "the table must be left alone"
	assert "# a comment" in text, "comments are preserved"


def test_the_config_file_is_private_however_it_was_made (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The signing key can be added to a file the user created with their own umask."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("port = 1\n", encoding="utf-8")
	path.chmod(0o644)

	subroutine.config.store_setting("secret_key", "private")

	assert path.stat().st_mode & 0o077 == 0, "the file must not be group- or world-readable"


def test_the_config_file_is_never_briefly_readable_on_the_way_to_being_private (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#205`. It was written at the umask and tightened a statement later.

	On a fresh install the signing key therefore existed group- and world-readable for the
	window in between — on a shared machine, readable by every other account for exactly as
	long as it took the next line to run.

	**Asserted by taking the chmod away**, because the test beside this one cannot see the
	defect: write-then-chmod and open-with-a-mode both end at ``0600``, so a mode checked
	afterwards is the same either way. What distinguishes them is whether the mode depends on
	the second step at all — so this removes the second step and asks again. That is also what
	makes the guard honest about `keep_private`, which suppresses its own failures and would
	otherwise look like the same protection.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
	monkeypatch.setattr(pathlib.Path, "chmod", lambda self, mode: None)

	# A umask that would let the file be created 0644 if the mode were not on the open.
	previous = os.umask(0o022)

	try:
		subroutine.config.store_setting("secret_key", "private")

	finally:
		os.umask(previous)

	path = subroutine.config.config_file_path()

	assert path.read_text(encoding="utf-8").count("private") == 1, "and it still wrote it"
	assert path.stat().st_mode & 0o077 == 0, (
		"the file was created at the umask and only tightened afterwards, so the key was "
		"readable by other accounts for that window"
	)


def test_a_blanked_key_is_regenerated_without_corrupting_the_file (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""An empty value is falsy, so ensure_secret_key regenerates — in place, not appended."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
	monkeypatch.delenv("SUBROUTINE_SECRET_KEY", raising=False)

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text('secret_key = ""\n', encoding="utf-8")

	key, written = subroutine.config.ensure_secret_key(subroutine.config.load_settings())

	assert key
	assert written is not None
	assert tomllib.loads(path.read_text(encoding="utf-8"))["secret_key"] == key


def test_an_existing_key_is_not_rewritten (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Rotating the signing key on every start would break every cursor in flight."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
	monkeypatch.delenv("SUBROUTINE_SECRET_KEY", raising=False)

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text('secret_key = "already here"\n', encoding="utf-8")

	key, written = subroutine.config.ensure_secret_key(subroutine.config.load_settings())

	assert key == "already here"
	assert written is None


def test_a_value_with_awkward_characters_round_trips (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Keys are base64url, but a database URL can contain anything."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	awkward = 'postgresql://u:p"a\\ss@host/db'
	subroutine.config.store_setting("database_url", awkward)

	parsed = tomllib.loads(subroutine.config.config_file_path().read_text(encoding="utf-8"))

	assert parsed["database_url"] == awkward
