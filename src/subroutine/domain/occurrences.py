"""The one open occurrence of a repeat, and the refusal of an act given the repeat itself.

**Below both ``tasks`` and ``claims``** (`#4031`, L-11 (6) of the cold review of 2026-09-30).
A claim given the repeat itself was refused by each transport before it called ``claims.claim``,
since the refusal lived in ``tasks``, which imports ``claims``, so a third caller would have
claimed the series. Here, both import it and the domain stays free of an import cycle.
"""

import typing

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.domain.refs
import subroutine.errors


def live_occurrence (
	session: sqlalchemy.orm.Session, template: subroutine.db.models.work.Task
) -> subroutine.db.models.work.Task | None:
	"""Return the one unfinished occurrence of a series, or ``None`` if there is none.

	**There is exactly one at a time**, which is what makes decision `#1249` §4's write-through
	well defined: `materialise` mints the next only when the last is finished. ``None`` is an
	ordinary answer rather than a failure — a series whose rule is spent has no live row, and
	neither has a template somebody is holding mid-creation.

	Ordered by the slot it was minted for so that a database which has somehow been left with
	two answers the same question the same way twice, rather than differently each call.
	"""

	task = subroutine.db.models.work.Task

	return session.scalars(
		sqlalchemy.select(task)
		.where(
			task.recurrence_template_id == template.id,
			task.completed_at.is_(None),
			task.deleted_at.is_(None),
		)
		.order_by(task.occurrence_at.asc().nulls_last(), task.id.asc())
		.limit(1)
	).first()


def refuse_the_repeat_itself (
	session: sqlalchemy.orm.Session,
	series: subroutine.db.models.work.Task,
	*,
	act: str,
	verb: str,
	field: str,
) -> typing.NoReturn:
	"""Refuse an act that is only ever for one occurrence, given the repeat itself - `#3748`.

	**Refused by name rather than carried to the occurrence** (decision `#3795`). A series number
	means the series to every verb - ``done`` completes the series row - so a verb that read it as
	the occurrence would be the one exception a person had to learn. ``show`` prints that number as
	*from repeat #3* so that a rename or a reminder can reach the series (`#1247`), which is why it
	gets typed where the occurrence was meant: so the refusal names the occurrence, and the remedy
	is one retype.
	"""

	itself = subroutine.domain.refs.format_ref(series.ref)
	occurrence = live_occurrence(session, series)
	hint = (
		f"{verb.capitalize()} {subroutine.domain.refs.format_ref(occurrence.ref)}, the occurrence "
		"in front of you."
		if occurrence is not None
		else f"It has no occurrence open to {verb}."
	)

	raise subroutine.errors.ValidationError(
		f"{itself} is the repeat itself, and {act} is only ever for one occurrence of it.",
		code="invalid_field_value",
		hint=hint,
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{act[0].upper()}{act[1:]} is for one occurrence, never for the repeat itself.",
				hint=hint,
			)
		],
	)
