"""Rehearse the upgrade from the last release to this build, and refuse on a server error or a crash.

Item ``#5063``. **The suite cannot see an upgrade, by construction**: it builds its schema from the
models and seeds from the current code, so no test holds a row only an older release would have
written, and none runs this build over an older release's schema. Two defects lived in that gap
before 1.0 - a 500 on ``/v1/me`` over a database 0.10.0 had filled, met on a served instance, and
an internal error on most reads over a 0.9.x database while it waited for ``db upgrade``.

**So this does what an operator does.** It installs the last release from the package index into
a throwaway environment, makes an installation with that program and fills it through the
program's own commands - people of every standing, agents, narrowed credentials, calendar feeds,
work of every kind and state - and then meets it with this build, on SQLite and PostgreSQL at once:

1. every read route, as every credential the installation holds, **before** ``db upgrade`` -
   where an operator's service is serving reads from the moment the package is replaced;
2. ``db upgrade``;
3. every read again, after it;
4. the terminal's commands, reads and writes, as somebody does the morning after.

**It refuses on a server error, a crash, or a step that did not work** - a read answered 5xx (bar
``/readyz``'s 503 before the upgrade, which is it doing its job), an upgrade that failed, a
command that crashed or that this build does not take as written, and a step of the fill the
release could not do. A refusal is any other answer and is not a fault: the commands are every
kind of person's, and a viewer is meant to be turned away.

    python scripts/rehearse_upgrade.py                  # from the newest release in docs/releases.json
    python scripts/rehearse_upgrade.py --from 0.9.13    # from another
    python scripts/rehearse_upgrade.py --build <dir>    # rehearse another checkout's src

``scripts/release.py`` runs it before it writes anything. It needs ``uv`` and the package index,
and PostgreSQL where the suite finds it (``SUBROUTINE_TEST_POSTGRES_ADMIN_URL``); it takes about
half an hour, and keeps nothing unless ``--keep`` is given.

**The fill is written for the last release**, so it uses only commands that release has. When a
release removes one, the rehearsal after it refuses at the step that uses it, which is the moment
to change this file.
"""

import argparse
import concurrent.futures
import dataclasses
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import typing
import uuid

import sqlalchemy

#: The repository, resolved from this file - it is run from wherever somebody is standing.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The published record of every release, newest first, which ``scripts/release.py`` writes. Its
#: first entry is the last release - and a tag nothing was published from is never in it.
RELEASES = ROOT / "docs" / "releases.json"

#: The reading half, run under the build: it imports that build, and this must not.
SWEEP = ROOT / "scripts" / "rehearse_sweep.py"

#: Where PostgreSQL is, as the suite finds it, so a machine that can run the gate can run this.
POSTGRES_ADMIN_URL = os.environ.get(
	"SUBROUTINE_TEST_POSTGRES_ADMIN_URL", "postgresql+psycopg:///postgres"
)

#: A credential as the program prints one, so none is ever printed back.
TOKEN = re.compile(r"sr_[A-Za-z0-9]+_[A-Za-z0-9_.-]+")

#: The box-drawing characters a refusal's panel is drawn with, which say nothing in a report.
BORDERS = re.compile(r"[\u2500-\u257f]+")

#: How long one command may take before it counts as hung, and one sweep.
COMMAND_SECONDS = 300
SWEEP_SECONDS = 3600


@dataclasses.dataclass(frozen=True)
class Program:
	"""A ``subroutine`` to run commands with: the release, or the build being rehearsed."""

	#: What a report calls it.
	name: str

	#: The argument vector that starts it.
	command: tuple[str, ...]

	#: A build's ``src``, put first on the path; ``None`` for the release, which is installed.
	source: pathlib.Path | None = None


class Refused (Exception):
	"""The rehearsal could not be held at all, with the sentence that says why."""


@dataclasses.dataclass(frozen=True)
class Ran:
	"""One command's exit code and everything it printed."""

	code: int
	said: str


