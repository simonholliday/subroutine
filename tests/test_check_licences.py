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


@pytest.mark.parametrize(
	"declared",
	[
		"GPL", "GPL v3", "GNU GPL", "Other/Proprietary License", "Commercial", "BUSL-1.1",
		"SSPL-1.0", "EUPL-1.2", "OSL-3.0", "CC-BY-SA-4.0",
	],
)
def test_a_licence_nobody_has_allowed_is_never_read_as_permissive (
	licences: types.ModuleType, declared: str
) -> None:
	"""`SR#4029`, L-9 (3) of the cold review of 2026-09-30: the gate was a deny-list.

	Every one of these read as permissive, since no fragment it denied matched. Under the FSL a
	copyleft dependency makes distributing unlawful, so **what is not on the list is not known**,
	and the gate fails on it until somebody reads it.
	"""

	assert licences._classify([declared]) != "permissive", declared


def test_the_licences_the_closure_declares_today_are_all_allowed (
	licences: types.ModuleType,
) -> None:
	"""The other side: an allow-list that turned down what ships today would stop every build."""

	for declared in ("MIT", "MIT License", "MIT-0", "BSD-3-Clause", "BSD License", "Apache-2.0"):
		assert licences._classify([declared]) == "permissive", declared

	assert licences._classify(["LGPL-3.0-only"]) == "flagged"
	assert licences._classify([]) == "unknown"


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
