"""``subroutine help <topic>`` — the concepts, not the commands.

``--help`` teaches the verbs. This teaches the model: what a ref is, what dates mean, what
the capture grammar will and will not read. §12.2a asks for both because a user who knows
every flag and none of the ideas still cannot use the tool — and because the alternative is
a documentation site, which is a worse place for this than the terminal the person is
already in.

Written as data rather than as code so that the same text can be served at
``/v1/docs/agent`` when the API lands, instead of being written twice and diverging.
"""

import dataclasses
import textwrap
import typing

import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations


@dataclasses.dataclass(frozen=True)
class Topic:
	"""One concept, explained in the terms somebody meets it in."""

	name: str
	summary: str
	body: str


def _wrapped (items: typing.Sequence[str], *, indent: int) -> str:
	"""Return a comma-separated list wrapped to a narrow terminal.

	Help that runs off the right edge of an eighty-column terminal is help nobody reads,
	and these lists are generated from the parser so their length is not fixed.
	"""

	lines = textwrap.wrap(", ".join(items), width=78 - indent)

	return f"\n{' ' * indent}".join(lines)


def _dates_body () -> str:
	"""Build the dates topic, reading the vocabulary from the parser that enforces it.

	Generated rather than transcribed: a help page that lists a keyword the parser does not
	accept is worse than no help page, and the two would drift within a release.
	"""

	# The seven full names in week order, not every accepted spelling. "monday, tues,
	# wednesday, thurs" is a parser's inventory rather than an explanation, so each day
	# contributes its longest spelling and the abbreviations get one line of their own.
	longest: dict[int, str] = {}

	for name, number in subroutine.domain.capture.WEEKDAYS.items():
		if len(name) > len(longest.get(number, "")):
			longest[number] = name

	weekdays = ", ".join(longest[number] for number in sorted(longest))

	keywords = _wrapped(subroutine.domain.dates.KEYWORDS, indent=17)

	return f"""Three date fields, kept apart on purpose.

  due          A deadline. The date something has to be finished by.
  planned for  The day you intend to do it. This is what 'today' shows.
  hidden until A defer. The task does not appear at all before this.

Most tasks use one of them. Many use none.

Ways to write a date:

  a weekday      {weekdays}
                 — or mon, tue, wed, thu, fri, sat, sun
                 — the soonest such day, counting today
  next <weekday> the one in the following week
  today, tomorrow
  a date         2026-08-01
  a time         2026-08-01T17:00:00Z
  an expression  {keywords}
                 with offsets: now+7d, end_of_week-1d, today+1w

Offset units are m minutes, h hours, d days, w weeks, M months, y years.
Case matters: 'm' is minutes and 'M' is months.

'Due Friday' means the end of Friday, so a task due Friday is not late on
Friday morning. Everything is read in your own timezone."""


def _capture_body () -> str:
	"""Build the capture topic from the grammar's own constants."""

	units = ", ".join(unit for unit, _minutes in subroutine.domain.durations.UNITS)

	rows = (
		(f"{', '.join(subroutine.domain.capture.DEADLINE_WORDS)} <date>", "sets a deadline"),
		(
			f"{', '.join(subroutine.domain.capture.PLANNED_WORDS)} <date>",
			"sets the day you will do it",
		),
		(", ".join(subroutine.domain.capture.BARE_PLANNED_WORDS), "the same, said shorter"),
		(f"{', '.join(subroutine.domain.capture.DEFER_WORDS)} <date>", "hides it until then"),
		("#tag", "labels it, creating the tag if it is new"),
		("@name", "assigns it to somebody"),
		("!1 to !5", "how important it is"),
		("~90m, ~2h", "how long you think it will take"),
		("+KEY", "puts it in a particular list"),
	)

	# Aligned here rather than in the template, because the left column is generated from
	# the grammar's own constants and its width changes whenever a word is added.
	width = max(len(token) for token, _meaning in rows)
	table = "\n".join(f"  {token.ljust(width)}  {meaning}" for token, meaning in rows)

	return f"""One line becomes a task. Anything not understood stays in the title.

  subroutine add "Call the dentist before Sunday !3 ~15m #health"

{table}

Rules worth knowing:

  Nothing is ever lost. 'Email Bob re: 3pm' stays exactly as typed, because
  none of it is grammar. So does 'Fix issue #12' — a tag starts with a letter.

  An estimate needs a unit ({units}), so '~5 people' is not five minutes.

  'every monday' is not read yet. It stays in the title and is left alone."""


TOPICS: tuple[Topic, ...] = (
	Topic(
		name="dates",
		summary="Deadlines, planned days and defers, and every way to write one.",
		body=_dates_body(),
	),
	Topic(
		name="capture",
		summary="The shorthand `add` understands, and what it deliberately does not.",
		body=_capture_body(),
	),
	Topic(
		name="refs",
		summary="How tasks are named, and how to address them without typing a name.",
		body="""Every task has a number of its own — its ref — and that number never
changes. Not when the task moves to another project, not when something
above it in a list is finished, not ever. Numbers are shared with
documents and are never reused, so they grow and leave gaps.

Any list this tool prints shows it:

  subroutine today
    #1  Call the dentist  (due Sun 2 Aug)
    #7  Buy milk

  subroutine done 7

The # is how a ref is written down — in a note, a commit message, or a
task's own description, where #7 becomes a link to that task. You do
not have to type it, and mostly you should not: a shell treats # as the
start of a comment, so

  subroutine done #7

reaches this tool as 'subroutine done' with nothing after it. Type the
bare number, or quote it as '#7'.

Because the number belongs to the task rather than to the list, one you
remember goes on working tomorrow, in another terminal, after anything
else has been finished.""",
	),
	Topic(
		name="scripting",
		summary="Machine-readable output, and how to run commands as somebody else.",
		body="""Every command that reads takes --json:

  subroutine today --json
  subroutine ls --json
  subroutine add "Buy milk" --json

The JSON carries the ref, the title, the dates and the tags — enough to
act on without asking again.

There is no login for local use: the file permissions on your database
are what protect it. If you want to give an agent narrower access than
your own, issue it a token and set SUBROUTINE_TOKEN; the same limits
then apply here as would apply over the network.""",
	),
)

_BY_NAME = {topic.name: topic for topic in TOPICS}


def find (name: str) -> Topic | None:
	"""Return a topic by name, or ``None``."""

	return _BY_NAME.get(name.strip().lower())


def names () -> typing.Sequence[str]:
	"""Return every topic name, in the order they are offered."""

	return [topic.name for topic in TOPICS]
