"""``subroutine explain <topic>`` — the concepts, not the commands.

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

import subroutine.db.models.saved
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.filtering


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

	for name, number in subroutine.domain.dates.WEEKDAYS.items():
		if len(name) > len(longest.get(number, "")):
			longest[number] = name

	weekdays = ", ".join(longest[number] for number in sorted(longest))

	# **The full month names, in calendar order** (`#1210`), on the same argument the weekdays
	# are trimmed by: an inventory of every accepted spelling is a parser's list rather than an
	# explanation, and the abbreviations get their own line.
	named: dict[int, str] = {}

	for name, number in subroutine.domain.dates.MONTHS.items():
		if len(name) > len(named.get(number, "")):
			named[number] = name

	months = _wrapped(
		tuple(named[number].title() for number in sorted(named)), indent=17
	)

	keywords = _wrapped(subroutine.domain.dates.KEYWORDS, indent=17)

	return f"""Four date fields, kept apart on purpose.

  due          A deadline. The date something has to be finished by.
  starts       When it begins. This is what 'agenda' shows.
               It takes a time, so 'monday at 14:00' is an appointment.
  until        When it is over, for something that lasts more than a
               moment - a holiday, a conference, a code freeze. Set it
               with 'plan 7 "14 august" --until "28 august"', or write
               it: 'Dentist on monday 2pm-3pm'. A calendar you have
               subscribed shows the whole run of days.
  deferred until
               The task does not appear at all before this. It is the
               only one of the four that hides anything.

Most tasks use one of them. Many use none.

Ways to write a date. **Everything below works at the command line wherever a
date is asked for; all but the last also work in a captured line, and the ones
marked (api) are accepted in a `due`, `starts`, `ends` or `snooze` field over
HTTP.** The one exception is `plan`, which asks for a day and refuses a time
of day - so a timestamp sets a deadline or a defer here and does not plan one.
A weekday name is
shorthand this tool resolves for you, so `subroutine plan 1 friday` works
while `{{"due": "friday"}}` is refused - send `2026-07-31` or `end_of_week`
there instead. /v1/meta publishes the exact list the API takes, under
grammars.relative_dates.

  a weekday      {weekdays}
                 - or mon, tue, wed, thu, fri, sat, sun
                 - the soonest such day, counting today
  next <weekday> the one in the following week
  a written date 1 September, 1 Sep, Sept 1, 14 March
                 {months}
                 - either way round, with or without the 'st'
                 - the soonest such date, counting today, so one written
                   in October means next year's
                 - no year: write 2027-03-14 when the year matters
                 - as a deadline beside a start in a captured line, a
                   weekday or a written date counts from the start, so
                   'on 20 July by 5 August' is due the same year
  today, tomorrow                                              (api)
  a date         2026-08-01                                    (api)
  a time         2026-08-01T17:00:00Z                          (api)
                 - not to 'plan', which takes a day
  an expression  {keywords}                                    (api)
                 with offsets: now+7d, end_of_week-1d, today+1w
                 - in a captured line 'now' needs an offset, so 'by now'
                   and 'from now on' are left as words
  an offset      +7d, +2w - the same, counted from today
                 - the command line only: '+' opens a project in a
                   captured line, so '+7d' there is left as words

Offset units are m minutes, h hours, d days, w weeks, M months, y years.
Case matters: 'm' is minutes and 'M' is months.

'Due Friday' means the end of Friday, so a task due Friday is not late on
Friday morning. Everything is read in your own timezone.

The same words ask a list about the past:

  subroutine list --filter created_at.gte=yesterday
  subroutine list --filter completed_at.gte=start_of_week
  subroutine list --filter created_at.gte=2026-08-02 --filter created_at.lt=today

Write it as field.operator=value. The operators are gte and gt for 'from',
lt and lte for 'until'; a bound takes in the whole day it names, so
'created_at.lte=yesterday' includes all of yesterday. Repeat --filter for a
range, and combine it with --project, --assignee or a search.

For what was *worked on* rather than what changed, ask touched_at:

  subroutine list --filter touched_at.gte=yesterday
  subroutine list --filter touched_at.gte=start_of_week --filter touched_by.eq=si

