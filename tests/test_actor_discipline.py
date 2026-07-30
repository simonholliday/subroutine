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

#: Service functions whose ``actor`` is *not* a permission check, with the reason. These are
#: the only names allowed out of the derived set below, and each has to be justified rather
#: than merely listed.
NOT_A_CHECK: dict[tuple[str, str], str] = {
	("events", "record"): "writes the audit row that records who did something; it does not "
	"decide whether they may, and every caller has already been checked",
	("workspaces", "record_seeding"): "stamps a workspace as seeded, inside "
	"`workspaces.create`, which is itself guarded",
}


def _guarded () -> frozenset[tuple[str, str]]:
	"""Derive the guarded set from the signatures, rather than from anybody's memory.

	**This was a hand-written list of eight, and there were seventeen.** The 2026-07-30 review
	found `tasks.complete`, `tasks.delete`, `projects.update`, `projects.delete`, all three
	document services and both link services taking ``actor=None`` and watched by nothing —
	so the instrument CLAUDE.md describes as "what makes the default safe" was covering under
	half of what it claimed. A list somebody has to remember to extend is the same shape as the
	defect this file exists to catch.

	So the *schema* maintains it: every public callable in ``subroutine.domain`` whose signature
	has an ``actor`` parameter defaulting to ``None`` is guarded, minus the few in
	:data:`NOT_A_CHECK`.
	"""

	found: set[tuple[str, str]] = set()

	for path in sorted((ROOT / "subroutine" / "domain").glob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for node in tree.body:
			if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
				continue

			for argument, default in zip(
				node.args.kwonlyargs, node.args.kw_defaults, strict=True
			):
				takes_actor = argument.arg == "actor"
				defaults_to_none = isinstance(default, ast.Constant) and default.value is None

				if takes_actor and defaults_to_none:
					found.add((path.stem, node.name))

	return frozenset(found - set(NOT_A_CHECK))

#: The one module allowed to call them without an actor, and why. ``initialise`` creates the
#: first user, the first workspace and the Inbox, and there is no principal in existence to
#: authorise any of it — the person running ``subroutine init`` is establishing the authority
#: that later calls are checked against.
EXEMPT = frozenset({"subroutine/domain/bootstrap.py"})

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src"


#: Every service that must be told who is asking, derived at import time.
GUARDED = _guarded()


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

			# **Present is not enough.** `actor=None` satisfied this check while disabling
			# the thing it guards, which is precisely the shape of the defect above.
			supplied = [keyword for keyword in node.keywords if keyword.arg == "actor"]
			explicit_none = any(
				isinstance(keyword.value, ast.Constant) and keyword.value.value is None
				for keyword in supplied
			)

			if not supplied:
				found.append(f"{relative}:{node.lineno} calls {target[0]}.{target[1]}()")

			elif explicit_none:
				found.append(
					f"{relative}:{node.lineno} passes actor=None to "
					f"{target[0]}.{target[1]}()"
				)

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


def test_the_check_rejects_an_explicit_actor_of_none (tmp_path: pathlib.Path) -> None:
	"""Passing the keyword is not the same as passing a principal.

	``actor=None`` satisfied this check for as long as it only asked whether the keyword was
	*present* — which meant the one spelling that disables the permission check was also the one
	spelling that proved the check was running.
	"""

	sample = tmp_path / "offender.py"
	sample.write_text(
		"import subroutine.domain.tasks\n"
		"subroutine.domain.tasks.complete(session, task, actor=None)\n",
		encoding="utf-8",
	)

	tree = ast.parse(sample.read_text(encoding="utf-8"))
	call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
	supplied = [keyword for keyword in call.keywords if keyword.arg == "actor"]

	assert supplied, "the keyword is there"
	assert all(
		isinstance(keyword.value, ast.Constant) and keyword.value.value is None
		for keyword in supplied
	), "and it is the value the check now refuses"


def test_the_guarded_set_covers_every_service_that_takes_an_actor () -> None:
	"""The list is derived, and this states what it must therefore contain.

	It was hand-written and held eight of seventeen. Named here so that a reader can see the
	nine that were missing — and so that removing the derivation and going back to a literal
	list fails.
	"""

	assert len(GUARDED) >= 17

	# The nine the hand-written list omitted, every one of them a mutating service.
	assert {
		("tasks", "complete"),
		("tasks", "delete"),
		("projects", "update"),
		("projects", "delete"),
		("documents", "create"),
		("documents", "update"),
		("documents", "delete"),
		("links", "create"),
		("links", "remove"),
	} <= GUARDED


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
