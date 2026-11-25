"""The upgrade rehearsal's own judgement - ``scripts/rehearse_upgrade.py``, item ``#5063``.

**The rehearsal itself is not run here.** It installs a release from the package index and takes
half an hour, so ``scripts/release.py`` runs it before every tag, and ``test_release_script``
holds where. What is tested here is what it decides from what it saw - which answers are server
errors and which commands crashed - and that its reads leave no query parameter unasked, because
a sweep that has quietly stopped asking reads exactly like one that found nothing wrong.
"""

import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import types
import typing

import pytest
import sqlalchemy.exc

import subroutine.api.app
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.errors

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load (name: str) -> types.ModuleType:
	"""Load one script by path, the way the other scripts' tests do."""

	spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


@pytest.fixture(scope="module")
def rehearsal () -> types.ModuleType:
	"""Return ``scripts/rehearse_upgrade.py`` as a module."""

	return _load("rehearse_upgrade")


def test_readyz_answering_503_before_the_upgrade_is_not_a_server_error (
	rehearsal: types.ModuleType
) -> None:
	"""Before ``db upgrade``, ``/readyz`` saying the database is behind is it doing its job."""

	answers = [
		{"who": "keanu", "path": "/readyz", "status": 503, "detail": "This database is at schema x"}
	]

	assert rehearsal.server_errors(answers, before_upgrade=True) == []


def test_after_the_upgrade_readyz_answering_503_is_one (rehearsal: types.ModuleType) -> None:
	"""After it, the same answer means the upgrade left the database somewhere this cannot serve."""

	answers = [
		{"who": "keanu", "path": "/readyz", "status": 503, "detail": "This database is at schema x"}
	]

	assert len(rehearsal.server_errors(answers, before_upgrade=False)) == 1


def test_every_other_server_error_before_the_upgrade_is_one (rehearsal: types.ModuleType) -> None:
	"""`SR#5057`: a read in the window answered 500, where it should have said the schema
	was behind.

	That is what the Hyperfence instance met on ``/v1/me``: an instance serves reads while it
	waits for ``db upgrade``, so its reads are where an operator meets a fault first.
	"""

	answers = [
		{"who": "laurence", "path": "/v1/me", "status": 500, "detail": "Internal server error"},
		{
			"who": "keanu",
			"path": "/v1/tasks?workspace_id=zion",
			"status": 409,
			"detail": "Schema mismatch",
		},
	]

	found = rehearsal.server_errors(answers, before_upgrade=True)

	assert len(found) == 1, found
	assert "500 on /v1/me" in found[0], found
	assert "laurence" in found[0], found


def test_one_fault_on_every_item_is_one_line_naming_everybody_who_met_it (
	rehearsal: types.ModuleType
) -> None:
	"""An item's number is folded into the path's shape, and the commonest fault comes first."""

	answers = [
		*(
			{"who": who, "path": f"/v1/tasks/{number}/events", "status": 500}
			for who in ("joe", "keanu")
			for number in (3, 17)
		),
		{"who": "hugo", "path": "/v1/journal?limit=2", "status": 502},
	]

	found = rehearsal.server_errors(answers, before_upgrade=False)

	assert len(found) == 2, found
	assert found[0].startswith("4 x 500 on /v1/tasks/N/events, as joe, keanu"), found
	assert found[1].startswith("1 x 502 on /v1/journal, as hugo"), found


@pytest.mark.parametrize(
	("code", "said", "crash"),
	[
		(0, "Done: Pay the gas bill", False),
		(1, "There is no #9 in metacortex.", False),
		(2, "No such option: --binds", True),
		(1, "Traceback (most recent call last):\n  File ...", True),
		(1, "Something went wrong that should not have.", True),
		(124, "It did not finish within 300 seconds.", True),
		(-9, "", True),
		(1, "local could not be read: no such column: task.ref", True),
	],
	ids=["an answer", "a refusal", "a command line this build does not take", "a traceback",
	     "the program's own crash report", "a command that hung", "a process killed",
	     "an SQL error the local client words as unreadable"],
)
def test_what_counts_as_a_crash (
	rehearsal: types.ModuleType, code: int, said: str, crash: bool
) -> None:
	"""A refusal is an answer; a traceback, a hang and a command this build does not take are not.
	"""

	assert rehearsal.crashed(rehearsal.Ran(code, said)) is crash