That covers a comment or a status change as well as an edit - neither of which
moves updated_at on the item itself. Claiming something does not count."""


def _capture_body () -> str:
	"""Build the capture topic from the grammar's own constants."""

	units = ", ".join(unit for unit, _minutes in subroutine.domain.durations.UNITS)

	# **Each meaning kept short on purpose.** The agent guide inlines this table under §13.3's
	# budget (`#1578`), and the span row `#2687` added was paid for by trimming meanings rather
	# than by raising it - `#12` is explained in the rules below, so the row no longer repeats it.
	rows = (
		(f"{', '.join(subroutine.domain.capture.DEADLINE_WORDS)} <date>", "sets a deadline"),
		(
			f"{', '.join(subroutine.domain.capture.PLANNED_WORDS)} <date>",
			"sets when it starts",
		),
		(", ".join(subroutine.domain.capture.BARE_PLANNED_WORDS), "the same, shorter"),
		(f"{', '.join(subroutine.domain.capture.DEFER_WORDS)} <date>", "hides it until then"),
		# **`#2687`**: a span is told from a defer by the word after its first date.
		("from <date> to <date>", "a span of days; also 'until', '2-12 October'"),
		("at <time>", "a time of day, after a date or alone"),
		# **`#675`**: the same signal reads two times, and two times are an appointment.
		("<time> to <time>", "an appointment's ends: 'at 2pm-3pm', 'at 9am til 5pm'"),
		# Added when the page was found still saying repeats were unread, four days after
		# they shipped (`#929`). The grammar reads them, so the table that lists the grammar
		# has to say so.
		("every <phrase>", "repeats: 'every day', 'every other tuesday'"),
		("#tag", "labels it, creating the tag if it is new"),
		("@name", "assigns it to somebody"),
		("!1 to !5", "how important it is"),
		("!3/5", "important and urgent, as a list shows it back"),
		("~90m, ~2h", "how long it will take"),
		# **"that already exists" is the whole of `#588`.** A tag and a project are the two
		# structural tokens here and they behave oppositely on first use — `#errand` creates
		# a tag silently and `+music` is refused — which is defensible, since a tag is a
		# label and a project is structure, and was said nowhere. A reader met the asymmetry
		# as a refusal on the first realistic line they wrote.
		("+KEY", "puts it in a project that already exists"),
	)

	# Aligned here rather than in the template, because the left column is generated from
	# the grammar's own constants and its width changes whenever a word is added.
	width = max(len(token) for token, _meaning in rows)
	table = "\n".join(f"  {token.ljust(width)}  {meaning}" for token, meaning in rows)

	return f"""One line becomes a task. Anything not understood stays in the title.

  subroutine add 'Call the dentist before Sunday !3 ~15m #health'

{table}

Rules worth knowing:

  Nothing is ever lost. 'Email Bob re: 3pm' stays exactly as typed, because
  none of it is grammar. So does 'Fix issue #12': a reference is *entirely*
  digits and a tag is anything else, so #12 is item 12 and #3d-printing is a tag.

  An estimate needs a unit ({units}), so '~5 people' is not five minutes.

  A tag is made as you write it and a project is not. '#errand' invents the
  tag; '+errand' is refused unless that project is already there, because a
  label is cheap and a place to put work is structure. Make one first with
  'subroutine project create', or leave the +KEY off and it goes where this
  connection ordinarily files things.

  A repeat it cannot read stays in the title and says so, rather than being
  guessed at: 'every fortnight' is left alone and 'every 14 days' is read.

  A line with a '!' in it needs single quotes. In an interactive bash or zsh
  a '!' fetches an earlier command, and double quotes do not stop it: the
  line is refused, or an old command's text silently lands in the title.

  A title that starts with two hyphens needs '--' in front of it, because the
  shell hands it over looking exactly like an option:

    subroutine add -- '--json is the machine-readable form'

  Everything after the '--' is the line, whatever it begins with."""


