"""Keeping ``path`` and ``depth`` honest about ``parent_id``.

``parent_id`` is the truth; ``path`` is a maintained denormalisation of it, in the form
``/uuid/uuid/`` including the node's own id (docs/design.md §10.6). It exists so that "this and
everything under it" is one indexed prefix scan rather than a recursive query — the shape
of nearly every question anyone asks of a tree.

The cost is that a move rewrites every descendant, and that the two representations can
disagree if anything writes ``parent_id`` without coming through here. docs/design.md §10.7
invariant 1 is the rule; this module is the only thing that should be maintaining it.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.errors

PATH_SEPARATOR = "/"

#: Bounds the length of a materialised path, and with it the cost of a move. Ten is deep
#: enough that no real structure has hit it and shallow enough that a path stays inside
#: the 1024-character column with room to spare (docs/design.md §5.4).
DEFAULT_MAX_DEPTH = 10


class Node(typing.Protocol):
	"""What this module needs of a row: an id, a place in a tree, and a workspace.

	``path`` and ``depth`` are the two it writes, so they are declared as variables;
	``id`` and ``workspace_id`` are read-only here, which also matches how the mixin
	supplying ``workspace_id`` is typed.
	"""

	path: str
	depth: int

	@property
	def id (self) -> uuid.UUID:
		"""Return the row's primary key."""

	@property
	def workspace_id (self) -> uuid.UUID:
		"""Return the workspace the row belongs to."""


def build_path (parent_path: str | None, identifier: uuid.UUID) -> str:
	"""Return the path of a node, given its parent's path and its own id."""

	prefix = PATH_SEPARATOR if parent_path is None else parent_path

	return f"{prefix}{identifier}{PATH_SEPARATOR}"


def path_segments (path: str) -> list[str]:
	"""Return the ids along a path, outermost first."""

	return [segment for segment in path.split(PATH_SEPARATOR) if segment]


def depth_of (path: str) -> int:
	"""Return the depth a path implies, counting a root as zero."""

	return max(len(path_segments(path)) - 1, 0)


def within (allowed: typing.Collection[str], *, identifier: str, path: str | None) -> bool:
	"""Report whether a node is one of ``allowed``, or filed anywhere under one of them.

	**"This project and everything under it" is one rule, and it now has one implementation**
	(item ``#413``). A credential's reach reads it against a loaded row; the check that refuses
	a write set outside that reach reads it against ids and a path fetched for the purpose. They
	disagreed: the reach was subtree-inclusive and the issue-time check was a flat set subset,
	so a credential reaching ``SR`` was refused a write set of ``SR/WEB`` — for a project it
	could read perfectly well, in a refusal that said it could not.

	``path`` is ``None`` for a node whose row could not be found, which is a legitimate state
	rather than an error: a credential may name a project its issuer cannot see, or one created
	later. Such a node is covered only by being named outright, because nothing here can place
	it in a tree — the conservative direction, and the one that keeps this from becoming a
	question about what exists.
	"""

	if identifier in allowed:
		return True

	if path is None:
		return False

	return any(segment in allowed for segment in path_segments(path))


def is_ancestor_of (candidate: Node, node: Node) -> bool:
	"""Report whether one node lies on another's path.

	A node counts as its own ancestor, because moving something under itself is the
	degenerate cycle and has to be refused alongside the rest.
	"""

	return node.path.startswith(candidate.path)