def test_the_local_client_s_unreadable_sentence_is_the_one_held (tmp_path: pathlib.Path) -> None:
	"""`SR#5304`: :data:`UNREADABLE` is what the local client says of an unknown SQL error."""

	rehearsal = _load("rehearse_upgrade")
	database = tmp_path / "x.db"
	database.write_bytes(b"")
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.load_settings(database_url=f"sqlite:///{database}"),
	)

	with pytest.raises(subroutine.errors.ServiceUnavailable) as refused, client._reported():
		raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("no such column: x"))

	assert rehearsal.UNREADABLE in refused.value.detail, refused.value.detail


def test_a_read_answered_before_the_upgrade_and_refused_after_it_is_one (
	rehearsal: types.ModuleType,
) -> None:
	"""`SR#5304`: a credential the upgrade stopped working is a 401 on every read, and no 5xx."""

	before = [
		{"who": "hugo", "path": "/v1/tasks/12", "status": 200},
		{"who": "hugo", "path": "/v1/tasks/13?fields=title", "status": 200},
		{"who": "nobody", "path": "/v1/tasks/12", "status": 401},
		{"who": "hugo", "path": "/v1/projects/web", "status": 404},
	]
	after = [
		{"who": "hugo", "path": "/v1/tasks/12", "status": 401},
		{"who": "hugo", "path": "/v1/tasks/13?fields=title", "status": 401},
		{"who": "nobody", "path": "/v1/tasks/12", "status": 401},
		{"who": "hugo", "path": "/v1/projects/web", "status": 404},
	]

	assert rehearsal.server_errors(after, before_upgrade=False) == []
	assert rehearsal.lost(before, after) == [
		"2 x 401 on /v1/tasks/N where it was answered before, as hugo"
	]
	assert rehearsal.lost(before, before) == []


def test_a_credential_is_never_printed_back (rehearsal: types.ModuleType) -> None:
	"""The rehearsal's credentials are throwaway, and still never shown: a report gets pasted.

	**The whole masked text, exactly** (`SR#5304`, L-7's RH1 of the cold review of 2026-11-11): a
	secret is ``token_urlsafe``, so it holds ``-`` and ``_``, and a mask narrowed to letters and
	digits left *-Value.x*, the rest of it, while asking only that ``s3cr3t`` was gone passed.
	"""

	said = "Your token: sr_ab12cd34_s3cr3t-Value.x_y\nStore it now."

	assert rehearsal.masked(said) == "Your token: sr_…\nStore it now."
	assert rehearsal.tail(said) == "Your token: sr_… | Store it now."


#: **A stand-in for a build** (`SR#5304`): it answers every command, refuses one, and meets an SQL
#: error at ``add``, so :func:`drive` is driven without the half-hour rehearsal.
STUB = (
	"import sys\n"
	"asked = sys.argv[1:]\n"
	"if asked[:1] == ['add']:\n"
	"    print('local could not be read: no such column: no_such_column'); sys.exit(1)\n"
	"if 'token:admin' in asked:\n"
	"    print('That credential may not issue token:admin.'); sys.exit(1)\n"
	"print('{\"ref\": 7}')\n"
)


def test_the_terminal_s_morning_after_sorts_what_it_ran (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path
) -> None:
	"""`SR#5304`: :func:`drive` had never run outside the rehearsal, so M-16 went unseen."""

	here = rehearsal.Installation(tmp_path / "installation", "sqlite", None)
	here.kept["refs"] = {"deploy": 3, "standup": 4}
	stub = rehearsal.Program("a stub", (sys.executable, "-c", STUB))

	count, crashes, refused = rehearsal.drive(here, stub)

	assert count > 100, count
	assert [line.split(" -> ")[0] for line in crashes] == [
		"add Check the upgrade went through tomorrow ~15m !3/3 #upgrade --json"
	], crashes
	assert refused == 1, refused
	logged = (tmp_path / "installation" / "commands.log").read_text(encoding="utf-8")

	assert "no_such_column" in logged, "every command is written to the installation's log"


def test_a_refusal_drawn_in_a_panel_is_reported_as_its_words (rehearsal: types.ModuleType) -> None:
	"""The command line draws a refusal in a box; a report line is the sentence, not the box."""

	said = (
		"\u256d\u2500 Error \u2500\u2500\u2500\u256e\n"
		"\u2502 Missing argument 'slug'.      \u2502\n\u2570\u2500\u2500\u2500\u256f\n"
	)

	assert rehearsal.tail(said) == "Error | Missing argument 'slug'."


