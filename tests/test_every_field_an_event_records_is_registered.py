"""Every field an event's changes carry is in a register saying what it names - `SR#5306`.

**EV1 and EV2 of the cold review of 2026-11-11.** Deleting ``recurrence_template_id`` or
``parent_task_id`` from :data:`subroutine.domain.events.NAMES_A_ROW` passed every event and journal
test, because the guard holding that list compared it with the journal's lookups, which spread it
into themselves: an extra field could fail it, a missing one never. That guard stays, for the extra.

**This register is declared here, apart from the program, and held both ways**, as
``tests/test_api_writability.py`` holds the views: against every field the program records, read
from its source, and against what its renderers say as unseen. So a field added tomorrow fails until
somebody says what it names, and a field dropped from what the renderers hide fails at once.
"""

import ast
import functools
import pathlib
import textwrap
import typing

import subroutine.domain.events

#: The package the scan reads.
SOURCE = pathlib.Path(subroutine.domain.events.__file__).parents[1]

#: Where an event is written, and the one function a change's two sides are compared by.
RECORD = "subroutine.domain.events.record"
BETWEEN = "subroutine.domain.events.changes_between"

#: Methods that could give a local dict a key no assignment shows.
ADDING = frozenset({"update", "setdefault"})

#: A field an event records: the kind of thing the event is about, and the field's name.
Field = tuple[str, str]

#: Fields naming a row its reader may not see, and the kind of row: ``NAMES_A_ROW`` or
#: ``NAMES_A_ROW_ON`` (`SR#5148`, `SR#5241`, `SR#5290`, `SR#5453`).
ROWS: dict[Field, str] = {
	("document", "parent_id"): "document",
	("document", "project_id"): "project",
	("project", "parent_id"): "project",
	("task", "parent_task_id"): "task",
	("task", "project_id"): "project",
	("task", "recurrence_template_id"): "task",
	("workspace", "prioritised_project_id"): "project",
}

#: Fields holding a path of such rows, and the kind along it: ``PATHS_ON`` (`SR#5241`, `SR#5290`).
PATHS: dict[Field, str] = {
	("document", "path"): "document",
	("project", "path"): "project",
	("task", "path"): "task",
}

#: Fields naming such a row by its number, and the kind: ``REFS_ON`` (`SR#5290`).
REFS: dict[Field, str] = {("task", "moved_with"): "task"}

#: Fields naming such a row that nothing says as unseen yet, with the kind and the item that will.
#: **Deleting an entry is what closes its item**: the agreement test fails once the renderers hide
#: the field, and the entry moves to :data:`ROWS`.
OWED: dict[Field, tuple[str, str]] = {}

#: Why a field names no project, task or document its reader may not see.
ITS_OWN = "the item's own number, name or text"
A_VALUE = "a value of its own: a date, a count, a flag, a rule or a setting"
AN_ACCOUNT = "an account, not a project, task or document"
VOCABULARY = "one of the workspace's statuses, types, tags or roles"
A_LINK_END = "a link's end, which unseen_ends hides through the event's second subject (SR#4575)"

