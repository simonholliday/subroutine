"""Build the README's screenshots of the browser from nothing - items ``#765`` and ``#3827``.

**One command, and nothing on screen is real.** It makes a throwaway instance under a home of
its own, fills it with the example company every published page uses - MetaCortex, its website
rebuild and the cast of the film, as decision ``#3728`` sets out - serves it on a free port,
signs in with a link made before the server starts, and photographs each view in both themes
with Playwright, the browser the test suite already drives. The instance is deleted afterwards.

**Isolated the way a probe is** (``#3601``): the program runs with an environment built here and
nothing inherited, so no credential or configuration of the machine it runs on can reach the
demo, and the demo cannot reach anything else. **A ``.subroutine`` marker is the exception**
(``#3940``): the program looks for one from where it runs up to ``/``, so a run is refused where
one sits above the demo's home.

**Dates are counted from the day it runs**, so the agenda holds something overdue, something
due soon and something later whenever the pictures are retaken - which is before any tag that
changes the browser.

Usage, from the checkout, for the pictures the README shows::

	python scripts/readme_shots.py docs/images

``--every-view`` photographs the agenda, the list and an item page as well, for choosing again.
"""

import argparse
import contextlib
import dataclasses
import datetime
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import typing
import urllib.error
import urllib.request
import zoneinfo

import playwright.sync_api

import subroutine.directory

#: The example company and its one project (decision ``#3728``): the workspace is `metacortex`
#: in an address and MetaCortex wherever a reader sees its name.
WORKSPACE = "metacortex"
WORKSPACE_TITLE = "MetaCortex"
INSTANCE_NAME = "MetaCortex"
PROJECT = "web"
PROJECT_TITLE = "Website rebuild"
PROJECT_DESCRIPTION = "The new company website: its pages, its blog and where it is hosted."

#: Laurence runs the instance, so ``init`` makes him, and the pictures are taken as him.
OPERATOR = "laurence"

#: The other people, by account, with the actor's full name each is shown by.
PEOPLE = {"keanu": "Keanu Reeves", "carrieanne": "Carrie-Anne Moss"}

#: The agent, which answers to Laurence because he makes it.
AGENT = "claude"

#: The size of every picture: a laptop's window, at twice the pixel density.
VIEWPORT = {"width": 1280, "height": 860}
PIXEL_DENSITY = 2
THEMES: tuple[typing.Literal["light", "dark"], ...] = ("light", "dark")


@dataclasses.dataclass(frozen=True)
class Item:
	"""One item to file, as the title a reader sees and the capture tokens around it."""

	key: str
	title: str
	tokens: str = ""
	kind: str = "task"
	description: str = ""
	due_in_days: int | None = None
	state: str = "open"


#: The work on ``web``, and the errands beside it on Laurence's own list. Titles follow the moods
#: of ``#2390``: work is an instruction, a bug a symptom, a question ends in a question mark.
ITEMS = (
	Item(
		"deploy", "Fix the deploy script", f"+{PROJECT} !4/4 ~2h @{AGENT}",
		description="Every second deploy stops at the cache step, because the build cache is warm.",
		state="started",
	),
	Item("copy", "Rewrite the home page copy", f"+{PROJECT} !3/3 @keanu", due_in_days=3),
	Item("notes", "Write the release notes", f"+{PROJECT} !3/2 @{OPERATOR}", due_in_days=6),
	Item(
		"menu", "The menu covers the logo on a phone", f"+{PROJECT} !4/4 @carrieanne", kind="bug",
		description="Below 400 pixels wide the open menu sits over the logo, so there is no way "
		"back to the home page.",
		state="started",
	),
	Item(
		"form", "The contact form sends every message twice", f"+{PROJECT} !5/4 @{AGENT}",
		kind="bug", due_in_days=1,
	),
	Item("search", "Let readers search the blog", f"+{PROJECT} !3/2 ~1d @{AGENT}", kind="feature"),
	Item(
		"storage", "Move the images to the new storage", f"+{PROJECT} !2/2 @{AGENT}", kind="chore",
	),
	Item("hero", "Compress the hero images", f"+{PROJECT} @{AGENT}", state="done"),
	Item("redirects", "Redirect the old blog addresses", f"+{PROJECT} !4/3 @{AGENT}"),
	Item(
		"comments", "Should old blog posts keep their comments?", f"+{PROJECT} @{OPERATOR}",
		kind="question",
	),
	Item(
		"certificate", "Renew the TLS certificate", f"+{PROJECT} !4/5 @{OPERATOR}",
		due_in_days=-1,
	),
	Item(
		"launch", "Launch the new site", f"+{PROJECT}", kind="milestone", due_in_days=30,
	),
	Item("phone", "Order a new phone for reception", due_in_days=1),
	Item("dojo", "Book the dojo", due_in_days=3),
	Item("gloria", "Visit Gloria", due_in_days=4),
)