def test_a_command_that_asks_a_question_reads_nothing_rather_than_waiting (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path
) -> None:
	"""Run from a release at a terminal, a prompt must not wait on the operator's keyboard.

	**This process's own input holds something while it runs**, standing for that keyboard: the
	suite's is empty already, so a command that inherited it would read nothing and pass.
	"""

	here = rehearsal.Installation(tmp_path / "installation", "sqlite", None)
	reader = rehearsal.Program(
		"a reader", (sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))")
	)
	kept = os.dup(0)
	reading, writing = os.pipe()
	os.write(writing, b"typed by the operator\n")
	os.close(writing)

	try:
		os.dup2(reading, 0)
		said = here.run(reader).said

	finally:
		os.dup2(kept, 0)
		os.close(kept)
		os.close(reading)

	assert said.strip() == "''", f"a command read what was on the caller's input: {said!r}"


#: A rehearsal's database, as :func:`database_address` names one.
REHEARSED = "postgresql+psycopg:///rehearsal_0123456789ab"


def _stand_in (
	rehearsal: types.ModuleType,
	monkeypatch: pytest.MonkeyPatch,
	work: pathlib.Path,
	*,
	interrupted: bool,
) -> list[str]:
	"""Stand in for everything ``main`` runs, and return the databases it drops.

	Nothing is installed and no command runs: the PostgreSQL rehearsal records its database, and
	stands for one interrupted where asked.
	"""

	dropped: list[str] = []

	def rehearsed (
		backend: str,
		release: typing.Any,
		build: typing.Any,
		where: pathlib.Path,
		databases: list[str],
	) -> typing.Any:
		"""Record the database a PostgreSQL rehearsal makes, and stop where interrupted."""

		outcome = rehearsal.Outcome(backend)

		if backend == "postgresql":
			databases.append(REHEARSED)

			if interrupted:
				rehearsal._stop(signal.SIGINT, None)
				outcome.problems.append("it was interrupted")

		return outcome

	monkeypatch.setattr(rehearsal, "STOPPING", threading.Event())
	monkeypatch.setattr(rehearsal.shutil, "which", lambda _name: "/usr/bin/uv")
	monkeypatch.setattr(rehearsal.tempfile, "mkdtemp", lambda **_options: str(work))
	monkeypatch.setattr(
		rehearsal, "install", lambda version, _where, _uv: rehearsal.Program(version, ("true",))
	)
	monkeypatch.setattr(rehearsal, "rehearse", rehearsed)
	monkeypatch.setattr(rehearsal, "drop_database", dropped.append)

	return dropped


@pytest.mark.parametrize("keep", [True, False], ids=["--keep", "without it"])
def test_keep_leaves_the_rehearsal_s_database_and_names_what_drops_it (
	rehearsal: types.ModuleType,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	keep: bool,
) -> None:
	"""`SR#5327`, L-3 of the cold review of 2026-11-11: *Kept* beside a database already dropped.

	Every database was dropped before ``--keep`` was asked. **Under it the database is left, and
	named with the command that drops it**; without it, it is dropped and the work removed.
	"""

	work = tmp_path / "work"
	work.mkdir()
	dropped = _stand_in(rehearsal, monkeypatch, work, interrupted=False)

	assert rehearsal.main(["--from", "0.10.0", *(["--keep"] if keep else [])]) == 0

	said = capsys.readouterr().out
	named = "Kept: the PostgreSQL database rehearsal_0123456789ab"

	if keep:
		assert dropped == [], "the kept rehearsal's database was dropped"
		assert f"{named} - drop it with: dropdb rehearsal_0123456789ab" in said, said
		assert work.exists()

	else:
		assert dropped == [REHEARSED], said
		assert "Kept" not in said, said
		assert not work.exists()