#: Every other field an event records, and why it names nothing a reader may not see.
NAMES_NOTHING: dict[Field, str] = {
	("comment", "body"): ITS_OWN,
	("document", "binds"): A_VALUE,
	("document", "body"): ITS_OWN,
	("document", "descendants_rewritten"): A_VALUE,
	("document", "owner_id"): AN_ACCOUNT,
	("document", "ref"): ITS_OWN,
	("document", "status_id"): VOCABULARY,
	("document", "tags"): VOCABULARY,
	("document", "title"): ITS_OWN,
	("document", "type_id"): VOCABULARY,
	("link", "link_type"): VOCABULARY,
	("link", "source"): A_LINK_END,
	("link", "target"): A_LINK_END,
	("project", "descendants_rewritten"): A_VALUE,
	("project", "description"): ITS_OWN,
	("project", "key"): ITS_OWN,
	("project", "owner_id"): AN_ACCOUNT,
	("project", "settings"): A_VALUE,
	("project", "status_id"): VOCABULARY,
	("project", "title"): ITS_OWN,
	("project", "visibility"): A_VALUE,
	("project_member", "user_id"): AN_ACCOUNT,
	("task", "assignee_id"): AN_ACCOUNT,
	("task", "completed_at"): A_VALUE,
	("task", "descendants_rewritten"): A_VALUE,
	("task", "description"): ITS_OWN,
	("task", "due_at"): A_VALUE,
	("task", "due_is_all_day"): A_VALUE,
	("task", "ends_at"): A_VALUE,
	("task", "estimate_minutes"): A_VALUE,
	("task", "expires_at"): A_VALUE,
	("task", "importance"): A_VALUE,
	("task", "recurrence"): A_VALUE,
	("task", "ref"): ITS_OWN,
	("task", "reminder_minutes"): A_VALUE,
	("task", "renewed"): A_VALUE,
	("task", "snoozed_is_all_day"): A_VALUE,
	("task", "snoozed_until"): A_VALUE,
	("task", "starts_at"): A_VALUE,
	("task", "starts_is_all_day"): A_VALUE,
	("task", "status_id"): VOCABULARY,
	("task", "tags"): VOCABULARY,
	("task", "timezone"): A_VALUE,
	("task", "title"): ITS_OWN,
	("task", "type_id"): VOCABULARY,
	("task", "urgency"): A_VALUE,
	("verification", "passed"): A_VALUE,
	("workspace", "description"): ITS_OWN,
	("workspace", "item_types"): A_VALUE,
	("workspace", "link_types"): A_VALUE,
	("workspace", "roles"): A_VALUE,
	("workspace", "seed_version"): A_VALUE,
	("workspace", "settings"): A_VALUE,
	("workspace", "slug"): ITS_OWN,
	("workspace", "statuses"): A_VALUE,
	("workspace", "timezone"): A_VALUE,
	("workspace", "title"): ITS_OWN,
	("workspace_member", "role"): VOCABULARY,
	("workspace_member", "user_id"): AN_ACCOUNT,
}


class Scan (typing.NamedTuple):
	"""What the scan found: each field recorded, with where, and each call it could not read."""

	recorded: dict[Field, list[str]]
	unread: list[str]


