"""``scripts/readme_shots.py``, which builds the README's picture of the browser - items ``#3827`` and ``#765``.

**What is worth holding is what the script puts in front of strangers**, and what ties it to the
README. The first demo for ``#765`` was built with invented names on the day the example
convention was decided (``#3728``), and nothing refused it; these tests are what refuses the next
one. Running the script needs a browser and most of a minute, so it is run by hand before a tag
that changes the browser (decision ``#3830``), and these read it rather than run it.
"""

import importlib.util
import pathlib
import re
import types

import pytest

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