#: What the launch counts, and what has to happen first.
INCLUDED_IN_LAUNCH = ("copy", "notes", "menu", "form", "search", "redirects")
BLOCKS = (("form", "notes"),)

#: The decision behind one piece of work, which the item page shows under *Read first*.
DECISION = (
	"Keep every old blog address working, each redirecting to its new page",
	"Links to the old posts are spread across other sites and search results. A redirect for "
	"each address keeps every one of them working, where a single catch-all would land readers "
	"on the home page.",
	"redirects",
)

#: What has been said on the work so far.
COMMENTS = (
	("deploy", "Reproduced: the second deploy stops at the cache step whenever the cache is warm."),
	("menu", "Happens in every browser on a phone; the open menu is drawn above the header."),
)

#: Every view there is a picture of: a name for the file, and the address after the host. The
#: item page is the one a decision governs, and its number is filled in once it has one.
VIEWS = {
	"board": f"/{WORKSPACE}/{PROJECT}?view=board",
	"agenda": f"/{WORKSPACE}",
	"list": f"/{WORKSPACE}/{PROJECT}?view=list",
	"item": f"/{WORKSPACE}/{PROJECT}/{{redirects}}",
}

#: What the README shows (decision ``#3830``): the board alone, in each reader's own theme.
README_VIEWS = ("board",)


def _program () -> str:
	"""Return the ``subroutine`` beside this interpreter, which is the checkout's own."""

	beside = pathlib.Path(sys.executable).with_name("subroutine")

	if beside.is_file():
		return str(beside)

	found = shutil.which("subroutine")

	if found is None:
		raise SystemExit("There is no 'subroutine' beside this Python or on the PATH to run.")

	return found


def _free_port () -> int:
	"""Return a port on this machine that nothing is listening on."""

	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))

		return int(probe.getsockname()[1])


def _environment (root: pathlib.Path, port: int) -> dict[str, str]:
	"""Return the whole environment the demo runs in, inheriting nothing."""

	return {
		"PATH": "/usr/bin:/bin",
		"HOME": str(root / "home"),
		"XDG_CONFIG_HOME": str(root / "config"),
		"XDG_DATA_HOME": str(root / "data"),
		"XDG_STATE_HOME": str(root / "state"),
		"XDG_CACHE_HOME": str(root / "cache"),
		"TZ": "Europe/London",
		"LANG": "C.UTF-8",
		"COLUMNS": "120",
		"SUBROUTINE_PORT": str(port),
	}


class Demo:
	"""The throwaway instance: its directory, its environment and the program that runs it."""

	def __init__ (self, root: pathlib.Path, port: int) -> None:
		"""Prepare the directories the demo lives in."""

		self.root = root
		self.port = port
		self.program = _program()
		self.environment = _environment(root, port)

		for place in ("home", "config", "data", "state", "cache"):
			(root / place).mkdir(parents=True, exist_ok=True)

	def run (self, *arguments: str) -> str:
		"""Run the program in the demo, from its home, and return what it printed."""

		# From the demo's home, which `main` has made sure has no `.subroutine` marker above it.
		ran = subprocess.run(
			[self.program, *arguments],
			cwd=self.root / "home",
			env=self.environment,
			capture_output=True,
			text=True,
			check=False,
		)

		if ran.returncode != 0:
			raise SystemExit(f"subroutine {' '.join(arguments)} failed:\n{ran.stdout}{ran.stderr}")

		return ran.stdout

	def ref (self, *arguments: str) -> int:
		"""Run a command that prints JSON, and return the number of what it made."""

		return int(json.loads(self.run(*arguments, "--json"))["ref"])


