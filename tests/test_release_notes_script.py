"""What a release says on its GitHub page — ``scripts/release_notes.py``, item ``#241``.

The thing worth guarding is not that it finds a section. It is that **it refuses when there is
nothing to find**, because the failure otherwise is silent: a release publishes with an empty
page, nothing errors, and nobody notices until somebody goes looking for what changed.

Every test drives the module rather than a subprocess, so a broken import fails here rather than
as an unexplained non-zero somewhere in a workflow.
"""

import importlib.util
import pathlib
import types

import pytest

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY / "scripts" / "release_notes.py"

CHANGELOG = """# Changelog

Some preamble that is not a release.

## Unreleased

Work that has not gone out.

## 1.2.0 — 2026-08-02

### Fixed

- The thing that was wrong.

## 1.1.0 — 2026-08-01

The one before.
"""


@pytest.fixture(scope="module")
def script () -> types.ModuleType:
	"""Load the script by path, the way `test_release_notes.py` loads its sibling."""

	spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


def test_it_reads_the_section_for_the_version_asked_for (script: types.ModuleType) -> None:
	"""**Not the topmost section**, which is a different answer on every later day.

	The top of the file is the release being cut only at the moment of cutting it. This runs
	from a tag, and a tag can be built again long after the next release has landed — so
	matching on the version is the difference between correct notes and last month's.
	"""

	found = script._section_for(CHANGELOG, "1.1.0")

	assert found is not None
	assert "The one before." in found
	assert "The thing that was wrong." not in found, "it reached into the newer section"


def test_it_stops_at_the_next_heading (script: types.ModuleType) -> None:
	"""A section is the lines under one heading, not everything to the end of the file."""

	found = script._section_for(CHANGELOG, "1.2.0")

	assert found is not None
	assert "The thing that was wrong." in found
	assert "The one before." not in found


def test_a_leading_v_is_accepted (script: types.ModuleType) -> None:
	"""It is handed ``GITHUB_REF_NAME``, which is a tag, and our tags carry a ``v``.

	Against the real changelog, because ``main`` reads that rather than the fixture above —
	and named from the file rather than written in, so this does not pin today's version.
	"""

	text = (REPOSITORY / "CHANGELOG.md").read_text(encoding="utf-8")
	newest = script.re.findall(r"^##\s+(\d+\.\d+\.\d+)\b", text, script.re.MULTILINE)[0]

	assert script.main([f"v{newest}"]) == 0


def test_a_version_with_no_section_is_refused (
	script: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
	"""The silent failure this exists to prevent, in the direction it would actually happen.

	A release cut without its changelog section would otherwise publish a page saying nothing
	— which reads as "this release changed nothing" rather than as a mistake.
	"""

	assert script.main(["9.9.9"]) == 1
	assert "no '## 9.9.9' section" in capsys.readouterr().err


def test_an_empty_section_is_refused (
	script: types.ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A heading with nothing under it is the same failure wearing a heading."""

	empty = REPOSITORY / "CHANGELOG.md"
	monkeypatch.setattr(script, "CHANGELOG", empty)
	monkeypatch.setattr(
		script.pathlib.Path, "read_text", lambda self, **kwargs: "## 1.2.0 — 2026-08-02\n\n"
	)

	assert script.main(["1.2.0"]) == 1
	assert "is empty" in capsys.readouterr().err


def test_this_repository_can_produce_notes_for_its_own_newest_release (
	script: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Against the real file, because a parser that only works on a fixture is not a guard.

	Reads the newest released heading out of the changelog rather than naming a version, so it
	goes on being true after the next release rather than pinning today's.
	"""

	text = (REPOSITORY / "CHANGELOG.md").read_text(encoding="utf-8")
	released = script.re.findall(r"^##\s+(\d+\.\d+\.\d+)\b", text, script.re.MULTILINE)

	assert released, "the changelog has no released section at all"
	assert script.main([released[0]]) == 0
	assert capsys.readouterr().out.strip(), "the newest release has empty notes"