class Installation:
	"""One installation: its own home and data directories, its database, and what it was given.

	**Every command runs with an environment built from nothing** but the path, so neither
	program can read this machine's configuration or a credential in it - the reason a probe of
	this kind once reached a served instance with a real one (`#3601`).
	"""

	def __init__ (self, root: pathlib.Path, backend: str, database_url: str | None) -> None:
		"""Make its directories; the database, if it has one, already exists."""

		self.root = root
		self.backend = backend
		self.database_url = database_url
		self.kept: dict[str, typing.Any] = {}

		(root / "home").mkdir(parents=True)

	def environment (self, program: Program, extra: dict[str, str] | None = None) -> dict[str, str]:
		"""Return the whole environment of one command."""

		built = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(self.root / "home"),
			"XDG_CONFIG_HOME": str(self.root / "config"),
			"XDG_DATA_HOME": str(self.root / "data"),
			"XDG_STATE_HOME": str(self.root / "state"),
			"SUBROUTINE_LOCAL_USER": "keanu",
			"COLUMNS": "200",
			"NO_COLOR": "1",
			"LANG": "C.UTF-8",
			**(extra or {}),
		}

		if self.database_url is not None:
			built["SUBROUTINE_DATABASE_URL"] = self.database_url

		if program.source is not None:
			built["PYTHONPATH"] = str(program.source)

		return built

	def run (
		self,
		program: Program,
		*arguments: str,
		stdin: str | None = None,
		extra: dict[str, str] | None = None,
	) -> Ran:
		"""Run one command with one program, write it to this installation's log, and return it."""

		try:
			# **No input is nothing to read**, never the caller's: run from a release at a terminal,
			# a command that asked a question would otherwise wait on the operator's keyboard. Not
			# an empty pipe either - a program told something was piped in says so, after its answer.
			ran = subprocess.run(
				[*program.command, *arguments],
				env=self.environment(program, extra),
				input=stdin,
				stdin=subprocess.DEVNULL if stdin is None else None,
				capture_output=True,
				text=True,
				timeout=COMMAND_SECONDS,
				cwd=self.root / "home",
			)
			result = Ran(ran.returncode, ran.stdout + ran.stderr)

		except subprocess.TimeoutExpired:
			result = Ran(124, f"It did not finish within {COMMAND_SECONDS} seconds.")

		with (self.root / "commands.log").open("a", encoding="utf-8") as log:
			log.write(f"\n$ [{program.name}] subroutine {' '.join(arguments)}\n[exit {result.code}]\n")
			log.write(masked(result.said))

		return result

	def crash_reports (self) -> set[pathlib.Path]:
		"""Return every crash report either program has written here."""

		return set((self.root / "state").glob("**/crash-*.txt"))


@dataclasses.dataclass
class Outcome:
	"""What one backend's rehearsal did, and every reason it gives to refuse."""

	backend: str
	lines: list[str] = dataclasses.field(default_factory=list)
	problems: list[str] = dataclasses.field(default_factory=list)


def masked (said: str) -> str:
	"""Return ``said`` with every credential in it cut to its scheme."""

	return TOKEN.sub("sr_…", said)


def tail (said: str, width: int = 400) -> str:
	"""Return the end of what a command said, on one line, masked, without a panel's borders."""

	flat = " | ".join(line.strip() for line in BORDERS.sub(" ", masked(said)).splitlines() if line.strip())

	return re.sub(r" {2,}", " ", flat)[-width:]


def number_in (said: str) -> int | None:
	"""Return the number of the item a ``--json`` answer describes, or ``None``.

	The object is read where it starts and no further, because a program may say something
	after it - a release told that input was piped in notes it was not read.
	"""

	try:
		answer, _end = json.JSONDecoder().raw_decode(said[said.index("{"):])

		return int(answer.get("ref") or answer["item"]["ref"])

	except (ValueError, KeyError, TypeError, AttributeError):
		return None


def problem_said (detail: str) -> str:
	"""Return the sentence an answer's problem document gives, or the end of the answer.

	A server error here is the application's own problem document, whose ``detail`` is the
	sentence to read; anything else is shown as it ended.
	"""

	try:
		problem = json.loads(detail)

		return f"{problem['detail']} ({problem.get('code', 'no code')})"

	except (ValueError, KeyError, TypeError):
		return tail(detail, 300)


def last_release () -> str:
	"""Return the newest version the published record names."""

	record = json.loads(RELEASES.read_text(encoding="utf-8"))

	return str(record["releases"][0]["version"])


