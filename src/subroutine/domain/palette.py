"""The colours a workspace or a project may be marked with — design `#1023`, item `#1026`.

**A name, never a value.** Decision `#102` restricts the terminal to the sixteen basic ANSI
names precisely so that a hue belongs to the reader's own theme rather than to us — so a stored
hex is unrenderable on a surface this program publishes, and anything appearing on more than one
surface has to be stored as a name. That is `#524`'s lesson stated once more: store the stable
classifier and let each client render it.

**So this module holds words and no colours at all.** What each name looks like is
``web/assets/app.css``'s business, in both themes, held to a contrast floor by
``tests/test_browser.py``; what the terminal would make of one is nobody's yet (`#1023` §8.5,
Simon's decision of 2026-08-19: not now). A surface that cannot draw a hue ignores the field
rather than needing a renderer it will never use.

**Eight, which is the conventional maximum for a categorical palette.** The consistent finding
across data-visualisation practice is eight as a conservative ceiling, ten commonly and twelve as
the outer limit of established systems — and those are for a chart somebody studies, not a mark
glanced at in a row. Past that, neighbours stop separating and a colour that looks like it means
something and does not is worse than no colour.

**Red and blue are deliberately absent**, and that is not taste: ``--warn`` is red and carries
overdue and blocked, ``--accent`` is blue and carries focus and a live claim. A project wearing
either would put two meanings on one hue on the same page, which is the duplicated-rule defect
in a stylesheet.
"""

import subroutine.errors

#: Every colour a workspace or a project may be given, in the order a chooser offers them.
#:
#: **Ordered by hue rather than alphabetically**, so a person picking one moves along a spectrum
#: rather than hunting a list — and so that two names next to each other here are the two most
#: likely to be confused, which is where a reader looking for separation will look first.
#:
#: ``slate`` is the neutral and is last for that reason: it is the one choice that says *marked,
#: but quietly*, which is what somebody wants for the project everything else is measured
#: against.
NAMES: tuple[str, ...] = (
	"amber",
	"green",
	"teal",
	"cyan",
	"indigo",
	"violet",
	"magenta",
	"slate",
)


def refuse_unknown (value: str, *, key: str) -> str:
	"""Return the colour named, or refuse it with the whole vocabulary.

	**By name and with the alternatives**, because a palette is a closed set and the caller
	cannot discover it from a rejection that only says no. The same shape every vocabulary
	refusal here takes.
	"""

	if value in NAMES:
		return value

	raise subroutine.errors.ValidationError(
		f"{value!r} is not a colour this instance offers.",
		errors=[
			subroutine.errors.FieldError(
				field=key,
				code="invalid_field_value",
				message=f"Unknown colour {value!r}.",
				hint=f"Available colours: {', '.join(NAMES)}.",
			)
		],
	)
