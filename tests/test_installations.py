"""Which copies of this software are in play, and whether they agree — item ``#381``.

**The defect this covers cannot be reproduced by the rest of the suite**, and that is the
whole reason it exists. Every other test builds the plugin, the program and the instance from
one tree, at one commit, so all three agree by construction — which is the arrangement
``tests/test_compatibility.py`` was written about and this one extends. Here the versions are
supplied as data, so a disagreement is a case rather than an accident of deployment.

The three that can disagree, and the day each one did (2026-08-03):

- **plugin against program** — a cached plugin at ``0.1.1`` offering a tool argument to a
  program that had it, and to another that did not (`#379`, `#380`);
- **program against instance** — a client one commit ahead refusing the instance outright
  (`#345`), and a server running new code against an old database (`#376`);
- **program against its own schema** — the pair that made a working feature look broken.
"""

import json
import pathlib
import uuid

import pytest
import sqlalchemy.orm

import subroutine
import subroutine.db.migrate
import subroutine.db.session
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.installations
import subroutine.views

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The plugin as it is actually laid out in this repository. Pointed at by the last test in
#: the first group, which is the one that stops :data:`subroutine.installations.MANIFEST`
#: drifting away from the tree while every hand-built fixture goes on passing.
SHIPPED = ROOT / "plugins" / "subroutine"

#: Words a version report may never use. Ordering two version strings correctly needs
#: ``packaging``, which this project does not depend on, so nothing here may claim a
#: direction — `#401`'s neighbour in kind: a diagnostic asserting a cause it has not
#: established is worse than one that only states what it saw.
DIRECTIONAL = ("older", "newer", "behind", "ahead", "out of date", "outdated", "upgrade")


def _me (
	*,
	instance_version: str | None = "1.0.0",
	schema_revision: str | None = "abcdef123456",
) -> subroutine.views.Me:
	"""Build the smallest ``Me`` the renderer reads, with the two versions under test."""

	return subroutine.views.Me(
		api_version="1.0",
		instance_version=instance_version,
		schema_revision=schema_revision,
		user=subroutine.views.Caller(
			id=uuid.UUID(int=1),
			username="si",
			display_name=None,
			email=None,
			timezone=None,
			is_superuser=False,
			is_service_account=False,
		),
		credential=None,
		instance_permissions=[],
		workspaces=[],
	)


def _principal (
	session: sqlalchemy.orm.Session,
) -> subroutine.domain.authentication.Principal:
	"""Bring an instance into being and return the account it made, with no credential.

	No token, which is §12.1a's local mode — the shape ``views.me`` is asked in most often
	and the one with nothing to narrow the answer.
	"""

	built = subroutine.domain.bootstrap.initialise(
		session, username="si", instance_name="Test"
	)
	session.flush()

	return subroutine.domain.authentication.Principal(user=built.user)


class TestTheProgram:
	"""What ``installations.program`` reports."""

	def test_it_reports_the_installed_version (self) -> None:
		"""The number is the package's own, not a second copy kept somewhere."""

		assert subroutine.installations.program() == subroutine.__version__

	def test_it_always_answers (self) -> None:
		"""There is no state in which the program cannot say what it is.

		``__version__`` falls back to a placeholder when the package is not installed at all,
		so a caller never has to handle a missing program version — only a missing *plugin*
		one, which is a different question with a different meaning.
		"""

		assert subroutine.installations.program()