def server_errors (answers: list[dict[str, typing.Any]], *, before_upgrade: bool) -> list[str]:
	"""Return one line per kind of server error among the answers, the commonest first.

	**Before the upgrade, ``/readyz`` answering 503 is not one**: it is the probe saying the
	database is behind, which is what it is for. Every other 5xx is, there as after: an
	instance serves reads while it waits for ``db upgrade``, and a read it cannot honour is
	refused by name rather than answered with an internal error (`#5057`).

	A path's numbers are folded together, so one fault met on every item is one line, naming
	everybody who met it and the first answer's own words.
	"""

	grouped: dict[tuple[int, str], list[dict[str, typing.Any]]] = {}

	for answer in answers:
		status = int(answer["status"])
		path = str(answer["path"]).split("?", 1)[0]

		if status < 500 or (before_upgrade and path == "/readyz" and status == 503):
			continue

		shape = re.sub(r"/\d+(?=/|$)", "/N", path)
		grouped.setdefault((status, shape), []).append(answer)

	lines = []

	for (status, shape), met in sorted(grouped.items(), key=lambda item: -len(item[1])):
		who = ", ".join(sorted({str(answer["who"]) for answer in met}))
		detail = problem_said(str(met[0].get("detail", "")))
		lines.append(f"{len(met)} x {status} on {shape}, as {who}: {detail}")

	return lines


def crashed (ran: Ran) -> bool:
	"""Say whether a command crashed, rather than answering or refusing.

	Exit 0 is an answer and exit 1 a refusal, which these commands are meant to meet. Anything
	else is a crash - a traceback, the program's own *should not have* report - or exit 2, which
	is the command line refusing the command as written: the steps here live beside the build,
	so that is this file falling behind it, and worth stopping a release to fix.
	"""

	return ran.code not in (0, 1) or "Traceback (most recent call last)" in ran.said or "should not have" in ran.said


def install (version: str, work: pathlib.Path, uv: str) -> Program:
	"""Install one release from the package index into an environment of its own, and return it."""

	environment = work / f"release-{version}"
	python = environment / "bin" / "python"

	for step in (
		[uv, "venv", "--quiet", "--python", sys.executable, str(environment)],
		[uv, "pip", "install", "--quiet", "--python", str(python), f"subroutine[postgres]=={version}"],
	):
		ran = subprocess.run(step, capture_output=True, text=True, check=False)

		if ran.returncode != 0:
			raise Refused(
				f"{version} could not be installed from the package index: {tail(ran.stdout + ran.stderr)}"
			)

	return Program(version, (str(environment / "bin" / "subroutine"),))


def make_database () -> str:
	"""Make an empty PostgreSQL database for one rehearsal, and return its address.

	**Rendered with the password shown**, because a URL's ``str()`` masks it and the result then
	authenticates as the literal ``***`` - invisible on a machine with peer authentication.
	"""

	name = f"rehearsal_{uuid.uuid4().hex[:12]}"
	engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	try:
		with engine.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

	except sqlalchemy.exc.SQLAlchemyError as failure:
		raise Refused(f"PostgreSQL could not be reached at {POSTGRES_ADMIN_URL}: {failure}") from failure

	finally:
		engine.dispose()

	address = sqlalchemy.engine.make_url(POSTGRES_ADMIN_URL).set(database=name)

	return address.render_as_string(hide_password=False)


def drop_database (address: str) -> None:
	"""Drop one rehearsal's database, ending anything still connected to it."""

	name = sqlalchemy.engine.make_url(address).database
	engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	try:
		with engine.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

	finally:
		engine.dispose()