def _file (demo: Demo, item: Item, today: datetime.date) -> int:
	"""File one item, refusing if the capture grammar took any of its title as a token."""

	line = f"{item.title} {item.tokens}".strip()

	# Written as ISO, so the capture grammar reads the year rather than guessing it.
	if item.due_in_days is not None:
		line += f" by {(today + datetime.timedelta(days=item.due_in_days)).isoformat()}"

	arguments = ["add", line, "--type", item.kind]

	if item.description:
		arguments += ["--description", item.description]

	made = json.loads(demo.run(*arguments, "--json"))

	if made["title"] != item.title:
		raise SystemExit(f"{line!r} was filed as {made['title']!r}, so a word was read as a token.")

	return int(made["ref"])


def build (demo: Demo, today: datetime.date) -> dict[str, int]:
	"""Fill the demo with MetaCortex's work, and return each item's number by its key."""

	demo.run(
		"init", "--non-interactive", "--username", OPERATOR, "--workspace", WORKSPACE_TITLE,
		"--instance-name", INSTANCE_NAME,
	)

	for account, name in PEOPLE.items():
		demo.run("user", "create", account, "--name", name)

	demo.run("user", "create", AGENT, "--agent", "--name", "Claude")
	demo.run("project", "create", PROJECT, PROJECT_TITLE, "--description", PROJECT_DESCRIPTION)

	refs = {item.key: _file(demo, item, today) for item in ITEMS}

	demo.run(
		"link", str(refs["launch"]), "includes", ",".join(str(refs[key]) for key in INCLUDED_IN_LAUNCH)
	)

	for blocker, blocked in BLOCKS:
		demo.run("link", str(refs[blocker]), "blocks", str(refs[blocked]))

	title, body, governs = DECISION
	decision = demo.ref(
		"document", "create", title, "--type", "decision", "--project", PROJECT, "--body", body
	)
	demo.run("link", str(decision), "documents", str(refs[governs]))

	for item in ITEMS:
		if item.state == "started":
			demo.run("start", str(refs[item.key]))

		elif item.state == "done":
			demo.run("done", str(refs[item.key]))

	for key, said in COMMENTS:
		demo.run("comment", str(refs[key]), said)

	return refs


def _sign_in_link (demo: Demo) -> str:
	"""Make the operator a sign-in link, before the server is running."""

	printed = demo.run("login", "link")
	found = re.search(rf"http://127\.0\.0\.1:{demo.port}/signin\?link=\S+", printed)

	if found is None:
		raise SystemExit(f"'login link' printed no link for port {demo.port}:\n{printed}")

	return found.group(0)


def _serve (demo: Demo) -> subprocess.Popen[bytes]:
	"""Start the server in a session of its own, and return once it answers."""

	log = (demo.root / "serve.log").open("wb")
	server = subprocess.Popen(
		[demo.program, "serve"],
		cwd=demo.root / "home",
		env=demo.environment,
		stdin=subprocess.DEVNULL,
		stdout=log,
		stderr=subprocess.STDOUT,
		start_new_session=True,
	)
	# The server holds its own copy of the log's descriptor, so this one can go.
	log.close()
	deadline = time.monotonic() + 30

	# **Stopped here on any way out but an answer** (`#3940`): ``main`` holds the server only once
	# this returns, so a Ctrl-C during the wait left it running while its home was deleted. A
	# health check that timed out is one more try, like a refused connection.
	try:
		while time.monotonic() < deadline:
			try:
				with urllib.request.urlopen(f"http://127.0.0.1:{demo.port}/healthz", timeout=2):
					return server

			except (urllib.error.URLError, ConnectionError, TimeoutError):
				time.sleep(0.25)

	except BaseException:
		_stop(server)

		raise

	_stop(server)

	# **The log's end in the message** (`#3940`), since ``main`` deletes the directory it is in.
	said = (demo.root / "serve.log").read_text(encoding="utf-8", errors="replace")[-2000:]

	raise SystemExit(f"The demo's server did not answer. The end of its log:\n{said}")


def _stop (server: subprocess.Popen[bytes]) -> None:
	"""Stop the server and everything it started, by its own session, never by a pattern."""

	if server.poll() is None:
		os.killpg(server.pid, signal.SIGTERM)

		# **A server that will not stop is killed** (`#3940`). Waiting raised inside ``main``'s
		# ``finally``, which hid the real error, skipped removing the demo and left it running.
		try:
			server.wait(timeout=15)

		except subprocess.TimeoutExpired:
			with contextlib.suppress(ProcessLookupError):
				os.killpg(server.pid, signal.SIGKILL)

			server.wait()


