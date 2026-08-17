"""The absence of a value, as distinct from ``None``.

docs/design.md §8.3: on an update, a field that is *absent* is left alone and a field set to
``null`` is *cleared*. Collapsing those two into one makes it impossible to ever clear a
due date, which is why the distinction is pinned in the spec rather than left to taste.

**One sentinel, in one place.** Every service that takes partial updates compares with
``is UNSET``, so a second sentinel object defined somewhere else would not merely be
untidy: the comparison would quietly be false, and the field would be assigned the
sentinel itself rather than left alone. A module of its own, imported by both sides,
is what makes that impossible.

Its own module rather than a shared parent, because ``import x`` with fully-qualified
names makes a child of a loading package unreachable — the trap recorded in the project
notes. A sibling importing nothing can never take part in a cycle.
"""

import typing


class _Unset:
	"""A value nobody passed."""

	def __repr__ (self) -> str:
		"""Describe the sentinel in a way that reads clearly in a signature."""

		return "UNSET"

	def __bool__ (self) -> bool:
		"""Report as false, so ``if value:`` cannot mistake absence for a real value."""

		return False


#: Typed as ``Any`` so it can stand as the default for a parameter of any type without
#: every signature having to widen to admit it.
UNSET: typing.Any = _Unset()


def is_set (value: typing.Any) -> bool:
	"""Report whether a caller actually supplied this value."""

	return not isinstance(value, _Unset)