def _estimates_body () -> str:
	"""Explain what a unit of estimate means, in the terms somebody meets it in — `#544`.

	**The units table is generated from the vocabulary itself**, so a unit added or re-sized
	cannot leave this page describing the one before it. That is the whole failure being fixed:
	the program has always been right and consistent, and nothing told the person typing.
	"""

	# **Each unit in terms of the next one down**, which is the fact somebody is missing.
	# `humanize` renders a duration in the largest unit that fits, so asking it what a week is
	# answers `1w` — true, and the one answer that teaches nothing.
	units = subroutine.domain.durations.UNITS
	sizes = []

	for index, (unit, minutes) in enumerate(units[:-1]):
		below, size = units[index + 1]
		sizes.append(f"  1{unit}  is  {minutes // size}{below}")

	smallest, _one = units[-1]
	sizes.append(f"  1{smallest}  is  one minute, which is what everything is stored in")

	table = "\n".join(sizes)

	return f"""An estimate is how long you think something will take. Write it with a
tilde, in a captured line or with --estimate:

  subroutine add "Rewrite the importer ~4h"
  subroutine update 42 --estimate 90m

A day is twenty-four hours, and a week is seven of those:

{table}

**That is calendar time, not working time.** '~1d' is 24 hours and not the
day you would spend on it, and '~1w' is 168 hours and not a working week.
If you mean a working day, write '~8h'; a working week is '~40h'.

Nothing here knows about weekends, holidays, or how long your day is, and
it is deliberate: the moment a unit means "however long you work", it
means something different for each person reading the same number.

Units go largest to smallest and every duration has exactly one spelling,
so '1h30m' is right and '30m1h' is refused. You can always write plain
minutes instead - '90' is the same as '1h30m'.

This is not a deadline. An estimate says how long, and a deadline says by
when; 'subroutine explain dates' is the other one."""


def _searching_body () -> str:
	"""Explain the written search line — item `#1806`, design `#1801` §6.

	**The field names are read from the registry rather than written here**, which is the whole
	shape of `#1803`: a list in a help string is a second copy that falls behind, and this one
	would fall behind on the day somebody declares a field. Six were declared while this item
	was being built.
	"""

	names = _wrapped(sorted(subroutine.domain.filtering.filters("task")), indent=2)

	return f"""A search is words, and it can also carry terms that narrow it.

  subroutine search "deploy script"
  subroutine search "type:bug urgency>3 deploy"

A term is a field, a symbol and a value. ':' means equals; '>' '<' '>='
'<=' and '!=' compare. A comma means any of them, so 'status:open,done'
is either. 'set' and 'unset' ask whether the field has a value at all,
so 'assignee:unset' is what nobody has picked up.

The fields a task can be narrowed by:

  {names}

Anything that is not a term is looked for as written. '15:30' is a time,
not a field called 15, so an ordinary search needs no escaping and works
exactly as it always has.

If you write a term the field cannot take - 'created_at:today', where a
date is compared with '>' or '<' rather than matched exactly - it is
looked for as text and you are told, rather than quietly dropped.

To mean a value with a space in it, quote it: 'tag:"garden work"' is
one tag. Quoting also reaches an account named after a reserved word
before such names were refused, as in 'assignee:"unset"'."""


