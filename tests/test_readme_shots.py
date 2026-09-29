"""``scripts/readme_shots.py``, which builds the README's picture of the browser - items ``#3827`` and ``#765``.

**What is worth holding is what the script puts in front of strangers**, and what ties it to the
README. The first demo for ``#765`` was built with invented names on the day the example
convention was decided (``#3728``), and nothing refused it; these tests are what refuses the next
one. Running the script needs a browser and most of a minute, so it is run by hand before a tag
that changes the browser (decision ``#3830``), and these read it rather than run it.
"""

import datetime
import importlib.util
import pathlib
import re
import signal
import subprocess
import types
import typing
import zoneinfo

import pytest

import subroutine.directory

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The accounts decision ``#3728`` allows for people: the film's cast.
CAST = {"laurence", "keanu", "carrieanne", "hugo", "gloria"}

#: Names the convention retired, and those the first demo invented before it. Whole words only,
#: so ``sample`` and ``same`` are not ``sam``.
RETIRED = ("acme", "alex", "sam", "morpheus", "thomas", "tasks.example.com")

#: A picture the README takes from this repository, by the address GitHub and PyPI both fetch.
_OURS = re.compile(
	r'(?:src|srcset)="https://raw\.githubusercontent\.com/simonholliday/subroutine/[^/"]+/'
	r'(?P<path>[^"]+)"'
)


@pytest.fixture(scope="module")
def shots () -> types.ModuleType:
	"""Load ``scripts/readme_shots.py`` by path, the way the other scripts' tests do."""

	spec = importlib.util.spec_from_file_location(
		"readme_shots", ROOT / "scripts" / "readme_shots.py"
	)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