def test_an_interrupted_rehearsal_puts_away_what_it_made_and_says_so (
	rehearsal: types.ModuleType,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""`SR#5327`: once interrupted, its database is dropped, its work removed, and it says so."""

	work = tmp_path / "work"
	work.mkdir()
	dropped = _stand_in(rehearsal, monkeypatch, work, interrupted=True)

	assert rehearsal.main(["--from", "0.10.0"]) == 1

	said = capsys.readouterr().out

	assert said.rstrip().endswith("Not rehearsed: it was interrupted."), said
	assert "Stopping the rehearsal and removing what it made..." in said, said
	assert dropped == [REHEARSED], said
	assert not work.exists()


def test_an_interrupted_rehearsal_starts_no_further_command (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5327`, L-3 of the cold review of 2026-11-11: both workers went on running commands.

	**Asked before every command**, including the sweep's: interrupted, the next is refused
	unrun, so each rehearsal ends within one command and its clean-up runs.
	"""

	monkeypatch.setattr(rehearsal, "STOPPING", threading.Event())
	here = rehearsal.Installation(tmp_path / "installation", "sqlite", None)
	ran = tmp_path / "ran"
	marking = rehearsal.Program(
		"a marker", (sys.executable, "-c", f"open({str(ran)!r}, 'w').close()")
	)

	here.run(marking)

	assert ran.exists(), "the command did not run before the interrupt"

	ran.unlink()
	rehearsal._stop(signal.SIGINT, None)

	with pytest.raises(rehearsal.Refused, match="interrupted"):
		here.run(marking)

	with pytest.raises(rehearsal.Refused, match="interrupted"):
		rehearsal.sweep(here, marking, "after the upgrade")

	assert not ran.exists(), "a command ran after the interrupt"


def test_a_rehearsal_s_database_is_recorded_before_it_is_made (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5327`: recorded once made, one made just before an interruption was left behind."""

	databases: list[str] = []
	recorded: list[list[str]] = []

	def made (address: str) -> None:
		"""Note what was recorded when the database is made, and fail as an unreachable server."""

		recorded.append(list(databases))

		raise rehearsal.Refused("PostgreSQL could not be reached")

	monkeypatch.setattr(rehearsal, "make_database", made)
	program = rehearsal.Program("a release", ("true",))

	with pytest.raises(rehearsal.Refused):
		rehearsal.rehearse("postgresql", program, program, tmp_path, databases)

	assert len(databases) == 1 and databases[0].startswith("postgresql"), databases
	assert recorded == [databases], "the database was made before it was recorded"


class _Interrupted:
	"""A rehearsal whose wait the operator interrupts, and which then stops within ``stops``."""

	def __init__ (self, stops: bool) -> None:
		"""Remember whether it stops when asked."""

		self.stops = stops
		self.waits: list[float | None] = []
		self.signals: list[int] = []
		self.killed = False

	def wait (self, timeout: float | None = None) -> int:
		"""Be interrupted the first time, and then stop or outlast the time given."""

		self.waits.append(timeout)

		if len(self.waits) == 1:
			raise KeyboardInterrupt

		if timeout is not None and not self.stops:
			raise subprocess.TimeoutExpired("rehearse_upgrade.py", timeout)

		return 1

	def send_signal (self, number: int) -> None:
		"""Record the signal sent."""

		self.signals.append(number)

	def kill (self) -> None:
		"""Record that it was ended."""

		self.killed = True


@pytest.mark.parametrize("stops", [True, False], ids=["it stops", "it does not"])
def test_a_release_interrupted_mid_rehearsal_gives_it_time_to_put_away (
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stops: bool
) -> None:
	"""`SR#5327`, L-3 of the cold review of 2026-11-11: the rehearsal was killed at once.

	``subprocess.run`` kills its child a quarter of a second after an interrupt, so the clean-up
	never ran. **Told to stop, and waited for**, and ended only after the time given, saying what
	may then be left.
	"""

	release = _load("release")
	child = _Interrupted(stops)
	monkeypatch.setattr(release.subprocess, "Popen", lambda *_arguments, **_options: child)

	with pytest.raises(KeyboardInterrupt):
		release._rehearse()

	said = capsys.readouterr().out

	assert child.signals == [signal.SIGINT]
	assert child.waits[:2] == [None, release.REHEARSAL_STOPS_SECONDS]
	assert child.killed is not stops
	assert ("may be left behind" in said) is not stops, said


def test_an_item_number_is_read_past_what_follows_the_answer (rehearsal: types.ModuleType) -> None:
	"""A release told that input was piped in notes, after its answer, that it read none."""

	said = (
		'{\n  "ref": 21,\n  "title": "We run PostgreSQL in production"\n}\n'
		'Anything piped in was not read.\n'
	)

	assert rehearsal.number_in(said) == 21
	assert rehearsal.number_in("Nothing is numbered here.") is None


def test_a_server_error_is_reported_in_the_problem_s_own_words (
	rehearsal: types.ModuleType
) -> None:
	"""The line is the sentence the problem document gives, not the end of its JSON."""

	body = json.dumps(
		{
			"detail": "Something went wrong that should not have.",
			"code": "internal_error",
			"instance": "/v1/me",
		}
	)
	found = rehearsal.server_errors(
		[{"who": "keanu", "path": "/v1/me", "status": 500, "detail": body}], before_upgrade=True
	)

	assert (
		found
		== [
			"1 x 500 on /v1/me, as keanu: Something went wrong that should not "
			"have. (internal_error)"
		]
	), found


def test_it_starts_from_the_newest_release_the_record_names (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The record ``scripts/release.py`` writes, whose first entry is the last release published.

	A tag nothing was published from is never in it - ``v0.6.2`` and ``v0.7.0`` are tags and not
	releases - so it is the version an operator can be running, which the newest tag may not be.
	"""

	record = tmp_path / "releases.json"
	record.write_text(
		json.dumps({"releases": [{"version": "1.2.3"}, {"version": "1.2.2"}]}), encoding="utf-8"
	)
	monkeypatch.setattr(rehearsal, "RELEASES", record)

	assert rehearsal.last_release() == "1.2.3"


def test_every_query_parameter_a_read_route_declares_is_asked () -> None:
	"""A route is asked once for each sample of each parameter it declares, so each needs one.

	**Both ways.** A parameter with no sample and no written reason is one the sweep never sends,
	and a sample or a reason naming a parameter no route declares is about something gone.
	"""

	sweep = _load("rehearse_sweep")
	declared = {
		one["name"]
		for methods in subroutine.api.app.create_app().openapi()["paths"].values()
		for one in methods.get("get", {}).get("parameters", [])
		if one["in"] == "query"
	}
	covered = set(sweep.samples("1")) | set(sweep.NOT_SAMPLED)

	assert (
		not declared - covered
	), f"asked by no sample and excused nowhere: {sorted(declared - covered)}"
	assert (
		not covered - declared
	), f"a sample or an excuse for a parameter no read route declares: {sorted(covered - declared)}"
	assert (
		not set(sweep.samples("1")) & set(sweep.NOT_SAMPLED)
	), "a parameter is both sampled and excused"


def test_every_route_taking_a_cursor_is_paged_unless_excused (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#5328`, L-3 of the cold review of 2026-11-11: a list written by hand paged six of 32.

	Fed routes made up for it: one taking a cursor is paged, one excused is not, and one taking no
	cursor is not.
	"""

	sweep = _load("rehearse_sweep")
	monkeypatch.setattr(sweep, "NOT_PAGED", {"/v1/excused": "a reason"})
	declared = {
		"/v1/listing": ["cursor", "limit"], "/v1/excused": ["cursor"], "/v1/one": ["fields"]
	}

	assert sweep.paged(declared) == ["/v1/listing"]


def test_every_route_left_unpaged_takes_a_cursor_and_the_rest_are_paged () -> None:
	"""The register against the application, and a floor under the routes paged."""

	sweep = _load("rehearse_sweep")
	declared = {
		path: [
			one["name"]
			for one in methods.get("get", {}).get("parameters", [])
			if one["in"] == "query"
		]
		for path, methods in subroutine.api.app.create_app().openapi()["paths"].items()
	}
	taking = {path for path, names in declared.items() if "cursor" in names}

	assert set(sweep.NOT_PAGED) <= taking, "an excuse for a route that takes no cursor"
	assert set(sweep.paged(declared)) == taking - set(sweep.NOT_PAGED)
	assert len(taking) > 20 and {"/v1/tasks", "/v1/documents"} <= taking, sorted(taking)


def test_a_revoked_feed_must_be_asked_and_refused (rehearsal: types.ModuleType) -> None:
	"""`SR#5328`: the revoked feed answered is a problem, refused is none, and unasked is one."""

	kept = {rehearsal.REVOKED_FEED: "sr_cal_ab12cd34_s3cr3t_x"}
	address = "/v1/calendars/ab12cd34/s3cr3t_x.ics"

	def swept (*statuses: int) -> dict[str, typing.Any]:
		"""Return a sweep that met the revoked feed's address once for each status."""

		return {
			"answers": [{"who": "keanu", "path": address, "status": one} for one in statuses],
			"never_asked": [],
		}

	assert rehearsal.sweep_problems(swept(404, 404), kept) == []
	assert rehearsal.sweep_problems(swept(404, 200), kept) == [
		"1 x the revoked calendar feed answered 200, as keanu"
	]
	assert rehearsal.sweep_problems(swept(), kept) == ["the revoked calendar feed was never asked"]
	assert rehearsal.sweep_problems(swept(), {}) == [], "a fill keeping no feed is reported already"


def test_a_route_the_sweep_never_asked_is_a_problem (rehearsal: types.ModuleType) -> None:
	"""`SR#5328`: the sweep named it and nothing read it, so a route it stopped reaching passed."""

	swept = {"answers": [], "never_asked": ["/v1/views/{key}"]}

	assert rehearsal.sweep_problems(swept, {}) == ["the sweep never asked /v1/views/{key}"]