def photograph (
	base: str, link: str, refs: dict[str, int], out: pathlib.Path, names: tuple[str, ...]
) -> list[pathlib.Path]:
	"""Sign in once, then photograph the views named in both themes, and return the files."""

	views = [(name, VIEWS[name].format(**refs)) for name in names]
	taken: list[pathlib.Path] = []
	failures: list[str] = []

	with playwright.sync_api.sync_playwright() as driver:
		browser = driver.chromium.launch()
		signed_in: playwright.sync_api.StorageState | None = None

		for theme in THEMES:
			context = browser.new_context(
				viewport={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
				device_scale_factor=PIXEL_DENSITY,
				color_scheme=theme,
				locale="en-GB",
				timezone_id="Europe/London",
				storage_state=signed_in,
			)
			page = context.new_page()
			page.on("pageerror", lambda error: failures.append(str(error)))

			# The link works once, so only the first theme spends it; the second reuses the session.
			if signed_in is None:
				page.goto(link)
				page.wait_for_load_state("networkidle")
				signed_in = context.storage_state()

			for name, address in views:
				page.goto(f"{base}{address}")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(750)

				picture = out / f"{name}-{theme}.png"
				page.screenshot(path=str(picture))
				taken.append(picture)

			context.close()

		browser.close()

	if failures:
		raise SystemExit("The browser reported errors while taking them:\n" + "\n".join(failures))

	return taken


def main (argv: list[str] | None = None) -> int:
	"""Build the demo, photograph it into the directory given, and remove the demo."""

	parser = argparse.ArgumentParser(description="Build the README's screenshots of the browser.")
	parser.add_argument("out", type=pathlib.Path, help="where the pictures go, e.g. docs/images")
	parser.add_argument(
		"--every-view", action="store_true", help="the agenda, the list and an item page as well"
	)
	parser.add_argument(
		"--keep", action="store_true", help="leave the demo's directory behind, to look inside"
	)
	arguments = parser.parse_args(argv)
	arguments.out.mkdir(parents=True, exist_ok=True)

	root = pathlib.Path(tempfile.mkdtemp(prefix="readme-shots-"))
	demo = Demo(root, _free_port())
	server: subprocess.Popen[bytes] | None = None
	placed: list[pathlib.Path] = []

	try:
		_refuse_a_marker_above(root / "home")

		# **The day in London** (`#3940`), where the demo and the browser keep their days: the host's
		# own clock put every due-in-N item a day out near midnight on a host elsewhere.
		refs = build(demo, datetime.datetime.now(zoneinfo.ZoneInfo("Europe/London")).date())
		link = _sign_in_link(demo)
		server = _serve(demo)
		names = tuple(VIEWS) if arguments.every_view else README_VIEWS

		# **Taken beside the demo, and moved only once nothing failed** (`#3940`): photographed
		# straight into the README's directory, a run the browser reported errors in had already
		# replaced the pictures.
		shot = root / "pictures"
		shot.mkdir()
		taken = photograph(f"http://127.0.0.1:{demo.port}", link, refs, shot, names)
		out = arguments.out.resolve()

		for picture in taken:
			placed.append(pathlib.Path(shutil.move(str(picture), str(out / picture.name))))

	finally:
		if server is not None:
			_stop(server)

		if arguments.keep:
			print(f"The demo is kept in {root}.")

		else:
			shutil.rmtree(root, ignore_errors=True)

	for picture in placed:
		print(picture)

	return 0


def _refuse_a_marker_above (home: pathlib.Path) -> None:
	"""Stop before building where a ``.subroutine`` marker sits at or above the demo's home.

	The program looks for one from where it runs up to ``/`` (`#3940`), so one in the temporary
	directory, or above it, would file the demo's items into that project instead.
	"""

	found = subroutine.directory.find(home)

	if found is not None:
		raise SystemExit(
			f"A .subroutine marker at or above {home} would file the demo into its project: "
			f"{found.path}. Move it, or set TMPDIR to a directory with none above it."
		)


if __name__ == "__main__":
	sys.exit(main())
