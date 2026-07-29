"""The last listing, so ``subroutine done 1`` means something.

Positional addressing is the difference between a to-do list you use and one you type
identifiers into (SPEC.md §12.2a). A CLI is stateless between invocations, so the numbers
have to be written down somewhere, and this is that somewhere: a small JSON file under
``$XDG_STATE_HOME`` mapping the positions printed last time to the refs behind them.

State, not data. Deleting it costs one re-run of ``subroutine today`` — so it is written
where losing it is expected, never beside the database, and every failure to read or write
it is swallowed. **A broken cache must never break a command**: the whole feature is a
convenience over refs, which keep working.
"""

import json
import pathlib
import typing

import subroutine.config

#: What the file holds. Bumped if the shape changes, which makes an old file unreadable
#: rather than misread — positions pointing at the wrong tasks is the one failure this
#: must not have.
FORMAT_VERSION = 1

FILENAME = "last-listing.json"


def path () -> pathlib.Path:
	"""Return where the last listing is kept."""

	return subroutine.config.state_home() / FILENAME


def remember (refs: typing.Sequence[str]) -> None:
	"""Record the refs that were just printed, in the order they appeared.

	Position 1 is the first line shown, which is the only mapping a person can be expected
	to hold in their head.
	"""

	document = {"version": FORMAT_VERSION, "refs": list(refs)}

	try:
		target = path()
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(json.dumps(document), encoding="utf-8")

	except OSError:
		# A read-only home, a full disk, a container without the directory mounted. None of
		# them is a reason to fail the command the user actually asked for.
		return


def resolve (position: int) -> str | None:
	"""Return the ref shown at a position last time, or ``None`` if it cannot be known."""

	try:
		document = json.loads(path().read_text(encoding="utf-8"))

	except (OSError, ValueError):
		return None

	if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION:
		return None

	refs = document.get("refs")

	if not isinstance(refs, list) or not 1 <= position <= len(refs):
		return None

	found = refs[position - 1]

	return found if isinstance(found, str) else None


def forget () -> None:
	"""Discard the last listing, for tests and for ``doctor``."""

	try:
		path().unlink(missing_ok=True)

	except OSError:
		return