def test_a_server_that_will_not_stop_is_killed (
	shots: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#3940`, L-10 of the cold review of 2026-09-28: waiting for it raised in main's ``finally``.

	That hid the error the run met, skipped removing the demo, and left the server running.
	**Asked to stop, then killed**, by its own process group.
	"""

	sent: list[int] = []

	class Stubborn:
		"""A server that outlives any wait with a time limit."""

		pid = 4242

		def poll (self) -> None:
			"""Still running."""

		def wait (self, timeout: float | None = None) -> int:
			"""Outlive a limited wait, and end once killed."""

			if timeout is not None:
				raise subprocess.TimeoutExpired("serve", timeout)

			return -signal.SIGKILL

	monkeypatch.setattr(shots.os, "killpg", lambda _group, sent_now: sent.append(sent_now))
	shots._stop(Stubborn())

	assert sent == [signal.SIGTERM, signal.SIGKILL], sent


def test_the_pictures_reach_their_directory_only_from_a_run_with_no_errors (
	shots: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#3940`: taken straight into the README's directory, before any error was looked at.

	**Taken beside the demo and moved once nothing failed**, and with the day counted in London,
	where the demo and the browser keep theirs, rather than on the host's clock.
	"""

	days: list[datetime.date] = []

	def taking (failing: bool) -> typing.Callable[..., list[pathlib.Path]]:
		"""Return a stand-in for ``photograph`` that takes one picture, and then fails if told to."""

		def photograph (
			_base: str, _link: str, _refs: object, out: pathlib.Path, _names: object
		) -> list[pathlib.Path]:
			"""Take one picture into ``out``."""

			picture = out / "agenda-light.png"
			picture.write_bytes(b"a picture")

			if failing:
				raise SystemExit("The browser reported errors while taking them:\nboom")

			return [picture]

		return photograph

	monkeypatch.setattr(shots.tempfile, "tempdir", str(tmp_path))
	def building (_demo: object, today: datetime.date) -> dict[str, int]:
		"""Record the day the demo is built for, and build nothing."""

		days.append(today)

		return {}

	monkeypatch.setattr(shots, "build", building)
	monkeypatch.setattr(shots, "_sign_in_link", lambda _demo: "http://127.0.0.1/link")
	monkeypatch.setattr(shots, "_serve", lambda _demo: None)
	out = tmp_path / "images"

	monkeypatch.setattr(shots, "photograph", taking(failing=True))

	with pytest.raises(SystemExit):
		shots.main([str(out)])

	assert list(out.iterdir()) == [], "a run the browser reported errors in replaced the pictures"

	before = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/London")).date()
	monkeypatch.setattr(shots, "photograph", taking(failing=False))

	assert shots.main([str(out)]) == 0
	assert [one.name for one in out.iterdir()] == ["agenda-light.png"]

	after = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/London")).date()

	assert days[-1] in {before, after}, f"the demo was dated {days[-1]}, and London is on {before}"


def test_a_marker_above_the_demo_stops_the_run_before_anything_is_built (
	shots: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#3940`: the program looks for a marker from the demo's home all the way up to ``/``.

	So one in the temporary directory, or above it, would file the demo's items into its project,
	and the script said nothing could reach the demo. **Refused before anything is built.**
	"""

	subroutine.directory.write(tmp_path, workspace="elsewhere", project="theirs")
	built: list[object] = []

	monkeypatch.setattr(shots.tempfile, "tempdir", str(tmp_path))
	def building (demo: object, _today: datetime.date) -> dict[str, int]:
		"""Record that the demo was built, which it must not be."""

		built.append(demo)

		return {}

	monkeypatch.setattr(shots, "build", building)

	with pytest.raises(SystemExit) as refused:
		shots.main([str(tmp_path / "images")])

	assert ".subroutine" in str(refused.value), refused.value
	assert built == [], "the demo was built under a marker naming another project"


def _retired_in (text: str) -> list[str]:
	"""Return the retired names that stand in ``text`` as words of their own."""

	lowered = text.lower()

	return [
		name for name in RETIRED if re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", lowered)
	]


def test_the_demo_is_metacortex_and_its_one_project (shots: types.ModuleType) -> None:
	"""The workspace, its title and the project are the ones every published example uses."""

	assert (shots.WORKSPACE, shots.WORKSPACE_TITLE, shots.PROJECT) == (
		"metacortex", "MetaCortex", "web"
	)


def test_everybody_on_the_demo_is_from_the_cast (shots: types.ModuleType) -> None:
	"""The people are the film's cast by account, and the agent is the one every example names."""

	people = {shots.OPERATOR, *shots.PEOPLE}

	assert people <= CAST, f"{sorted(people - CAST)} are not in #3728's cast"
	assert shots.AGENT == "claude"


def test_every_assignee_is_somebody_the_demo_makes (shots: types.ModuleType) -> None:
	"""An ``@name`` on an item names an account the script creates, or the add would refuse it."""

	made = {shots.OPERATOR, *shots.PEOPLE, shots.AGENT}
	named = {name for item in shots.ITEMS for name in re.findall(r"@(\w+)", item.tokens)}

	assert named, "no item is assigned to anybody, so this test reads nothing"
	assert named <= made, f"{sorted(named - made)} are assigned work and never made"


def test_the_script_names_nobody_the_convention_retired () -> None:
	"""No retired or invented name stands anywhere in the script, comments included."""

	source = (ROOT / "scripts" / "readme_shots.py").read_text(encoding="utf-8")

	assert not _retired_in(source)


def test_the_retired_name_check_can_fail () -> None:
	"""The scan finds a retired name as a word, and not inside a longer one."""

	assert _retired_in("Alex files it for Sam at tasks.example.com") == [
		"alex", "sam", "tasks.example.com"
	]
	assert _retired_in("the same sample, as an example.com address") == []


def test_every_picture_the_readme_shows_is_one_the_script_takes (shots: types.ModuleType) -> None:
	"""The README shows exactly the pictures the script takes for it, and each is in the tree.

	So renaming a view, dropping a theme or moving the directory fails here, rather than as a
	broken image on the page most visitors see first.
	"""

	readme = (ROOT / "README.md").read_text(encoding="utf-8")
	shown = {found.group("path") for found in _OURS.finditer(readme)}
	taken = {
		f"docs/images/{name}-{theme}.png"
		for name in shots.README_VIEWS
		for theme in shots.THEMES
	}

	assert shown, "the README shows no picture from this repository, so this reads nothing"
	assert shown == taken, f"the README shows {sorted(shown)} and the script takes {sorted(taken)}"

	missing = sorted(path for path in shown if not (ROOT / path).is_file())

	assert not missing, f"the README shows {missing}, which are not in the repository"
