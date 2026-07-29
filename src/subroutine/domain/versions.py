"""Optimistic concurrency: the check that stops one writer silently overwriting another.

SPEC.md §8.9. Every entity carries a ``version`` that moves on every change a client can
see. A caller that read version 7, thought about it, and now wants to write may say so —
and if the entity is at 9 by then, the change is refused rather than applied over whatever
happened in between.

**Optional by default, and that is deliberate.** A solo person adding a task does not want
the ceremony, and making it compulsory would put a read-modify-write cycle in front of
every one-line edit. It is the *long* cycles that need it: an agent that fetched, reasoned
and is now writing, and a person who has had a task open in a text editor for ten minutes
(§12.2b). Both are the case where "last write wins" quietly loses somebody's work.

In the service layer rather than the routers, for the same reason the permission check is:
the CLI's local mode calls these services directly and would otherwise be the one path
without the protection.
"""

import typing

import subroutine.errors


def require (entity: typing.Any, expected: int | None, *, noun: str = "item") -> None:
	"""Refuse a change when the caller's version is not the current one.

	``None`` means the caller did not ask for the check, which is not the same as asking for
	it and passing — so it returns without looking. Version numbers start at 1, so there is
	no falsy-zero trap here, but the comparison is against ``None`` rather than truthiness
	anyway: the day a version can be 0, truthiness would silently stop checking.
	"""

	if expected is None:
		return

	current = int(entity.version)

	if current == expected:
		return

	reference = getattr(entity, "ref", None) or getattr(entity, "key", None) or noun

	raise subroutine.errors.Conflict(
		f"{reference} has changed since you read it: you have version {expected}, and it "
		f"is now at version {current}.",
		code="version_conflict",
		errors=[
			subroutine.errors.FieldError(
				field="expected_version",
				code="version_conflict",
				message=f"Expected version {expected}, found {current}.",
				hint="Read it again, apply your change to the current version, and retry.",
			)
		],
		hint="Nothing was changed. Re-read the item — the current one is in this response — "
		"merge your change into it, and send it again.",
		# Machine-readable, because "merge and retry" is a thing a program does and picking
		# two numbers out of a sentence is not.
		extensions={"expected_version": expected, "current_version": current},
	)
