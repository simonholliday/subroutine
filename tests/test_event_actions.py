"""Every action an event can record is one something records - `#2723`.

``EventAction`` declared ``status_changed`` and ``completed`` as *reserved for the completion
work in slice 2*, and nothing ever wrote either: finishing something has always been recorded
as ``updated``, with the status and ``completed_at`` in its ``changes``. The comment went on
promising them long after slice 2 finished, and a reader building on the change feed (`#2721`)
would have looked for an action that never arrives.

That is the second signature defect in the project notes - a control that is specified,
documented and inert - and like `#303` it is closed by deleting the declaration and **measuring
the rest**: the actions are read out of the calls that record them, so a member nothing writes
fails here instead of waiting to be noticed.
"""

import ast
import pathlib

import subroutine
import subroutine.domain.events

SOURCE = pathlib.Path(subroutine.__file__).parent


def _recorded (root: pathlib.Path = SOURCE) -> dict[str | None, list[str]]:
	"""Return every ``EventAction`` member passed as ``action`` to ``events.record``, and where from.

	**The tree is an argument so the guard can be shown a defect** (`#405`): a scan that stopped
	matching would find no member recorded, which fails - but one that matched too much, such as
	a comparison against a member, would pass a member nothing writes.

	An ``action`` that is not ``EventAction.<member>`` is reported under ``None``, because a value
	this cannot read is one it cannot check.
	"""

	found: dict[str | None, list[str]] = {}

	for path in sorted(root.rglob("*.py")):
		if "migrations" in path.parts:
			continue

		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue

			owner = node.func.value

			if node.func.attr != "record" or not isinstance(owner, ast.Attribute):
				continue

			if owner.attr != "events":
				continue

			for keyword in node.keywords:
				if keyword.arg != "action":
					continue

				value = keyword.value
				member = (
					value.attr
					if isinstance(value, ast.Attribute)
					and isinstance(value.value, ast.Attribute)
					and value.value.attr == "EventAction"
					else None
				)
				found.setdefault(member, []).append(f"{path.relative_to(root)}:{node.lineno}")

	return found


def test_every_action_an_event_can_record_is_recorded_by_something () -> None:
	"""A member nothing writes is a promise to every reader of the change feed that is never kept."""

	recorded = _recorded()
	declared = {member.name for member in subroutine.domain.events.EventAction}

	assert declared - set(recorded) == set(), (
		"these actions are declared and nothing records them - record them where the thing "
		"happens, or delete them"
	)
	assert None not in recorded, (
		f"an action is recorded as something this cannot read: {recorded.get(None)}"
	)


def test_the_scan_reads_the_action_a_record_is_given_and_nothing_else (
	tmp_path: pathlib.Path,
) -> None:
	"""Shown a defect through its own entry point (`#405`), in each direction it could be wrong.

	A member only *compared* against - ``filtering`` does that with two of them - is not one that
	is written, and an ``action`` given as anything but a member is reported rather than skipped.
	"""

	(tmp_path / "writes.py").write_text(
		"import subroutine.domain.events\n"
		"subroutine.domain.events.record(action=subroutine.domain.events.EventAction.CREATED)\n",
		encoding="utf-8",
	)
	(tmp_path / "reads.py").write_text(
		"import subroutine.domain.events\n"
		"COMPARED = row.action == subroutine.domain.events.EventAction.CLAIMED\n"
		"somebody.calendar.record(action=subroutine.domain.events.EventAction.RELEASED)\n",
		encoding="utf-8",
	)
	(tmp_path / "unread.py").write_text(
		"import subroutine.domain.events\n"
		"subroutine.domain.events.record(action=chosen)\n",
		encoding="utf-8",
	)

	recorded = _recorded(tmp_path)

	assert set(recorded) == {"CREATED", None}, recorded
	assert recorded[None] == ["unread.py:2"], recorded
