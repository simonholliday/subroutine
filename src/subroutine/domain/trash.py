"""What may be done to an item in the trash: read it, restore it, or delete it again (§6.9).

**Nothing else, and every writer asks here** (`#3935`). A comment on a trashed item was refused
by name from the start, while an edit, a completion, a skip, a claim and a move answered as though
it were live - and finishing a trashed occurrence of a repeat brought the next one. A row in the
trash is somebody saying they are done with it, so work on it waits until it is taken back out.
"""

import subroutine.db.models.work
import subroutine.domain.refs
import subroutine.errors


def refuse (
	row: subroutine.db.models.work.Task | subroutine.db.models.work.Document, *, doing: str
) -> None:
	"""Refuse ``doing`` to a task or a document in the trash, saying how to go on.

	``doing`` finishes the sentence: *#42 is in the trash, so it cannot be* ``changed``. Refused
	as a comment on one is, and with the same remedy.
	"""

	if row.deleted_at is None:
		return

	raise refusal(row.ref, doing=doing)


def refusal (ref: int, *, doing: str) -> subroutine.errors.ValidationError:
	"""Return :func:`refuse`'s sentence, for a caller that has to say it before it asks - `#4005`.

	``document edit`` finds a document trash included and runs the editor before it saves, so the
	domain's refusal arrived after the typing and the text was gone. The terminal asks first, in
	these same words.
	"""

	return subroutine.errors.ValidationError(
		f"{subroutine.domain.refs.format_ref(ref)} is in the trash, so it cannot be {doing}.",
		hint="Restore it first if you meant to keep working on it.",
	)