#: Every topic ``subroutine explain`` knows.
#:
#: **Some of these are published twice, and the second place has a byte budget** (`#1578`).
#: ``/v1/docs/agent`` inlines each topic named in :data:`subroutine.api.meta.GUIDE_TOPICS`, and
#: ``tests/test_api_meta.py`` holds that guide under a fixed size, so a sentence added to one of
#: those topics can fail a test about the API. Look there before writing more: the remedy is to
#: trim, or to take a topic out of the guide, and not to raise the budget.
def _views_body () -> str:
	"""Build the saved-views topic, reading the arrangements from the vocabulary itself.

	Generated rather than transcribed for the reason this module opens with: a page offering
	an arrangement nothing accepts is worse than no page, and the two would drift inside a
	release. The word a terminal draws is named in ``cli/personal.py`` and checked against
	this body by ``tests/test_personal_path.py``, because prose cannot import it.
	"""

	names = list(subroutine.db.models.saved.ARRANGEMENTS)
	offered = f"{', '.join(names[:-1])} or {names[-1]}"

	return f"""A saved view is two things under one name: what it narrows to, and
how it is drawn. The narrowing is a search line - the same line you
would type into 'subroutine search'. The arrangement is how a browser
lays the results out.

Save what you would otherwise retype:

  subroutine view save "My bugs" --q "type:bug assignee:me"

The name you run it by is made from the title, so "My bugs" answers to
'my-bugs':

  subroutine view run my-bugs

A view is yours alone until you say otherwise. Share one when it is the
queue a team works from, and anybody here can run it by name:

  subroutine view save "Team queue" --q "urgency>=4" --shared

Only the person who saved a view can change or remove it, shared or
not.

WHAT A TERMINAL DOES WITH THE ARRANGEMENT

A view is saved as one of {offered}, and can ask for
the results to be grouped by a field. A terminal has a list and no
board, so it keeps the narrowing, draws it flat, and says which part
it is not drawing on the line above the results. Nothing is quietly
turned into something else.

That line goes to standard error, so a view piped somewhere, or asked
for with --json, gives you the results and nothing else.

CHANGING ONE

Name only the parts you are changing:

  subroutine view edit my-bugs --q "type:bug urgency>=4"

  subroutine view edit team-queue --private

Writing nothing after --q, --order or --group-by clears that part,
which is a different instruction from leaving the flag off. Renaming
changes the name you run it by, because one is made from the other."""


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
		name="estimates",
		summary="How long something will take, and what a day means when you write one.",
		body=_estimates_body(),
	),
	Topic(
		name="searching",
		summary="Finding things by their words, and narrowing the same line by their fields.",
		body=_searching_body(),
	),
	Topic(
		name="views",
		summary="Saving a narrowing under a name, and what a terminal does with one.",
		body=_views_body(),
	),
	Topic(
		name="refs",
		summary="How tasks are named, and how to address them without typing a name.",
		body="""Every task has a number of its own - its ref - and that number never
changes. Not when the task moves to another project, not when something
above it in a list is finished, not ever. Numbers are shared with
documents and are never reused, so they grow and leave gaps.

Any list this tool prints shows it:

  subroutine agenda
    #1  Call the dentist  (due Sun 2 Aug)
    #7  Buy milk

  subroutine show 7
  subroutine done 7

The # is how a ref is written down - in a note, a commit message, or a
task's own description, where #7 shows up as a reference back on task
7. You do not have to type it, and mostly you should not: a shell
treats # as the start of a comment, so

  subroutine done #7

reaches this tool as 'subroutine done' with nothing after it. Type the
bare number, or quote it as '#7'.

Because the number belongs to the task rather than to the list, one you
remember goes on working tomorrow, in another terminal, after anything
else has been finished.""",
	),
	Topic(
		name="connecting",
		summary="Where your work lives, and how to reach it from here or from an agent.",
		body="""Your work can be on this machine, on a server somebody runs, or
both at once. Each of those is a connection, and your own database is
one of them - it is called 'local' and it exists whether or not you
say so. 'subroutine connections' lists them.

To reach a server as well, you need its address and a token from
whoever runs it. Then:

  subroutine connections add work --url https://tasks.example.com

It asks for the token, reaches the instance to check both, and writes
nothing until they work. The name - 'work' here - is yours, and it
becomes the first part of every address that server's items print as.

From then on one list shows both:

  subroutine list
    Local
                #1  Pay the gas bill
    work
      work/acme/#1  Fix the deploy script

Reading always spans everything you can reach, so nothing is hidden by
being in the wrong place. Only writing picks one, and 'subroutine use
work' is how you move it.

An agent reaches an instance a different way - through a plugin rather
than through this program, and if the work is on somebody else's
server it needs nothing installed at all. That is a longer story than
a terminal needs, and it is written up in docs/connecting.md in the
project's repository.

Where a token is kept decides what can take it away. 'connections add'
keeps it in credentials.toml, which only you change. A token typed into
a Claude Code plugin's field is kept by Claude Code, which deletes it
when you sign out of Claude Code, uninstall the plugin or remove its
marketplace. An agent's own credential is safest in its project, put
there by 'subroutine agent create <name> --here'.

And somebody who wants none of this can use the web interface, which
needs nothing installed and no token. Whoever runs the instance hands
them a sign-in link:

  subroutine login link --username keanu

It signs in as whoever it names, once, and stops working after half an
hour. A token is not a substitute for it and will not sign anybody in
to a browser.""",
	),
	Topic(
		name="scripting",
		summary="Machine-readable output, and how to run commands as somebody else.",
		body="""Every command that reads takes --json:

  subroutine agenda --json
  subroutine list --json
  subroutine show 7 --json
  subroutine add "Buy milk" --json

A listing's JSON carries the ref, the title, the dates and the tags -
enough to act on without asking again. 'show' carries the whole item
instead, with its links and everything recorded against it, because the
reason to ask about one thing is to read what a list left out.

There is no login for local use: the file permissions on your database
are what protect it. If you want to give an agent narrower access than
your own, issue it a token and set SUBROUTINE_TOKEN. Every command that
reads or changes your work then obeys it exactly as it would over the
network - a token scoped to task:read cannot add anything.

What it does not bound is anybody who can reach the file. The 'db'
commands open the database directly, because they have to work when the
service will not start, so 'db backup' and 'db restore' answer to the
file permissions rather than to a token. Neither could anything else:
somebody who can run these can read your config.toml, find the database
and open it themselves. **If the boundary has to hold, it needs a server
between them and the file** - that is what 'subroutine serve' and a
token over the network are for.""",
	),
	Topic(
		name="handing-back",
		summary="What to do with work you cannot finish, and whom it goes back to.",
		body="""When something you were given cannot go on without an answer, hand it
back rather than stopping or guessing. That means a question the item,
the decisions behind it and the code do not answer - anything resting on
taste, priority, scope or a word somebody will read - or a permission
you do not have. Hard is not the same as blocked.

It goes back to one person, chosen in this order:

  1. Whoever assigned it to you. 'subroutine show 42' says who, as
     'assigned by @jo'. Not when that is you, and not when it came to
     you as a question: that one you answer, or pass up.
  2. Otherwise your account parent, the account yours was created by.
     'subroutine whoami' names it. Every agent answers to a person in
     the end, so a question always reaches one.
  3. Whoever answers gives it back to whoever asked.

How you hand it over says what you mean:

  Asking       needs_input   I can do this once you answer.
  Giving back  unchanged     This is not mine to do.
  Answering    open          Here is what you needed. Carry on.
  Finishing    done          Nothing moves: it stays with you.

Asking is three commands:

  subroutine update 42 --status needs_input --assignee jo
  subroutine comment 42 "Which way should the flag read? I'd pick the second."
  subroutine release 42

The comment is the hand-back. Say what you need, why it is theirs to
decide, what you would choose, and - when you are passing a question up
- who below you is waiting on it, because an item names only whoever
assigned it last.

A question that came to you is never sent back unanswered: handing back
is an assignment too, so the one who asked is now named as the assigner.
An item going back and forth - asked, answered, asked again - is not
that, because its status changes every time.

Finishing does not hand anything back. Left with you, the item still
names who assigned it, and that is how they find it:

  subroutine list --filter assigned_by.eq=me --filter completed_at.gte=yesterday

If they need to check the work first, give it back open and say it is
ready to check. Cancelling something you were given is theirs to decide,
so give it back instead.

One hand-back is no reason to stop: carry on with something else. Two
in a row are, because they say the work is not clear enough to do, and
that is for the person to put right.""",
	),
	# **`#3397`, on Simon's decision of 2026-09-24.** The program's own channel is current with
	# whatever program is installed, where the plugin's skill reaches a session through a cache
	# that lags (`#499`) and is held for `#3496`'s update besides (`#3510`). So this is where an
	# agent with a shell learns decision `#3391` first.
	Topic(
		name="milestones",
		summary="Features, milestones and the three links that plan work.",
		body="""A feature is a parent task: its sub-tasks are its parts, all in one
project, and it cannot be started while any of them is unfinished.

A milestone is an item of the 'milestone' type - a release, a phase, a
launch. It is what work counts toward, never work itself, so nothing
offers one as the next thing to start: not 'list --ready', not an
agent's next work, and not the agenda's Next.

Its date is a deadline, so write it with 'by'. It can go overdue:

  subroutine add "Ship 1.0 by 2026-10-01" --type milestone

What counts toward it is an 'includes' link, from the milestone to the
work. It includes tasks - another milestone among them, so a phase can
sit inside a roadmap - and never a document, which is never finished.
One piece of work may count toward several milestones.

  subroutine link 12 includes 43,44,45

Three links plan work, and they say different things:

  includes   12 includes 43     43 counts toward milestone 12
  blocks     12 blocks 13       12 has to be finished or reached first
  precedes   12 precedes 13     12 is planned first; nothing waits

Only 'blocks' holds anything up, so work counted toward a milestone
is still offered as ready.

'show' counts what a milestone includes, as 'Links (3 of 5 included
done)', its row on every listing says the same, and 'show --tree'
walks what it includes and what that waits for.

Nothing completes a milestone by itself. When all of it is done its row
says 'included done', and whether it has been reached is yours to
decide: 'subroutine done 12'. A milestone with no date is on no day of
the agenda, which counts how many there are instead.

A roadmap is the milestones in date order:

  subroutine list --type milestone --order due_at""",
	),
)

_BY_NAME = {topic.name: topic for topic in TOPICS}


def find (name: str) -> Topic | None:
	"""Return a topic by name, or ``None``."""

	return _BY_NAME.get(name.strip().lower())


def names () -> typing.Sequence[str]:
	"""Return every topic name, in the order they are offered."""

	return [topic.name for topic in TOPICS]
