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
import subroutine.permissions

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
		assert ("never as an administrator" in also) == (
			(act.own or act.chain) and not act.never_through_the_agent_tools()
		), act.key
		assert act.rule is None or act.rule in also, act.key

	assert {act.group for act in subroutine.acts.ACTS} == {
		line[3:] for line in page.splitlines() if line.startswith("## ")
	} - {"The roles every workspace is given"}

	header = next(line for line in page.splitlines() if line.startswith("| Permission |"))

	for role in ("Owner", "Admin", "Member", "Contributor", "Viewer"):
		assert f" {role} |" in header, role


#: What the cold review of 2026-10-17 measured an account's chain may do to it (`SR#5123`, M-21),
#: by what each act says: stop it, and nothing else. The code is the side decided right (`#5097`).
CHAIN_MAY = {
	"Sign another person out everywhere": True,
	"Revoke another person's credential": True,
	"Revoke another person's calendar feed": True,
	"Mark somebody as having left": True,
	"Mint a credential for somebody else": False,
	"Mint a sign-in link for somebody else": False,
	"Hand somebody's agent to another person": False,
}


def test_the_page_says_an_account_s_chain_may_stop_it_and_start_nothing () -> None:
	"""`SR#5123`, M-21 of the cold review of 2026-10-17: decision `#4516` as `#5097` kept it.

	The page gave every act on another account one rule saying its chain may do it, so it said the
	chain could issue it a credential, mint it a sign-in link and hand it over, which the code has
	never let it, and that stopping it never went through the agent tools, which it does for its
	chain. The rows of the seven acts say each now, and only the four that stop are the chain's.
	"""

	page = subroutine.acts.page_markdown()
	acts = {act.says: act for act in subroutine.acts.ACTS}

	assert {says for says, act in acts.items() if act.chain} == {
		says for says, may in CHAIN_MAY.items() if may
	}

	for says, may in CHAIN_MAY.items():
		row = next(line for line in page.splitlines() if line.startswith(f"| {says} |"))

		assert ("whoever the account answers to" in row) is may, row
		assert ("Only an administrator of accounts" in row) is not may, row
		assert ("Through the agent tools, never as an administrator." in row) is may, row
		assert ("Never through the agent tools." in row) is not may, row


def test_every_view_act_asks_task_write () -> None:
	"""`SR#5123`: the four acts on a saved view listed no permission; each asks ``task:write``.

	Saving lands on the asker's own account, which every credential reaches, and the three on a
	shared view on the workspace. Forgetting one is for whoever saved it, whoever they answer to, or
	an administrator where it is shared (decision `#4525`), where the page said its saver alone.
	"""

	views = {
		act.key: act
		for act in subroutine.acts.ACTS
		if act.key in ("save view", "update saved view", "forget saved view", "stop sharing a view")
	}

	assert len(views) == 4

	for act in views.values():
		assert act.verbs == (subroutine.permissions.TASK_WRITE,), act.key

	assert views["save view"].place == subroutine.acts.ACCOUNT
	assert {act.place for key, act in views.items() if key != "save view"} == {subroutine.acts.WORKSPACE}
	assert "whoever they answer to" in (views["forget saved view"].rule or "")
	assert "| Save a view | `task:write` | one's own account | - |" in subroutine.acts.page_markdown()
