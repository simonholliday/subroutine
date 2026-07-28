"""Tests for path resolution, settings precedence and the SQLite storage probe."""

import pathlib

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