def scanned (root: pathlib.Path) -> Scan:
	"""Return every field an ``events.record`` call under ``root`` gives an event's changes.

	**Read from the source rather than from a run**, so a field written on a path no test drives is
	found too. The shapes read are the ones the program uses: a dict written out,
	``changes_between`` of two of them, a local built by assignment, by string keys or by a loop
	over pairs of a name and a value, and the module's own function returning one. **Anything else
	is unread, and that fails**: a shape the scan cannot follow is taught to it, never skipped.
	"""

	recorded: dict[Field, list[str]] = {}
	unread: list[str] = []

	for path in sorted(root.rglob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"))
		module = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
		parents = {
			child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
		}

		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or ast.unparse(node.func) != RECORD:
				continue

			where = f"{path.relative_to(root)}:{node.lineno}"
			words = {word.arg: word.value for word in node.keywords}
			kind = _text(words.get("entity_type"))
			changes = words.get("changes")

			if kind is None:
				unread.append(where)
				continue

			if changes is None or (isinstance(changes, ast.Constant) and changes.value is None):
				continue

			fields = _keys(changes, _enclosing(node, parents), module, frozenset())

			if fields is None:
				unread.append(where)
				continue

			for field in sorted(fields):
				recorded.setdefault((kind, field), []).append(where)

	return Scan(recorded, unread)


@functools.cache
def _ours () -> Scan:
	"""Return the scan of the program itself, read once for every test here."""

	return scanned(SOURCE)


def _enclosing (node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
	"""Return the function ``node`` is written in, or the module where it is in none."""

	while node in parents and not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
		node = parents[node]

	return node


def _text (node: ast.expr | None) -> str | None:
	"""Return ``node`` if it is a string written out, and ``None`` otherwise."""

	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value

	return None


def _is_name (node: ast.expr, name: str) -> bool:
	"""Say whether ``node`` is the local ``name``."""

	return isinstance(node, ast.Name) and node.id == name


def _union (parts: list[set[str] | None]) -> set[str] | None:
	"""Return the parts together, or ``None`` if there are none or any could not be read."""

	if not parts or any(part is None for part in parts):
		return None

	return set[str]().union(*(part for part in parts if part is not None))


def _keys (
	node: ast.expr,
	scope: ast.AST,
	module: dict[str, ast.FunctionDef],
	seen: frozenset[tuple[int, str]],
) -> set[str] | None:
	"""Return the keys ``node`` can hold in ``scope``, or ``None`` where they cannot be read."""

	if isinstance(node, ast.Dict):
		keys = [_text(key) for key in node.keys]

		return None if None in keys else {key for key in keys if key is not None}

	# `{name: ... for name in changed}` holds the keys of what it walks.
	if isinstance(node, ast.DictComp):
		walked = node.generators[0]
		target = walked.target

		if (
			len(node.generators) == 1
			and isinstance(target, ast.Name)
			and _is_name(node.key, target.id)
		):
			return _keys(walked.iter, scope, module, seen)

		return None

	if isinstance(node, ast.Call):
		called = ast.unparse(node.func)

		if called == BETWEEN and len(node.args) == 2:
			return _union([_keys(side, scope, module, seen) for side in node.args])

		if called in module:
			function = module[called]
			returned = [
				one.value
				for one in ast.walk(function)
				if isinstance(one, ast.Return) and one.value is not None
			]

			return _union([_keys(one, function, module, seen) for one in returned])

		return None

	if isinstance(node, ast.Name):
		return _local(node.id, scope, module, seen)

	return None


def _local (
	name: str,
	scope: ast.AST,
	module: dict[str, ast.FunctionDef],
	seen: frozenset[tuple[int, str]],
) -> set[str] | None:
	"""Return the keys the local ``name`` of ``scope`` is given, or ``None`` where unreadable."""

	if (id(scope), name) in seen:
		return None

	seen = seen | {(id(scope), name)}
	parts: list[set[str] | None] = []

	for node in ast.walk(scope):
		if _adds_unseen(node, name):
			return None

		targets: list[ast.expr]
		value: ast.expr | None

		if isinstance(node, ast.Assign):
			targets, value = node.targets, node.value
		elif isinstance(node, ast.AnnAssign):
			targets, value = [node.target], node.value
		else:
			continue

		for target in targets:
			if _is_name(target, name) and value is not None:
				parts.append(_keys(value, scope, module, seen))
			elif isinstance(target, ast.Subscript) and _is_name(target.value, name):
				parts.append(_index(target.slice, scope))

	return _union(parts)


def _adds_unseen (node: ast.AST, name: str) -> bool:
	"""Say whether ``node`` could give the local ``name`` a key no assignment shows."""

	if isinstance(node, ast.AugAssign):
		return _is_name(node.target, name)

	return isinstance(node, ast.Attribute) and _is_name(node.value, name) and node.attr in ADDING


def _index (index: ast.expr, scope: ast.AST) -> set[str] | None:
	"""Return the keys a subscript gives: a string written out, or a name a loop of pairs takes."""

	text = _text(index)

	if text is not None:
		return {text}

	if isinstance(index, ast.Name):
		return _looped(index.id, scope)

	return None


def _looped (name: str, scope: ast.AST) -> set[str] | None:
	"""Return the names ``for name, value in (("title", ...), ...)`` gives ``name``, or ``None``.

	**Every loop in ``scope`` taking ``name`` first is asked**, and one walking anything but pairs
	written out is unread: the scan cannot tell which loop a subscript sits in.
	"""

	loops = [
		node.iter
		for node in ast.walk(scope)
		if isinstance(node, ast.For)
		and isinstance(node.target, ast.Tuple)
		and node.target.elts
		and _is_name(node.target.elts[0], name)
	]

	return _union([_pairs(walked, scope) for walked in loops])


def _pairs (walked: ast.expr, scope: ast.AST) -> set[str] | None:
	"""Return the first of each pair ``walked`` writes out, or ``None`` if it is anything else."""

	if not isinstance(walked, ast.Tuple):
		return None

	return _union(
		[
			_index(pair.elts[0], scope) if isinstance(pair, ast.Tuple) and pair.elts else None
			for pair in walked.elts
		]
	)


def _said () -> dict[Field, tuple[str, str] | None]:
	"""Return how the register says the renderers hide each field, each field said once."""

	said: dict[Field, tuple[str, str] | None] = {}
	hidden: list[tuple[str, dict[Field, str]]] = [("row", ROWS), ("path", PATHS), ("ref", REFS)]

	for how, part in hidden:
		for field, kind in part.items():
			assert field not in said, f"{field} is in the register twice"

			said[field] = (how, kind)

	for field in [*OWED, *NAMES_NOTHING]:
		assert field not in said, f"{field} is in the register twice"

		said[field] = None

	return said


def _hidden_by_the_program (field: Field) -> tuple[str, str] | None:
	"""Return how the renderers say ``field`` as unseen, asked as ``_unseen_rows_named`` asks."""

	events = subroutine.domain.events

	if field in events.REFS_ON:
		return ("ref", events.REFS_ON[field])

	if field in events.PATHS_ON:
		return ("path", events.PATHS_ON[field])

	kind = events.NAMES_A_ROW.get(field[1]) or events.NAMES_A_ROW_ON.get(field)

	return None if kind is None else ("row", kind)


def test_every_field_an_event_records_is_in_the_register () -> None:
	"""Both ways: a field the program records and the register does not name, and the reverse."""

	scan = _ours()
	said = _said()
	unnamed = {field: where for field, where in scan.recorded.items() if field not in said}
	stale = sorted(set(said) - set(scan.recorded))

	assert scan.unread == [], f"the scan cannot read what these give an event: {scan.unread}"
	assert unnamed == {}, f"say what each of these names, in the register here: {unnamed}"
	assert stale == [], f"nothing records these any more, so delete them from the register: {stale}"


def test_what_the_register_says_each_field_names_is_what_the_renderers_hide () -> None:
	"""EV1's and EV2's mutants fail here: a field dropped from what an event may name."""

	said = _said()
	disagree = {
		field: {"register": said[field], "renderers": _hidden_by_the_program(field)}
		for field in sorted(said)
		if said[field] != _hidden_by_the_program(field)
	}

	assert disagree == {}, (
		f"the register and the renderers disagree on {disagree}. A field the renderers no longer "
		f"hide still names a row its reader may not see; one they now hide that the register owes "
		f"moves from OWED to its list, which closes the item it names."
	)


def test_every_field_the_renderers_hide_is_one_an_event_records () -> None:
	"""The other direction: an entry in what the renderers hide that nothing records is stale."""

	recorded = _ours().recorded
	names = {name for _kind, name in recorded}
	events = subroutine.domain.events
	lists: list[tuple[str, typing.Mapping[Field, str]]] = [
		("NAMES_A_ROW_ON", events.NAMES_A_ROW_ON),
		("PATHS_ON", events.PATHS_ON),
		("REFS_ON", events.REFS_ON),
	]
	stale = [f"NAMES_A_ROW[{name!r}]" for name in events.NAMES_A_ROW if name not in names] + [
		f"{label}[{field!r}]" for label, part in lists for field in part if field not in recorded
	]

	assert stale == []


def test_the_scan_finds_a_field_nobody_named_and_refuses_what_it_cannot_read (
	tmp_path: pathlib.Path,
) -> None:
	"""A defect fed through the scan's own entry point (`#405`): each shape it reads, two not."""

	(tmp_path / "written.py").write_text(
		textwrap.dedent(
			"""
			import subroutine.domain.events


			def _snapshot (task):
				return {"title": task.title}


			def update (session, task):
				before = _snapshot(task)
				task.title = "Learn kung fu"
				changes = subroutine.domain.events.changes_between(before, _snapshot(task))
				subroutine.domain.events.record(session, entity_type="task", changes=changes)


			def move (session, task, came_from):
				changes: dict = {}

				for field, value in (("owner_id", task.owner_id),):
					changes[field] = {"from": None, "to": value}

				changes["secret_id"] = {"from": came_from, "to": None}
				subroutine.domain.events.record(session, entity_type="task", changes=changes)


			def renamed (session, workspace, changed):
				before = {name: getattr(workspace, name) for name in changed}
				changed = {"slug": "metacortex"}
				differences = subroutine.domain.events.changes_between(before, changed)
				subroutine.domain.events.record(
					session, entity_type="workspace", changes=differences
				)


			def worked_out (session, task):
				subroutine.domain.events.record(
					session, entity_type="task", changes=elsewhere(task)
				)


			def widened (session, task):
				changes = {"title": None}
				changes.update(more(task))
				subroutine.domain.events.record(session, entity_type="task", changes=changes)
			"""
		),
		encoding="utf-8",
	)

	scan = scanned(tmp_path)

	assert set(scan.recorded) == {
		("task", "title"),
		("task", "owner_id"),
		("task", "secret_id"),
		("workspace", "slug"),
	}
	assert [where.split(":")[0] for where in scan.unread] == ["written.py", "written.py"]
