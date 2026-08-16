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

import sqlalchemy.orm.exc

import subroutine.domain.refs
import subroutine.errors

#: What SQLAlchemy raises when a versioned ``UPDATE`` matches no row.
#:
#: Named here rather than spelled at each of the two places that catch it, so the answer to
#: *what does a lost update look like on the way out* lives beside the answer to *what does a
#: stale one look like on the way in*.
RACED = sqlalchemy.orm.exc.StaleDataError


def raced () -> subroutine.errors.Conflict:
	"""Report a change that was overtaken between being checked and being written.

	:func:`require` answers the commoner case — the caller read version 7 and the entity is
	already at 9 — before any work is done, and it can name both numbers. This answers the
	case it structurally cannot see: both writers read the same version, both passed, and the
	database refused the second one's ``UPDATE`` because ``VersionMixin`` writes it under a
	condition (`#927` H-12).

	**Deliberately carrying no ``current`` entity**, where a stale ``expected_version`` does.
	§8.9 promises the 409 holds the current one so a caller can merge rather than refetch, and
	``api.concurrency.reporting`` supplies it — but it can only do that because ``require``
	fires *before* a service has assigned anything. Here the flush has already failed, so the
	only session in reach is one whose transaction is being rolled back, and a value read
	through it would be a guess wearing the authority of a field. The remedy is the same
	either way and it is in the hint.

	The code is ``version_conflict`` too, and that is the point: a caller's remedy does not
	change with *when* it lost the race, so a second code would be a second thing to handle
	for no difference in what to do about it.
	"""

	return subroutine.errors.Conflict(
		"Somebody else changed this while your change was being written.",
		code="version_conflict",
		hint="Nothing was changed. Read it again, apply your change to the current version, "
		"and send it again.",
	)


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

	reference = _name(entity, noun)

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


def _name (entity: typing.Any, noun: str) -> str:
	"""Return what to call an entity in a sentence: ``#42``, ``SR``, or a fallback noun.

	Tested against ``None`` rather than for truthiness. A ref is an integer now, and while
	they start at 1, ``or`` would silently fall through to the noun the day one could be 0 —
	which is the same trap this module's docstring warns about for versions.
	"""

	ref = getattr(entity, "ref", None)

	if ref is not None:
		return subroutine.domain.refs.format_ref(int(ref))

	key = getattr(entity, "key", None)

	return noun if key is None else str(key)