class TestThePlugin:
	"""What ``installations.plugin`` reports, and every way it declines to.

	**Each of these returns ``None`` rather than raising**, and they are written out one at a
	time rather than collapsed into a parametrised list because the point is not that the
	function is total — it is that a *diagnostic* run on a broken machine must not be the
	thing that breaks. A single test asserting "does not raise" would pass on a function that
	caught everything and reported nonsense.
	"""

	def test_no_plugin_started_this_process (self, monkeypatch: pytest.MonkeyPatch) -> None:
		"""A command line has no plugin, and that is the ordinary case rather than a fault."""

		monkeypatch.delenv(subroutine.installations.PLUGIN_ROOT, raising=False)

		assert subroutine.installations.plugin() is None

	def test_an_empty_variable_is_the_same_as_an_absent_one (
		self, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""An editor that exports the name with nothing in it has still said nothing.

		The same rule ``SUBROUTINE_TOKEN_<CONNECTION>`` follows (`#337`): an empty value falls
		through cleanly rather than being taken as a value that happens to be empty.
		"""

		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, "")

		assert subroutine.installations.plugin() is None

	def test_a_root_that_is_not_there (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""A stale path — the cache cleared, the plugin removed — reports nothing."""

		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path / "gone"))

		assert subroutine.installations.plugin() is None

	def test_a_root_with_no_manifest_in_it (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""A directory that exists and is not a plugin."""

		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

		assert subroutine.installations.plugin() is None

	def test_a_manifest_that_is_not_json (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""A half-written or truncated manifest is unreadable, not fatal."""

		_write_manifest(tmp_path, "{not json")
		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

		assert subroutine.installations.plugin() is None

	def test_a_manifest_that_is_not_an_object (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""Valid JSON of the wrong shape. ``["0.1.0"].get`` would be an `AttributeError`."""

		_write_manifest(tmp_path, json.dumps(["0.1.0"]))
		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

		assert subroutine.installations.plugin() is None

	def test_a_manifest_with_no_version (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""A plugin that never declared one."""

		_write_manifest(tmp_path, json.dumps({"name": "subroutine"}))
		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

		assert subroutine.installations.plugin() is None

	def test_a_version_that_is_not_a_string (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""``{"version": 2}`` would otherwise be rendered as ``plugin 2`` and believed."""

		_write_manifest(tmp_path, json.dumps({"version": 2}))
		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(tmp_path))

		assert subroutine.installations.plugin() is None

	def test_the_manifest_wins_over_the_directory_name (
		self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
	) -> None:
		"""The version is read from the file, never from the path it was found at.

		**This is the shortcut that would have worked today and lied later.** The editor
		caches a plugin under a directory named for its version — the real one on this machine
		is ``…/cache/subroutine/subroutine/0.1.1`` — so parsing the path gives the right answer
		for as long as that layout holds, and confident nonsense afterwards. The two are made
		to disagree here so that a version taken from the path cannot pass.
		"""

		root = tmp_path / "cache" / "subroutine" / "0.9.9"
		root.mkdir(parents=True)
		_write_manifest(root, json.dumps({"version": "1.2.3"}))
		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(root))

		assert subroutine.installations.plugin() == "1.2.3"

	def test_it_reads_the_plugin_this_repository_ships (
		self, monkeypatch: pytest.MonkeyPatch
	) -> None:
		"""Pointed at the real plugin directory, it finds the real version.

		**The one test here that is not built from a fixture**, and the only one that can fail
		when the plugin's layout moves. Everything above constructs the directory it then
		reads, so ``MANIFEST`` could name the wrong path and all of them would still pass —
		a guard checking the shape it was written from, which is this project's most expensive
		recurring defect.

		**The expected value is read through a path written out here, not through
		``installations.MANIFEST``.** Falsified: pointing the constant at ``plugin.json`` in
		the plugin root made this fail, but with a ``FileNotFoundError`` from the *test's* own
		read rather than from an assertion — which is luck. Had the constant named some other
		JSON file carrying a ``version``, expectation and subject would have moved together
		and the test would have passed on a broken module.
		"""

		monkeypatch.setenv(subroutine.installations.PLUGIN_ROOT, str(SHIPPED))
		declared = json.loads(
			(SHIPPED / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
		)

		assert subroutine.installations.plugin() == declared["version"]


class TestTheRenderedLine:
	"""What ``views.versions`` says, and what it refuses to say."""

	def test_everything_agrees_and_no_plugin_started_this (self) -> None:
		"""The ordinary command line: one line, three facts, nothing to act on."""

		lines = subroutine.views.versions(_me(instance_version="1.0.0"), program="1.0.0")

		assert lines == ["Program 1.0.0, instance 1.0.0, schema abcdef123456."]

	def test_a_plugin_is_named_first (self) -> None:
		"""An agent's session has three, and the one it can least easily check leads."""

		lines = subroutine.views.versions(
			_me(instance_version="1.0.0"), program="1.0.0", plugin="1.0.0"
		)

		assert lines == ["Plugin 1.0.0, program 1.0.0, instance 1.0.0, schema abcdef123456."]

	def test_the_program_and_the_instance_disagree (self) -> None:
		"""`#345`, in one line: a field one of them has and the other does not."""

		lines = subroutine.views.versions(_me(instance_version="0.9.0"), program="1.0.0")

		assert lines[0].startswith("Program 1.0.0, instance 0.9.0")
		assert len(lines) == 2
		assert "refused for a field one of them does not have" in lines[1]

	def test_the_plugin_and_the_program_disagree (self) -> None:
		"""`#379`: a tool offering an argument its program had never heard of."""

		lines = subroutine.views.versions(
			_me(instance_version="1.0.0"), program="1.0.0", plugin="0.1.1"
		)

		assert len(lines) == 2
		assert "argument the program does not accept" in lines[1]

	def test_both_disagreements_are_reported_separately (self) -> None:
		"""They are different failures with different fixes, so neither stands for the other."""

		lines = subroutine.views.versions(
			_me(instance_version="0.9.0"), program="1.0.0", plugin="0.1.1"
		)

		assert len(lines) == 3

	def test_an_instance_too_old_to_say_says_so (self) -> None:
		"""A null version is the answer, not a gap.

		An instance that sends no version predates this field — which is itself why the
		feature somebody is asking about is missing. Rendering it blank would hide the reason
		at the moment it was being looked for.
		"""

		lines = subroutine.views.versions(_me(instance_version=None), program="1.0.0")

		assert "instance too old to say" in lines[0]

	def test_an_instance_too_old_to_say_still_counts_as_a_disagreement (self) -> None:
		"""Silence is not agreement. ``None != "1.0.0"``, and the advice line follows."""

		lines = subroutine.views.versions(_me(instance_version=None), program="1.0.0")

		assert len(lines) == 2

	def test_a_schema_nothing_migrated_is_left_out (self) -> None:
		"""A database built by ``create_all`` has no revision, and no clause about one."""

		lines = subroutine.views.versions(
			_me(instance_version="1.0.0", schema_revision=None), program="1.0.0"
		)

		assert lines == ["Program 1.0.0, instance 1.0.0."]

	@pytest.mark.parametrize("word", DIRECTIONAL)
	def test_it_never_claims_which_one_is_newer (self, word: str) -> None:
		"""No wording here may assert a direction it has not established.

		Comparing ``0.2.1`` against ``0.2.1.dev51`` correctly needs ``packaging``, which is
		not a declared dependency — so a sentence saying "the plugin is older" would be a
		guess dressed as a finding. Naming the disagreement is what a reader can act on.
		"""

		rendered = " ".join(
			subroutine.views.versions(
				_me(instance_version="0.9.0"), program="1.0.0", plugin="0.1.1"
			)
		).lower()

		assert word not in rendered


class TestWhatTheInstanceReports:
	"""``views.me`` carries the answering installation's own numbers."""

	def test_it_reports_the_running_program (
		self, session: sqlalchemy.orm.Session
	) -> None:
		"""Not a constant, and not configuration: the version of the process that answered."""

		answered = subroutine.views.me(session, _principal(session))

		assert answered.instance_version == subroutine.__version__

	def test_it_reports_the_revision_the_database_is_at (
		self, session: sqlalchemy.orm.Session
	) -> None:
		"""The head, read off the same connection the answer was assembled over.

		**I wrote this test the other way round first**, asserting null on the grounds that
		the suite builds its schema with ``create_all``. It does — and then *stamps* it, so
		that a test database describes itself the way a real one does. The assumption was
		wrong and the test said so immediately, which is the whole argument for asserting a
		value rather than a shape.
		"""

		answered = subroutine.views.me(session, _principal(session))

		assert answered.schema_revision == subroutine.db.migrate.head_revision()

	def test_a_database_no_migration_ever_touched_reports_nothing (
		self, tmp_path: pathlib.Path
	) -> None:
		"""Null is "never migrated", and it must not be an exception.

		No installation this program creates reaches this state — ``init`` migrates — so the
		case exists to hold the promise that a *diagnostic* cannot be the call that fails.
		Built here rather than assumed, because "alembic returns None for a missing version
		table" is somebody else's behaviour and this is the only place we depend on it.
		"""

		url = f"sqlite:///{tmp_path / 'unstamped.db'}"
		engine = subroutine.db.session.create_engine(url)

		try:
			subroutine.db.session.create_all(engine)
			factory = subroutine.db.session.create_session_factory(engine)

			with factory() as opened:
				answered = subroutine.views.me(opened, _principal(opened))

			assert answered.schema_revision is None

		finally:
			engine.dispose()


def _write_manifest (root: pathlib.Path, body: str) -> None:
	"""Put a plugin manifest at the path the module looks for one."""

	manifest = root / subroutine.installations.MANIFEST
	manifest.parent.mkdir(parents=True, exist_ok=True)
	manifest.write_text(body, encoding="utf-8")
