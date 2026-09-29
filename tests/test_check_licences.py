"""The licence check, at the two places it passed having looked at nothing - `SR#3940`.

L-8 of the cold review of 2026-09-28. ``scripts/check_licences.py`` reads every runtime
dependency's licence out of the installed metadata, so a package that says nothing, and a run
where this package itself is not installed, are the two ways it can be told nothing and pass.
"""

import email.message
import importlib.util
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def licences () -> types.ModuleType:
	"""Load ``scripts/check_licences.py`` by path, the way the other scripts' tests do."""

	spec = importlib.util.spec_from_file_location(
		"check_licences", ROOT / "scripts" / "check_licences.py"
	)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


def test_a_package_that_says_unknown_has_declared_no_licence (
	licences: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A literal ``UNKNOWN`` was a licence, and one no rule matched, so it was judged permissive.

	It is what packaging tools write when a package declares nothing, so it is read as nothing,
	which is reported as unknown unless somebody has acknowledged the package by name.
	"""

	said = email.message.Message()
	said["License"] = "UNKNOWN"
	monkeypatch.setattr(licences.importlib.metadata, "metadata", lambda _name: said)

	assert licences._licences("anything") == []


def test_the_check_refuses_when_this_package_is_not_installed (
	licences: types.ModuleType,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""The closure is read from the installed package, so without it there was nothing to read.

	It printed that every runtime dependency was permissively licensed and passed. **Refused,
	saying how to put it right.**
	"""

	monkeypatch.setattr(licences, "ROOT", "no-such-package-for-this-test")

	assert licences.main() == 1
	assert "is not installed" in capsys.readouterr().err
