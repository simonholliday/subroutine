"""Every mutating service call in ``src`` names an actor.

This is the mechanism that makes ``actor=None`` safe. The services skip the permission
check when there is no actor, because ``domain.bootstrap`` genuinely runs before any
principal exists — and a default that disables a security check is a hole unless something
proves it is never taken by accident.

The slice-2 review found the whole permission layer unenforced: `authorize()` existed, four
documents said it ran on every service call, and nothing anywhere called it. A token scoped
to ``task:read`` created a task. That failure was invisible because it was an *absence*, and
absences are what a static check is for.

SPEC.md §7.3 already prescribes this shape for workspace scoping — "a test asserts that no
query in the codebase reaches the task or project tables without passing through it". This
is the same instrument pointed at permissions.
"""

import ast
import pathlib
import typing

import pytest

#: Service functions that change something and therefore must be told who is asking.
GUARDED: frozenset[tuple[str, str]] = frozenset(
	{
		("tasks", "create"),
		("tasks", "create_from_text"),
		("tasks", "update"),
		("projects", "create"),
		("projects", "move"),
		("workspaces", "create"),
		("workspaces", "add_member"),
		("users", "create"),
	}
)

#: The one module allowed to call them without an actor, and why. ``initialise`` creates the
#: first user, the first workspace and the Inbox, and there is no principal in existence to
#: authorise any of it — the person running ``subroutine init`` is establishing the authority
#: that later calls are checked against.
EXEMPT = frozenset({"subroutine/domain/bootstrap.py"})

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src"


def _module_of (node: ast.Call) -> tuple[str, str] | None:
	"""Return ``(module, function)`` for a call written as ``subroutine.domain.x.y(...)``."""

	if not isinstance(node.func, ast.Attribute):
		return None

	parent = node.func.value

	if not isinstance(parent, ast.Attribute):
		return None

	return parent.attr, node.func.attr


def _offenders () -> list[str]:
	"""Return every guarded call in ``src`` that does not pass ``actor=``."""

	found: list[str] = []

	for path in sorted(ROOT.rglob("*.py")):
		relative = path.relative_to(ROOT).as_posix()

		if relative in EXEMPT or "migrations/" in relative:
			continue

		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue

			target = _module_of(node)

			if target is None or target not in GUARDED:
				continue

			if not any(keyword.arg == "actor" for keyword in node.keywords):
				found.append(f"{relative}:{node.lineno} calls {target[0]}.{target[1]}()")

	return found


def test_every_mutating_service_call_names_an_actor () -> None:
	"""No module under ``src`` may change anything anonymously, except bootstrap."""

	offenders = _offenders()

	assert offenders == [], (
		"These calls would skip the permission check:\n  "
		+ "\n  ".join(offenders)
		+ "\n\nPass actor=<Principal>. If the caller genuinely runs before any principal "
		"exists, add it to EXEMPT in this file with the reason."
	)


def test_the_check_itself_catches_a_missing_actor (tmp_path: pathlib.Path) -> None:
	"""The guard is only worth having if it fails on the thing it is guarding against.

	A static check that cannot fail is indistinguishable from one that always passes, and
	this one has no natural failing case in a healthy tree — so it is given one.
	"""

	sample = tmp_path / "offender.py"
	sample.write_text(
		"import subroutine.domain.tasks\n"
		"subroutine.domain.tasks.create(session, project=p, title='x')\n",
		encoding="utf-8",
	)

	tree = ast.parse(sample.read_text(encoding="utf-8"))
	calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

	assert [_module_of(call) for call in calls] == [("tasks", "create")]
	assert not any(keyword.arg == "actor" for keyword in calls[0].keywords)


def test_the_exemption_list_is_not_a_dumping_ground () -> None:
	"""Every exemption is a hole. There should be one, and it should be bootstrap's."""

	assert sorted(EXEMPT) == ["subroutine/domain/bootstrap.py"]


@pytest.mark.parametrize("module,function", sorted(GUARDED))
def test_every_guarded_function_exists (module: str, function: str) -> None:
	"""A typo in ``GUARDED`` would silently stop guarding something.

	The name is checked against the real module, so renaming a service without updating this
	list fails here rather than quietly removing its protection.
	"""

	import importlib

	imported: typing.Any = importlib.import_module(f"subroutine.domain.{module}")

	assert callable(getattr(imported, function))
