"""Which connection and workspace a bare number means — SPEC.md §13.7's resolution order.

Steps 1 to 3 live in :mod:`subroutine.context` and are tested here; 4 and 5 need a connection
to have been asked what it reaches and are tested through the CLI in
``tests/test_cli_connections.py``.

The property this module is built around is that **losing the stored file must degrade to a
question, never to a different outcome** — which is what distinguishes it from the
number-to-item map deleted in §12.2a, whose loss silently changed what an identifier meant.
There is a test for that below and it is the important one.
"""

import os
import pathlib
import typing

import pytest

import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.directory
import subroutine.errors


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point the configuration and state directories at a fresh temporary home."""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

	for name in list(os.environ):
		if name.startswith(("SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			monkeypatch.delenv(name, raising=False)

	return tmp_path


def configured (home: pathlib.Path, text: str) -> None:
	"""Write a configuration file."""

	where = home / "config" / "subroutine"
	where.mkdir(parents=True, exist_ok=True)
	(where / "config.toml").write_text(text, encoding="utf-8")


def roster (**overrides: typing.Any) -> subroutine.connections.Roster:
	"""Read the roster from whatever is on disk."""

	return subroutine.connections.roster(subroutine.config.load_settings(**overrides))


TWO_CONNECTIONS = """
[connections.work]
url = "https://tasks.example.com"
"""


def test_with_one_connection_and_nothing_stored_there_is_still_a_context (
	home: pathlib.Path,
) -> None:
	"""There is always a context. What there may not be is a chosen workspace."""

	current = subroutine.context.resolve(roster())

	assert current.connection == "local"
	assert current.connection_source == subroutine.context.FROM_SOLE
	assert current.workspace is None


def test_a_flag_beats_everything (home: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Step 1."""

	configured(home, TWO_CONNECTIONS)
	monkeypatch.setenv(subroutine.context.WORKSPACE_VARIABLE, "from-environment")
	subroutine.context.store("local", "from-file")

	current = subroutine.context.resolve(roster(), connection="work", workspace="asked")

	assert (current.connection, current.workspace) == ("work", "asked")
	assert current.connection_source == subroutine.context.FROM_FLAG
	assert current.workspace_source == subroutine.context.FROM_FLAG