def fill (here: Installation, release: Program) -> list[str]:
	"""Make and fill the installation through the release's own commands, and say what failed.

	Everything is done the way somebody using that release would do it, so every row is one it
	wrote. What each step printed that later steps need - a credential, an item's number - is
	kept, and handed to the sweep. A step that fails is a reason to refuse: an installation
	missing what this was meant to make proves less than the report would say.
	"""

	failed: list[str] = []
	today = datetime.date.today()

	def on (days: int) -> str:
		"""Return a day ``days`` from today, written so no release can misread it."""

		return (today + datetime.timedelta(days=days)).isoformat()

	def step (*arguments: str, stdin: str | None = None, keep: str | None = None, may_fail: bool = False) -> str:
		"""Run one step with the release, keeping the credential it prints under ``keep``."""

		ran = here.run(release, *arguments, stdin=stdin)

		if ran.code != 0 and not may_fail:
			failed.append(f"{' '.join(arguments)} -> exit {ran.code}: {tail(ran.said, 300)}")

		if keep is not None:
			found = TOKEN.findall(ran.said)

			if found:
				here.kept[keep] = found[-1]

			else:
				failed.append(f"{' '.join(arguments)} printed no credential")

		return ran.said

	def numbered (*arguments: str, stdin: str | None = None) -> int:
		"""Run a step that makes an item, and return the item's number."""

		found = number_in(step(*arguments, "--json", stdin=stdin))

		if found is None:
			failed.append(f"{' '.join(arguments)} printed no number")

			return 0

		return found

	def feed (title: str, *flags: str, keep: str) -> None:
		"""Make a calendar feed and keep its address, which needs the instance's own address."""

		said = step("calendar", "create", title, *flags)
		found = re.search(r"calendar reset ([0-9a-f]+)", said)

		if found is None:
			failed.append(f"calendar create {title} printed no reference")

			return

		shown = here.run(
			release, "calendar", "reset", found.group(1),
			extra={"SUBROUTINE_PUBLIC_URL": "https://nebuchadnezzar.example"},
		).said
		address = re.search(r"/v1/calendars/([0-9a-f]+)/([A-Za-z0-9_-]+)\.ics", shown)

		if address is None:
			failed.append(f"calendar reset for {title} printed no address")

			return

		here.kept[keep] = f"sr_cal_{address.group(1)}_{address.group(2)}"

	# The installation, with a password the release stored and nothing signs in with. Nothing
	# after a failed ``init`` can work, so that is the one failure said.
	step(
		"init", "--username", "keanu", "--workspace", "Metacortex", "--instance-name", "Nebuchadnezzar",
		"--password-stdin", "--non-interactive", stdin="follow-the-white-rabbit\n",
	)

	if failed:
		return failed

	# People, of every standing.
	step(
		"user", "create", "carrieanne", "--name", "Carrie-Anne Moss", "--email",
		"carrieanne@metacortex.example", "--role", "admin", "--terminal", keep="carrieanne",
	)
	step("user", "create", "hugo", "--terminal", keep="hugo")
	step("user", "create", "gloria", "--role", "viewer", "--browser")
	step("user", "create", "laurence", "--superuser", "--terminal", keep="laurence")
	step("user", "create", "joe", "--role", "viewer", "--terminal", keep="joe")
	step("user", "create", "marcus", "--terminal", keep="marcus")
	step("user", "create", "tank")
	step("user", "timezone", "Europe/London")
	step("instance", "update", "--timezone", "Europe/London")

	# A second workspace, and a third that ends in the trash.
	step("workspace", "create", "zion", "Zion", "--description", "The last city", "--timezone", "Europe/London")
	step("user", "add", "hugo", "-w", "zion", "--role", "member")
	step("user", "add", "marcus", "-w", "zion", "--role", "admin")
	step("workspace", "create", "construct", "The Construct")
	step("use", "metacortex")

	# Projects: nested, private, shared, private and shared with nobody, archived, coloured.
	step("project", "create", "web", "Website rebuild", "--description", "The website rebuild")
	step("project", "create", "api", "API", "--parent", "web")
	step("project", "create", "vault", "Vault", "--private")
	step("project", "share", "vault", "carrieanne")
	step("project", "create", "den", "Den", "--private")
	step("project", "create", "old-launch", "Old launch")
	step("project", "update", "old-launch", "--status", "archived")
	step("project", "update", "web", "--colour", "teal", "--hide-status", "blocked")
	step("project", "prioritise", "web")

	# Agents and credentials, of every narrowing the release offers.
	step("agent", "create", "claude", "--workspace", "metacortex", keep="claude")
	step("agent", "create", "scout", "--workspace", "metacortex", "--profile", "worker", "--project", "web", keep="scout")
	step(
		"agent", "create", "reporter", "--workspace", "metacortex", "--profile", "observer",
		"--expires", "now+30d", keep="reporter",
	)
	step(
		"agent", "create", "sam", "--workspace", "metacortex", "--profile", "collaborator",
		"--project", "web", "--project", "vault", "--write", "web", keep="sam",
	)
	step("project", "share", "vault", "sam")
	step("token", "create", "--title", "ci, reading", "--scope", "task:read", "--scope", "comment:read", keep="ci")
	step(
		"token", "create", "--title", "a scope this release had", "--scope", "token:admin",
		"--scope", "task:read", keep="tokenadmin",
	)
	step(
		"token", "create", "--service-account", "legacy-bot", "--workspace", "metacortex",
		"--title", "legacy bot", keep="legacy",
	)
	step(
		"token", "create", "--username", "hugo", "--workspace", "metacortex", "--scope", "task:read",
		"--scope", "comment:write", "--title", "hugo narrowed", keep="hugo-narrowed",
	)
	step("token", "create", "--title", "web only", "--project", "web", "--write", "web", keep="webonly")
	step("token", "create", "--title", "keanu's own", keep="keanu")
	step("token", "create", "--title", "pinned to zion", "--username", "hugo", "--workspace", "zion", keep="hugo-zion")
	step("token", "create", "--title", "to revoke", keep="revoked")
	step("token", "revoke", str(here.kept.get("revoked", "sr_none_x")).split("_")[1])

	# An agent that answers to somebody who then leaves.
	step("agent", "create", "morpheus-bot", "--workspace", "zion", keep="morpheus-bot")
	step("user", "transfer", "morpheus-bot", "--to", "marcus", may_fail=True)
	step("user", "deactivate", "marcus", "--yes")

	# Calendar feeds and sign-in links.
	feed("Work", "-w", "metacortex", keep="feed-work")
	feed("Mine on the web", "-w", "metacortex", "--mine", "--project", "web", "--expires", "now+90d", keep="feed-mine")
	feed("To stop", "-w", "metacortex", keep="feed-stop")
	step("login", "link")
	step("login", "link", "--username", "gloria")

	# Work, of every kind and in every state.
	refs = {
		"dentist": numbered("add", "Ring the dentist tomorrow at 2pm ~30m !4/2 #errand"),
		"standup": numbered("add", "Standup tomorrow at 9:30am", "--repeat", "every day"),
		"deploy": numbered("add", "+web Fix the deploy script @hugo !5/4 ~2h"),
		"pricing": numbered("add", f"+web Rebuild the pricing page by {on(9)}"),
		"versions": numbered("add", "+api Version the endpoints"),
		"pill": numbered("add", "+vault Take the red pill"),
		"cat": numbered("add", "+den Feed the cat"),
		"bill": numbered("add", f"Pay the phone bill on {on(30)}"),
		"plants": numbered("add", "Water the plants tomorrow", "--repeat", "every 3 days", "--repeat-from", "completion"),
		"bug": numbered("add", "Deploy is broken on staging", "--type", "bug", "--description", "It answers 502 after the release."),
		"question": numbered("add", "Which host do we move to?", "--type", "question"),
		"launch": numbered("add", "Launch the new site", "--type", "milestone"),
		"event": numbered("add", f"Team lunch on {on(3)} at 1pm til 2pm", "--type", "event"),
		"trashed": numbered("add", "Buy a new phone"),
		"restored": numbered("add", "Renew the passport"),
	}
	refs["child"] = numbered("add", "+web Write the redirects", "--under", str(refs["pricing"]))

	step("start", str(refs["deploy"]))
	step("claim", str(refs["deploy"]))
	step("comment", str(refs["deploy"]), f"The script fails on the second host. See #{refs['bug']}.")
	step("done", str(refs["dentist"]))
	step("done", str(refs["plants"]))
	step("skip", str(refs["standup"]), "--because", "A bank holiday")
	step("plan", str(refs["pricing"]), on(4), "--until", on(40))
	step("defer", str(refs["bill"]), on(20), "--because", "Waiting for the statement")
	step("link", str(refs["bug"]), "blocks", str(refs["deploy"]))
	step("link", str(refs["launch"]), "includes", f"{refs['pricing']},{refs['versions']}")
	step("link", str(refs["pricing"]), "precedes", str(refs["versions"]))
	step(
		"update", str(refs["versions"]), "--estimate", "3h", "--tags", "api,release", "--assignee", "carrieanne",
		"--importance", "3", "--urgency", "5", "--remind", "1d", "--because", "The partner integration needs it",
	)
	step("update", str(refs["question"]), "--status", "blocked")
	step("update", str(refs["cat"]), "--due", on(6), "--timezone", "America/New_York")
	step("update", str(refs["standup"]), "--title", "Daily standup", "--from-now-on")
	step("move", str(refs["child"]), "--top")
	step("move", str(refs["child"]), "--under", str(refs["pricing"]))
	step("delete", str(refs["trashed"]))
	step("delete", str(refs["restored"]))
	step("restore", str(refs["restored"]))

	refs["decision"] = numbered(
		"document", "create", "We run PostgreSQL in production", "--type", "decision", "--binds", "workspace",
		"--body", "Decided at the start. SQLite stays for one machine.", "--no-body",
	)
	refs["note"] = numbered(
		"document", "create", "Notes on the deploy", "--type", "note", "--project", "web", "--body", "-", "--no-body",
		stdin=f"The second host has an older kernel.\n\nSee #{refs['deploy']}.\n",
	)
	step("document", "edit", str(refs["note"]), "--body", "The second host has an older kernel, and is being replaced.")
	numbered(
		"document", "create", "What the redirects must keep", "--type", "spec", "--parent", str(refs["decision"]),
		"--body", "Every old address.", "--no-body",
	)
	refs["draft"] = numbered(
		"document", "create", "A dead end: moving to another host", "--type", "dead_end", "--status", "draft",
		"--body", "Not now.", "--no-body",
	)
	step("comment", str(refs["decision"]), "Carried at the first meeting.")
	step("link", str(refs["decision"]), "documents", str(refs["deploy"]))

	step("view", "save", "Urgent on the web", "--q", "+web urgency>3", "--as", "board", "--group-by", "status_category", "--shared")
	step("view", "save", "Mine", "--q", "assignee:me", "--order", "-priority_score")

	# Work in the second workspace, and the third into the trash.
	step("use", "zion")
	refs["zion_task"] = numbered("add", "Hold the dock")
	step("comment", str(refs["zion_task"]), "The machines are digging.")
	step("use", "metacortex")
	step("workspace", "delete", "construct", "--yes")

	# What an operator does.
	step("db", "backup")
	step("whoami")
	step("doctor", may_fail=True)

	here.kept["refs"] = refs

	return failed


