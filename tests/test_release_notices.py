"""Each surface says which installation is behind what has been released, and what to type - `#2224`.

Simon's answers of 2026-09-17 are the whole shape. ``whoami`` answers the question; the bare
``subroutine`` says one line while the program is behind; the browser tells an administrator,
and only an administrator, that the instance is; the server's log writes a warning when the
instance is behind and a note when a check fails. A development build says nothing anywhere.

**The guard is derived from** :data:`subroutine.views.NOTICE_SURFACES`, which `#2224` asks for by
name: every surface is driven through the same cases and compared with what the list says it
names, and a notice rendered from a place the list does not name fails. A rule three surfaces
keep and the fourth does not - this codebase's signature defect - has nowhere to go.
"""

import json
import logging
import pathlib
import re
import typing

import pytest
import sqlalchemy.orm
import typer.testing

import api_support
import subroutine.api.app
import subroutine.cli.main
import subroutine.clients.http
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.errors
import subroutine.installations
import subroutine.permissions
import subroutine.releases
import subroutine.views
import test_api_tasks
import test_installations
import test_release_checking
import test_web

#: What the instance heard was released, newest first. The newest two share a schema and the
#: oldest does not, so one release behind moves nothing and two behind moves the database.
RECORD = (
	subroutine.releases.Release(version="0.8.20", schema="b" * 12, date="2026-09-20"),
	subroutine.releases.Release(version="0.8.19", schema="b" * 12, date="2026-09-18"),
	subroutine.releases.Release(version="0.8.18", schema="a" * 12, date="2026-09-14"),
)

#: The whole record, with each plugin's present number.
PUBLISHED = subroutine.releases.Published(
	releases=RECORD, plugins={"subroutine": "0.8.23", "subroutine-remote": "0.8.23"}
)

#: What a check that answered heard.
ANSWERED = subroutine.releases.Asked(at=test_release_checking.START, record=PUBLISHED)

#: What a check that could not be made kept instead.
FAILED = subroutine.releases.Asked(
	at=test_release_checking.START, failure="Could not read the list of releases."
)

#: A development build's version, which no record holds.
DEVELOPMENT = "0.8.21.dev3+gabcdef123"

#: How each notice begins, which is how a rendering is read for whom it names.
MARKS = {
	subroutine.views.PROGRAM: "The program is ",
	subroutine.views.PLUGIN: "The plugin is ",
	subroutine.views.INSTANCE: "The instance is ",
}


class Case (typing.NamedTuple):
	"""One arrangement of the three installations, what the instance heard, and who is reading."""

	name: str
	program: str
	plugin: str
	instance: str
	administers: bool = True

	#: Whether the operator agreed to ask, and what the watch holds if so.
	checking: bool = True
	asked: subroutine.releases.Asked | None = ANSWERED

	#: Which installations are behind, as a fact about the versions - before anybody's reach.
	behind: frozenset[str] = frozenset()


EVERYTHING = frozenset(MARKS)

CASES = (
	Case(
		"all three behind, read by an administrator",
		program="0.8.18", plugin="0.8.22", instance="0.8.19", behind=EVERYTHING,
	),
	Case(
		"all three behind, read by somebody who cannot administer the instance",
		program="0.8.18", plugin="0.8.22", instance="0.8.18", administers=False, behind=EVERYTHING,
	),
	Case(
		"the instance two releases behind across a migration",
		program="0.8.20", plugin="0.8.23", instance="0.8.18",
		behind=frozenset({subroutine.views.INSTANCE}),
	),
	Case("development builds", program=DEVELOPMENT, plugin="0.8.23", instance=DEVELOPMENT),
	Case("everything current", program="0.8.20", plugin="0.8.23", instance="0.8.20"),
	Case(
		"a check that could not be made", program="0.8.18", plugin="0.8.22", instance="0.8.18",
		asked=FAILED,
	),
	Case(
		"a check not made yet", program="0.8.18", plugin="0.8.22", instance="0.8.18", asked=None,
	),
	Case(
		"an instance that does not check", program="0.8.18", plugin="0.8.22", instance="0.8.18",
		checking=False, asked=None,
	),
)


def _watch (case: Case) -> subroutine.releases.Watch | None:
	"""Return the watch this case's instance holds, which is none unless its operator agreed."""

	if not case.checking:
		return None

	watch = subroutine.releases.Watch(fetch=lambda: PUBLISHED)
	watch.latest = case.asked

	return watch


def _me (case: Case) -> subroutine.views.Me:
	"""Answer ``/v1/me`` as this case's instance would, through the renderer the route uses."""

	return test_installations._me(instance_version=case.instance).model_copy(update={
		"releases": subroutine.views.release_news(_watch(case)),
		"instance_permissions": (
			[subroutine.permissions.INSTANCE_ADMIN] if case.administers else []
		),
	})


