"""``docs/permissions.md`` is generated from the act table, and cannot drift from it - `SR#4579`.

Decision `#4512`: the page section 7 of the design promised, written from the acts the permission
matrix drives (:data:`subroutine.acts.ACTS`) and the roles the seed gives every workspace, with a
test that fails when the page and the code disagree, as ``docs/errors.md`` has.
"""

import importlib.util
import pathlib
import types

import pytest

import subroutine.acts

#: The page on disk.
PUBLISHED = pathlib.Path(__file__).resolve().parent.parent / "docs" / "permissions.md"


def test_the_published_page_is_the_table () -> None:
	"""``docs/permissions.md`` is generated; a hand-edit or a forgotten regeneration fails here."""

	assert PUBLISHED.is_file(), f"{PUBLISHED} is missing"

	assert PUBLISHED.read_text(encoding="utf-8") == subroutine.acts.page_markdown(), (
		"docs/permissions.md is out of date. Run: python scripts/permissions_page.py"
	)


def _permissions_page () -> types.ModuleType:
	"""Load ``scripts/permissions_page.py`` by path, as the errors page's test loads its script."""

	spec = importlib.util.spec_from_file_location(
		"permissions_page",
		pathlib.Path(__file__).resolve().parent.parent / "scripts" / "permissions_page.py",
	)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


def test_the_command_the_guard_names_writes_what_the_guard_compares (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Handed a stale file of its own and asked twice, the script writes exactly what the guard
	compares against, and then says there was nothing to do."""

	page = tmp_path / "permissions.md"
	page.write_text("# Permissions\n\nA table somebody pasted a week ago.\n", encoding="utf-8")

	script = _permissions_page()

	assert script.main(page) == 0
	assert page.read_text(encoding="utf-8") == subroutine.acts.page_markdown()
	assert "Wrote permissions.md" in capsys.readouterr().out

	assert script.main(page) == 0
	assert "already current" in capsys.readouterr().out
	assert script.PAGE == PUBLISHED, "and by default it writes the page the guard reads"


def test_every_act_and_every_role_is_on_the_page () -> None:
	"""**The page is the table, row for row**: every act's words appear once, under its group, with
	what the agent tools may do with it, and every role a workspace is given heads a column - so a
	renderer that dropped an act, a group or the roles, or said the agent tools' reach wrongly,
	fails here rather than publishing a page that only matches itself."""

	page = subroutine.acts.page_markdown()
	rows = [line for line in page.splitlines() if line.startswith("| ") and not line.startswith("| Act ")]

	for act in subroutine.acts.ACTS:
		found = [row for row in rows if row.startswith(f"| {act.says} |")]

		assert len(found) == 1, act.key

		also = found[0].rsplit(" | ", 1)[1]

		assert ("Never through the agent tools." in also) == act.never_through_the_agent_tools(), act.key
		assert ("only on one's own" in also) == (act.own and not act.never_through_the_agent_tools()), (
			act.key
		)
		assert act.rule is None or act.rule in also, act.key

	assert {act.group for act in subroutine.acts.ACTS} == {
		line[3:] for line in page.splitlines() if line.startswith("## ")
	} - {"The roles every workspace is given"}

	header = next(line for line in page.splitlines() if line.startswith("| Permission |"))

	for role in ("Owner", "Admin", "Member", "Contributor", "Viewer"):
		assert f" {role} |" in header, role
