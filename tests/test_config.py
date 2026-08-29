"""Tests for path resolution, settings precedence and the SQLite storage probe."""

import os
import pathlib
import tomllib
import typing

import annotated_types
import pydantic
import pytest
import sqlalchemy

import subroutine.config
import subroutine.domain.hierarchy


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


def test_a_setting_lands_above_the_blank_line_before_a_table (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Where the key is parsed and where it *reads* as belonging are different questions.

	Inserted at the header's own index the key is still top-level and still correct, and it
	sits hard against ``[connections.work]`` with a blank line above it — so the file says, to
	anybody who opens it, that the key is part of that table. The one thing it is not.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text('port = 9000\n\n[connections.work]\nurl = "https://x.example"\n', encoding="utf-8")

	subroutine.config.store_setting("default_connection", "work")

	lines = path.read_text(encoding="utf-8").splitlines()
	placed = lines.index('default_connection = "work"')

	assert lines[placed - 1].strip(), "no blank line above it"
	assert not lines[placed + 1].strip(), "and the blank line separates it from the table"


def test_a_table_is_appended_whole_and_a_repeat_is_refused (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A second table under one name leaves the file meaning whatever TOML decides.

	Refused rather than merged, because the caller is the one that knows whether the person
	meant to replace a connection or has forgotten they already have one — and either answer
	given here would be a guess written into somebody's configuration.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("# mine\nport = 9000\n", encoding="utf-8")

	subroutine.config.store_table(
		"connections.work", {"url": "https://tasks.example.com", "read_only": True}
	)

	text = path.read_text(encoding="utf-8")
	parsed = tomllib.loads(text)

	assert parsed["port"] == 9000, "what was there is left alone"
	assert "# mine" in text, "comments included"
	assert parsed["connections"]["work"] == {
		"url": "https://tasks.example.com",
		"read_only": True,
	}

	with pytest.raises(ValueError):
		subroutine.config.store_table("connections.work", {"url": "https://other.example"})


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


def test_a_connection_table_is_not_reported_as_an_ignored_setting (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#259`. The warning for typos said the one table people hand-write was doing nothing.

	`unknown_settings` compares against `Settings.model_fields`, and `connections` is
	deliberately not a field — it is read by `subroutine.connections`, which does its own
	strict check of the keys inside each table. So adding a connection, which is the only way
	to add one, greeted the person with an authoritative statement that it was having no
	effect, immediately before it worked.

	Worse than a stray warning because of what the warning is for: `#175` built it so that a
	misspelled `protected` cannot silently do nothing. Somebody trusting it would delete a
	working connection.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

	written = subroutine.config.config_file_path()
	written.parent.mkdir(parents=True, exist_ok=True)
	# The typo goes *above* the table: in TOML every key after a table header belongs to it,
	# so written the other way round `protectd` is a key inside the connection and is caught
	# by `connections.KNOWN_KEYS` instead — a different check, and not the one under test.
	written.write_text(
		'protectd = true\n\n[connections.work]\nurl = "https://tasks.example.com"\n',
		encoding="utf-8",
	)

	reported = dict(subroutine.config.unknown_settings())

	assert "connections" not in reported

	# And the real typo beside it is still caught, with its suggestion — otherwise this would
	# pass just as well against a check that had stopped looking at anything.
	assert reported["protectd"] == "protected"


def test_a_loopback_instance_can_say_where_a_browser_reaches_it () -> None:
	"""`#1007`. The bind is the whole truth when nothing off the machine can reach it.

	This is the state the README sends every new self-hoster into — `subroutine serve`, then
	`subroutine login link` — and it refused, because a check written for the proxy case was
	applied to every case. The port was never a guess: it is the setting `serve` binds to and
	prints back.
	"""

	settings = subroutine.config.Settings(host="127.0.0.1", port=8471)

	assert subroutine.config.browsable_url(settings) == "http://127.0.0.1:8471"


def test_the_operator_is_believed_over_the_socket () -> None:
	"""`public_url` wins wherever it is set, because a proxy makes the bind the wrong answer.

	Falsified by removing the first branch: the assertion then reads the loopback address,
	which is what a link built behind a proxy would wrongly say.
	"""

	settings = subroutine.config.Settings(
		host="127.0.0.1", port=8471, public_url="https://tasks.example.com/"
	)

	assert subroutine.config.browsable_url(settings) == "https://tasks.example.com"


def test_a_wildcard_bind_is_not_an_address_anybody_browses_to () -> None:
	"""`0.0.0.0` accepts connections and names no destination, so this refuses to answer.

	This is `serve --insecure` on a trusted LAN, and it is the one case where the old refusal
	was right: the reader is on another machine, and nothing here knows which of this
	machine's addresses they will type. Left as `None` deliberately rather than guessing a
	hostname.
	"""

	for host in ("0.0.0.0", "::", "192.168.0.14"):
		settings = subroutine.config.Settings(host=host, port=8471)

		assert subroutine.config.browsable_url(settings) is None, (
			f"{host} was answered for, and it names no destination"
		)


def test_an_ipv6_loopback_is_bracketed_so_the_port_is_still_a_port () -> None:
	"""`http://::1:8471` is a malformed address that looks plausible in a message.

	Somebody may genuinely have bound to `::1`, and unbracketed it parses as a host of `:`
	with a port of `:1` — so the link would be printed, look reasonable and resolve nowhere.
	"""

	settings = subroutine.config.Settings(host="::1", port=8471)

	assert subroutine.config.browsable_url(settings) == "http://[::1]:8471"


@pytest.mark.parametrize(
	"address",
	[
		"",
		"https://tasks.example.com",
		"https://tasks.example.com:8471/subroutine",
		"http://127.0.0.1:8471",
		"https://[2001:db8::1]:8471",
		"https://desk.tailnet-example.ts.net",
		"https://internal_host.example",
		"https://xn--bcher-kva.example",
	],
)
def test_a_usable_public_url_is_left_alone (address: str) -> None:
	"""`#1257`'s refusal must not turn down anything anybody actually serves on.

	It runs at startup, so what it refuses is an instance that may have been serving perfectly
	well yesterday — which makes a false positive the expensive direction. An underscore and a
	punycode label are both real; an empty value means nobody has said, which is not a fault.
	"""

	assert subroutine.config.public_url_fault(address) is None


@pytest.mark.parametrize(
	("address", "because"),
	[
		("https://desk.<your-tailnet>.ts.net", "host"),
		("tasks.example.com", "http"),
		("ftp://tasks.example.com", "http"),
		("https://", "host"),
		("https://tasks example.com", "host"),
		("https://tasks.example.com:not-a-port", "port"),
	],
)
def test_a_public_url_that_is_not_an_address_is_named_as_one (
	address: str, because: str
) -> None:
	"""`#1257`. The first of these is how it was met, and it is the ordinary mistake.

	A configuration template with a placeholder in it, pasted as given — which is exactly what
	``docs/hosting.md`` invites, since it uses ``tasks.example.com`` in that position. The
	service started, accepted it, and announced it as the address to reach the instance on.
	"""

	fault = subroutine.config.public_url_fault(address)

	assert fault is not None
	assert because in fault


#: Numeric settings that may legitimately take any integer, with the reason — `SR#1559`.
#:
#: Empty, and the emptiness is the point: everything numeric here bounds a resource, a count
#: or a size, and none of them means anything below one except a timeout, which means *no
#: bound* at zero. An entry added later has to say why the next reader should not be surprised.
UNBOUNDED_ON_PURPOSE: dict[str, str] = {}


def test_every_numeric_setting_says_what_it_will_not_take () -> None:
	"""`SR#1559`. Nine of ten took any integer, and zero was the bad one every time.

	The precedent was already in the file, two fields above the worst of them.
	``claim_lease_minutes``'s comment (`SR#358`) argues exactly this case: *"the path a caller
	controls was bounded and the path the operator controls — the one that applies by default
	to everybody — was not. Zero was the bad one: claiming succeeded, printed a confirmation,
	and did nothing, silently, for every worker."* ``max_page_size = 0`` is that sentence one
	field over, hiding every row instead of every claim, and reporting an empty instance rather
	than a misconfigured one.

	**A ratchet rather than a list of values.** What matters is not that today's ten are
	bounded — it is that the eleventh cannot arrive without somebody deciding. The population
	comes off ``model_fields``, so a numeric setting added tomorrow fails this until it carries
	a bound or an excuse.
	"""

	unbounded = []

	for name, field in subroutine.config.Settings.model_fields.items():
		if field.annotation is not int or name in UNBOUNDED_ON_PURPOSE:
			continue

		if not any(isinstance(rule, annotated_types.Ge) for rule in field.metadata):
			unbounded.append(name)

	assert not unbounded, (
		f"{unbounded} take any integer, including 0 and -1. Give each a "
		f"`pydantic.Field(ge=...)`, or record in UNBOUNDED_ON_PURPOSE why the next reader "
		f"should not be surprised."
	)


def test_no_setting_is_excused_from_a_bound_it_already_has () -> None:
	"""The other direction, so an excuse cannot outlive its reason.

	Every allow-list in this repository owes an answer to *what makes this entry go away*, and
	the ones that do not are how a stale reason comes to justify a live rule.
	"""

	for name in UNBOUNDED_ON_PURPOSE:
		field = subroutine.config.Settings.model_fields.get(name)

		assert field is not None, f"{name!r} is excused from a bound and is not a setting"
		assert not any(isinstance(rule, annotated_types.Ge) for rule in field.metadata), (
			f"{name!r} carries a bound now, so its entry in UNBOUNDED_ON_PURPOSE is spent"
		)


def test_a_value_outside_a_bound_is_refused_rather_than_used () -> None:
	"""The bounds fire, and the message names the setting a reader has to change.

	Driven rather than inspected, because a constraint declared and never exercised is the
	thing this whole item is about.
	"""

	# **Written out rather than splatted from a mapping.** `Settings` is a pydantic-settings
	# model whose ``__init__`` is typed for its own sources, so `**{name: value}` is not
	# something mypy can check — and the gate runs mypy over `tests` as well as `src`.
	attempts: tuple[tuple[str, typing.Callable[[], subroutine.config.Settings]], ...] = (
		("max_page_size", lambda: subroutine.config.Settings(max_page_size=0)),
		("default_page_size", lambda: subroutine.config.Settings(default_page_size=0)),
		(
			"request_timeout_seconds",
			lambda: subroutine.config.Settings(request_timeout_seconds=-1),
		),
		("max_hierarchy_depth", lambda: subroutine.config.Settings(max_hierarchy_depth=99)),
		("port", lambda: subroutine.config.Settings(port=0)),
	)

	for name, attempt in attempts:
		with pytest.raises(pydantic.ValidationError) as refused:
			attempt()

		assert refused.value.errors()[0]["loc"] == (name,)

	# **Zero is legitimate for exactly one of them**, and the reason is in `db/session.py`:
	# it reads the value as falsy and sets no statement timeout at all, which is how every
	# caller behaved before the setting existed.
	assert subroutine.config.Settings(request_timeout_seconds=0).request_timeout_seconds == 0


def test_the_depth_a_setting_may_ask_for_fits_the_column_that_stores_it () -> None:
	"""`SR#1560`'s L-1, and the seam that keeps two literals honest.

	``config`` may not import the domain, so ``max_hierarchy_depth``'s ceiling is written out
	there as a number. This is what holds it to the derivation — and holds that derivation to
	the real column, which ``hierarchy`` deliberately cannot see either, because :class:`Node`
	is a protocol so the rule can be applied without importing a model.

	**Three declarations, none of which can see the other two, agreeing here or nowhere.**
	Until `SR#1560` the setting reached nothing, so none of this mattered; making it live
	without this turns a dead setting into a way to overflow ``path`` — silently on SQLite,
	which ignores ``VARCHAR`` lengths, and as a ``StringDataRightTruncation`` on PostgreSQL.
	"""

	import subroutine.db.models.work

	kind = subroutine.db.models.work.Task.__table__.columns["path"].type

	assert isinstance(kind, sqlalchemy.String), "`path` stopped being a bounded string"
	assert kind.length == subroutine.domain.hierarchy.PATH_COLUMN_LENGTH

	ceiling = next(
		rule.le
		for rule in subroutine.config.Settings.model_fields["max_hierarchy_depth"].metadata
		if isinstance(rule, annotated_types.Le)
	)

	assert ceiling == subroutine.domain.hierarchy.MAX_DEPTH

	# And the derivation itself, against a real path rather than the formula that produced it.
	import uuid

	path = None

	for _ in range(subroutine.domain.hierarchy.MAX_DEPTH + 1):
		path = subroutine.domain.hierarchy.build_path(path, uuid.uuid4())

	assert len(path or "") <= kind.length, "the deepest allowed tree overflows `path`"

	overflowing = subroutine.domain.hierarchy.build_path(path, uuid.uuid4())

	assert len(overflowing) > kind.length, (
		"one level past the ceiling still fits, so the ceiling is lower than it needs to be"
	)