def test_the_environment_beats_the_stored_context (
	home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Step 2, which is how one terminal pane pins itself."""

	subroutine.context.store("local", "from-file")
	monkeypatch.setenv(subroutine.context.WORKSPACE_VARIABLE, "from-environment")

	current = subroutine.context.resolve(roster())

	assert current.workspace == "from-environment"
	assert current.workspace_source == subroutine.context.WORKSPACE_VARIABLE


def test_the_stored_context_is_read_back (home: pathlib.Path) -> None:
	"""Step 3, which is what ``subroutine use`` writes."""

	configured(home, TWO_CONNECTIONS)
	subroutine.context.store("work", "acme")

	current = subroutine.context.resolve(roster())

	assert (current.connection, current.workspace) == ("work", "acme")
	assert current.workspace_source == subroutine.context.FROM_STORED


def test_the_two_halves_resolve_independently (
	home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``-w acme`` must not throw away the connection somebody chose with ``use``.

	And exporting ``SUBROUTINE_WORKSPACE`` in one terminal must not silently move which
	instance a write lands on, which is the more expensive half of the same mistake.
	"""

	configured(home, TWO_CONNECTIONS)
	subroutine.context.store("work", "acme")

	current = subroutine.context.resolve(roster(), workspace="other")

	assert current.connection == "work", "the stored connection survived a workspace flag"
	assert current.workspace == "other"


def test_a_stored_workspace_does_not_follow_a_connection_change (home: pathlib.Path) -> None:
	"""It would name a workspace on an instance that has never heard of it."""

	configured(home, TWO_CONNECTIONS)
	subroutine.context.store("local", "personal")

	current = subroutine.context.resolve(roster(), connection="work")

	assert current.connection == "work"
	assert current.workspace is None, "the workspace belonged to the other connection"


def test_a_stored_connection_that_has_gone_is_refused_by_name (home: pathlib.Path) -> None:
	"""The common way to arrive here is a connection removed and forgotten.

	Refused now rather than at the first request: "there is no connection called 'work'" is a
	great deal more useful than a timeout.
	"""

	subroutine.context.store("work", "acme")

	with pytest.raises(subroutine.errors.NotFound) as raised:
		subroutine.context.resolve(roster())

	assert "work" in raised.value.detail


def test_losing_the_file_costs_a_question_and_never_a_different_outcome (
	home: pathlib.Path,
) -> None:
	"""The property that makes this file safe to keep in ``STATE_HOME`` at all.

	It holds only which workspace is current, and every ref stays absolute within one — so
	after losing it a bare number resolves to *nothing* rather than to something else. That is
	the whole difference from the number-to-item map deleted in §12.2a.
	"""

	configured(home, TWO_CONNECTIONS)
	subroutine.context.store("work", "acme")

	assert subroutine.context.resolve(roster()).workspace == "acme"

	removed = subroutine.context.clear()

	assert removed is not None
	assert not removed.exists()

	after = subroutine.context.resolve(roster())

	assert after.workspace is None, "unanswered, rather than answered differently"
	assert after.connection == "local", "and back to the configured default"


def test_clearing_nothing_is_not_an_error (home: pathlib.Path) -> None:
	"""``use --reset`` on a fresh installation has nothing to say and should not fail."""

	assert subroutine.context.clear() is None


def test_an_unreadable_file_is_treated_as_absent (home: pathlib.Path) -> None:
	"""The one place in this program where that is right.

	The design of this file is that losing it costs a question, so refusing to run because of
	it would be a worse outcome than the one that refusal protects against.
	"""

	path = subroutine.context.file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("this is not [ valid toml", encoding="utf-8")

	assert subroutine.context.read() == {}
	assert subroutine.context.resolve(roster()).workspace is None


def test_the_description_names_where_the_context_came_from (home: pathlib.Path) -> None:
	"""Provenance is the part that earns its keep.

	The standing footgun in comparable tooling is not having a profile but not knowing whether
	it came from a flag, the environment or a file.
	"""

	configured(home, TWO_CONNECTIONS)
	subroutine.context.store("work", "acme")

	current = subroutine.context.resolve(roster())

	assert current.describe(qualified=True) == "work/acme (from 'subroutine use')"

	# With one connection the name is noise: there is nothing to disambiguate, and §13.5b's
	# output has no room for a word nobody needs.
	assert current.describe(qualified=False) == "acme (from 'subroutine use')"


def test_an_unsettled_workspace_says_so_rather_than_printing_nothing (
	home: pathlib.Path,
) -> None:
	"""A blank where a name should be reads as a bug in the program."""

	described = subroutine.context.resolve(roster()).describe(qualified=False)

	assert "not chosen yet" in described


def test_a_marker_naming_a_connection_that_is_gone_is_dropped () -> None:
	"""Item ``#409``. `#166`'s rule, which was implemented for one half of the marker.

	**It broke reading, which makes it worse than `#324`.** §13.7's load-bearing rule is that
	reads span everything reachable — forgetting your context must never cost you something —
	and before this a `subroutine list` in a marked directory could not run at all.

	Two ordinary ways in: a connection renamed in `config.toml`, and one set ``enabled =
	false``, which the roster drops so it is indistinguishable from a missing one.
	"""

	roster = subroutine.connections.Roster(
		connections=(subroutine.connections.Connection(name="local"),), default="local"
	)
	marker = subroutine.directory.Marker(
		path=pathlib.Path(".subroutine"), connection="gone", workspace="personal"
	)

	current = subroutine.context.resolve(roster, marker=marker)

	assert current.connection == "local"
	assert current.unusable_marker_connection == "gone"


def test_dropping_the_marker_takes_its_workspace_with_it () -> None:
	"""A slug means nothing on an instance that has never heard of it.

	The condition that governs this was already written for the case where a marker names a
	*different* connection from the one in force, and it covers this one without a second
	rule — which is worth pinning, because the alternative is a workspace from one instance
	being applied to another and refused much later, about the wrong thing.
	"""

	roster = subroutine.connections.Roster(
		connections=(subroutine.connections.Connection(name="local"),), default="local"
	)
	marker = subroutine.directory.Marker(
		path=pathlib.Path(".subroutine"), connection="gone", workspace="somewhere-else"
	)

	current = subroutine.context.resolve(roster, marker=marker)

	assert current.workspace is None


def test_a_connection_named_on_the_command_line_still_refuses () -> None:
	"""The line this draws: a marker is a file, a flag is somebody speaking now.

	Without this the leniency above would spread to everything, and a typo in ``-c`` would
	quietly act somewhere the person did not name — which is the failure `roster.require`
	exists to prevent and is far worse than the one being fixed.
	"""

	roster = subroutine.connections.Roster(
		connections=(subroutine.connections.Connection(name="local"),), default="local"
	)

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.context.resolve(roster, connection="gone")


def test_a_stored_connection_that_is_gone_still_refuses () -> None:
	"""And ``subroutine use`` is also somebody speaking, so it is told rather than overruled.

	The stored context is a decision somebody took on this machine; a marker is a file that
	arrived with a checkout. Being quiet about the second and loud about the first is the
	whole of the distinction, and it is asserted here so that "be lenient" cannot creep down
	the chain.
	"""

	roster = subroutine.connections.Roster(
		connections=(subroutine.connections.Connection(name="local"),), default="local"
	)
	written = subroutine.context.file_path()
	written.parent.mkdir(parents=True, exist_ok=True)
	written.write_text('connection = "gone"\n', encoding="utf-8")

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.context.resolve(roster)