def drive (here: Installation, build: Program) -> tuple[int, list[str], int]:
	"""Use the upgraded installation through the build's terminal commands, as the morning after.

	Reads first, then the ordinary day's writes, then what an administrator does. Returns how many
	commands ran, the crashes among them, and how many were refused - which is expected, and not
	counted against the build.
	"""

	refs: dict[str, int] = here.kept.get("refs", {})
	crashes: list[str] = []
	ran_count = 0
	refused = 0

	def command (*arguments: str, extra: dict[str, str] | None = None) -> str:
		"""Run one command with the build and sort it into answered, refused or crashed."""

		nonlocal ran_count, refused

		ran_count += 1
		ran = here.run(build, *arguments, extra=extra)

		if crashed(ran):
			crashes.append(f"{' '.join(arguments)} -> exit {ran.code}: {tail(ran.said)}")

		elif ran.code != 0:
			refused += 1

		return ran.said

	def ref (name: str) -> str:
		"""Return the number of an item the fill made, as a command takes it."""

		return str(refs.get(name, 0))

	arguments: tuple[str, ...]

	# Reads, as the founder at their own terminal.
	for arguments in (
		("whoami",), ("agenda",), ("agenda", "tomorrow"), ("list",), ("list", "--limit", "100"),
		("list", "--project", "web"), ("list", "--json"), ("list", "--ready"), ("list", "--to-act-on"),
		("search", "deploy"), ("search", "type:bug urgency>3"), ("journal",), ("changes",),
		("project", "list"), ("project", "sharing", "vault"), ("project", "sharing", "den"),
		("project", "settings", "web"), ("workspace", "list"), ("workspace", "settings", "metacortex"),
		("instance", "workspaces"), ("instance", "projects"), ("user", "list"),
		("user", "list", "--workspace", "metacortex"), ("user", "list", "--workspace", "zion"),
		("token", "list"), ("calendar", "list"), ("view", "list"), ("view", "run", "urgent-on-the-web"),
		("view", "run", "mine"), ("config", "show"), ("db", "current"), ("db", "backups"), ("doctor",),
	):
		command(*arguments)

	for number in sorted(set(refs.values())):
		command("show", str(number))
		command("show", str(number), "--json")

	command("export", str(here.root / "export-before"))

	# The ordinary day's writes.
	made = str(number_in(command("add", "Check the upgrade went through tomorrow ~15m !3/3 #upgrade", "--json")) or 0)
	later = (datetime.date.today() + datetime.timedelta(days=21)).isoformat()

	for arguments in (
		("start", made), ("claim", made), ("comment", made, f"Written after the upgrade. See #{ref('deploy')}."),
		("update", made, "--estimate", "30m", "--tags", "upgrade,ops", "--assignee", "hugo"),
		("plan", made, "friday"), ("defer", made, later, "--because", "After the release"),
		("link", made, "relates-to", ref("deploy")), ("unlink", made, ref("deploy"), "--type", "relates-to"),
		("update", made, "--project", "web"), ("move", made, "--under", ref("pricing")), ("move", made, "--top"),
		("release", made),
		("stop", made), ("done", made), ("delete", made), ("restore", made), ("restore", ref("trashed")),
		("done", ref("standup")), ("skip", ref("standup"), "--because", "Rehearsal"), ("done", ref("deploy")),
		("update", ref("question"), "--status", "open"),
		("update", ref("versions"), "--project", "web", "--because", "Moved after the upgrade"),
		("comment", ref("decision"), "Still in force after the upgrade."),
		("document", "edit", ref("note"), "--body", "Revised after the upgrade."),
	):
		command(*arguments)

	# What an administrator does.
	for arguments in (
		("project", "create", "ops", "Operations", "--description", "Keeping it running"),
		("project", "create", "safe", "Safe", "--private"), ("project", "share", "safe", "hugo"),
		("project", "unshare", "safe", "hugo"), ("project", "share", "den", "carrieanne"),
		("project", "unshare", "vault", "sam"), ("project", "update", "old-launch", "--status", "active"),
		("project", "update", "den", "--public"), ("project", "update", "den", "--private"),
		("project", "rename", "ops", "operations", "--yes"), ("project", "move", "api", "--root", "--yes"),
		("project", "move", "api", "--under", "web", "--yes"), ("project", "prioritise", "api"),
		("user", "create", "nona", "--role", "member", "--terminal", "-w", "metacortex"),
		("user", "create", "helmut", "--role", "viewer", "--browser", "-w", "metacortex"),
		("user", "add", "nona", "-w", "zion", "--role", "member"), ("user", "role", "nona", "admin", "-w", "zion"),
		("user", "remove", "nona", "-w", "zion"), ("user", "role", "joe", "member", "-w", "metacortex"),
		("user", "reactivate", "marcus"), ("user", "deactivate", "marcus", "--yes"),
		("user", "deactivate", "tank", "--yes"), ("user", "reactivate", "tank"),
		("user", "transfer", "legacy-bot", "--to", "carrieanne"), ("user", "transfer", "morpheus-bot", "--to", "hugo"),
		("agent", "create", "trinity-bot", "--workspace", "metacortex"),
		("agent", "create", "pinned-bot", "--workspace", "zion", "--pin"),
		("agent", "create", "claude", "--workspace", "metacortex", "--profile", "worker", "--project", "web"),
		("token", "create", "--title", "an old scope, asked for now", "--scope", "token:admin"),
		("token", "create", "--title", "after the upgrade", "--scope", "task:read"),
		("token", "create", "--username", "hugo", "--title", "hugo, after"),
		("token", "create", "--service-account", "legacy-bot", "--title", "legacy, after", "--workspace", "metacortex"),
		("calendar", "create", "After the upgrade", "-w", "metacortex"), ("login", "link"),
		("login", "link", "--username", "gloria"), ("login", "revoke", "gloria"),
		("workspace", "restore", "construct"), ("workspace", "update", "construct", "--description", "Loading"),
		("workspace", "rename", "construct", "loading", "--yes"), ("workspace", "delete", "loading", "--yes"),
		("workspace", "create", "sublevel", "Sublevel"), ("view", "save", "After", "--q", "tag:upgrade", "--as", "list"),
		("view", "edit", "after", "--q", "tag:ops"), ("view", "forget", "after"),
		("instance", "update", "--name", "Nebuchadnezzar II"), ("user", "timezone", "America/New_York"),
		("db", "backup"), ("db", "upgrade", "--yes"), ("whoami",), ("agenda",), ("list", "--limit", "100"),
		("journal",), ("changes",), ("doctor",),
	):
		command(*arguments)

	command("db", "archive", extra={"SUBROUTINE_EVENTS_RETENTION_DAYS": "30"})
	command("export", str(here.root / "export-after"))

	return ran_count, crashes, refused