def subtree (model: type[typing.Any], node: Node) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching a node and everything beneath it.

	``LIKE`` rather than a range, and the reason is worth recording because the range looks
	obviously better and is **wrong**. A half-open ``path >= prefix AND path < prefix'``
	compares under the database's collation, and PostgreSQL's default here is
	``en_GB.UTF-8``, which does not sort byte-wise: measured against a real server, that
	predicate returns ``/aaa/`` and silently *omits* ``/aaa/bbb/``. It would have looked
	right on SQLite, whose default collation is binary, and quietly lost descendants in
	production. ``LIKE 'prefix%'`` is collation-independent for this purpose and correct on
	both.

	The cost is that neither backend turns it into an index seek, so a subtree query scans
	the workspace's rows. That is acceptable at any size this will see for a long time, and
	the fix when it stops being acceptable is an index, not a different predicate: a
	``text_pattern_ops`` index on PostgreSQL makes exactly this form index-usable under any
	collation. Filed rather than done, because it is a schema change with no measurement
	behind it yet.

	``autoescape`` is belt-and-braces: paths are lowercase hex and separators, so they carry
	no ``%`` or ``_`` to escape.
	"""

	predicate: sqlalchemy.ColumnElement[bool] = model.path.startswith(
		node.path, autoescape=True
	)

	return predicate


def place (
	node: Node, parent: Node | None, *, max_depth: int = DEFAULT_MAX_DEPTH
) -> None:
	"""Set a new node's path and depth from its parent. Does not touch the database.

	Validates before assigning, so a refused placement leaves the object as it was — the
	same rule :func:`reparent` follows, and worth keeping identical between them.
	"""

	path = build_path(None if parent is None else parent.path, node.id)
	depth = depth_of(path)

	if depth > max_depth:
		raise subroutine.errors.Conflict(
			f"That would nest {depth} levels deep, and the limit is {max_depth}.",
			code="cycle_detected",
			hint="Move it somewhere shallower, or raise max_hierarchy_depth.",
		)

	node.path = path
	node.depth = depth


def reparent (
	session: sqlalchemy.orm.Session,
	model: type[typing.Any],
	node: Node,
	parent: Node | None,
	*,
	max_depth: int = DEFAULT_MAX_DEPTH,
) -> int:
	"""Move a node and rewrite its whole subtree, returning how many rows changed.

	Refuses a move that would make the node its own ancestor, and one that would push any
	descendant past ``max_depth`` — checked against the *deepest* descendant, not the node
	itself, because the node arrives with everything below it.
	"""

	if parent is not None:
		if parent.workspace_id != node.workspace_id:
			raise subroutine.errors.ValidationError(
				"A parent must be in the same workspace as the thing it contains.",
				code="invalid_field_value",
			)

		if is_ancestor_of(node, parent):
			raise subroutine.errors.Conflict(
				"That would make it its own ancestor.",
				code="cycle_detected",
				hint="Choose a parent that is not inside this subtree.",
			)

	old_path = node.path
	new_path = build_path(None if parent is None else parent.path, node.id)

	if new_path == old_path:
		return 0

	shift = depth_of(new_path) - depth_of(old_path)
	deepest = _deepest_below(session, model, node)

	if deepest + shift > max_depth:
		raise subroutine.errors.Conflict(
			f"That would nest part of this subtree {deepest + shift} levels deep, and the "
			f"limit is {max_depth}.",
			code="cycle_detected",
			hint="Move it somewhere shallower, or raise max_hierarchy_depth.",
		)

	# One statement covers the node and everything beneath it, since the node's own row
	# matches its own path prefix. The new path is spliced onto the tail rather than
	# produced by a `replace()`, because that is what a prefix change actually is and it
	# cannot be surprised by the old prefix appearing again further along.
	statement = (
		sqlalchemy.update(model)
		.where(model.workspace_id == node.workspace_id, subtree(model, node))
		.values(
			path=sqlalchemy.literal(new_path)
			+ sqlalchemy.func.substr(model.path, len(old_path) + 1),
			depth=model.depth + shift,
		)
		.execution_options(synchronize_session=False)
	)
	# Typed as a plain Result, but DML always yields a cursor result and only that carries
	# the row count.
	result = typing.cast("sqlalchemy.CursorResult[typing.Any]", session.execute(statement))

	# A bulk update leaves every loaded row in the session believing its old path.
	session.expire_all()

	return int(result.rowcount)


def _deepest_below (
	session: sqlalchemy.orm.Session, model: type[typing.Any], node: Node
) -> int:
	"""Return the depth of the deepest row in a node's subtree, including the node."""

	deepest = session.scalar(
		sqlalchemy.select(sqlalchemy.func.max(model.depth)).where(
			model.workspace_id == node.workspace_id, subtree(model, node)
		)
	)

	return node.depth if deepest is None else int(deepest)
