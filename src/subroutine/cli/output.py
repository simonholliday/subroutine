"""What reaches the terminal is text, not instructions to it.

Its own module because both ``cli/main`` and ``cli/personal`` print, and ``personal`` is
registered *by* ``main`` — so anything they share has to sit beside them rather than in
either one.
"""

import datetime
import re
import typing

import rich.console
import rich.text

#: Everything a terminal reads as an instruction rather than as text: ``ESC``, the rest of the
#: C0 controls Rich does not already drop, ``DEL``, and the C1 range some terminals still
#: decode as escapes. Newline and tab are text here and are kept.
_INSTRUCTIONS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain (message: str) -> str:
	"""Return a line with anything a terminal would obey rather than show taken out.

	**Rich's markup is off and that is a different problem.** ``[bold]`` in a title is
	neutralised already (`#682`); an ANSI escape is not — measured, ``ESC[2K`` reaches the
	terminal exactly as written, where ``BEL`` is dropped. So a title could clear the line
	above it, repaint what was there, or move the cursor, and every one of the four things
	that print here carries a title.

	**Titles arrive from other people, from agents and from merged remote instances**, which
	is what makes this worth doing rather than theoretical: on a shared instance the text
	being printed was written by somebody who is not the reader, and §13.7 merges an agenda
	across connections that are not even the same installation.

	Removed rather than shown as an escape: they are instructions rather than characters, and
	the job here is to print the title. Anything wanting them verbatim reads ``--json``, where
	JSON's own escaping already renders them as text.
	"""

	return _INSTRUCTIONS.sub("", message)


class Terminal (rich.console.Console):
	"""A console that prints what it is given as text rather than as instructions.

	**Rich's markup is off everywhere here and that is a different problem.** ``[bold]`` in a
	title is neutralised already (`#682`); an ANSI escape is not — measured, ``ESC[2K`` reaches
	the terminal exactly as written, through a plain string *and* through a ``rich.text.Text``,
	where ``BEL`` is dropped by Rich itself. So a title could clear the line above it, repaint
	what was there, or move the cursor.

	**Titles arrive from other people, from agents and from merged remote instances**, which is
	what makes this worth doing rather than theoretical: on a shared instance the text being
	printed was written by somebody who is not the reader, and §13.7 merges an agenda across
	connections that are not even the same installation.

	**Here rather than at each place a title is put into a line**, because that is a list of
	the places somebody thought of — the listing alone builds lines in nine of them — and this
	is the one object all of them print through.

	A ``Text`` is left exactly as it is unless something really needs removing, so styling and
	search highlighting are untouched on every ordinary line. Where something does, the spans
	go with it: a line carrying a terminal escape has more wrong with it than its colours.
	"""

	def print (self, *objects: typing.Any, **options: typing.Any) -> None:
		"""Print, with anything a terminal would obey rather than show taken out."""

		super().print(*(_shown(item) for item in objects), **options)


def _shown (item: typing.Any) -> typing.Any:
	"""Return one thing to print, as text rather than as instructions to the terminal."""

	if isinstance(item, str):
		return plain(item)

	if isinstance(item, rich.text.Text):
		cleaned = plain(item.plain)

		return item if cleaned == item.plain else rich.text.Text(cleaned, style=item.style)

	return item


def sign_in_lines (
	*, username: str, url: str, minutes: int, address_assumed: bool
) -> list[str]:
	"""Return what to print when a sign-in link has just been made — item `#587`.

	**Here rather than in either command, because two of them mint one now.** ``login link``
	has printed this since `#248`; ``user create --browser`` prints it as the last step of
	onboarding somebody. A second copy is this codebase's signature defect, and the half that
	would have rotted is the ``public_url`` note — it was added by `#1007` to one caller, and a
	caller written afterwards would not have known to carry it.

	**Said as a duration rather than a clock time, deliberately.** Half an hour is short
	enough that "until 14:12" makes a reader work out what that means from now, in a timezone
	they then have to be sure about — and the answer they want is how long they have.

	Takes plain values rather than the view, so this module goes on importing nothing but the
	terminal: its own first paragraph is that it sits beside ``cli/main`` and ``cli/personal``
	rather than inside either, and that only holds while it is cheap to import.
	"""

	said = [
		f"A sign-in link for {username}, good for the next "
		f"{minutes} {'minute' if minutes == 1 else 'minutes'}.",
		"",
		url,
		"",
		"That is the only time it is shown, and it works once.",
	]

	# **Said, not asked** (`#1007`). The assumption is the bind this instance listens on, which
	# is a fact the program holds and the reader does not — so a prompt would ask somebody to
	# confirm a value they have less evidence about than the program does, break every script
	# and agent that runs this, and grow the one path §1.4 wants shortest. A wrong address
	# costs one re-run, because the host in a link is navigation rather than authority.
	if address_assumed:
		said.extend(
			[
				"",
				"Nobody has set public_url, so that address is where this instance listens. "
				"It works in a browser on this machine; if you reach it another way — a "
				"proxy, another machine — set public_url and make a fresh link.",
			]
		)

	return said


def minutes_until (moment: datetime.datetime, now: datetime.datetime) -> int:
	"""Return how many whole minutes are left before this instant — item `#587`.

	Beside :func:`sign_in_lines` because it is the number that sentence reads, and rounding it
	somewhere else would let two callers of one rendering disagree about *the same link* by a
	minute. Rounded rather than truncated: a link with 29 minutes and 40 seconds left has half
	an hour on it in every sense a reader cares about.
	"""

	return round((moment - now).total_seconds() / 60)