def sweep (here: Installation, build: Program, label: str) -> list[dict[str, typing.Any]]:
	"""Ask every read route of the installation under the build, and return what each answered."""

	kept = here.root / "kept.json"
	answers = here.root / f"answers-{label}.json"
	kept.write_text(json.dumps(here.kept), encoding="utf-8")

	try:
		ran = subprocess.run(
			[sys.executable, str(SWEEP), str(kept), str(answers)],
			env=here.environment(build),
			stdin=subprocess.DEVNULL,
			capture_output=True,
			text=True,
			timeout=SWEEP_SECONDS,
			cwd=here.root / "home",
		)

	except subprocess.TimeoutExpired as late:
		raise Refused(f"the reads {label} did not finish within {SWEEP_SECONDS} seconds") from late

	if ran.returncode != 0:
		raise Refused(f"the reads {label} could not be made: {tail(ran.stdout + ran.stderr, 1500)}")

	return typing.cast(list[dict[str, typing.Any]], json.loads(answers.read_text(encoding="utf-8"))["answers"])


def rehearse (backend: str, release: Program, build: Program, work: pathlib.Path, databases: list[str]) -> Outcome:
	"""Fill one installation with the release, then meet it with the build, on one backend."""

	outcome = Outcome(backend)
	database_url = None

	if backend == "postgresql":
		database_url = make_database()
		databases.append(database_url)

	here = Installation(work / backend, backend, database_url)

	try:
		failed = fill(here, release)
		outcome.lines.append(f"filled by {release.name}: {len(failed)} step(s) failed")
		outcome.problems += [f"{release.name} could not: {line}" for line in failed]
		before = here.crash_reports()

		answers = sweep(here, build, "before the upgrade")
		errors = server_errors(answers, before_upgrade=True)
		outcome.lines.append(f"before the upgrade: {len(answers):,} reads, {len(errors)} kind(s) of server error")
		outcome.problems += [f"before the upgrade, {line}" for line in errors]

		upgraded = here.run(build, "db", "upgrade", "--yes")
		outcome.lines.append(f"db upgrade: exit {upgraded.code}")

		if upgraded.code != 0:
			outcome.problems.append(f"db upgrade failed: {tail(upgraded.said)}")

			return outcome

		answers = sweep(here, build, "after the upgrade")
		errors = server_errors(answers, before_upgrade=False)
		outcome.lines.append(f"after the upgrade: {len(answers):,} reads, {len(errors)} kind(s) of server error")
		outcome.problems += [f"after the upgrade, {line}" for line in errors]

		count, crashes, refused = drive(here, build)
		reports = sorted(here.crash_reports() - before)
		outcome.lines.append(
			f"the terminal, after: {count} commands, {refused} refused, {len(crashes)} crashed, "
			f"{len(reports)} crash report(s)"
		)
		outcome.problems += [f"the terminal crashed: {line}" for line in crashes]
		outcome.problems += [f"a crash report was written: {tail(path.read_text(encoding='utf-8'), 600)}" for path in reports]

	except Refused as refusal:
		outcome.problems.append(str(refusal))

	return outcome


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Read which release to rehearse from, which build, and whether to keep what was made."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
	parser.add_argument("--from", dest="release", help="the release to upgrade from (default: the newest published)")
	parser.add_argument("--build", type=pathlib.Path, default=ROOT, help="the checkout to rehearse (default: this one)")
	parser.add_argument("--keep", action="store_true", help="keep the installations and their logs, and say where")

	return parser.parse_args(argv)