def _named (said: str) -> set[str]:
	"""Return which installations a rendering says are behind."""

	return {who for who, mark in MARKS.items() if mark in said}


def _in_the_browser (tmp_path: pathlib.Path, answers: list[typing.Any]) -> list[str | None]:
	"""Render the browser's notice for each ``/v1/me`` answer, in the app that is served."""

	module = test_web._staged(tmp_path)

	return list(test_web._ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const answers = {json.dumps(answers)};

		process.stdout.write(JSON.stringify(answers.map((me) => app.instanceBehind(me))));
	"""))


def _by_whoami (cases: typing.Sequence[Case], _tmp_path: pathlib.Path) -> list[str]:
	"""What both ``whoami`` commands print after the versions they name."""

	return [
		"\n".join(subroutine.views.versions(_me(case), program=case.program, plugin=case.plugin))
		for case in cases
	]


def _by_the_bare_command (cases: typing.Sequence[Case], _tmp_path: pathlib.Path) -> list[str]:
	"""What the bare ``subroutine`` prints under its agenda."""

	return [subroutine.views.program_behind(_me(case), program=case.program) or "" for case in cases]


def _by_the_browser (cases: typing.Sequence[Case], tmp_path: pathlib.Path) -> list[str]:
	"""What the browser shows beside the work."""

	return [
		said or ""
		for said in _in_the_browser(
			tmp_path, [_me(case).model_dump(mode="json") for case in cases]
		)
	]


def _by_the_log (cases: typing.Sequence[Case], _tmp_path: pathlib.Path) -> list[str]:
	"""What the server's log is written, about the program the server itself is running."""

	written = []

	for case in cases:
		said = (
			None
			if not case.checking or case.asked is None
			else subroutine.views.instance_log_line(case.asked, running=case.instance)
		)
		written.append("" if said is None else said[1])

	return written


#: One driver per surface, each rendering it the way the surface does.
DRIVERS: dict[str, typing.Callable[[typing.Sequence[Case], pathlib.Path], list[str]]] = {
	"whoami": _by_whoami,
	"subroutine": _by_the_bare_command,
	"browser": _by_the_browser,
	"log": _by_the_log,
}

#: Where each surface renders its notice, as a file under ``src/subroutine`` and the renderer.
SITES = {
	("cli/personal.py", "versions"): "whoami",
	("mcp/tools.py", "versions"): "whoami",
	("cli/personal.py", "program_behind"): "subroutine",
	("web/assets/app.js", "instanceBehind"): "browser",
	("api/app.py", "instance_log_line"): "log",
}

#: Everything in :mod:`subroutine.views` that renders a notice or the answer holding one.
RENDERERS = (
	"versions", "release_lines", "program_behind", "plugin_behind", "instance_behind",
	"instance_log_line", "program_behind_in_words", "plugin_behind_in_words",
	"instance_behind_in_words",
)


def _sites (root: pathlib.Path) -> set[tuple[str, str]]:
	"""Return every place under ``root`` that renders a release notice, with what it calls.

	**The tree is an argument** (`#405`), so a synthetic offender reaches the real scan.
	"""

	python = re.compile(r"subroutine\.views\.(" + "|".join(RENDERERS) + r")\(")
	browser = re.compile(r"\binstanceBehind\(")
	found: set[tuple[str, str]] = set()

	for path in sorted(root.rglob("*.py")):
		relative = path.relative_to(root).as_posix()
		found.update((relative, name) for name in python.findall(path.read_text(encoding="utf-8")))

	for path in sorted(root.rglob("*.js")):
		if browser.search(path.read_text(encoding="utf-8")):
			found.add((path.relative_to(root).as_posix(), "instanceBehind"))

	return found


def test_every_surface_names_what_is_behind_and_only_to_whoever_can_act_on_it (
	tmp_path: pathlib.Path,
) -> None:
	"""Every surface on the list, through every case, against what the list says it names.

	**The instance is named to an administrator only** on every surface a person reads, and the
	log is an administrator's by where it is. Nothing is named when a check failed, was not made,
	or is not made at all, and a development build names nothing (Simon, 2026-09-17).
	"""

	named: set[str] = set()

	for surface, audiences in subroutine.views.NOTICE_SURFACES.items():
		rendered = DRIVERS[surface](CASES, tmp_path)

		assert len(rendered) == len(CASES), surface

		named.update(_named(rendered[0]))

		for case, said in zip(CASES, rendered, strict=True):
			expected = set(case.behind) & set(audiences)

			if surface != "log" and not case.administers:
				expected.discard(subroutine.views.INSTANCE)

			assert _named(said) == expected, (surface, case.name, said)

	# **Every audience is named somewhere**, so a case list that stopped reaching one is a failure
	# rather than a quieter pass.
	assert named == EVERYTHING


def test_the_surfaces_driven_are_the_surfaces_listed () -> None:
	"""A surface added to the list without a driver, or a driver left for one removed, fails."""

	assert set(DRIVERS) == set(subroutine.views.NOTICE_SURFACES)


def test_a_notice_is_rendered_only_where_the_list_says () -> None:
	"""Both directions: every place that renders one is a listed surface, and every surface does.

	A fifth caller - a notice under ``subroutine list``, say, which Simon's answer keeps clean for
	scripts and agents - fails here until it is a surface with a driver.
	"""

	root = pathlib.Path(subroutine.views.__file__).parent

	assert _sites(root) == set(SITES)
	assert set(SITES.values()) == set(subroutine.views.NOTICE_SURFACES)


def test_the_scan_finds_a_notice_rendered_somewhere_new (tmp_path: pathlib.Path) -> None:
	"""The scan above can fail: fed a new caller in each language, it names both."""

	(tmp_path / "cli").mkdir()
	(tmp_path / "cli" / "listing.py").write_text(
		"said = subroutine.views.program_behind(me, program=running)\n", encoding="utf-8"
	)
	(tmp_path / "web").mkdir()
	(tmp_path / "web" / "board.js").write_text(
		"const said = instanceBehind(me);\n", encoding="utf-8"
	)

	assert _sites(tmp_path) == {
		("cli/listing.py", "program_behind"), ("web/board.js", "instanceBehind")
	}


def test_the_browser_writes_the_instance_notice_as_the_views_do (tmp_path: pathlib.Path) -> None:
	"""``instanceBehind`` is ``views.instance_behind``'s twin, word for word, across every case."""

	cases = [
		*CASES,
		Case(
			"one release behind, reading its version off a longer record",
			program="0.8.20", plugin="0.8.23", instance="0.8.19",
		),
	]
	answers = [_me(case) for case in cases]
	views = [subroutine.views.instance_behind(me) for me in answers]
	browser = _in_the_browser(tmp_path, [me.model_dump(mode="json") for me in answers])

	assert browser == views

	assert views[-1] == (
		"The instance is 0.8.19 and 0.8.20 is out, one release behind. Upgrading it does not "
		"change the database: install the new version and restart it."
	)
	assert views[2] == (
		"The instance is 0.8.18 and 0.8.20 is out, 2 releases behind. Upgrading it changes the "
		"database, so plan a short outage: stop it, install the new version, run 'subroutine db "
		"upgrade', then start it."
	)


def test_whoami_answers_what_the_check_found_and_says_so_when_it_could_not () -> None:
	"""The line that named a command becomes the answer, and each kind of *not known* is itself.

	`#2223` kept a check that failed, one not made yet and an instance that does not check apart,
	so that none of them reads as *nothing newer*; this is where a reader finally sees that.
	"""

	def said (case: Case) -> list[str]:
		"""Return what ``whoami`` says about releases for one case."""

		return subroutine.views.release_lines(_me(case), program=case.program, plugin=case.plugin)

	by_name = {case.name: case for case in CASES}

	assert said(by_name["an instance that does not check"]) == [
		subroutine.views.HOW_TO_ASK_IF_IT_IS_OLD
	]
	assert said(by_name["a check not made yet"]) == [
		"This instance checks for new releases once a day, and has not heard back yet."
	]
	assert said(by_name["a check that could not be made"]) == [
		"This instance's last check for new releases failed. Could not read the list of releases."
	]
	assert said(by_name["everything current"]) == ["0.8.20 is the newest release."]
	assert said(by_name["development builds"]) == []
	assert said(by_name["all three behind, read by an administrator"]) == [
		"The program is 0.8.18 and 0.8.20 is out, 2 releases behind. Upgrade it with 'uv tool "
		"upgrade subroutine', or however you installed it.",
		"The plugin is 0.8.22 and 0.8.23 is out. Refresh it with 'claude plugin marketplace "
		"update subroutine', then 'claude plugin update subroutine@subroutine'.",
		"The instance is 0.8.19 and 0.8.20 is out, one release behind. Upgrading it does not "
		"change the database: install the new version and restart it.",
	]

	# **A plugin with no program beside it is the remote one**, which is what its refresh names.
	remote = subroutine.views.release_lines(
		_me(by_name["all three behind, read by an administrator"]), program=None, plugin="0.8.22"
	)

	assert "'claude plugin update subroutine-remote@subroutine'" in remote[0], remote


def test_the_log_is_written_when_the_answer_changes_and_not_on_every_check (
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""Behind, behind, failing, failing, current, behind: three lines, not six.

	**Forgotten when the answer goes quiet**, so the instance falling behind again after being
	current is written about again rather than taken for the line already written.
	"""

	replies: list[subroutine.releases.Published | None] = [
		PUBLISHED,
		PUBLISHED,
		None,
		None,
		subroutine.releases.Published(releases=RECORD[2:]),
		PUBLISHED,
	]
	clock = test_release_checking.Clock()

	def fetch () -> subroutine.releases.Published:
		"""Answer with the next reply, or fail the way the real fetch does."""

		reply = replies.pop(0)

		if reply is None:
			raise subroutine.errors.ServiceUnavailable("Could not read the list of releases.")

		return reply

	watch = subroutine.releases.Watch(
		fetch=fetch,
		clock=clock,
		tell=lambda asked: subroutine.views.instance_log_line(asked, running="0.8.18"),
	)

	with caplog.at_level(logging.INFO, logger="subroutine.releases"):
		for day in range(6):
			clock.now = test_release_checking.START + day * subroutine.releases.CHECK_EVERY
			asking = watch.ask_if_due()

			assert asking is not None
			asking.join(timeout=10)

	behind = (
		"The instance is 0.8.18 and 0.8.20 is out, 2 releases behind. Upgrading it changes the "
		"database, so plan a short outage: stop it, install the new version, run 'subroutine db "
		"upgrade', then start it."
	)

	assert [
		(record.levelno, record.getMessage())
		for record in caplog.records
		if record.name == "subroutine.releases"
	] == [
		(logging.WARNING, behind),
		(logging.INFO, "Could not check for new releases. Could not read the list of releases."),
		(logging.WARNING, behind),
	]
	assert replies == []


def test_an_instance_that_checks_tells_its_log_about_the_program_it_runs (
	monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
	"""The application's watch is built to write to the log, about the version the server runs."""

	monkeypatch.setattr(subroutine.installations, "program", lambda: "0.8.19")
	monkeypatch.setattr(subroutine.releases, "record", lambda *_args, **_kwargs: PUBLISHED)

	settings = subroutine.config.Settings(dev_mode=True, releases={"check": True})
	watch = subroutine.api.app.create_app(settings=settings).state.releases

	assert isinstance(watch, subroutine.releases.Watch)

	with caplog.at_level(logging.INFO, logger="subroutine.releases"):
		asking = watch.ask_if_due()

		assert asking is not None
		asking.join(timeout=10)

	assert [
		record.getMessage() for record in caplog.records if record.name == "subroutine.releases"
	] == [
		"The instance is 0.8.19 and 0.8.20 is out, one release behind. Upgrading it does not "
		"change the database: install the new version and restart it."
	]


def test_the_bare_command_says_under_its_agenda_that_the_program_is_behind (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
	"""Driven end to end, over a connection to an instance that has heard: under the signpost.

	**And ``subroutine agenda`` says nothing**, which is the half of Simon's answer that keeps
	every other command's output clean for whatever reads it.
	"""

	world = test_api_tasks._world(session)
	watch = subroutine.releases.Watch(fetch=lambda: PUBLISHED)
	watch.latest = ANSWERED
	world.application.state.releases = watch

	written = subroutine.config.config_file_path()
	written.parent.mkdir(parents=True, exist_ok=True)
	written.write_text(
		'default_connection = "work"\n\n[connections.local]\nenabled = false\n\n'
		'[connections.work]\nurl = "https://tasks.example.com"\n',
		encoding="utf-8",
	)
	monkeypatch.setenv("SUBROUTINE_TOKEN_WORK", world.secret)
	monkeypatch.setattr(subroutine.installations, "program", lambda: "0.8.18")

	def opened (
		connection: subroutine.connections.Connection,
		_roster: subroutine.connections.Roster,
		_settings: subroutine.config.Settings,
		*,
		token: str | None = None,
	) -> subroutine.clients.http.Client:
		"""Reach the test's instance in process, whatever address the connection names."""

		return subroutine.clients.http.Client(
			connection,
			token=token or world.secret,
			transport=api_support.SyncTransport(world.application),
			base_url=api_support.BASE_URL,
		)

	monkeypatch.setattr(subroutine.clients.opening, "for_connection", opened)

	runner = typer.testing.CliRunner()
	bare = runner.invoke(subroutine.cli.main.app, [])

	assert bare.exception is None, bare.output

	lines = [line.strip() for line in bare.output.splitlines()]
	signpost = next(
		index for index, line in enumerate(lines) if line.startswith("Tip: subroutine --help")
	)

	assert lines[signpost + 1] == (
		"The program is 0.8.18 and 0.8.20 is out, 2 releases behind. Upgrade it with 'uv tool "
		"upgrade subroutine', or however you installed it."
	), bare.output

	agenda = runner.invoke(subroutine.cli.main.app, ["agenda"])

	assert agenda.exception is None, agenda.output
	assert "The program is" not in agenda.output
