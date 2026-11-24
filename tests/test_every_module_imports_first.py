"""Every module imports first, in a fresh interpreter - `SR#5350`.

**Found building `SR#5294`, on 2026-11-13.** Its first patch imported ``subroutine.api.logs`` at the
top of ``subroutine.api.problems``; ``logs`` imports ``subroutine.api.app``, whose routers import
``problems``, so a process importing ``subroutine.api.policy`` first read ``subroutine.api.web``
while it was half-loaded and stopped with ``AttributeError``. The whole suite passed on both
Pythons, because every test file reaches ``app`` first but ``tests/test_browser.py``, which imports
``policy``: only the gate's browser step failed. A served instance or a script importing that
module first would have failed at start.

**One interpreter per module**, since an import is first only once a process; the cost, about a
second each, is the price Simon accepted for 1.0 (2026-11-24).
"""

import pathlib
import subprocess
import sys

import pytest

import subroutine

#: The package as this suite imports it, so an overlay's tree is the one walked.
PACKAGE = pathlib.Path(subroutine.__file__).parent

#: How long one module may take to import, far above the second each takes.
SECONDS = 120


def _modules (root: pathlib.Path, name: str) -> list[str]:
	"""Return the dotted name of every module in the package at ``root``, which is called ``name``.

	**A directory without an ``__init__.py`` is not walked**: it is no package anything imports.
	That leaves out the migrations, which only Alembic runs, inside a context it sets up first.
	"""

	found = [name]

	for path in sorted(root.iterdir()):
		if path.is_dir() and (path / "__init__.py").is_file():
			found.extend(_modules(path, f"{name}.{path.name}"))

		elif path.suffix == ".py" and path.name != "__init__.py":
			found.append(f"{name}.{path.stem}")

	return found


def _first (name: str, *, path: pathlib.Path | None = None) -> str | None:
	"""Import one module first in a fresh interpreter, and return what it raised, if anything.

	``path`` goes first on that interpreter's path, for a package written to try this out.
	"""

	probe = "import importlib, sys; importlib.import_module(sys.argv[-1])"
	command = [sys.executable, "-c", probe, name]

	if path is not None:
		command[2] = f"import sys; sys.path.insert(0, {str(path)!r}); {probe}"

	ran = subprocess.run(command, capture_output=True, text=True, timeout=SECONDS)

	return None if ran.returncode == 0 else ran.stderr


@pytest.mark.parametrize("name", _modules(PACKAGE, "subroutine"))
def test_every_module_imports_first_in_a_fresh_interpreter (name: str) -> None:
	"""Importing the module before anything else of ours works, whatever it imports in turn."""

	raised = _first(name)

	assert raised is None, f"{name} cannot be imported first:\n{raised}"


def test_the_walk_finds_the_package_and_leaves_out_the_migrations () -> None:
	"""A floor under the walk, since a walk finding nothing would pass every case it made.

	It finds the module that broke, and the commands' and the tools' entry points, and leaves
	out what only Alembic runs.
	"""

	found = _modules(PACKAGE, "subroutine")

	assert len(found) > 150, len(found)
	assert {"subroutine.api.policy", "subroutine.cli.main", "subroutine.mcp.tools"} <= set(found)
	assert not [name for name in found if name.startswith("subroutine.db.migrations")]


def test_a_module_that_cannot_be_imported_first_is_found (tmp_path: pathlib.Path) -> None:
	"""The check, fed the defect: two modules, one of which reads the other while it is half-loaded.

	``loop.first`` imports ``loop.second`` before it sets ``VALUE``, and ``loop.second`` reads
	it, so importing ``loop.first`` first stops with ``AttributeError``, while importing
	``loop.second`` first, the order every caller so far happened to use, works.
	"""

	package = tmp_path / "loop"
	package.mkdir()
	(package / "__init__.py").write_text("", encoding="utf-8")
	(package / "first.py").write_text("import loop.second\n\nVALUE = 1\n", encoding="utf-8")
	(package / "second.py").write_text(
		"import loop.first\n\nCOPY = loop.first.VALUE\n", encoding="utf-8"
	)

	assert _modules(package, "loop") == ["loop", "loop.first", "loop.second"]
	assert "AttributeError" in (_first("loop.first", path=tmp_path) or "")
	assert _first("loop.second", path=tmp_path) is None