def main (argv: list[str] | None = None) -> int:
	"""Rehearse the upgrade on both backends at once, report, and exit non-zero on any problem."""

	arguments = _arguments(argv)
	version = arguments.release or last_release()
	build = Program("this build", (sys.executable, "-m", "subroutine"), source=(arguments.build / "src").resolve())
	uv = shutil.which("uv")

	if uv is None:
		print("Not rehearsed: 'uv' is not on the path, and it is what installs the release to upgrade from.")

		return 1

	print(f"Rehearsing the upgrade from {version} to {arguments.build.resolve()}, on SQLite and PostgreSQL.")
	print("It installs that release, fills an installation with it and meets it with this build: about half an hour.", flush=True)

	work = pathlib.Path(tempfile.mkdtemp(prefix="subroutine-rehearsal-"))
	databases: list[str] = []
	outcomes: list[Outcome] = []

	try:
		release = install(version, work, uv)

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			outcomes = list(pool.map(lambda backend: rehearse(backend, release, build, work, databases), ("sqlite", "postgresql")))

	except Refused as refusal:
		print(f"Not rehearsed: {refusal}")

		return 1

	finally:
		for address in databases:
			try:
				drop_database(address)

			except sqlalchemy.exc.SQLAlchemyError as failure:
				print(f"The rehearsal's database {sqlalchemy.engine.make_url(address).database} could not be dropped: {failure}")

		if arguments.keep:
			print(f"Kept: {work}")

		else:
			shutil.rmtree(work, ignore_errors=True)

	problems = [f"{outcome.backend}: {problem}" for outcome in outcomes for problem in outcome.problems]

	for outcome in outcomes:
		print(f"\n{outcome.backend}")

		for line in outcome.lines:
			print(f"  {line}")

	if problems:
		print(f"\nThe upgrade from {version} failed the rehearsal, {len(problems)} time(s):")

		for problem in problems:
			print(f"  - {problem}")

		return 1

	print(f"\nThe upgrade from {version} passed the rehearsal: no server error, no crash, every step done.")

	return 0


if __name__ == "__main__":
	sys.exit(main())
