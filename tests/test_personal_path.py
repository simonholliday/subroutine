"""The personal path end to end — docs/design.md §13.5b, run as a test rather than asserted.

The gating criterion is four commands on a fresh installation, and **none of their output
mentioning a workspace, a status, a project, a criterion, a verification, a session or a
claim**. That vocabulary check is the guard on §1.4's progressive-disclosure rule, and it
is meant to fail the first time somebody adds a required field for an agent's benefit.

These run the real CLI against a real database in a temporary XDG home, because the parts
most likely to break are the ones only the wiring exercises: the config file, the state
directory, the local-mode principal, and the numbering that makes ``done 1`` work.
"""

import ast
import datetime
import json
import os
import pathlib
import re
import shlex
import sys
import typing
import uuid

import click
import pytest
import rich.console
import rich.text
import typer.main
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.directory
import subroutine.domain.capture
import subroutine.domain.comments
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.schedule
import subroutine.errors
import subroutine.fanout
import subroutine.views

#: docs/design.md §13.5b, verbatim. A person setting up a to-do list has not asked about any of
#: these, and meeting one means the personal path has started leaking the full model.
FORBIDDEN = (
	"workspace",
	"status",
	"project",
	"criterion",
	"verification",
	"session",
	"claim",
)

#: Words §13.5b does not list and this product still never says to a person.
#:
#: **Kept apart from `FORBIDDEN` because that tuple is the specification verbatim**, and a test
#: that quietly widens a quoted list stops being able to say what the specification requires.
#:
#: *template* is the one that has bitten (`#1310`). The vocabulary was already decided —
#: `views.THE_SERIES` is *"the repeat itself"* and `FROM_THE_REPEAT` is *"from repeat"* — and
#: `recurrence_template_id` still rendered as *"recurrence template"*, because a rendering was
#: only ever asked about the seven words above and about an `_id` suffix.
OUR_WORD_NOT_THEIRS = ("template",)


@pytest.fixture
def home (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> typing.Iterator[pathlib.Path]:
	"""Point every XDG directory at a fresh temporary home.

	``tmp_path`` rather than anywhere in the working tree: this repository lives on a
	network share where SQLite cannot take a lock.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	# Nothing about the shell the suite was started in may reach these commands. A token
	# would narrow local mode; a workspace or a connection would move the current context
	# (§13.7), and a test that passes or fails depending on the developer's exports is the
	# least useful kind of flake.
	for name in list(os.environ):
		if name.startswith(("SUBROUTINE_TOKEN", "SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	yield tmp_path


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Return a runner for the real CLI, failing loudly on an unexpected non-zero exit."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0, input: str | None = None) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		# Each call is a fresh shell, and in a real one each command is its own process —
		# so the once-per-process configuration warning is once per command. Reset here
		# rather than per test, or the first `init` in a test consumes it for the rest.
		subroutine.cli.main._said_unknown_settings = False

		result = runner.invoke(subroutine.cli.main.app, list(arguments), input=input)

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


@pytest.mark.parametrize(
	"command",
	[("list",), ("agenda",), ("add", "Buy milk"), ("show", "1")],
	ids=["list", "agenda", "add", "show"],
)
def test_an_instance_nobody_created_says_so_and_names_the_one_command (
	run: typing.Callable[..., typer.testing.Result], command: tuple[str, ...]
) -> None:
	"""`#165`. Very likely the first thing anybody sees, and it used to dead-end.

	The plugin installs cleanly and the MCP server starts fine against an instance that does
	not exist, so the tools *are* available and every call fails with ``unable to open database
	file`` and advice to check ``database_url`` — a reachability remedy for a problem that is
	not one. The answer is ``subroutine init``, and nothing said so: the correct instruction
	existed only in ``marketplace.json`` and ``plugin.json``, neither of which anybody reads.

	Every command, not one, because there is no reason to think the person's first word will
	be the one we tested.
	"""

	refused = run(*command, expect=1)

	assert "no Subroutine instance has been set up here yet" in refused.output
	assert "subroutine init" in refused.output

	# And the wrong remedy is gone rather than merely joined by the right one.
	assert "database_url" not in refused.output


def test_the_four_command_personal_test (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§13.5b: a fresh installation to a completed task, in four commands, no documentation."""

	first = run("init")

	assert first.output.strip() == 'Ready. Try: subroutine add "something to do"'

	second = run("add", "Call the dentist before Sunday")

	assert "Added: Call the dentist" in second.output

	third = run("agenda")

	assert "Call the dentist" in third.output

	fourth = run("done", "1")

	assert "Done: Call the dentist" in fourth.output

	# And it is gone from the list afterwards, which is the whole point of the fourth
	# command.
	assert "Call the dentist" not in run("agenda").output


def test_no_command_in_the_personal_path_mentions_the_full_model (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The guard on docs/design.md §1.4, and the one meant to fail when somebody forgets it.

	Every word here names something a person setting up a to-do list has not asked about.
	The moment one appears, the personal path has stopped being a personal path.
	"""

	run("init")
	run("add", "Call the dentist before Sunday")
	run("add", "Buy milk")

	transcript = "\n".join(
		run(*command).output
		for command in (("agenda",), ("ls",), ("done", "1"), ("plan", "1", "tomorrow"))
	)

	for word in FORBIDDEN:
		assert word not in transcript.lower(), f"the personal path said {word!r}:\n{transcript}"


def test_a_bare_invocation_shows_the_agenda_rather_than_a_help_wall (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""docs/design.md §12.2a: the first thing this tool does unprompted should be useful."""

	run("init")
	run("add", "Buy milk")

	result = run()

	assert "Buy milk" in result.output
	assert "Usage:" not in result.output


def test_every_command_suggests_the_next_one (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a's most valuable habit: the user is never left wondering what exists."""

	run("init")

	assert "subroutine add" in run("init").output
	assert "Tip: subroutine agenda" in run("add", "Buy milk").output
	assert "Tip: subroutine done" in run("agenda").output
	assert "Tip: subroutine agenda" in run("done", "1").output


def test_the_agenda_never_advises_ticking_off_somebody_s_birthday (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1236`, decision `SR#1235` §5 — the defect was the *advice*, not the permission.

	Measured on a disposable instance on 2026-08-25: a birthday dated 14 March sat under
	**Today**, five months late, with the agenda's own closing line reading ``subroutine done
	2``. Completing one is still accepted — §13.5 says an error names what to do next and a
	refusal here would be a wall — but a product that suggests ticking off somebody's birthday
	is answering a question nobody asked.

	**Both halves, because either alone passes against a mistake.** The tip must name the task,
	and it must reach for it *past* the event: an implementation that simply skipped the whole
	first section would pass an assertion about the birthday and lose the tip on a day whose
	only work was in that section.
	"""

	run("init")
	# **A whole date rather than "14 march"**, which the grammar reads as *the next one* and
	# resolves to 2027 — so the assertion below would have passed because the birthday is past
	# the look-ahead rather than because it has gone by, which is a different feature entirely.
	run("add", "Anna's birthday on 2026-03-14", "--type", "event")
	run("add", "Water the plants")

	shown = run("agenda").output

	assert "Anna's birthday" not in shown, (
		f"an event five months past is still on the agenda:\n{shown}"
	)
	assert "1 already past" in shown, (
		f"it left the day without the page accounting for it, which is the unexplained "
		f"difference against a listing at the same scope:\n{shown}"
	)

	# The same page with the event happening *now*, so it is on it and the tip has to step
	# over it rather than over the section.
	run("add", "Code freeze", "--type", "event")
	run("plan", "3", "today")

	shown = run("agenda").output

	assert "Code freeze" in shown, f"an event happening today is not on the agenda:\n{shown}"
	assert "Happening" in shown, f"it is not under a heading of its own:\n{shown}"
	assert "Tip: subroutine done 2" in shown, (
		f"the agenda advises finishing something that merely happens:\n{shown}"
	)


def test_the_agenda_never_advises_finishing_work_somebody_else_is_holding_up (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1288`, and it is `SR#1235` §5's defect one bucket along.

	Measured on a disposable instance on 2026-08-25, while driving `SR#1285`: a blocked task sat
	under **Waiting on somebody else** — the heading whose whole job is to say nobody can move
	on it — with ``subroutine done 1`` printed directly beneath. ``done`` does not refuse it,
	so the advice is not merely useless; it invites a write that skips the thing being waited on.

	**Both halves, for the birthday test's reason.** The tip must not name the blocked row
	*and* the page must still get one: an implementation that stopped looking on reaching this
	section would pass the first assertion and lose the tip on every day the section appears.

	**The blocker itself is no longer on this page, and that sentence used to say the
	opposite** (`SR#1265`, decision `SR#1267` §1). It read *"the agenda is not narrowed by
	assignee — `#1265` is that, and it is not built"*, which was true when it was written and
	is a comment naming the item that would expire it. It has: an agenda is one person's now,
	so Bob's row is off this page and counted as *assigned to somebody else* instead of
	offered under *Next* as work to pick up.

	**Which is why there is a third row.** The tip has to come from work the reader can
	actually do, and with the blocker gone the fixture had none — so this would have passed
	the first assertion and lost the second for a reason that has nothing to do with what it
	guards.
	"""

	run("init")
	run("user", "create", "bob")

	run("add", "Ship the release")
	run("add", "Sign off the copy")
	run("add", "Water the plants")
	run("update", "2", "--assignee", "bob")
	run("link", "2", "blocks", "1")

	shown = run("agenda").output

	assert "Waiting on somebody else" in shown, (
		f"work held up by somebody else has no section of its own:\n{shown}"
	)
	assert "Ship the release" in shown, f"the blocked row is not on the page at all:\n{shown}"
	assert "Tip: subroutine done 1" not in shown, (
		f"the agenda advises finishing the one row it has just said nobody can start:\n{shown}"
	)
	assert "Tip: subroutine done 3" in shown, (
		f"it gave up at the blocked section instead of reading past it, so a day whose work "
		f"sits below that heading loses the tip entirely:\n{shown}"
	)
	assert "Sign off the copy" not in shown, (
		f"Bob's row is on this reader's agenda, which decision `SR#1267` §1 says it is "
		f"not:\n{shown}"
	)
	assert "1 assigned to somebody else" in shown, (
		f"and it left with nothing saying so, which is what `SR#649`'s amendment "
		f"forbids:\n{shown}"
	)


def test_an_event_is_put_away_rather_than_achieved (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half of decision `SR#1235` §3: accepted, recorded, and reworded.

	*Done* would be the program congratulating the reader on a day going by. It is not refused,
	because a refusal is a wall with nothing to do next — it is simply described as what it is.
	"""

	run("init")
	run("add", "Anna's birthday", "--type", "event")
	run("add", "Water the plants")

	assert "Marked as past" in run("done", "1").output
	assert "Done" in run("done", "2").output


def test_the_record_of_finishing_an_occasion_says_what_the_screen_said (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1312`: the word was avoided on the line that scrolls and not on the one that keeps.

	``_because`` writes the act and the reason as a **comment on the item**, so
	``done <ref> --because "..."`` on a birthday left *"Done — ..."* on the permanent record
	while the ephemeral line said *"Marked as past"*. Decision `SR#1235` §3's argument is about
	what the product calls a day going by; the record is the one place that has to hold, and it
	was the one place it did not.

	Read back through ``show`` rather than asserted against the writer, because the defect was
	two renderings of one act disagreeing while only one of them was ever looked at. The test
	above passes on the defect — it reads ``done``'s own output.
	"""

	run("init")
	run("add", "Anna's birthday", "--type", "event")
	run("add", "Water the plants")

	run("done", "1", "--because", "she had a lovely day")
	run("done", "2", "--because", "they were dry")

	assert "Marked as past — she had a lovely day" in run("show", "1").output
	assert "Done — she had a lovely day" not in run("show", "1").output

	assert "Done — they were dry" in run("show", "2").output, "ordinary work is still Done"


def test_the_older_name_for_the_agenda_says_where_it_went (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1003`, reversing `SR#996` within the afternoon on Simon's decision.

	`SR#996` kept `today` as a hidden synonym on `ls`/`list`'s precedent — nothing anybody has
	typed stops working. **That argument does not transfer**: `ls` is a convenience nobody ever
	had to unlearn, where this was the *former primary name* for the thing `SR#990` was about
	unifying, so keeping it sustained two names for one answer.

	**A signpost rather than a bare removal**, which is `SR#509`'s shape and its recorded rule:
	it refuses, which is what a removed command should do. Typer offers a near-miss where it can
	find one, and with this gone there is none — so without this, a reader with `today` in their
	shell history gets `No such command 'today'.` and nothing else.
	"""

	run("init")
	run("add", "Buy milk")

	moved = run("today", expect=2)

	assert "subroutine agenda" in moved.output, "a removed command names the one that works"
	assert "Buy milk" not in moved.output, (
		"it refuses rather than printing an agenda, or it is an alias wearing a notice"
	)

	# **Asked of the command rather than of the help text**, because the word appears in prose
	# there — *"this shows today's agenda"* — and a scan over rendered help would be reading a
	# sentence rather than a registration.
	#
	# **Typed loosely, with the reason written down**, exactly as `tests/test_cli_help.py`
	# records it: Typer vendors its own click shim, so what `get_command` returns is a
	# `typer._click.core.Command` — a private class that is not a `click.Command` and that
	# Typer exports no name for. Claiming either type here would be a cast asserting something
	# untrue.
	root: typing.Any = typer.main.get_command(subroutine.cli.main.app)
	context = click.Context(root, info_name="subroutine")

	assert root.get_command(context, "today").hidden, (
		"a command that only says where it went is not offered in the help"
	)
	assert "agenda" in set(root.list_commands(context)), "the command itself is in the help"


def test_the_agenda_can_be_asked_about_another_day (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1005`, Simon 2026-08-18: cover later items *when requested*.

	**No new grammar.** `schedule.interpret_written_day` is the human day vocabulary `plan`
	and `defer` already take — split from §9.3's expression grammar by `SR#167` precisely so a
	person and a program get different words — so this reuses it and invents nothing.

	**And it names the day.** Asked about a future one, `Overdue` becomes a *projection*:
	everything due before then, which is true and reads as a fault with nothing saying what
	you are looking at.
	"""

	run("init")
	run("add", "Dentist by tomorrow")

	today = run("agenda").output

	assert "Next 7 days" in today, "tomorrow's deadline is in the look-ahead from today"

	ahead = run("agenda", "tomorrow").output

	assert "Today" in ahead and "Next 7 days" not in ahead, (
		"asked about tomorrow, tomorrow's deadline is today's work"
	)
	# **The month comes from the same clock the command read, never from a literal** (`SR#1699`).
	# This asserted `"Aug"`, which was true for thirty days and false on the thirty-first —
	# tomorrow is `Tue 1 Sep` then, and the failure lands inside whoever's change happens to be
	# running, reading as a regression in it. The claim is that the day being shown is *named*,
	# so the expected month is derived from the day being shown.
	tomorrow = datetime.date.today() + datetime.timedelta(days=1)

	assert tomorrow.strftime("%b") in ahead.splitlines()[0], (
		f"the day being shown is named first, or Overdue reads as a fault: {ahead}"
	)


def test_the_agenda_takes_every_word_its_siblings_take (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One grammar, which is `SR#167`'s whole point and the reason this cost almost nothing.

	That item exists because `plan 1 friday` was promised by five surfaces and refused by the
	parser. A day argument that took a *different* set of words from the command next to it
	would be the same defect with the surfaces swapped.
	"""

	run("init")

	for written in ("tomorrow", "friday", "next tuesday", "2026-08-01", "today+2w", "+2w"):
		assert "is not a day" not in run("agenda", written).output, (
			f"{written!r} is a day `subroutine plan` takes and this refused it"
		)

	# **The new spelling reaches the siblings too**, because it went into the grammar rather
	# than into this command. `+2w` working here and refused by `plan` would be `SR#167`
	# exactly, which is the item that made these one function in the first place.
	run("add", "Buy milk")

	assert "is not a day" not in run("plan", "1", "+2w").output
	assert "is not a day" not in run("defer", "1", "+2w").output


def test_a_day_nobody_understands_is_refused_in_the_same_words_everywhere (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The refusal is the shared one, so two commands cannot explain one mistake two ways."""

	run("init")
	run("add", "Buy milk")

	refused = run("agenda", "someday", expect=1).output
	elsewhere = run("plan", "1", "someday", expect=1).output

	assert "is not a day this understands" in refused
	assert subroutine.domain.schedule.WRITTEN_DAY_HINT in refused
	assert subroutine.domain.schedule.WRITTEN_DAY_HINT in elsewhere


def test_the_look_ahead_can_be_widened_and_the_heading_follows (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1005`: a window gets a number rather than a word.

	`weekend` and `next week` name a *window*, which the agenda expresses as a date and a
	horizon — so mapping either word to a pair would be the new vocabulary this deliberately
	avoids. `agenda saturday --days 2` is the weekend and adds nothing to learn.

	**The heading is asserted because it is the part that would have shipped broken.** It is
	built from the default look-ahead at import, so a two-day window under a heading saying
	seven is a defect the flag itself causes.
	"""

	run("init")
	run("add", "Dentist by tomorrow")
	run("add", "File the return by today+5d")

	assert "Next 7 days" in run("agenda").output

	narrowed = run("agenda", "--days", "2").output

	assert "Next 2 days" in narrowed, "the heading names the window that was asked for"
	assert "File the return" not in narrowed, "and the window is the one that was asked for"

	assert "Next 1 day" in run("agenda", "--days", "1").output, (
		"singular, because 'Next 1 days' is the sort of thing nobody proofreads"
	)


def test_the_agenda_says_how_much_dated_work_is_past_the_look_ahead (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#997`, Simon's decision of 2026-08-18: the edge stays and gets said.

	A deadline further out than the look-ahead is in **no bucket at all** — `unscheduled`
	requires both dates to be null, so dated work leaves that pile and there is nowhere else to
	go. The agenda stays a day view (§8.6) and a listing already answers *what is due this
	quarter*, so what was missing was never the work: it was any sign the view had left some
	out.

	**And the count names the command that shows it**, because a number nobody can act on is
	worse than no number — §12.2a's habit of ending with the next thing to type.
	"""

	run("init")
	run("add", "File the return by today+30d")
	run("add", "Buy milk")

	printed = run("agenda").output

	assert "File the return" not in printed, "thirty days out is past a seven-day look-ahead"
	assert "1 dated further out" in printed
	assert "subroutine list --filter due_at.gte=today" in printed, (
		"a count with no way to see what it counts is a number nobody can act on"
	)


def test_the_agenda_leads_with_what_is_already_in_hand (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Simon's decision of 2026-08-25 — `SR#1243`.

	*"I would naturally complete a task before starting another."* Everything below this
	section is a candidate to **begin**; this is the only one already in hand, so it leads.

	**The order is asserted rather than the membership**, because membership is what
	`tests/test_agenda_surfaces.py` already compares across all three surfaces. What that file
	cannot see is which heading a person meets first, which is the whole of what was decided.
	"""

	run("init")
	run("add", "Already going")
	run("update", "1", "--status", "in_progress")
	run("add", "Somebody is waiting")
	run("update", "2", "--status", "needs_input")

	printed = run("agenda").output

	assert "In progress" in printed and "Waiting on you" in printed, printed
	assert printed.index("In progress") < printed.index("Waiting on you"), (
		"work already in hand is the one section that is not something to pick up"
	)


def test_a_task_you_have_started_and_are_late_on_still_reads_as_late (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The consequence of `SR#1243`'s reorder, and the reason the mark moved to the row.

	**The buckets are disjoint in order**, so `in_progress` leading means a started task whose
	deadline has passed is reported there rather than under *Overdue* — and the heading and the
	colour both used to be properties of the section. Two of `SR#102`'s three signals would have
	gone with it, leaving a late item looking ordinary.

	**Driven through the command rather than by calling the helper**, because a helper that
	returns the right answer to nobody is exactly the shape this guards against: the assertion
	is on what a person sees.
	"""

	run("init")
	run("add", "Started and late by today-3d")
	run("update", "1", "--status", "in_progress")

	printed = run("agenda").output

	assert "Started and late" in printed
	assert printed.index("In progress") < printed.index("Started and late"), (
		"a started task belongs under what is in hand, not under Overdue"
	)

	# **The date is the signal that survives in plain text**, and it is the one `SR#102` says
	# must be there whatever the colour does: no information exists only in a colour. The style
	# itself is asserted below, off the rendered row rather than off stripped output.
	assert "due " in printed, "a late row that does not say when is a colour on its own"


def test_a_document_is_never_late_however_a_section_is_marked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1243`. ``_is_late`` narrows to a task before asking, and a document has no deadline.

	**The guard that matters is the narrowing**, not the comparison: the row-level mark is
	applied to every row in every section, and a document reaches those sections through the
	same listing a task does (§12.2a). Asking a document about a deadline it cannot have would
	be an `AttributeError` in the middle of somebody's agenda.
	"""

	run("init")
	printed = run("doc", "create", "A conclusion", "--body", "Something concluded.").output

	assert "A conclusion" in printed

	# Rendered through the listing that applies the mark, which is what exercises the narrowing.
	assert "A conclusion" in run("list").output


def test_an_agenda_showing_everything_says_nothing_about_what_it_left_out (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: a line on every page that says the same thing says nothing.

	The ordinary day is most days — measured on this project's own instance, 11 of 170 open
	tasks carry a deadline at all — so a zero printed beside every agenda would be noise on
	almost all of them.
	"""

	run("init")
	run("add", "Buy milk")

	assert "further out" not in run("agenda").output


def test_a_bare_invocation_still_prints_the_agenda (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1003`'s near miss, and it is why this has a test of its own.

	The bare `subroutine` invocation called the `today` command **by name**, so replacing that
	command with a signpost would have made the first thing anybody types print a notice about
	a rename. Ruff caught it as an undefined name only because the function was renamed at the
	same time — had the signpost kept the name, nothing would have seen it.
	"""

	run("init")
	run("add", "Buy milk")

	printed = run().output

	assert "Buy milk" in printed, "a bare invocation is the agenda (§12.2)"
	assert "is now" not in printed, "and not a notice about a command that moved"


def test_a_suggestion_is_marked_as_one_without_relying_on_colour (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#128``, and decision ``#102``: no information exists only in a colour.

	This test is the guard rather than the marker being there, and the distinction matters —
	the version of ``test_every_command_suggests_the_next_one`` above that only looked for
	``"subroutine agenda"`` passed just as happily on the broken output as on the fixed one,
	which is why the defect survived to be found by somebody reading the README.

	Colour is already gone here: the runner has no terminal, so rich emits none. What is left
	has to be enough on its own, because that is also what a pipe, a log, a screen reader and a
	fenced block in Markdown get.
	"""

	run("init")

	printed = run("add", "Buy milk").output

	assert "\033[" not in printed, "no colour to lean on, which is the point"

	suggestions = [line for line in printed.splitlines() if "subroutine agenda" in line]

	assert suggestions, printed
	assert all(line.strip().startswith("Tip:") for line in suggestions), printed


def test_an_empty_list_says_what_to_do_about_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A blank screen is a dead end; the remedy costs one line."""

	run("init")

	assert 'subroutine add "something to do"' in run("agenda").output
	assert 'subroutine add "something to do"' in run("list").output


def test_a_bare_number_addresses_a_task_by_its_ref_number (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The difference between a to-do list you use and one you type identifiers into."""

	run("init")
	run("add", "First")
	run("add", "Second")

	shown = {
		line.split(maxsplit=1)[0]: line.split(maxsplit=1)[1].strip()
		for line in run("agenda").output.splitlines()
		if line.strip().startswith("#")
	}

	assert set(shown) == {"#1", "#2"}, "listings print the ref with its sigil"

	# Typed without the sigil, because a shell would eat it (docs/design.md §12.2a).
	run("done", "2")

	remaining = run("agenda").output

	assert shown["#2"] not in remaining
	assert shown["#1"] in remaining


def test_a_number_goes_on_meaning_the_same_task_after_something_is_completed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The defect this addressing scheme replaced, kept as a regression.

	Positions were re-derived on every listing, so completing the first task renumbered
	everything below it. Re-running ``done 1`` — one up-arrow away — then marked a
	*different* task done and reported success. A ref number is allocated once and never
	reused, so the number cannot come to mean something else.
	"""

	run("init")
	run("add", "Buy wine")
	run("add", "Buy salad")
	run("add", "Test task")

	assert "Done: Buy wine" in run("done", "1").output

	listed = run("ls").output

	assert "#1" not in listed, "the completed task is gone from the list"
	assert "#2" in listed and "#3" in listed
	assert "Buy salad" in listed

	# The absent-minded up-arrow. It must not touch the salad.
	repeated = run("done", "1")

	assert "Already done: Buy wine" in repeated.output
	assert "Buy salad" in run("ls").output, "nothing else was completed"


def test_a_number_that_matches_nothing_is_refused_with_the_remedy (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the refusal says how to find out what does exist.

	It also says it **without naming a workspace**. A refusal is the last output anybody
	re-reads for stray vocabulary, and this one was telling a person with one workspace that
	there is no ``#9`` "in si" — a word they had never met, introduced by an error message
	about their shopping. §13.5b's transcript cannot catch that, because a refusal is not in
	the transcript.
	"""

	run("init")
	run("add", "Buy milk")

	result = run("done", "9", expect=1)

	assert "no #9" in result.output
	assert "subroutine list" in result.output

	for word in FORBIDDEN:
		assert word not in result.output.lower(), f"the refusal mentions a {word}"


def _second_workspace (home: pathlib.Path, slug: str = "work") -> None:
	"""Add a second workspace to the installation in ``home``.

	Reaching past the CLI because there is no ``subroutine workspace create`` yet — but this
	is a supported state, not a contrived one: ``init`` makes the first user a superuser
	precisely so they can create more (docs/design.md §7.1).
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.session
	import subroutine.domain.local
	import subroutine.domain.projects
	import subroutine.domain.tasks
	import subroutine.domain.workspaces

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			principal = subroutine.domain.local.principal(session)
			workspace = subroutine.domain.workspaces.create(
				session, slug=slug, title=slug.title(), owner=principal.user
			)
			session.flush()

			project = subroutine.domain.projects.create(
				session,
				workspace_id=workspace.id,
				key="SR",
				title="Work",
				owner_id=principal.user.id,
				actor=principal,
			)
			subroutine.domain.tasks.create(
				session, project=project, title="Deploy to production", actor=principal
			)
			session.commit()

	finally:
		engine.dispose()


def test_a_bare_number_is_refused_when_two_workspaces_both_have_it (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""It used to silently complete whichever row the database yielded first.

	Refs are unique per workspace, so two workspaces each reach ``#1``. ``_lookup`` took
	``.first()`` on an unordered query across every readable workspace — no refusal, no
	warning, and which task got completed was up to the database. That is the same defect as
	the positional numbering this addressing scheme replaced, and no test could see it
	because every fixture had exactly one workspace.
	"""

	run("init")
	run("add", "Pay the gas bill")
	_second_workspace(home)

	result = run("done", "1", expect=1)

	assert "could mean any of these" in result.output
	assert "Pay the gas bill" in result.output, "the candidates are named, with their titles"
	assert "Deploy to production" in result.output
	assert "/1" in result.output, "and the refusal shows how to say which"

	# Nothing was completed by the refusal.
	assert "Pay the gas bill" in run("ls").output


def test_a_workspace_qualified_address_resolves (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""The refusal above suggests ``workspace/1``, so that had better work."""

	run("init")
	run("add", "Pay the gas bill")
	_second_workspace(home)

	listed = run("ls").output
	slug = next(
		line.split("/")[0].strip()
		for line in listed.splitlines()
		if "/" in line and "Pay the gas bill" in line
	)

	assert f"Done: {slug}/#1" in run("done", f"{slug}/1").output


def test_a_listing_qualifies_every_ref_once_a_bare_one_would_not_resolve (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""What is printed has to be what can be typed back.

	With one workspace a bare ``#1`` resolves and is what shows. With two it does not, so
	every row carries its workspace — otherwise a listing invites the very ambiguity the
	lookup then refuses, which is a worse experience than either alone.
	"""

	run("init")
	run("add", "Pay the gas bill")

	assert "#1" in run("ls").output

	_second_workspace(home)

	qualified = run("ls").output

	assert "/#1" in qualified, "both rows now name their workspace"
	assert "Deploy to production" in qualified, "and reads still span everything readable"


def test_the_sigil_is_accepted_as_well_as_the_bare_number (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#1`` is what a listing prints, so it has to work when somebody types it back.

	A shell eats an unquoted ``#``, which is why the bare form is the one the CLI advertises
	— but a quoted one, or one pasted into a script, must not be a refusal.
	"""

	run("init")
	run("add", "Buy milk")

	assert "Done: Buy milk" in run("done", "#1").output


def test_a_ref_is_a_number_in_json_not_a_string (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The scripted path gets the same type the API sends, so the two cannot drift."""

	run("init")
	run("add", "Buy milk")

	listed = json.loads(run("ls", "--json").output)

	assert listed[0]["ref"] == 1


def test_the_scripted_listing_carries_what_the_terminal_shows (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: the human path and the scripted path are one code path and cannot drift.

	They had. ``type`` was added to the terminal listing and not to the JSON, so a script
	reading the same command could not see what a person could; and ``urgency`` had been
	absent beside ``importance`` since §6.3 paired them, so a script sorting on the half it
	was given would rank a 5/1 above a 4/5.
	"""

	run("init")
	run("add", "Buy milk !4")

	row = json.loads(run("ls", "--json").output)[0]

	assert row["type"] == "task"
	assert row["importance"] == 4
	assert "urgency" in row, "half a priority is worse than none"


def test_the_capture_grammar_reaches_the_database (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§6.13 end to end: the tokens become fields, and the title loses them."""

	run("init")
	run("add", "Write the report by friday !3 ~2h #work #urgent")

	result = run("ls", "--json")

	assert '"title": "Write the report"' in result.output
	assert '"importance": 3' in result.output
	assert '"estimate_minutes": 120' in result.output
	assert '"work"' in result.output and '"urgent"' in result.output


def test_a_recurring_phrase_is_kept_and_explained (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A phrase this cannot read survives, and the user is told why.

	**`#94` reversed which phrases those are.** `every monday` is a rule now, so the sentence
	is about a repeat *phrased* in a way this does not know rather than about the feature not
	existing — and the readable case is asserted beside it, because a complaint printed on
	every capture is one nobody reads.
	"""

	run("init")

	result = run("add", "Water the plants every fortnight")

	assert "Water the plants every fortnight" in result.output
	assert "not a repeat this understands" in result.output

	read = run("add", "Water the plants every monday")

	assert "Left as written" not in read.output
	assert "every Monday" in read.output, "the repeat was read and not confirmed"


def test_plan_and_defer_move_a_task_between_days (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The two verbs that make an agenda something you steer rather than watch."""

	run("init")
	run("add", "Buy milk")
	run("agenda")

	# The confirmation echoes the day that was just set, not the deadline. `_when` prefers
	# a deadline, which is right in a list and wrong here — the user said "tomorrow" and
	# used to be shown Friday.
	assert "Starts " in run("plan", "1", "tomorrow").output

	run("agenda")

	hidden = run("defer", "1", "2026-12-01")

	assert "Hidden until" in hidden.output

	# Deferred means hidden: the agenda is empty again.
	assert "Buy milk" not in run("agenda").output


def test_a_planned_day_can_be_taken_off_again_from_the_command_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1316`. Setting a start was reachable here and clearing one was not.

	**The three states are the whole of it**, and two of them were one value: the argument
	defaulted to ``""`` and an omitted day and an explicit empty one were indistinguishable, so
	the empty one prompted. The only route was ``PATCH {"starts": null}`` over HTTP — on a
	command whose own ``--until`` documents ``''`` as the clear, one option along.

	Driven rather than asserted about, because the defect was in what somebody could *do*: the
	client method has taken ``UNSET`` against ``None`` since it was written.
	"""

	run("init")
	run("add", "Buy milk")
	run("plan", "1", "tomorrow")

	assert "starts " in run("show", "1").output

	cleared = run("plan", "1", "")

	assert cleared.exit_code == 0, cleared.output
	assert "No longer starts on a day" in cleared.output
	assert "starts " not in run("show", "1").output

	# And the omitted argument still asks rather than clearing — the two states this
	# separated must not have been collapsed the other way.
	assert "Buy milk" in run("list").output


def test_a_defer_keeps_the_time_of_day_it_was_given (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#858`. Found by Simon deferring his own work until six in the morning.

	The six hours were parsed, discarded and **not mentioned**: the echo said *"Hidden until
	Fri 14 Aug"*, which is what a working command would also have said, so the confirmation
	could not tell the two apart. §6.13 rule 1's shape — a value somebody typed, read, and
	lost — on the command named after the field.

	**The store and the echo are both asserted, because either alone passes for the wrong
	reason.** Storing it and rendering a bare day leaves the user unable to confirm it worked,
	which is `#925`'s finding; rendering a time off the *input* would say six o'clock whatever
	the row held, which is the same finding pointed the other way.
	"""

	run("init")
	run("add", "Call the plumber")

	timed = run("defer", "1", "2026-12-01 06:00")

	assert "Hidden until" in timed.output
	assert "06:00" in timed.output

	stored = json.loads(run("show", "1", "--json").output)["item"]

	assert stored["snoozed_until"].startswith("2026-12-01T06:00")
	assert stored["snoozed_is_all_day"] is False


def test_planning_a_timed_event_keeps_the_clock_it_was_created_with (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1299`. Found trying to give a doctor's appointment its thirty minutes.

	``plan`` says which *day*, and it re-snapped the whole field to a whole day — so the 11:00
	that ``add`` had just read and stored was destroyed by the next command anybody would run.

	**It is silent twice over.** ``plan`` echoes *"Starts Thu 27 Aug"*, which is what a working
	command would print too; and no terminal surface renders a time on ``starts_at`` at all
	(`SR#1298`), so the output before and after the damage is byte for byte identical. There is
	nothing a person could look at to notice.

	**The clock is compared to what was there rather than to a literal**, because the property
	is *this command did not touch the time*, and a literal would also be asserting what the
	capture grammar read.
	"""

	run("init")
	run("add", "Doctor's appointment on 2026-12-01 at 14:00", "--type", "event")

	before = json.loads(run("show", "1", "--json").output)["item"]

	assert before["starts_is_all_day"] is False, "the fixture is not a timed event"
	assert before["starts_at"] is not None

	run("plan", "1", "2026-12-02")

	after = json.loads(run("show", "1", "--json").output)["item"]

	assert after["starts_at"] is not None
	assert after["starts_at"][:10] == "2026-12-02", "the day the command was given did not land"
	assert after["starts_at"][11:] == before["starts_at"][11:], (
		"planning it destroyed the time it was created with"
	)
	assert after["starts_is_all_day"] is False, (
		"a timed event was re-snapped to a whole day by a command that only names days"
	)


def test_planning_an_ordinary_task_still_makes_it_a_whole_day (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other side of `SR#1299`, and without it the fix passes by never snapping anything.

	Nearly everything has no clock on ``starts_at``, and *plan it for Tuesday* means the whole
	of Tuesday. The rule is *keep a clock the field already had*, which is silent on a field
	that never had one — and a version that simply stopped snapping would leave every ordinary
	planned task at midnight with its all-day flag off.
	"""

	run("init")
	run("add", "Buy milk")
	run("plan", "1", "2026-12-02")

	planned = json.loads(run("show", "1", "--json").output)["item"]

	assert planned["starts_is_all_day"] is True, "an ordinary task is planned for a whole day"
	assert planned["starts_at"] is not None
	assert planned["starts_at"][:10] == "2026-12-02"


def test_a_day_only_argument_refuses_a_time_rather_than_dropping_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1299`. ``--until`` is documented as *"The last day of it"* and did not refuse a time.

	It accepted ``2026-12-03T11:30:00``, kept the date, dropped the time and reported success.
	A written time is something somebody said, and §6.13 rule 1's whole subject is a value that
	is read and then lost — so the choice is to honour it or to say so, never to discard it in
	silence. Honouring it is an event's real span and is `SR#1238`.

	**The refusal already existed for a phrase and not for a timestamp**, which is the asymmetry
	underneath this: ``plan 1 "tomorrow at 11:00"`` is turned down because the grammar cannot
	read it at all, so the one form that *parses* was the one that lost data.
	"""

	run("init")
	run("add", "The conference")

	refused = run("plan", "1", "2026-12-02", "--until", "2026-12-03T11:30:00", expect=1)

	assert "11:30" in refused.output, (
		"the refusal has to quote the time it will not take, or it reads as a bad date"
	)

	refused_day = run("plan", "1", "2026-12-02T09:00:00", expect=1)

	assert "09:00" in refused_day.output, "the day argument drops a time as silently as --until"

	# **A day still works**, because a refusal that also turned down the documented form would
	# be a worse defect than the one it replaced.
	run("plan", "1", "2026-12-02", "--until", "2026-12-05")


def test_a_day_long_until_on_a_timed_event_is_refused_rather_than_flattening_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1299`'s deliberate remainder, pinned here so it is a decision rather than a surprise.

	Keeping the start's clock means a span can no longer be half timed and half whole-day, and
	an end named as a bare day *is* whole-day — so ``plan 1 <day> --until <day>`` on a timed
	event now meets the shape refusal instead of silently flattening the start to midnight.

	**That is a capability regression and it is the right trade.** What it replaces is a command
	that succeeded by destroying the time somebody had just written, with output identical to
	the working case. Nothing is written now: the assertion below reads the row back to say so,
	because a refusal that had already half-applied would be worse than either.

	Giving both ends a time is the fix and it is a feature — no terminal or agent surface can
	set one today, which is what `SR#1320` is for. Until then ``PATCH /v1/tasks`` is the route,
	and ``explain dates`` already marks the timestamp form ``(api)``.
	"""

	run("init")
	run("add", "The conference on 2026-12-01 at 14:00", "--type", "event")

	before = json.loads(run("show", "1", "--json").output)["item"]

	assert before["starts_is_all_day"] is False, "the fixture is not a timed event"

	refused = run("plan", "1", "2026-12-02", "--until", "2026-12-05", expect=1)

	assert "whole day" in refused.output, (
		f"the refusal has to say what is inconsistent about the two ends:\n{refused.output}"
	)

	after = json.loads(run("show", "1", "--json").output)["item"]

	assert after["starts_at"] == before["starts_at"], "a refused command moved the start anyway"
	assert after["ends_at"] == before["ends_at"], "a refused command set the end anyway"


def test_a_timed_event_says_its_o_clock_and_a_whole_day_one_does_not (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1298`. A doctor's appointment and a birthday were the same line everywhere here.

	The capture grammar reads *at 11:00* and stores it with the flag off; ``add``, ``list``,
	``show`` and the agenda all printed *starts Tue 1 Dec*, so the terminal could not tell a
	reader which of the two they were looking at. ``explain dates`` says of ``starts``: *"It
	takes a time, so 'monday at 14:00' is an appointment"* — true of the store and false of
	everything that drew it.

	**All four renderings, because one of them being right is the condition under which the
	others look fine.** They are one function now (`_render_moment`), and the pair is asserted
	together so a fix that simply appended a time everywhere fails on the birthday.
	"""

	run("init")
	added = run("add", "Doctor's appointment on 2026-12-01 at 11:00", "--type", "event")
	birthday = run("add", "Anna's birthday on 2026-12-01", "--type", "event")

	assert "at 11:00" in added.output, f"the confirmation dropped the time:\n{added.output}"
	# ``Dec at`` rather than ``at``, because the tip line below every command contains *what
	# happened* and a looser check reads the ``at`` inside *what* as a time.
	assert "Dec at" not in birthday.output, (
		f"a whole day was given an o'clock nobody wrote:\n{birthday.output}"
	)

	for surface, output in (
		("list", run("list").output),
		("show", run("show", "1").output),
		("agenda", run("agenda", "2026-12-01").output),
	):
		assert "at 11:00" in output, f"{surface} does not say the appointment is at 11:00:\n{output}"

	standing = run("show", "2").output

	assert "starts Tue 1 Dec" in standing, f"show lost the birthday's day:\n{standing}"
	assert "Dec at" not in standing, f"show gave a birthday a time:\n{standing}"


def test_the_scripted_listing_row_says_whether_a_start_names_a_whole_day (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1298`, on the path with no eyes and no `?fields=` to ask with.

	``list --json`` carried ``due_is_all_day`` and not ``starts_is_all_day``, so a script could
	read the shape of a deadline and not the shape of a start — and a null instant would then
	be indistinguishable from a whole-day one. Unlike the API's row there is no way to request
	a field by name here, so a key that is absent is a fact that cannot be had.
	"""

	run("init")
	run("add", "Doctor's appointment on 2026-12-01 at 11:00", "--type", "event")
	run("add", "Anna's birthday on 2026-12-01", "--type", "event")

	rows = {row["ref"]: row for row in json.loads(run("list", "--json").output)}

	assert rows[1]["starts_is_all_day"] is False, "a timed start is reported as a whole day"
	assert rows[2]["starts_is_all_day"] is True, "a whole day is reported as timed"
	assert "snoozed_is_all_day" in rows[1], "the defer's own flag is still missing"


def test_the_repeat_itself_is_turned_down_by_name_rather_than_denied (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1322`. Three commands, one ref, and one of them said the number was not real.

	``SR#921`` made a template's ref resolve and ``SR#1247`` made the product print it, so
	*from repeat #1* is on the screen precisely so somebody can act on that row. ``show`` read
	it and ``done`` stopped the series — and ``delete`` answered *"There is no task #1"*, then
	pointed at a listing that excludes templates by design, so following the advice confirmed
	the false statement.

	**The exclusion is not the defect and is not changed.** ``_in_the_trash_too`` declines a
	template with its reasons written down: widening it would make a series a legal parent to
	move work under, which is a decision about the model that nobody has taken. What was wrong
	is a refusal asserting something untrue — worse than a vague one, because the reader has no
	thread to pull.

	**All three are driven in one test**, because that is what makes the contradiction visible:
	each command alone is defensible and the set of them is not.
	"""

	run("init")
	run("add", "Take the bins out every tuesday")

	occurrence = run("show", "2").output

	assert "from repeat #1" in occurrence, (
		f"the fixture does not offer the number this is about:\n{occurrence}"
	)

	assert "the repeat itself" in run("show", "1").output, "show cannot read the template"

	refused = run("delete", "1", expect=1)

	assert "There is no" not in refused.output, (
		f"delete denied a row show and done both reach:\n{refused.output}"
	)
	assert "repeat" in refused.output, (
		f"the refusal has to say what the ref names:\n{refused.output}"
	)
	assert "done 1" in refused.output, (
		f"the refusal has to name the command that does work:\n{refused.output}"
	)

	# **The ordinary refusal is unchanged**, and without this the fix could pass by calling
	# every missing ref a repeat.
	missing = run("delete", "99", expect=1)

	assert "repeat" not in missing.output, (
		f"a ref that names nothing was described as a repeat:\n{missing.output}"
	)

	# And the command the refusal names actually works, which is the half a message cannot
	# promise on its own.
	run("done", "1")


def test_a_listing_can_be_narrowed_to_one_tag (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1319`, Simon's decision of 2026-08-28: a tag is a filter and it had no read side.

	`#home` was captured, stored, rendered on the row and on `show`, published in the API's
	view — and no surface could find work by one. The product invites it in the README, in
	`explain capture` and in both plugin skills, so somebody writes tags for months before
	discovering they cannot get them back out.

	**Asked for without the `#`**, because a POSIX shell eats one as a comment before this
	program sees it — the same reason a ref is typed bare.
	"""

	run("init")
	run("add", "Buy compost #home")
	run("add", "Fix the deploy script #ops")
	run("add", "Nothing filed under anything")

	home = run("list", "--tag", "home").output

	assert "Buy compost" in home, home
	assert "Fix the deploy script" not in home, home
	assert "Nothing filed under anything" not in home, home

	# **However it was capitalised**, or a tag is several things that look like one.
	assert "Buy compost" in run("list", "--tag", "HOME").output

	# **A tag nobody uses is turned down by name**, rather than answered with an empty list —
	# a typo and an unused tag produce the same nothing, and the second is far the rarer.
	assert "nosuchtag" in run("list", "--tag", "nosuchtag").output

	# **And `search` takes it too**, so narrowing and searching compose rather than being two
	# ways to ask that cannot be combined.
	assert "Buy compost" in run("search", "compost", "--tag", "home").output


def test_searching_for_a_tag_with_its_sigil_finds_what_carries_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1576`. The terminal half of Simon's report, driven where he met it.

	The domain is guarded on both backends in ``tests/test_api_tasks.py``; this says the search
	*command* reaches it, which is the surface a person types into. `#1319` gave that command a
	``--tag`` and nobody will find it — the search box is where somebody who tagged something
	goes looking for it again.
	"""

	run("init")
	run("add", "Look at the pile #research")
	run("add", "Write about research methods")
	run("add", "Prose only", "--description", "We discussed #research at length")

	tagged = run("search", "#research").output

	assert "Look at the pile" in tagged, tagged
	assert "Prose only" in tagged, tagged
	assert "Write about research methods" not in tagged, tagged

	# **A tag nothing uses answers nothing rather than refusing**, unlike `--tag`: the two
	# commands answer different questions and `#1319` settled which is which.
	assert "Nothing matches" in run("search", "#nosuchtag").output


def test_a_tag_filter_survives_a_workspace_that_has_not_got_the_tag (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1575`. One workspace hid this, and the test above is why: it calls `init` and stops.

	A read spans every workspace a credential can reach (§13.7) and `-w` only settles where a
	*write* goes, so a second workspace is enough. A tag row belongs to one workspace exactly as
	a status does, and `domain.tags.carrying` refuses by name where there is none — so the
	moment a second workspace existed, `--tag home` was turned down in that one and the refusal
	took the whole listing with it, including the rows the right workspace had returned.

	**The sentence it printed was false twice over**: the tag existed, and something carried it.
	Measured on the served instance at the time — five workspaces, `--tag ui` refused, and
	`GET /v1/tasks?tag=ui` answered with the row.

	**`#1468`'s defect a third time**, and its own comment had named the shape: a status and a
	type are per-workspace vocabularies and are tolerated per workspace; nobody added the third.
	One register now, so a fourth is a decision rather than an omission.

	**Both halves of the fan-out**, because a task listing resolves the status first and a
	document listing resolves the project first — the asymmetry that made `#1468` reachable at
	all — so each has its own `except` and each had to learn the word.
	"""

	run("init")
	run("add", "Buy compost #home")
	run("doc", "create", "Why we chose compost", "--body", ".", "--tag", "home")
	run("workspace", "create", "second", "Second")

	found = run("list", "--tag", "home").output

	assert "Buy compost" in found, (
		f"a second workspace with no such tag swallowed the workspace that has it:\n{found}"
	)
	assert "Why we chose compost" in found, (
		f"the document half of the listing was lost the same way:\n{found}"
	)

	# **A tag that is nowhere is still refused by name**, which is what `#1319` built the
	# refusal for: a typo and an unused tag produce the same empty listing, and tolerating
	# every workspace would have traded that away to fix this.
	assert "nosuchtag" in run("list", "--tag", "nosuchtag").output


def test_a_narrowing_filter_given_twice_is_refused_rather_than_halved (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1484`, Simon's decision of 2026-08-28: refuse the second, do not union them.

	Repeating one silently kept the **last**, so `--type finding --type note` answered about
	notes alone — and on a project holding none it printed *"Nothing on your list"*, which reads
	as *nothing has ever been filed here*. That is how it was found: an agent following the
	import process ran a four-type filter and concluded the project was empty, at the step that
	document calls the most valuable thing it will read all session.

	Same shape as `SR#1468` — a listing answering a narrower question than it was asked and not
	saying so — and the one option the item rules out, because the other two are each defensible.

	**Five filters share this shape**, so the refusal is one function reading a declared list
	rather than a rule about `--type`. Two of them are driven here and the population is asserted
	against the declarations, so a sixth cannot be added without either being covered or failing
	this.
	"""

	run("init")
	run("add", "Buy milk")

	refused = run("list", "--type", "task", "--type", "bug", expect=1)

	assert "takes one value" in refused.output, refused.output
	assert "task" in refused.output and "bug" in refused.output, (
		f"a refusal about repetition has to quote what was repeated:\n{refused.output}"
	)

	# **One value still works**, or this is a check that the flag was broken rather than that
	# repeating it was.
	assert "Buy milk" in run("list", "--type", "task").output

	# **And it is not a rule about one flag.** `search` carries only `--project` of the five,
	# so driving it here says the refusal reaches a second command as well as a second option.
	both = run("search", "milk", "--project", "one", "--project", "two", expect=1)

	assert "takes one value" in both.output, both.output

	# **The declarations and the register agree**, so a sixth narrowing filter is either
	# covered or fails this rather than joining silently.
	#
	# **Read off the command rather than out of its help** (`SR#1537`). This compared each flag
	# against `list --help`, was green on every machine here and red on all four of CI's: with
	# colour on, rich styles an option name in parts — a styled `-`, a reset, then `-project` —
	# so the literal never appears in the output at all, about a page that displays it
	# perfectly. The subject here is the command's *parameters*, and `get_command` answers that
	# with no renderer in the way.
	#
	# Typed loosely for the reason `tests/test_cli_help.py` writes out: Typer vendors its own
	# click shim, so what `get_command` returns is a private class that is not a `click.Command`
	# and that Typer exports no name for.
	root: typing.Any = typer.main.get_command(subroutine.cli.main.app)
	listing = root.get_command(click.Context(root, info_name="subroutine"), "list")
	offered = {name for parameter in listing.params for name in parameter.opts}

	assert offered, "no options were read off the command, so this is checking nothing"

	for flag in subroutine.cli.personal.ONE_VALUE_EACH:
		assert flag in offered, (
			f"{flag} is registered as taking one value and 'list' does not offer it"
		)


def test_deleting_one_turn_of_a_repeat_says_the_repeat_is_still_standing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1294`, and it is the refusal above told from the other end.

	``delete`` on the *series* is turned down and names the occurrence. Deleting the
	**occurrence** went through in silence and left the series present, drawn by no listing and
	no agenda, and unable to produce another — ``materialise`` mints the next occurrence when
	the last one is *finished*, so with the only finishable row in the trash there is no route
	to a successor and the series is reachable solely by its number.

	Deleting the visible row is what somebody reaches for when they mean *stop the repeat*, and
	it did something strictly worse than stopping: ``done 1`` leaves a tidy one-off, while this
	left an orphan nobody could see.

	**The line does not say it will come back**, because it will not. A message implying the
	series still runs on a clock would be a refusal asserting a cause it has not established,
	which is the failure this file exists to catch.

	Three cases, and the last two are what stop this passing by putting the sentence on
	everything.
	"""

	run("init")
	run("add", "Take the bins out every tuesday")

	assert "from repeat #1" in run("show", "2").output, (
		"the fixture does not offer the two rows this is about"
	)

	gone = run("delete", "2").output

	assert "Deleted: Take the bins out" in gone
	assert "#1" in gone, f"the line has to name the row it is talking about:\n{gone}"
	assert "done 1" in gone, f"the line has to name the command that ends it:\n{gone}"

	# **An ordinary task says nothing**, or this is a sentence on every delete rather than a
	# statement about what the delete did not reach.
	run("add", "Buy milk")

	plain = run("delete", "3").output

	assert "Deleted: Buy milk" in plain
	assert "repeat" not in plain, f"a task that repeats nothing was told about one:\n{plain}"

	# **And neither does an occurrence of a repeat somebody has already stopped.** Stopping
	# completes the template rather than clearing a column, so the row still points at one —
	# and there is no rule left to end, so naming one would be advice about a series that will
	# never fire again. That is `SR#920`'s rule, reached here through the same resolution.
	run("restore", "2")
	run("done", "1")

	stopped = run("delete", "2").output

	assert "Deleted: Take the bins out" in stopped
	assert "repeat" not in stopped, (
		f"a stopped series was offered as something to stop:\n{stopped}"
	)


def test_an_item_says_nothing_about_the_type_its_workspace_defaults_to (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1135`. The rule is *say nothing about a type nobody chose*, and it was hardcoded.

	``_facts`` asked ``item.type not in ("task", "note")`` — the right question answered by
	naming the two keys this installation's seeder happens to use. ``ItemType.is_default`` has
	always held the answer; the item view simply did not carry it.

	**So the test renames which type is the default**, which is the only thing that tells the two
	apart: under the old rule ``story`` prints on every line because it is not one of the two
	names, and under the new one it is silent because it is what everything starts as. That state
	is unreachable from the CLI today — `SR#1129` is the command — and it is a supported one, not
	a contrived one: §5.5 says the vocabulary is a workspace's own.

	Both directions, because either alone is weak: the old rule agrees with the new one about
	`bug`, so an assertion that a chosen type still prints would pass against the defect.
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.models.vocabulary
	import subroutine.db.session

	run("init")
	run("add", "Something ordinary")
	run("add", "Something wrong")

	assert "task" not in run("show", "1").output, "the seeded default was already announced"

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			model = subroutine.db.models.vocabulary.ItemType
			types = {
				one.key: one
				for one in session.scalars(
					sqlalchemy.select(model).where(model.entity_type == "task")
				)
			}

			# `bug` renamed to `story` and made the default, so the two keys the old rule knows
			# about are both wrong: `story` is the default and `task` is not.
			types["task"].is_default = False
			types["bug"].key = "story"
			types["bug"].is_default = True

			session.commit()

	finally:
		engine.dispose()

	run("update", "2", "--type", "story")

	assert "story" not in run("show", "2").output, (
		"the workspace's own default type was announced on every item that has it"
	)
	assert "task" in run("show", "1").output, (
		"a type somebody would now have had to choose was not reported"
	)


def test_the_shape_a_commit_hook_reads_is_the_shape_it_greps_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1153`: `hooks/post-commit` decides from two whole lines of this output.

	It is shell, so it cannot parse JSON — it matches ``  "entity_type": "task",`` and
	``    "status_category": "…",`` by *whole line*, indentation included, because both keys
	appear again more deeply indented inside every linked item this output carries and a loose
	grep would read a blocker's category as this one's.

	**So the hook is guarding a spelling, and this is what stops that being a silent bet.** The
	hook's own tests stub `subroutine`, which means they assert my opinion of this output rather
	than the output; reindent it or move either key and they would all still pass while the hook
	quietly stopped recording anything. This is the only place the two are tied together.
	"""

	run("init")
	run("add", "Call the plumber")

	lines = run("show", "1", "--json").output.splitlines()

	assert '  "entity_type": "task",' in lines, (
		"hooks/post-commit reads the kind off this line, anchored at two spaces"
	)
	assert any(
		line.startswith('    "status_category": "') for line in lines
	), "and the item's own category off a line anchored at four"

	# **An item with a *finished* blocker, which is the case the anchors exist for** — pointed
	# out by the cold review of 2026-08-24, which read the hook, agreed the depths discriminate,
	# and said plainly that the fixture above proves the spelling and not the discrimination.
	# An item with no links cannot tell a correct anchor from a loose one.
	run("add", "Order the part")
	run("done", "2")
	run("link", "2", "blocks", "1")

	lines = run("show", "1", "--json").output.splitlines()
	own = [line for line in lines if line.startswith('    "status_category": "')]

	assert own == ['    "status_category": "todo",'], (
		"the four-space anchor must match the item's own category and nothing else; it found "
		f"{own}"
	)

	# And the blocker's *is* in the output, more deeply indented — so the anchor is doing work
	# rather than being the only line there was.
	assert any(
		line.startswith('        "status_category": "done"') for line in lines
	), "the finished blocker's category is carried too, which is what a loose grep would read"


@pytest.mark.parametrize("written", ["2026-12-01", "monday", "today+2w"])
def test_a_defer_written_in_days_is_still_a_whole_day (
	run: typing.Callable[..., typer.testing.Result], written: str
) -> None:
	"""`#858`'s other half, and ``today+2w`` is the one that would have broken quietly.

	A §9.3 expression resolves against *now*, so honouring its clock would store whatever
	o'clock it happened to be when somebody typed it — a defer written in days landing at
	14:37 because that is when the command ran. **A time is honoured when it is written and
	not otherwise**, which is `#797`'s rule about clocks arriving from the other direction.

	Parametrised over the three vocabularies rather than asserted once, because they reach
	the answer by three different routes — a weekday is resolved before the grammar, a bare
	date inside it, and an expression by arithmetic — so one case proves nothing about the
	others.

	**Midnight is asserted in the task's own zone, not in UTC**, and the first version of this
	got it wrong: this fixture's instance is not UTC, so a correct whole-day defer is stored
	as ``23:00Z`` the day before and an ``endswith("T00:00:00Z")`` read that as a failure.
	The recorded trap — a test comparing a boundary must not assume the zone it is computing
	for — met while writing the test rather than by a summer.
	"""

	run("init")
	run("add", "Call the plumber")
	run("defer", "1", written)

	stored = json.loads(run("show", "1", "--json").output)["item"]

	assert stored["snoozed_is_all_day"] is True

	local = datetime.datetime.fromisoformat(stored["snoozed_until"]).astimezone(
		subroutine.domain.dates.zone(stored["timezone"])
	)

	assert local.time() == datetime.time.min


def test_a_priority_can_be_changed_from_the_cli (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#147`, and the asymmetry it closes.

	Changing a title, a priority, an estimate or a type was the one capability MCP had and
	the CLI did not — so an agent could rank a backlog and the person whose backlog it is
	could not. `#146` measured all thirty-six and this was the only cell that way round.
	"""

	run("init")
	run("add", "Fix the parser")

	assert "Changed" in run("update", "1", "--importance", "4", "--urgency", "3").output

	shown = run("show", "1").output

	assert "!4/3" in shown

	run("update", "1", "--estimate", "2h", "--type", "bug", "--title", "Fix the tokeniser")

	shown = run("show", "1").output

	assert "Fix the tokeniser" in shown
	assert "bug" in shown
	assert "2h" in shown


def test_update_leaves_alone_what_it_was_not_asked_about (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§8.3's semantics, which are the whole of what `PATCH` means, reaching the CLI intact.

	A shell has one way to say nothing, so a default of `""` would make "leave it alone" and
	"clear it" the same input — and clearing unreachable. `UNGIVEN` is what keeps them apart.
	"""

	run("init")
	run("add", "Fix the parser ~2h !4/3")

	run("update", "1", "--type", "bug")

	shown = run("show", "1").output

	assert "2h" in shown, "an estimate nobody mentioned was cleared"
	assert "!4/3" in shown, "a priority nobody mentioned was cleared"

	# And the other half: named with nothing in it *is* a clearance.
	run("update", "1", "--estimate", "")

	assert "2h" not in run("show", "1").output


def test_a_quoted_version_refuses_a_change_that_would_overwrite_somebody (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1696`, §8.9, brought to the terminal as an option rather than as a default.

	**Why it cannot be automatic here.** ``subroutine update`` resolves its ref through
	``_a_task``, which is a fresh read in the *same* invocation — so a version taken from
	there would be milliseconds old and would pass whatever landed while somebody was
	thinking. There is no session at a command line and nothing is held between invocations,
	so the only number that spans the gap is one a person carries by hand. A default-on check
	would catch a round-trip race and be blind to the collision this exists for.

	**The number comes from ``show --json``**, not from the plain command: §1.4 does not print
	a field nobody set, and a version is machinery rather than something anybody chose.

	Both halves are asserted, because the refusal alone would pass against a version that
	refuses *after* writing — and writing-then-refusing is what makes a row unrepairable rather
	than merely wrong (`SR#1561`'s rule).
	"""

	run("init")
	run("add", "Fix the parser")

	held = json.loads(run("show", "1", "--json").output)["item"]["version"]

	# Somebody else saves while the first reader is thinking.
	run("update", "1", "--description", "Their careful paragraph")

	refused = run(
		"update", "1", "--description", "Mine, written from the old text.",
		"--expected-version", str(held), expect=1,
	)

	assert "has changed since you read it" in refused.output, refused.output

	kept = json.loads(run("show", "1", "--json").output)["item"]

	assert kept["description"] == "Their careful paragraph", (
		"the change was refused and applied anyway"
	)

	# **Opt-in, and this half is the regression guard**: `None` means *did not ask*, never
	# *asked and passed*, so a caller who leaves the option out writes exactly as before. That
	# is what makes this shippable without announcing a behaviour change.
	run("update", "1", "--description", "Sent without a version.")

	assert json.loads(run("show", "1", "--json").output)["item"]["description"] == (
		"Sent without a version."
	)


def test_update_with_no_field_named_refuses_rather_than_doing_nothing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Matching the MCP tool, which decided this first.

	Somebody who ran this and named no field meant to change something. A cheerful
	"unchanged" hides the mistake at the one moment it could still be corrected.
	"""

	run("init")
	run("add", "Fix the parser")

	refused = run("update", "1", expect=1)

	assert "Nothing to change" in refused.output
	assert "--importance" in refused.output


def test_update_turns_a_document_down_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2c: one counter serves both kinds, so a ref may be a document.

	"There is no task #2" about something printed in the listing a moment ago is the answer
	that sent somebody looking for a missing item. Naming it is the answer they can act on.
	"""

	run("init")
	run("doc", "create", "Why the queue went", "--body", "Nobody wanted it.")

	refused = run("update", "1", "--importance", "4", expect=1)

	assert "document" in refused.output.lower()
	assert "Why the queue went" in refused.output


def test_a_priority_reads_back_the_way_it_is_written (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#151`, as a round trip rather than as two strings agreeing.

	`show` printed `!4/u3` where the listing printed `!4/3`, and only the listing's spelling
	is one §6.13 accepts — so retyping what the product had just displayed put it in the
	title verbatim with no priority set. Asserting the round trip states the requirement;
	asserting a literal would only pin today's spelling.
	"""

	run("init")
	run("add", "Fix the parser")
	run("update", "1", "--importance", "4", "--urgency", "3")

	# A second, differently ranked, so the listing keeps its priority column — §14.10 drops
	# one that says the same thing on every row, and one row always does.
	run("add", "Something else !1/1")

	shown = [word for word in run("show", "1").output.split() if word.startswith("!")]

	assert shown, "show printed no priority at all"

	# The listing spells it the same way...
	assert shown[0] in run("list").output

	# ...and the capture grammar takes it back, which is what "self-describing" has to mean.
	run("add", f"Another one {shown[0]}")

	written = run("show", "3").output

	assert "Another one" in written
	assert shown[0] in written, "the priority did not survive the round trip"
	assert shown[0] not in written.split("\n")[0], "the token stayed in the title"


def test_show_can_print_what_has_happened_to_an_item (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#150`. `GET /v1/tasks/{ref}/events` reached no client, so neither surface could read it.

	Found by `#148`'s second edge on its first run — and `#52` had spent a morning putting
	comments *into* that history while nothing outside HTTP could display the result.
	"""

	run("init")
	run("add", "Fix the parser")
	run("update", "1", "--importance", "4")
	run("comment", "1", "ran the suite")

	plain = run("show", "1").output

	# **Behind a flag.** Most items have one event saying they were created, and printing that
	# on every `show` is a section whose answer is almost always "nothing has happened" — §1.4's
	# rule about a default nobody chose, applied to a whole heading.
	assert "History" not in plain

	shown = run("show", "1", "--history").output

	assert "History" in shown
	assert "created" in shown
	# The reader's word rather than the column (`SR#1187`). The changes feed has said it this
	# way since it was written; the history said ``importance`` until both were given one map.
	assert "changed how it is ranked" in shown
	assert "commented" in shown, "a comment must reach the history — that is what #52 built"


def test_the_history_says_whether_it_was_asked_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#349`, and the original argument here was sound and collapsed two facts anyway.

	**The key stays unconditional and that half is unchanged**: one that appears only with a
	flag makes a script test for the key rather than read it, so *absent* and *nothing has
	happened* would be one shape for two facts — the `due_at: null` mistake `_as_json` already
	avoids for documents.

	**What was wrong is that `[]` was the answer to both questions.** It was written for *not
	asked* and is also what *asked, and nothing has happened* produces. A script knows which
	flags it passed and can tell them apart; **a reader assembling one answer out of several
	invocations cannot** — which is what an agent is, and on `#346` one read `"history": []` and
	reported that the history was empty. It had no way to know better.

	So the distinction moved into the value, where a reader of the *output* can see it, rather
	than staying in a comment only a reader of the source can.

	**The third state is honestly unreachable for a task and the shape is still right**:
	creation is an event, so `[]` cannot be produced here today. Asserting `null` against a
	populated list is what the reader actually has to distinguish.
	"""

	run("init")
	run("add", "Fix the parser")

	unasked = json.loads(run("show", "1", "--json").output)["history"]
	asked = json.loads(run("show", "1", "--history", "--json").output)["history"]

	assert unasked is None, (
		f"asking without --history answered {unasked!r}, which is what an empty history looks "
		f"like too — the two facts this key has to keep apart."
	)

	assert asked, "asking with --history answered nothing, so the flag reached nowhere"

	assert "history" in json.loads(run("show", "1", "--json").output), (
		"the key went missing without the flag, which makes a script test for it"
	)


def test_a_defer_can_say_what_it_is_waiting_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#99`, from decision `#96`: waiting is a defer with a reason.

	The reason is the thing a status could not carry, and `defer` is where it is
	load-bearing — it is the verb that *hides* the item, so without one the backlog holds
	something invisible that nobody can account for.
	"""

	run("init")
	run("add", "Chase the invoice")

	hidden = run("defer", "1", "2026-12-01", "--because", "waiting on the provider's reply")

	assert "Hidden until" in hidden.output

	shown = run("show", "1")

	# The act and the reason in one sentence, so the record reads without the event beside it.
	assert "Hidden until" in shown.output
	assert "waiting on the provider's reply" in shown.output


def test_a_reason_given_to_plan_or_done_is_recorded_too (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One flag on all three verbs, because a user who learns it on one will try the others.

	`#99` argued the reason matters most on `defer` and that the same argument reaches
	`plan` and `done`. Refusing it on two of the three would be a distinction only the
	implementation can see.
	"""

	run("init")
	run("add", "Fix the parser")
	run("add", "Write the release notes")

	run("plan", "1", "tomorrow", "--because", "the review is on monday")
	run("done", "2", "--because", "superseded by the changelog")

	assert "the review is on monday" in run("show", "1").output
	assert "superseded by the changelog" in run("show", "2").output


def test_an_act_without_a_reason_records_nothing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Silence is not a reason, and an empty entry would timestamp a claim nobody made."""

	run("init")
	run("add", "Buy milk")
	run("defer", "1", "2026-12-01")

	assert "Hidden until" not in run("show", "1").output


def test_each_reason_is_kept_rather_than_replacing_the_last (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The argument for a comment over a field, made falsifiable.

	A wait can happen repeatedly and each one has its own reason. A field would hold the
	newest and silently lose the account of why the thing has been sitting there since May —
	which is the question somebody is actually asking when they finally look.

	Written after `#99` claimed a second benefit — a `#42` in the reason becoming a visible
	backlink — that turns out to be indexed and unread until `#144`. This is the half that
	does hold, so it is the half with a test.
	"""

	run("init")
	run("add", "Chase the invoice")

	run("defer", "1", "2026-09-01", "--because", "waiting on the provider")
	run("defer", "1", "2026-12-01", "--because", "they asked for a purchase order")

	shown = run("show", "1")

	assert "waiting on the provider" in shown.output
	assert "they asked for a purchase order" in shown.output


def test_json_output_carries_enough_to_act_on (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The scripted path and the human path are the same code, so they cannot drift."""

	run("init")
	run("add", "Buy milk")

	document = json.loads(run("agenda", "--json").output)

	assert document["unscheduled"][0]["title"] == "Buy milk"
	assert document["unscheduled"][0]["ref"]
	assert document["timezone"] == "Europe/London"


def _read_by_the_terminal_row () -> dict[str, str]:
	"""Return each view field the terminal listing puts in a cell, by the cell that reads it.

	**Derived by reading the renderers, not listed.** A column added tomorrow is compared
	tomorrow — `#427`'s method, and the only thing that makes this a guard rather than a
	second list to keep up to date. Every cell in that listing is a module-level function
	named ``_…_cell`` taking one ``item``, so what it reads off that item is what the reader
	is shown.
	"""

	source = pathlib.Path(subroutine.cli.personal.__file__).read_text(encoding="utf-8")
	found: dict[str, str] = {}

	for node in ast.parse(source).body:
		if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_cell"):
			continue

		for read in ast.walk(node):
			if (
				isinstance(read, ast.Attribute)
				and isinstance(read.value, ast.Name)
				and read.value.id == "item"
			):
				found.setdefault(read.attr, node.name)

	return found


class Instead (typing.NamedTuple):
	"""What a script gets in place of a terminal cell, and why."""

	#: The key on the scripted row that carries the same fact. ``None`` where nothing does,
	#: which is a gap being recorded rather than a substitution being described.
	key: str | None

	#: Why it is not carried under the terminal's own name.
	why: str


#: A field the terminal row shows and the scripted row deliberately does not carry under that
#: name, and what a script gets instead. Same rule as the registers in
#: ``test_api_writability.py``, and for the same reason: "the JSON does not have it" describes
#: the code rather than giving a reason, and `#820` is what happens when nothing checks one.
#:
#: **The substitute is named rather than described, and it is verified** (`#840`, on `#925`'s
#: lesson). This was prose, so an entry could name a stand-in that did not exist — which is
#: exactly the defect `#925` found in the guard built to catch it, where an excuse saying *read
#: another way* made the whole comparison vacuous.
RENDERED_ONLY: dict[str, Instead] = {
	"estimate_human": Instead(
		"estimate_minutes",
		"§6.4's grammar is a rendering — a script handed '2h' has to parse the terminal's "
		"prose back into the number it was made from.",
	),
	"description": Instead(
		"matched",
		"`_match_cell` reads it to say *why* a search matched, and a listing row carries "
		"neither body — §14.10 makes response size a first-order cost, and the whole "
		"description on every row of a search is the opposite of that. A script gets the "
		"computed cell rather than the fields it was computed from.",
	),
	"body": Instead("matched", "The same, for a document."),
}


def test_the_scripted_row_carries_what_the_terminal_row_shows (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One row, two renderings, and until now nothing compared them.

	``subroutine list --json`` returned fifteen fields and ``assignee`` was not among them,
	while the terminal put ``@si`` on the row beside it (`#583`). `#511` had shipped the
	column and the human half only; `#674` is the same shape again on the agent's surface.
	Neither was reachable by ``test_reach``, which compares what a *client* can call — these
	two renderings are the same call, rendered twice.

	The comparison is what was missing, so this is deliberately not a list of the fields a row
	ought to have: it reads the cells the terminal renders and asks the scripted row for each
	one. The row is driven rather than constructed, because a guard that reads both sides
	statically confirms two spellings agree and nothing about what a caller receives.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something to hand over @si !4/3 ~2h #urgent by friday")

	rows = json.loads(run("list", "--json").output)
	shown = _read_by_the_terminal_row()

	assert shown, "No cell renderer was read — has the listing stopped being built this way?"
	assert rows, "Nothing was listed, so the scripted row cannot be compared against anything."

	missing = sorted(
		field for field in shown if field not in RENDERED_ONLY and field not in rows[0]
	)

	assert not missing, (
		f"The terminal row shows {missing} and the scripted row does not carry them. Add them "
		f"to `_as_json`, or record in RENDERED_ONLY what a script gets instead."
	)


def test_every_substitute_the_excuses_name_is_on_the_scripted_row (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#925`'s lesson, applied to this register rather than to the one that taught it.

	An excuse naming a stand-in nobody verifies is worth less than no excuse: it reads as a
	considered decision and asserts something the guard never checks. `#925` found exactly that
	in the guard written to catch it — an entry saying *read another way*, which made the whole
	comparison vacuous, so deleting the thing being excused left it green.

	**Driven rather than read**, for the reason the comparison above gives: a static check
	confirms two spellings agree and says nothing about what a caller receives. The key must be
	*present* on an unsearched row, not merely non-null — ``matched`` is null until somebody
	searches, exactly as ``relevance`` is, and that is how a script tells *not searched* from
	*searched and could not say*.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something to hand over @si !4/3 ~2h #urgent by friday")

	rows = json.loads(run("list", "--json").output)

	assert rows, "Nothing was listed, so no substitute can be looked for."

	wanted = {entry.key for entry in RENDERED_ONLY.values() if entry.key is not None}
	absent = sorted(key for key in wanted if key not in rows[0])

	assert not absent, (
		f"RENDERED_ONLY says a script gets {absent} instead, and the scripted row has no such "
		f"key. Either carry it in `_as_json` or say plainly that nothing carries this fact."
	)


def test_a_scripted_search_says_why_each_row_matched (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#840`. The terminal explains a hit and the scripted reader was handed the row alone.

	*A hit whose reason is invisible reads as a bug* is `_match_cell`'s own argument, and its
	worked example is searching this project for "pagination" and getting a document whose
	title says nothing about it. **That argument does not weaken for a caller with no eyes** —
	it is stronger, because a script cannot glance at the row and work it out.

	Three rows, three reasons, so the assertion is about the *value* rather than the key: a
	field that was always ``"title"`` would satisfy a check that only asked whether it was
	there.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Pagination resumes from the wrong cursor row")
	run("add", "Something else entirely", "--description", "the cursor is what breaks")

	rows = json.loads(run("search", "cursor", "--json").output)
	reasons = {row["title"]: row["matched"] for row in rows}

	assert len(reasons) == 2, f"the probe matched {reasons}, so it proves nothing"
	assert reasons["Pagination resumes from the wrong cursor row"] == "title"
	assert reasons["Something else entirely"] == "description"


def test_an_unsearched_scripted_row_says_nothing_about_matching (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half, and without it the field could be a constant and pass.

	Null means *nothing was searched for*, which is a different claim from *searched and could
	not say* — the second is the empty string, and collapsing them would be an absence two
	behaviours produce. The key is still present, exactly as ``relevance`` is on an unranked
	listing, because a script has to tell the two apart without knowing what it asked.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Nobody searched for this")

	row = json.loads(run("list", "--json").output)[0]

	assert "matched" in row, "the key vanishes when nothing was searched for"
	assert row["matched"] is None, f"an unsearched row claims a reason: {row['matched']!r}"


def test_every_field_excused_from_the_scripted_row_is_still_rendered () -> None:
	"""So this file cannot go on excusing a column the terminal has stopped having.

	The stale half of the guard above, and the one every register in this repository is
	required to have: an entry describing a cell nobody renders any more reads as a considered
	decision and silently excuses whatever later takes the name.
	"""

	shown = _read_by_the_terminal_row()
	unknown = sorted(field for field in RENDERED_ONLY if field not in shown)

	assert not unknown, f"RENDERED_ONLY names {unknown}, which no cell on the row reads."


def test_a_weekday_names_a_day_wherever_a_day_is_named (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#167`. Five published surfaces promised this and it did not work.

	``explain dates`` — one of exactly two places the README sends a beginner — carries
	``subroutine plan 1 friday`` as its worked example. ``plan --help`` and ``defer --help``
	each say it twice, and the refusal's own hint said "a weekday name". Meanwhile the capture
	grammar took the same word, so ``add "Something by friday"`` worked and ``plan 1 friday``
	did not: one product with two answers to what "friday" means.

	A clean-room tester named this as the single place a real user would have got stuck,
	because it is the first thing you try after the agenda.
	"""

	run("init")
	run("add", "Buy milk")

	assert "Starts " in run("plan", "1", "friday").output
	assert "Starts " in run("plan", "1", "next friday").output
	assert "Hidden until" in run("defer", "1", "monday").output

	# Abbreviations too — they are in the same table `explain dates` prints.
	assert "Starts " in run("plan", "1", "fri").output


def test_a_day_that_is_not_a_day_is_refused_in_this_commands_vocabulary (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half of `#167`: the refusal named the wrong grammar.

	``interpret_day`` raises §9.3's keyword inventory — ``start_of_month``, ``end_of_week`` —
	which is what a *program* may send. A person who typed a word into ``plan`` was handed the
	HTTP vocabulary and no mention of the weekday that would have worked.
	"""

	run("init")
	run("add", "Buy milk")

	refused = run("plan", "1", "bananas", expect=1)

	assert "friday" in refused.output
	assert "start_of_month" not in refused.output


def test_a_bad_date_is_refused_with_what_would_have_worked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: errors state the remedy."""

	run("init")
	run("add", "Buy milk")
	run("agenda")

	result = run("plan", "1", "someday", expect=1)

	assert "tomorrow" in result.output or "2026-08-01" in result.output


def test_help_explains_concepts_not_only_commands (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: users need the model, not just the verbs.

	``--help`` is a vocabulary. This is the grammar, and without it a user who knows every
	flag still does not know that "due Friday" means the end of Friday.
	"""

	listed = run("explain")

	for topic in ("dates", "capture", "refs", "connecting", "scripting"):
		assert topic in listed.output

	assert "deadline" in run("explain", "dates").output.lower()
	assert "Nothing is ever lost" in run("explain", "capture").output
	assert "#7" in run("explain", "refs").output
	assert "connections add" in run("explain", "connecting").output


def test_no_topic_names_a_command_that_does_not_exist () -> None:
	"""`#542`. The rule the plugin's skill has had since `#134`, applied to the other prose.

	``explain`` is where somebody who has only this program learns what it can do — it is the
	one channel a terminal user is guaranteed, and `#499` says the guaranteed channel has to
	name the ones they only get by going looking. That makes it the natural place for a command
	to be recommended, and until now nothing checked that the recommendation still resolved.

	**Whitespace-insensitive, and that is not a detail.** These bodies are hard-wrapped to a
	terminal, so ``subroutine use`` genuinely does break across a line — and a pattern with a
	literal space in it would silently stop reading half of them, which is `#544`'s
	neighbouring defect and the second time this repository has paid for it.
	"""

	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	unknown = []

	for topic in subroutine.cli.topics.TOPICS:
		for text in (topic.summary, topic.body):
			for match in re.finditer(r"\bsubroutine\s+([a-z][a-z-]*)", text):
				if match.group(1) not in registered:
					unknown.append(f"{topic.name}: 'subroutine {match.group(1)}'")

	assert not unknown, (
		f"a topic recommends a command that does not exist: {', '.join(unknown)}"
	)

	# A walk that matched nothing would satisfy the assertion above just as happily, and these
	# bodies are the only thing it reads.
	assert any(
		re.search(r"\bsubroutine\s+[a-z]", topic.body) for topic in subroutine.cli.topics.TOPICS
	), "no topic names a command at all — has this stopped reaching them?"


def test_the_help_topics_are_generated_from_the_parsers (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Help that lists a keyword the parser rejects is worse than no help at all.

	Both topics are built from the modules that do the parsing, so this asserts they agree
	rather than asserting a transcription.
	"""

	dates = run("explain", "dates").output

	for keyword in subroutine.domain.dates.KEYWORDS:
		assert keyword in dates

	capture = run("explain", "capture").output

	for word in subroutine.domain.capture.DEADLINE_WORDS:
		assert word in capture

	# `#544`: the same rule for the third topic, and here it is the *sizes* that matter rather
	# than the names. A page saying `1d is 24h` beside a vocabulary that had been re-sized would
	# be teaching the number that caused the confusion.
	estimates = run("explain", "estimates").output
	units = subroutine.domain.durations.UNITS

	for index, (unit, minutes) in enumerate(units[:-1]):
		below, size = units[index + 1]

		assert f"1{unit}  is  {minutes // size}{below}" in estimates, (
			f"`explain estimates` does not say what 1{unit} is, or says the wrong number: "
			f"the vocabulary makes it {minutes // size}{below}"
		)


def test_an_estimate_says_that_a_day_is_not_a_working_day (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#544`, found by summing the estimates on a milestone — the first thing that ever did.

	The total came to 99 hours for twelve items that were collectively about three weeks of
	ordinary work, because `d` and `w` mean something different to the program than to everybody
	who has typed one. `durations.UNITS` says `1d = 1440m` and the module says so plainly: *a
	day is twenty-four hours, not a working day.*

	**The program is right and consistent; nothing told the person typing it.** `explain` had no
	`estimates` topic at all, the capture grammar's own examples use `~4h` and `~2h` so a reader
	never met `d`, and the units are published in `/v1/meta` — the surface least likely to be
	read by somebody filing a task.

	**And the round trip is lossless, which is why it was invisible.** `~1d` is stored as 1440
	and renders back as `1d`, so nothing ever contradicted the reader. It only breaks when
	something *sums* — measured here at 24 of 62 estimated items using days or weeks, every one
	of them reading as a working estimate.

	Asserted through the driven command rather than on the topic's source, because a topic that
	exists and is unreachable teaches nobody.
	"""

	run("init")

	listed = run("explain").output

	assert "estimates" in listed, (
		"the topic is not offered, so it is reachable only by somebody who already knows it "
		"is there — which is nobody with this question"
	)

	body = run("explain", "estimates").output

	assert "24" in body and "168" in body, (
		f"the page does not say what a day or a week actually costs: {body!r}"
	)

	assert "8h" in body, (
		"the page names the problem and not the remedy — somebody who means a working day "
		"needs to be told what to write instead"
	)


def test_capture_says_a_tag_is_made_and_a_project_is_not (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#588`'s other half — the refusal is the moment it bites, the page is where it belongs.

	`+KEY` was described as *"puts it in a particular list"*, which is true and says nothing
	about whether the list has to be there already. Two documented tokens of one grammar with
	opposite behaviours on first use, and the difference reachable only by getting it wrong.
	"""

	run("init")

	body = run("explain", "capture").output

	assert "project create" in body, (
		"the page describes the token and not what to do when the project is not there"
	)

	assert "#errand" in body and "+errand" in body, (
		"the asymmetry is stated in the abstract rather than shown, and the whole difficulty "
		"is that the two tokens look alike"
	)


def test_a_project_can_say_it_offers_every_status (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1034`, found by driving `#1029` on the served instance minutes after it shipped.

	**The stored value has three meanings and the command line could spell two.** Absent
	inherits whatever is above; a list hides those; an **empty list** offers everything and
	overrides what is above. A project inside a workspace that hides a status could say
	*inherit* or *hide these* and could not say *offer them all anyway* — reachable over HTTP,
	and from an agent through `subroutine_call_api`, and from nowhere a person types.

	**A sentinel rather than a mirror**, which is the decision on the item. `--show-status`
	reads more naturally and composes, and it needs a rule for a key named on both sides — a
	contradiction the caller can express and somebody then has to resolve. `--hide-nothing`
	cannot contradict itself, and the state is rare: the ordinary project inherits.

	**Read back through what publishes it rather than through the stored value.**
	`views.Project.hidden_statuses` is the *resolved* answer, walked up the chain, so this
	asserts the thing a client is actually told — and inherit-versus-override are the two
	answers that differ only after that walk.
	"""

	run("init")
	run("project", "create", "web", "Web")

	# The workspace hides one, which is the only arrangement where the three states differ.
	run("workspace", "update", "projects", "--hide-status", "blocked")

	def offered () -> list[str]:
		"""What the listing says this project does not offer."""

		rows = json.loads(run("project", "list", "--json").output)
		web = next(one for one in rows if one["key"] == "web")
		hidden: list[str] = web["hidden_statuses"]

		return hidden

	assert offered() == ["blocked"], (
		"the project does not start out inheriting, so this cannot tell inherit from override"
	)

	run("project", "update", "web", "--hide-nothing")

	assert offered() == [], (
		"--hide-nothing left the project inheriting the workspace's hidden status, which is "
		"the state it exists to override"
	)

	# **And it goes back**, or the flag would be a one-way door — the shape `#969` refused on
	# a control that could name a state it could not return from.
	run("project", "update", "web", "--hide-status", "")

	assert offered() == ["blocked"], "clearing it stopped meaning inherit"

	refused = run(
		"project", "update", "web", "--hide-nothing", "--hide-status", "blocked", expect=1
	)

	assert "opposite" in refused.output, (
		f"saying both was accepted, so one of them silently won: {refused.output!r}"
	)


def test_an_unknown_help_topic_lists_the_real_ones (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: errors state the remedy."""

	result = run("explain", "quantum", expect=1)

	assert "dates" in result.output


def test_help_leads_with_examples (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: a flag list teaches vocabulary; an example teaches a sentence.

	Both are needed, in that order — so the worked example must appear before the options
	block, not after it.
	"""

	for command in ("add", "agenda", "done", "plan"):
		text = run(command, "--help").output

		assert "subroutine " + command in text, command
		assert text.index("Examples") < text.index("Options"), command


def test_output_is_plain_when_it_is_not_a_terminal (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: colour is detected, never configured.

	Captured output is not a terminal, so no escape sequence should reach it. There is no
	flag involved on either side — that is the point.
	"""

	run("init")
	run("add", "Buy milk before friday")

	assert "\x1b[" not in run("agenda").output
	assert "\x1b[" not in run("ls").output


def test_a_missing_argument_asks_rather_than_erroring (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: a required-argument error is a dead end where a question would do."""

	run("init")
	run("add", "Buy milk")
	run("agenda")

	result = run("done", input="1\n")

	assert "Which one?" in result.output
	assert "Done: Buy milk" in result.output


def test_add_with_no_text_asks_for_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The example §12.2a gives by name."""

	run("init")

	result = run("add", input="Buy milk\n")

	assert "Added: Buy milk" in result.output


def test_show_reads_one_item_without_naming_the_full_model (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4 applied to the command most likely to break it.

	``show`` exists to print everything an item carries, which is exactly the shape that
	leaks a status, a project and a type into a personal to-do list. The rule that keeps it
	honest is that a field nobody set is not printed and a default nobody chose is not a
	field: on a plain "buy milk" there is nothing to say beyond the title and the day.
	"""

	run("init")
	run("add", "Buy milk tomorrow")

	result = run("show", "1")

	assert "Buy milk" in result.output
	assert "#1" in result.output

	for word in FORBIDDEN:
		assert word not in result.output.lower(), f"'show' said {word!r}:\n{result.output}"


def test_show_prints_the_record_of_what_happened (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The half of docs/design.md §5.10 that had a service and an API and no way to read it."""

	run("init")
	run("add", "Fix the parser")

	noted = run("comment", "1", "ran the suite, two failures in the date parser")

	assert "Noted on: Fix the parser" in noted.output

	result = run("show", "1")

	assert "two failures in the date parser" in result.output


def test_a_comment_with_nothing_in_it_asks_rather_than_erroring (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a again: a blank argument is a question, not a refusal.

	Whitespace counts as blank. Somebody who typed ``subroutine comment 1 ""`` meant to say
	something and has not said it yet, which is a prompt — while an empty record entry that
	*was* accepted would timestamp a claim that nothing was said.
	"""

	run("init")
	run("add", "Fix the parser")

	result = run("comment", "1", "   ", input="found it in the tokeniser\n")

	assert "What happened?" in result.output
	assert "Noted on: Fix the parser" in result.output
	assert "found it in the tokeniser" in run("show", "1").output


def test_a_comment_longer_than_the_limit_is_refused_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A service-layer bound, reported through the CLI with the field named (§6.3's lesson).

	The path this really tests is the *translation*: a refusal raised in the domain has to
	arrive as a message rather than as a traceback, whichever transport carried it.
	"""

	run("init")
	run("add", "Fix the parser")

	result = run(
		"comment",
		"1",
		"x" * (subroutine.domain.comments.MAX_BODY_LENGTH + 1),
		expect=1,
	)

	assert "body" in result.output.lower()
	assert "Traceback" not in result.output


def test_show_reads_a_document_as_readily_as_a_task (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""One ref counter serves both (docs/design.md §6.2), so a reader that only knew tasks was wrong.

	This is the case that made ``show`` search documents at all: before it did,
	``subroutine show 2`` reported that ``#2`` did not exist while it sat in the same
	workspace, because the number happened to have been allocated to a specification.
	"""

	run("init")
	run("add", "Build the thing")
	_a_document(home, title="How the thing works", body="It works like this.")

	result = run("show", "2")

	assert "How the thing works" in result.output
	assert "It works like this." in result.output


def test_an_acting_command_says_a_document_is_a_document (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""Being told ``#2`` is a document is an answer; being told it is missing is not.

	The reason ``done`` searches documents it can never act on: the refusal has to be able
	to name what the number *did* find, or the user is left believing they misremembered a
	number that was correct all along.
	"""

	run("init")
	run("add", "Build the thing")
	_a_document(home, title="How the thing works", body="It works like this.")

	result = run("done", "2", expect=1)

	assert "is a document, not a task" in result.output
	assert "How the thing works" in result.output
	assert "subroutine show 2" in result.output


def _a_document (home: pathlib.Path, *, title: str, body: str) -> None:
	"""Write a document into the installation in ``home``.

	Reaching past the CLI for the same reason :func:`_second_workspace` does: there is no
	``subroutine document`` command yet, and a document arriving through the API or through
	an agent is the ordinary case rather than a contrived one.
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.session
	import subroutine.domain.bootstrap
	import subroutine.domain.documents
	import subroutine.domain.local
	import subroutine.domain.workspaces

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			principal = subroutine.domain.local.principal(session)
			workspace = subroutine.domain.workspaces.readable(session, principal)[0]
			project = subroutine.domain.bootstrap.inbox_for(session, workspace)

			assert project is not None, "a fresh installation has an Inbox"

			subroutine.domain.documents.create(
				session, project=project, title=title, body=body, actor=principal
			)
			session.commit()

	finally:
		engine.dispose()


def test_a_list_of_one_kind_of_thing_does_not_say_what_kind (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4 on the listing everybody sees: a column that says the same thing says nothing.

	This is the condition that made showing the item type safe at all. A personal to-do list
	is entirely ordinary tasks, and labelling every line ``task`` would put a word about the
	model on every row of the one output §13.5b measures.
	"""

	run("init")
	run("add", "Buy milk")
	run("add", "Call the dentist")

	listed = run("ls").output

	assert "Buy milk" in listed
	assert "task" not in listed.lower()

	for word in FORBIDDEN:
		assert word not in listed.lower(), f"'ls' said {word!r}"


def test_a_mixed_list_says_what_kind_each_thing_is (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""Simon's request, and the case it was made for.

	With bugs and features and plain tasks in one backlog, what kind of thing something is
	is the first thing you want — and the ref and title alone do not say.
	"""

	run("init")
	run("add", "Ordinary work")
	_a_typed_task(home, title="The parser drops a token", type_key="bug")

	listed = run("ls").output

	assert "bug" in listed
	assert "task" in listed, "once the column is there, every row is labelled"

	# Aligned, so the titles line up rather than stepping in and out with the type.
	starts = [
		line.index("The parser") if "The parser" in line else line.index("Ordinary")
		for line in listed.splitlines()
		if "The parser" in line or "Ordinary" in line
	]

	assert len(set(starts)) == 1, f"the title column does not line up:\n{listed}"


def _sections (printed: str) -> dict[str, list[str]]:
	"""Return each agenda heading and the refs printed beneath it, in order."""

	found: dict[str, list[str]] = {}
	heading = None

	for line in printed.splitlines():
		if line.strip().startswith("#"):
			if heading is not None:
				found[heading].append(line.split()[0].lstrip("#"))

		elif line.strip() and not line.startswith(" "):
			heading = line.strip()
			found[heading] = []

	return found


def test_the_agenda_offers_candidates_best_first_rather_than_oldest_first (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#853``, and the defect a person actually meets.

	The undated bucket was ordered by ``position`` — a column `#28` records as written by
	nothing — and then by capture order, so `!1/1 tidy the desk` sat above `!5/5 renew the
	passport`. **With no planned days and two deadlines across this project's 172 open tasks
	that bucket *is* the agenda**, so the answer to "what should I work on" was "whatever you
	wrote down first".

	It is the same rule ``?order=-priority_score`` applies, so the agenda and a ranked listing
	cannot disagree about which item is the one to start.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Tidy the desk !1/1")
	run("add", "Renew the passport !5/5")
	run("add", "Buy milk")
	run("add", "Fix the leaking tap !3/3")

	printed = _sections(run("agenda").output)

	assert printed["Next"] == ["2", "4", "1", "3"], printed

	# **Unranked last, not first.** Nulls sort after values in both directions (§10.3), which
	# is what stops "buy milk" heading a list of assessed work.
	assert printed["Next"][-1] == "3"


def test_the_agenda_picks_its_candidates_by_rank_before_it_stops_at_twenty (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Ordering before limiting is what decides *which* twenty, and it is the server's job.

	**The client re-sorts every bucket after merging, so on a short list the server's ordering
	is invisible** — which is exactly what a first version of these tests could not see. It
	only bites past ``DEFAULT_UNSCHEDULED_LIMIT``: order by capture and stop at twenty, and the
	best-ranked item on a two-hundred-item backlog is simply not in the answer, whatever the
	client then does with the twenty it was handed.

	Found by falsifying — reverting the server's ordering left every other test in this file
	green, because none of them had more than a handful of rows.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	for index in range(25):
		run("add", f"Captured earlier {index}")

	run("add", "The one that matters !5/5")

	printed = _sections(run("agenda").output)

	assert printed["Next"][0] == "26", printed
	assert len(printed["Next"]) == 20, "the cap moved; this test is about what survives it"


def test_the_agenda_keeps_the_order_it_was_given (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The client re-sorts every bucket after merging, and it was undoing the ranking.

	`#71`'s defect, which ``domain/ordering.py``'s docstring records: *a ``--order`` flag
	whose result was re-sorted by ``created_at`` one level further up, so the flag chose which
	items appeared and then discarded the arrangement.* It happened again the moment the
	agenda started ranking — the section came back best-first and the merge put it back to
	newest-first, and the output looked entirely reasonable.

	**Driven through the rendered agenda rather than the domain**, because the domain half was
	already right and the bug was a layer above it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Written first !5/5")
	run("add", "Written second !1/1")

	printed = _sections(run("agenda").output)

	# Newest-first would put the `!1/1` on top, which is what this used to do.
	assert printed["Next"] == ["1", "2"], printed


def test_the_agenda_says_what_is_already_started (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#853``, and `#841` from the agent's side.

	Work somebody is in the middle of is neither scheduled nor a candidate to pick up. Without
	a section for it a person had to find yesterday's half-finished task among everything they
	had ever captured, and an agent could not see its own.

	**The buckets stay disjoint**, so a started task appears once — the rule the whole agenda
	is built on, and the one a new bucket is most likely to break.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Renew the passport !5/5")
	run("add", "Fix the leaking tap !3/3")
	run("start", "2")

	printed = _sections(run("agenda").output)

	assert printed["In progress"] == ["2"], printed
	assert printed["Next"] == ["1"], printed

	# And the heading is dropped entirely when nothing is started, like every other bucket.
	run("stop", "2")

	assert "In progress" not in run("agenda").output


def test_the_scripted_agenda_carries_every_section_the_terminal_prints (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One agenda, two renderings — `#583`'s rule applied to the buckets rather than the row.

	A section the terminal prints and the scripted path omits is the same defect one level up:
	an agent asking for the agenda would be told about the day and not about what it had
	already started.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Renew the passport !5/5")
	run("start", "1")

	scripted = json.loads(run("agenda", "--json").output)

	assert [row["ref"] for row in scripted["in_progress"]] == [1]
	assert scripted["unscheduled"] == []


def test_the_agenda_labels_kinds_by_the_same_rule (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""Measured across the whole agenda, not per bucket.

	"Is this page all one kind of thing?" is a question about the agenda; asking it per
	bucket would label the bucket that happens to hold a bug and not the one below it.
	"""

	run("init")
	run("add", "Ordinary work")

	assert "task" not in run("agenda").output.lower()

	_a_typed_task(home, title="The parser drops a token", type_key="bug")

	mixed = run("agenda").output

	assert "bug" in mixed
	assert "task" in mixed


def _a_typed_task (home: pathlib.Path, *, title: str, type_key: str) -> None:
	"""Add a task of a given item type to the installation in ``home``.

	Reaching past the CLI because ``subroutine add`` deliberately has no ``--type``: the
	personal path does not name item types (§1.4), and a task that is a bug arrives from an
	agent or the API. That is the ordinary case for this feature, not a contrived one.
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.session
	import subroutine.domain.bootstrap
	import subroutine.domain.local
	import subroutine.domain.tasks
	import subroutine.domain.workspaces

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			principal = subroutine.domain.local.principal(session)
			workspace = subroutine.domain.workspaces.readable(session, principal)[0]
			project = subroutine.domain.bootstrap.inbox_for(session, workspace)

			assert project is not None, "a fresh installation has an Inbox"

			subroutine.domain.tasks.create(
				session, project=project, title=title, type_key=type_key, actor=principal
			)
			session.commit()

	finally:
		engine.dispose()


def test_the_list_says_when_it_did_not_show_everything (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The defect Simon nearly hit by asking whether his list was abbreviated.

	It was not — he had twelve items against a limit of fifty — but the list would have
	stopped dead at fifty with no count, no marker and nothing to suggest more existed. Refs
	are how items are addressed, so the list is where a number is found; a silent cut makes
	"not in the list" stop meaning "not in the system", which is the one inference the whole
	addressing scheme is built to support.
	"""

	run("init")

	for number in range(1, 8):
		run("add", f"Task number {number}")

	listed = run("list", "--limit", "5")

	assert "…and more" in listed.output
	assert "--limit 10" in listed.output, "the remedy, not just the fact"


def test_the_list_is_silent_when_it_did_show_everything (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The boundary, which is where an off-by-one would live.

	Exactly as many items as the limit is *not* truncation, and a list that cried wolf at the
	boundary would be as useless as one that never cried at all.
	"""

	run("init")

	for number in range(1, 6):
		run("add", f"Task number {number}")

	assert "…and more" not in run("list", "--limit", "5").output
	assert "…and more" not in run("list", "--limit", "6").output
	assert "…and more" in run("list", "--limit", "4").output


def test_the_list_holds_documents_as_well_as_tasks (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""Simon asked why #5-#8 were not in his list. They were documents (docs/design.md §12.2).

	Refs come from one counter per workspace and are shared between tasks and documents, and
	``show`` already takes either — so a list holding only tasks told a reader who had learned
	that a number names an item that half the numbers did not exist.
	"""

	run("init")
	run("add", "Ordinary work")
	_a_document(home, title="How the thing works", body="It works like this.")

	listed = run("list").output

	assert "Ordinary work" in listed
	assert "How the thing works" in listed

	# And the type column tells them apart, which is what makes one list readable.
	assert "note" in listed
	assert "task" in listed


def test_ls_is_the_same_command_under_a_shorter_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``list`` is the name the help teaches; ``ls`` keeps working (§12.2a).

	A real word teaches itself where ``ls`` only reads as "list" to somebody who already knows
	Unix — which is not the audience §1.4 is written for. But ``ls`` is in muscle memory and
	in every note anybody has written, so it stays, hidden rather than removed.
	"""

	run("init")
	run("add", "Buy milk")

	assert run("ls").output == run("list").output


def test_the_help_offers_the_real_word_and_not_the_abbreviation (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A synonym in the help is a second thing to choose between, for no gain."""

	run("init")

	listed = run("--help").output

	assert "list" in listed
	assert "\n  ls " not in listed, "the abbreviation is hidden, not advertised"


def test_a_scripted_row_says_which_kind_of_item_it_is (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""And a document carries the shared fields only, rather than task fields as nulls.

	A ``due_at`` of null on something that cannot have a deadline is a statement that it has
	none — a different claim, and a false one. ``entity_type`` is on every row so a script
	never has to test whether a key appeared.
	"""

	run("init")
	run("add", "Ordinary work")
	_a_document(home, title="How the thing works", body="It works like this.")

	rows = {row["title"]: row for row in json.loads(run("list", "--json").output)}

	assert rows["Ordinary work"]["entity_type"] == "task"
	assert "due_at" in rows["Ordinary work"]

	assert rows["How the thing works"]["entity_type"] == "document"
	assert "due_at" not in rows["How the thing works"]
	assert rows["How the thing works"]["ref"] == 2


def test_a_personal_list_gets_no_columns_at_all (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""docs/design.md §1.4 falling out of a layout rule rather than being enforced by one.

	The item type, the priority and the estimate are all worth a column on a mixed backlog
	and all say nothing on a to-do list — every row is an ordinary undated task with no
	priority, so every cell would be identical or empty. :func:`_column` drops a column with
	fewer than two distinct values, so this page is exactly what it was before any of them
	existed.

	The failure this guards against is the quiet one: a list that starts printing ``task``
	and a blank priority against ``buy milk`` has not broken anything, it has just become a
	page about the data model for somebody who only wanted their shopping.
	"""

	run("init")

	for line in ("buy milk", "call the dentist", "renew passport"):
		run("add", line)

	listed = run("list").output

	assert "buy milk" in listed

	for shown in ("task", "!", "None", "  0  "):
		assert shown not in listed, f"a plain personal list printed {shown!r}"


def test_a_column_appears_only_once_it_has_something_to_say (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One priority among four tasks is the moment the column earns its place."""

	run("init")

	for line in ("buy milk", "call the dentist", "renew passport"):
		run("add", line)

	assert "!" not in run("list").output

	run("add", "fix the boiler !4")

	assert "!4" in run("list").output, "the column did not appear once a row had one"


def test_an_axis_nobody_set_is_marked_rather_than_left_blank (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``!4/?`` reads as half-ranked; a blank reads as unranked, and they sort the same way.

	``priority_score`` is null unless *both* axes are set and every ordering is NULLS LAST, so
	a task with an importance and no urgency sinks below everything ranked while looking like
	something judged unimportant. That happened to this project's own backlog for a day.

	Quick capture reaches only one of the two axes (``!4`` and nothing for urgency), so this
	is not a corner case — it is what *every* captured priority looks like, which is what this
	rendering revealed the moment it existed. Tracked as its own defect.
	"""

	run("init")
	run("add", "fix the boiler !4")
	run("add", "something else")

	assert "!4/?" in run("list").output


def test_the_type_column_stays_hidden_when_everything_is_one_kind (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The rule is "fewer than two distinct values", which covers identical as well as empty.

	A page of nothing but ordinary tasks would otherwise carry the word ``task`` on every
	line — a word about the model, on every row, answering a question nobody asked.
	"""

	run("init")
	run("add", "buy milk")
	run("add", "call the dentist")

	assert "task" not in run("list").output


def test_a_list_nobody_delegates_on_has_no_assignee_column (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#511`. The half of §12.2a that must keep holding: §1.4's reader pays nothing for this."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "buy milk")
	run("add", "call the dentist")

	assert "@" not in run("list").output


def test_the_assignee_column_appears_once_one_item_is_handed_over (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#511`. Work could be handed over on every surface and no surface said to whom."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "buy milk")
	run("add", "fix the boiler @si")

	assert "@si" in run("list").output, "the column did not appear once a row had one"


def test_the_assignee_column_survives_everything_being_assigned_to_one_person (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#511`, and **deliberately the opposite answer to the type column above**.

	``_column``'s rule is "fewer than two distinct values", which is right for a kind or a
	priority: there is no reading of a uniform ``bug`` that means its own absence. The assignee
	has one, because its default is blank — so *nobody is assigned any of this* and *one person
	is assigned all of it* are both a single distinct value and would both render as no column,
	the second reading as the first.

	That is exactly `#511`'s defect, returning at the worst possible moment: the page where
	everything has been delegated is the page where hiding it costs most. Hence
	``drop_if_uniform=False`` for this column and this column alone.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "buy milk @si")
	run("add", "call the dentist @si")

	assert "@si" in run("list").output, (
		"a page where every row is assigned to the same person now reads as one where "
		"nothing is assigned at all"
	)


def test_show_says_who_the_work_is_with (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#511`. `#168`'s defect exactly, three lines below `#168`'s own comment in ``_facts``.

	``update 1 --assignee si`` answered *"Changed"* and then ``show`` printed the priority, the
	deadline and the tags and never mentioned it — a field somebody chose, with no surface.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "fix the boiler")

	assert "@si" not in run("show", "1").output

	run("update", "1", "--assignee", "si")

	assert "@si" in run("show", "1").output


def test_a_bare_invocation_says_there_is_more (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""docs/design.md §12.2a's habit, applied to the most likely first thing anybody types.

	Every command here prints the next one to try, and the bare invocation — the one a new
	user reaches for before they know any commands exist — printed no such line at all. It
	showed today's agenda and left them with no reason to think there was anything else.
	"""

	run("init")
	run("add", "buy milk")

	assert "subroutine --help" in run().output


def test_an_explicit_today_carries_no_beginner_signpost (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The same output, a different question, and that is why they are told apart.

	A bare `subroutine` is somebody arriving; `subroutine agenda` is somebody who already
	knows what they want. A daily habit should not carry a signpost forever, and the two are
	distinguishable because Typer reports whether a subcommand was invoked.
	"""

	run("init")
	run("add", "buy milk")

	assert "--help" not in run("agenda").output


def test_help_and_dash_dash_help_are_the_same_answer (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#154`, Simon 2026-08-01. One question must not have two answers.

	These used to differ — ``--help`` listed the commands and ``help`` explained concepts —
	so a reader had to learn which was which before learning either, and the epilog on one
	read as a correction to what they had just typed. ``help`` is what everybody types first,
	so it answers the commonest question; the concepts are ``explain``, whose name says what
	it is for.
	"""

	run("init")

	assert run("help").output == run("--help").output


def test_help_offers_explain_as_a_second_thing_rather_than_a_correction (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Each still names the other, which is what stopped either being undiscoverable.

	The direction is the change: ``help`` no longer sends somebody back to a different help,
	it offers a topic they can read next.
	"""

	run("init")

	assert "subroutine explain" in run("--help").output
	assert "subroutine help" in run("explain").output


def test_a_deferred_task_says_so_wherever_it_appears (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A defer hides a task from the agenda, and every other surface has to explain that.

	Before `#72` it explained it nowhere. `today` hid the task — correctly, §6.5 — `ls`
	printed it with no mark at all, and `show` printed the number and the title and nothing
	else, because `_facts` rendered every date field except this one. So a task could vanish
	from the agenda for four months with no way to find out why from the CLI, and the
	conclusion available to the user was that the agenda was broken.

	The word is `from`, which is one of §6.13's own `DEFER_WORDS` — the phrase reads back as
	something typeable rather than as a label invented for the listing.
	"""

	run("init")
	run("add", "Renew the passport from 2026-12-01 due 2026-12-15")

	# Hidden from the agenda, which is the behaviour being explained rather than a defect.
	assert "Renew the passport" not in run("agenda").output

	# **And hidden from the plain listing too, since `#73`.** The marker's job moved with it:
	# it no longer announces a deferral in a list somebody did not ask for, it labels the row
	# once they have. The two changes are one story — hide it by default, and make it legible
	# wherever it does appear — and a marker with no surface left would have been the sign
	# that `#73` had gone too far.
	assert "Renew the passport" not in run("ls").output

	listed = run("ls", "--deferred").output

	assert "from Tue 1 Dec" in listed

	# **The deadline survives alongside it.** "Not until December, and wanted by the
	# fifteenth" is two facts, and a phrase that could carry only one used to drop this one.
	assert "due Tue 15 Dec" in listed

	assert "from Tue 1 Dec" in run("show", "1").output


def test_a_defer_that_has_come_round_is_reported_only_where_it_is_asked_about (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The two surfaces answer different questions, so they disagree about a passed defer.

	A listing row has one short phrase to spend and it is spent explaining an absence; once
	the instant has passed the task is startable and the defer explains nothing. ``show`` is
	asked "what has been decided about this", and a decision somebody made does not stop
	being one because its date arrived — erasing it would make "why was this not on my list
	in June" permanently unanswerable.
	"""

	run("init")
	run("add", "Chase the invoice from 2020-01-05")

	# Still in the listing: `#73` hides work whose start has *not* arrived, and this one's
	# has. The row is shown, and shown without the marker.
	assert "Chase the invoice" in run("ls").output
	assert "from Sun 5 Jan" not in run("ls").output
	assert "from Sun 5 Jan" in run("show", "1").output


def test_the_listing_ranks_a_backlog_when_asked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#71`: the client layer could order a listing and the command could not expose it.

	The ordering has to survive **two** merges — one per connection inside `_listing`, and one
	across connections in `_merged` — and the second was a separate copy of the rule that
	sorted by `created_at` unconditionally. With that copy in place this test still gets the
	right *items*, because the ordering reaches the query and decides which page comes back;
	it gets them in creation order. That is why the assertion is on the arrangement rather
	than on membership.
	"""

	run("init")
	run("add", "Low stakes !1/1")
	run("add", "Everything is on fire !5/5")
	run("add", "Moderately pressing !3/3")

	listed = run("list", "--order", "-priority_score").output
	ranked = [line for line in listed.splitlines() if "!" in line]

	assert "Everything is on fire" in ranked[0]
	assert "Moderately pressing" in ranked[1]
	assert "Low stakes" in ranked[2]


def test_an_unranked_item_sorts_last_however_the_ranking_runs (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""NULLS LAST in both directions (§10.3), which is also what puts a document last.

	A document has no priority to be ranked by (§6.14), so a list ranked by one has to put it
	somewhere; last is the same answer §6.3a gives an unranked task, which is why the merge
	needs no separate rule for documents. An unranked *task* is the observable half of that.
	"""

	run("init")
	run("add", "Nobody judged this one")
	run("add", "Judged and urgent !5/5")

	for direction in ("-priority_score", "priority_score"):
		rows = [
			line
			for line in run("list", "--order", direction).output.splitlines()
			if "Nobody judged" in line or "Judged and urgent" in line
		]

		assert "Nobody judged" in rows[-1], direction


def test_the_listing_narrows_to_one_project (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`--project`, resolved by the same domain function the endpoint uses.

	It lived in `api/tasks.py` as a private helper, which the local client may not import, so
	a CLI flag would have had to grow a second resolver — the divergence S3-07 removed for the
	task shape and `domain/links` for the link view.
	"""

	run("init")
	run("add", "Filed nowhere in particular")

	# The Inbox is a real project and is what an unfiled task is in, so it is the one key
	# guaranteed to exist without this test having to create a project first.
	narrowed = run("list", "--project", "inbox").output

	assert "Filed nowhere in particular" in narrowed

	# **An unknown key is a failed connection, not a failed command**, because with several
	# connections a project may legitimately exist on one and not another. So it is named on
	# stderr and the command carries on — `--strict` is how a script says it would rather
	# stop, and that is the fan-out's contract rather than anything this flag invented.
	missing = run("list", "--project", "nosuch")

	assert "nosuch" in missing.output

	# And what it must *not* say is that the list is empty. That reads as "the project exists
	# and has nothing in it", which is the one wrong conclusion available.
	assert "Nothing on your list" not in missing.output

	assert run("list", "--project", "nosuch", "--strict", expect=1)


def test_a_truncated_listing_suggests_a_command_that_keeps_the_narrowing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A suggestion that dropped the flags would widen the list while claiming to extend it.

	The reader would then blame the flag rather than the advice, which is the worse of the two
	failures — a wrong suggestion that looks like a broken feature.
	"""

	run("init")

	for index in range(4):
		run("add", f"Thing {index} !2/2")

	suggested = run("list", "--limit", "2", "--order", "-priority_score").output

	assert "…and more" in suggested
	assert "--order -priority_score" in suggested


def test_an_unknown_sort_field_is_refused_before_anything_is_asked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One refusal, not one per workspace — the ordering is parsed before the fan-out."""

	run("init")
	run("add", "Something")

	refused = run("list", "--order", "banana", expect=1)

	assert refused.output.count("banana") <= 2


def test_a_refusal_says_what_the_valid_alternatives_are (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#79`: the CLI printed a field error's message and dropped its hint.

	The hint is the half a reader can act on — "Unknown sort field 'banana'" tells them
	nothing they did not just type. It survived because most field hints repeat their
	message, so the cases that differ are exactly the cases worth having, and it fails
	CLAUDE.md's fourth review dimension on the surface a person actually reads.
	"""

	run("init")
	run("add", "Something")

	refused = run("list", "--order", "banana", expect=1)

	assert "Sortable fields are" in refused.output
	assert "priority_score" in refused.output

	# And it is true of a CLI, which has no endpoints in it.
	assert "endpoint" not in refused.output


def test_a_refusal_does_not_say_one_thing_three_ways (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A lone field error that restates the detail, or the hint, is not printed again.

	A bad date used to print a 200-character remedy and then repeat it verbatim under
	``when:``, adding one word for the second copy. A refusal read as noise is one whose
	successor is read as noise too.

	The subject is a capture line with no title, rather than the bad date this was written
	from: ``plan 1 "next monday"`` **works** as of `#167`, and reaching for a still-broken
	weekday to keep this test would be keeping a defect to keep a guard.
	"""

	run("init")

	refused = run("add", "!3 #tag", expect=1)

	assert refused.output.count("A title is required.") == 1
	assert "title:" not in refused.output


def test_several_field_errors_are_still_named_individually (
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""The de-duplication applies to a lone field only, and that limit is the point.

	With several fields, naming each is the whole value of the list — however much any one
	of them repeats the detail, the reader needs to know which argument it is about.
	"""

	error = subroutine.errors.ValidationError(
		"Two things are wrong.",
		errors=[
			subroutine.errors.FieldError(
				field="importance",
				code="invalid_field_value",
				message="Two things are wrong.",
				hint="Use 1 to 5.",
			),
			subroutine.errors.FieldError(
				field="urgency", code="invalid_field_value", message="Two things are wrong."
			),
		],
	)

	with pytest.raises(typer.Exit):
		subroutine.cli.main._fail(error)

	# `_err` resolves `sys.stderr` on each write rather than at construction, so the capture
	# fixture sees it without the module having to be reloaded.
	printed = capsys.readouterr().err

	assert "importance:" in printed
	assert "urgency:" in printed
	assert "Use 1 to 5." in printed


def test_the_listing_holds_back_deferred_work_and_says_how_much (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#73`: §6.5's "default views hide it entirely" was true of one view out of three.

	`domain/agenda.py` implemented it; `subroutine list` showed work nobody could start yet.
	The fix is not just the hiding — **a hidden row is never silent**. A list that quietly
	omits things stops supporting the inference refs exist for, that "not in the list" means
	"not in the system", which is the failure `#33` was about.
	"""

	run("init")
	run("add", "Do this now")
	run("add", "Renew the passport from 2099-12-01")

	listed = run("list").output

	assert "Do this now" in listed
	assert "Renew the passport" not in listed
	assert "1 thing put off until later" in listed

	widened = run("list", "--deferred").output

	assert "Renew the passport" in widened

	# And when it is shown, it is labelled — `#72`'s marker is what makes the widened list
	# readable rather than just longer.
	assert "from" in widened


def test_a_list_that_is_entirely_parked_does_not_read_as_empty (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The case somebody hits after deferring the last thing they were avoiding.

	"Nothing on your list" would be false and the advice that follows it — add something —
	would be about a list they already have. It is also not "nothing to do today", which is
	the agenda's sentence: `list` is not the agenda.
	"""

	run("init")
	run("add", "Later problem from 2099-01-01")

	listed = run("list").output

	assert "Nothing on your list" not in listed
	assert "Nothing you can start yet" in listed
	assert "1 thing put off until later" in listed
	assert 'subroutine add "something to do"' not in listed


def test_the_scripted_listing_is_never_narrowed_by_a_presentation_rule (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`--json` keeps every open row, and that asymmetry is the point rather than an oversight.

	Hiding parked work is a decision about a list somebody *reads* — which is what §6.5's
	"default views" means, and the whole basis for leaving the API default alone. A script
	asking for open work must not silently lose rows, and every row already carries
	`snoozed_until`, so it can make the same choice for itself.
	"""

	run("init")
	run("add", "Do this now")
	run("add", "Renew the passport from 2099-12-01")

	rows = json.loads(run("list", "--json").output)
	titles = {row["title"] for row in rows}

	assert titles == {"Do this now", "Renew the passport"}

	# And the row carries what a script needs to apply the rule itself.
	parked = next(row for row in rows if row["title"] == "Renew the passport")

	assert parked["snoozed_until"] is not None


def _describe (ref: int, description: str) -> None:
	"""Put a description on a task, reaching past the CLI because nothing there can.

	Not contrived — it is the ordinary state of any task an agent created, and `#81` exists
	because that is where this project's own reasoning lives. What it exposes is a real gap:
	`add` takes no description, `subroutine edit` is §12.2b and unbuilt, and a task's
	description is settable over HTTP and from no command at all.
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.models.work
	import subroutine.db.session
	import subroutine.domain.local
	import subroutine.domain.scoping
	import subroutine.domain.tasks
	import subroutine.domain.workspaces

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			principal = subroutine.domain.local.principal(session)
			found = session.scalars(
				subroutine.domain.scoping.readable_tasks(
					principal,
					workspace_ids=[
						workspace.id
						for workspace in subroutine.domain.workspaces.readable(session, principal)
					],
				).where(subroutine.db.models.work.Task.ref == ref)
			).one()
			subroutine.domain.tasks.update(
				session, found, description=description, actor=principal
			)
			session.commit()

	finally:
		engine.dispose()


def test_search_finds_a_word_that_is_only_in_the_description (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#81`: the command, and the reason it is worth having.

	This project's own reasoning lives in descriptions and document bodies, so searching its
	backlog for a term it discusses at length returned nothing at all. A search that reads
	only titles gets tried once and not again.
	"""

	run("init")
	run("add", "Plain heading")
	run("add", "Something unrelated")

	# The word appears in neither title, which is what makes this a test of the new half.
	_describe(1, "The keyset cursor is decoded wrongly here.")

	found = run("search", "cursor").output

	assert "Plain heading" in found
	assert "Something unrelated" not in found


def test_search_spans_tasks_and_documents (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One counter names either (§6.2), so a search finding only half would lie about the rest.

	The same reasoning as `subroutine list` spanning both, and the same body implements it.
	"""

	run("init")
	run("add", "A task about migrations")

	found = run("search", "migrations").output

	assert "A task about migrations" in found


def test_a_search_that_matches_nothing_does_not_claim_the_list_is_empty (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The list is not empty; this search found nothing in it.

	Saying the first about the second is how somebody concludes their data is gone. The
	remedy offered widens rather than narrows, because a search that missed is usually one
	that was too narrow.
	"""

	run("init")
	run("add", "Buy milk")

	missed = run("search", "aardvark").output

	assert "Nothing matches" in missed
	assert "Nothing on your list" not in missed
	assert 'subroutine add "something to do"' not in missed


def test_a_search_row_says_where_the_word_was_found (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A hit whose reason is invisible reads as a bug rather than as an answer.

	And the column obeys the same rule as every other one: it appears when the rows disagree
	and vanishes when they do not, so a search whose hits are all in the title looks exactly
	like a listing (§12.2a).
	"""

	run("init")
	run("add", "Cursor handling")
	run("add", "Unrelated heading")
	_describe(2, "This one only mentions the cursor down here.")

	# The rows disagree about where the word is, so the column earns its place and both are
	# labelled — a blank beside a hit would read as missing data rather than as ordinary.
	mixed = run("search", "cursor").output

	assert "title" in mixed
	assert "description" in mixed

	# And with one row there is nothing to disagree with, so it is dropped again.
	uniform = run("search", "Cursor handling").output

	assert "Cursor handling" in uniform
	assert "title" not in uniform


def test_a_row_matched_by_its_number_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`#870`, found in driving output rather than by a test.**

	`#867` made a bare number match the item with that ref, and left that row's reason cell
	empty — the exact shape `_match_cell` exists to prevent, since a hit with no visible reason
	reads as a broken search rather than as an answer.

	Two rows, because §12.2a drops a column whose rows agree: one matched by its number and one
	by its text, so the column earns its place and the difference is what is being asserted.
	"""

	run("init")
	run("add", "Wholly unlike the query")
	run("add", "Follows on from #1 in some way")

	found = run("search", "1").output

	assert "Wholly unlike the query" in found, "the item with that ref"
	assert "number" in found, "and the row says that is why it is here"


def test_a_row_matched_where_the_listing_cannot_look_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#870`'s other half, and `#825` warned about it before `#83` was built.

	Its words: *"the cost of that is a blank cell on a row that did match"* — so the column had
	to be extended in the same change that gave it a third way to be empty.

	**The satisfier changed with `#881` and the intent did not.** This asserted the word
	`comment`, which the listing cannot prove: under the `native` backend a stemmed match is
	never a substring, so nothing here can tell a comment match from one in the title it failed
	to recognise. Measured on the served instance, the cell was saying `comment` about three
	rows in five that had none — a cell whose only job is to explain a match, stating a false
	one. `elsewhere` is what it can prove on both backends, and it is what this now asserts.
	"""

	run("init")
	run("add", "An ordinary heading")
	run("add", "A semi-join in the heading")
	run("comment", "1", "The planner turns this into a semi-join.")

	found = run("search", "semi-join").output

	assert "An ordinary heading" in found, "matched only by the comment on it"
	assert subroutine.cli.personal.ELSEWHERE in found
	assert "title" in found, "and the other row disagrees, which is why the column shows"


def test_a_row_matched_by_words_spread_across_its_title_says_which_field (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`#881`, and it is the case the old cell got wrong on every installation.**

	A search is a set of words, all of which must appear, in any order and in any column
	(`#620`) — and `_match_cell` tested the whole query as one contiguous substring. So a title
	holding both words in the other order matched in SQL, failed every branch in Python, and
	was reported as having matched a comment. Measured before the fix: **30% of the rows ten
	real multi-word searches returned could not be explained at all.**

	Reproduced here on the `like` backend, which is the default, on an instance with **no
	comments in it** — so the old answer was not merely imprecise, it named something that did
	not exist.

	Two rows, because §12.2a drops a column whose rows agree.
	"""

	run("init")
	run("add", "Pagination resumes from the wrong cursor row")
	run("add", "Wholly unrelated")
	run("comment", "2", "cursor pagination")

	found = run("search", "cursor pagination").output

	assert "Pagination resumes" in found, "both words are in its title, in the other order"
	assert "title" in found, f"the row must say where its match is — {found!r}"
	assert subroutine.cli.personal.ELSEWHERE in found, "and the other row disagrees"


def _parented (child_ref: int, parent_ref: int) -> None:
	"""Make one task the child of another, reaching past the CLI because nothing there can.

	`#44`: `parent_task_id` is accepted at creation and never afterwards, so an item cannot
	join a subtree it was not born into. That is the gap these two items are the *reading*
	half of — the structure is real and was invisible.
	"""

	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.models.work
	import subroutine.db.session
	import subroutine.domain.local
	import subroutine.domain.scoping
	import subroutine.domain.workspaces

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			principal = subroutine.domain.local.principal(session)
			statement = subroutine.domain.scoping.readable_tasks(
				principal,
				workspace_ids=[
					workspace.id
					for workspace in subroutine.domain.workspaces.readable(session, principal)
				],
			)
			rows = {
				row.ref: row
				for row in session.scalars(statement)
				if row.ref in (child_ref, parent_ref)
			}
			rows[child_ref].parent_task_id = rows[parent_ref].id
			session.commit()

	finally:
		engine.dispose()


def test_a_listing_says_which_items_have_a_parent (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#63`: a subtree printed exactly like every other row, so it read as unrelated items.

	**A column, not indentation**, and that is the design rather than a shortcut. A listing is
	ordered by recency or by priority, so a child is rarely adjacent to its parent — drawing
	a tree connector under an unrelated row states a relationship that is not there. A ref is
	true wherever the row lands.
	"""

	run("init")
	run("add", "The whole feature")
	run("add", "One part of it")
	_parented(2, 1)

	listed = run("list").output

	assert "^1" in listed


def test_a_list_with_no_subtree_never_mentions_parents (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4 falling out of the layout rule again, third application of `#26`'s `_column`.

	A personal to-do list has no subtasks, so every row's cell is empty, so the column does
	not earn its place and is not drawn. Nobody keeping a shopping list meets the word.
	"""

	run("init")
	run("add", "Buy milk")
	run("add", "Call the dentist")

	assert "^" not in run("list").output


def test_show_names_both_directions_of_the_relationship (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#62`'s remaining half: the parent by title, and the children with a rollup.

	The rollup counts *completed* children too — a parent reporting two of its four parts
	because the other two are finished would misreport the thing somebody opened it to see.
	`#84` decided the rollup is reported and completion stays an act, and this is where it is
	read.
	"""

	run("init")
	run("add", "The whole feature")
	run("add", "First part")
	run("add", "Second part")
	_parented(2, 1)
	_parented(3, 1)

	parent = run("show", "1").output

	assert "Sub-tasks" in parent
	assert "0 of 2 done" in parent
	assert "First part" in parent and "Second part" in parent

	child = run("show", "2").output

	assert "part of" in child
	assert "The whole feature" in child, "the parent is named by its title, not only its ref"

	# A finished part still counts and is still listed.
	run("done", "2")

	assert "1 of 2 done" in run("show", "1").output


def test_show_on_a_plain_task_still_says_nothing_about_hierarchy (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The rule that lets `show` exist at all: a field nobody set is not printed."""

	run("init")
	run("add", "Buy milk")

	shown = run("show", "1").output

	assert "part of" not in shown
	assert "Sub-tasks" not in shown


def test_a_default_install_can_make_a_project_and_file_into_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#134``, and the reason it blocked the first release.

	Until 2026-07-31 ``domain.projects.create`` had two callers: ``bootstrap``, which makes
	the Inbox during ``init``, and the HTTP router. **On a default install neither is
	reachable** — nothing runs ``serve`` unless somebody asks it to — so the only project
	anybody would ever have was the Inbox, and ``+KEY`` in a captured line could only ever
	refuse.

	This runs the whole path a person actually takes, on a temporary home with nothing else
	set up: make one, put something in it, and read it back out.
	"""

	run("init")
	run("project", "create", "web", "Website redesign")
	run("add", "Fix the header +web")

	assert "Fix the header" in run("list", "--project", "web").output


def test_a_project_key_is_refused_rather_than_repaired (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""It is permanent and it becomes part of every address, so a guess is not good enough.

	Case is the exception and is not a repair: ``web`` and ``WEB`` are the same key rather
	than one being fixed into the other, which is why the second of these collides.
	"""

	run("init")

	assert "not a usable key" in run("project", "create", "2FA", "Digits", expect=1).output

	run("project", "create", "web", "Website")

	assert "already in use" in run("project", "create", "web", "Again", expect=1).output


def test_the_project_listing_shows_what_is_inside_what (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Ordered by path so a child follows its parent, and indented so that is visible.

	The indentation is the only thing carrying the shape — decision ``#102`` forbids a colour
	being the sole bearer of anything, and a tree drawn in a colour would be exactly that.
	"""

	run("init")
	run("project", "create", "outer", "Outer thing")
	run("project", "create", "inner", "Inner thing", "--parent", "outer")

	printed = run("project", "list").output
	rows = [line for line in printed.splitlines() if line.strip()]

	assert any(line.startswith("outer") for line in rows)
	assert any(line.startswith("  inner") for line in rows), printed

	def where (key: str) -> int:
		"""Return which row a project is on."""

		return next(index for index, line in enumerate(rows) if key in line)

	assert where("outer") < where("inner"), printed


def test_a_document_write_can_answer_without_repeating_the_document (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1360`. Setting a 9 KB specification's status printed the whole specification back.

	**Both directions, because only one of them is the defect.** Omitting the body on request is
	the new half; still carrying it by default is the half that must not have changed, since the
	shape is published and a caller parses it. A guard asserting only the omission would pass on
	a version that had quietly dropped the body for everybody.

	And the answer must still say the text is *there* — ``size_bytes`` is what stops an omission
	reading as an empty document.
	"""

	run("init")

	made = run("doc", "create", "A conclusion", "--body", "the reasoning", "--json")
	ref = json.loads(made.output)["ref"]

	kept = json.loads(run("doc", "edit", str(ref), "--status", "active", "--json").output)
	spared = json.loads(
		run("doc", "edit", str(ref), "--status", "active", "--json", "--no-body").output
	)

	assert "body" in kept, "the default stopped carrying the body, which is a published shape"
	assert "body" not in spared, "--no-body still returned the document's text"
	assert spared["size_bytes"] == kept["size_bytes"], (
		"the answer no longer says the text is there, so an omission reads as an empty document"
	)


def test_a_title_beginning_with_two_hyphens_survives_the_separator (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1359`. The separator worked and nothing said so, which is the whole of the item.

	**Driven rather than asserted about**, because `#1263`'s rule is that a documented command
	has to be one that works: the topic now prints this exact invocation, so the test's job is to
	be the thing that fails if it ever stops being true.

	The title must arrive **verbatim** — the failure this replaces was the writer changing their
	wording to get past the parser, so a task that merely exists is not the outcome.
	"""

	run("init")

	wanted = "--format json emits the documented schema"
	run("add", "--", wanted)

	printed = run("list").output

	assert wanted in printed, f"the title did not survive the separator:\n{printed}"


def test_the_capture_topic_names_the_separator_it_needs (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half: a reader has to be able to find it.

	`SR#1359` was reported as *impossible*, by somebody who rewrote their title to work around
	it. The behaviour was already right; only the sentence was missing.
	"""

	said = run("explain", "capture").output

	assert "--" in said and "two hyphens" in said, said


def test_a_bare_project_lists_rather_than_printing_help (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1355`. The terminal printed help where the tools list, so one verb had two answers.

	**Driven against `project list` rather than against a remembered shape**, because the claim
	is that the two forms agree — asserting on the text separately would let them drift apart
	while both tests passed, which is the defect one level up.
	"""

	run("init")
	run("project", "create", "outer", "Outer thing")

	bare = run("project").output
	named = run("project", "list").output

	assert "Usage:" not in bare, f"a bare 'project' still printed help:\n{bare}"
	assert bare == named, f"'project' and 'project list' answer differently:\n{bare}\n---\n{named}"


def test_the_workspaces_a_listing_names_are_the_ones_whoami_names (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1355`. Every other workspace verb takes a slug and none of them printed one.

	**The two are compared rather than each checked against a fixture.** They read one identity,
	so the thing worth holding is that they cannot come apart — a guard that asserted the text
	of each would pass on the day one of them started answering a different question.
	"""

	run("init")

	listed = [line.split()[0] for line in run("workspace", "list").output.splitlines() if line.strip()]

	assert listed, "the workspace listing named nothing at all"

	said = run("whoami").output

	for slug in listed:
		assert slug in said, f"'workspace list' named {slug!r} and 'whoami' did not:\n{said}"


def test_a_listing_marks_work_that_cannot_be_started_yet (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#425``. The default listing put a blocked item above its blocker, unmarked.

	Reported by an agent on a fresh install: *"list default-orders a blocked item above its
	blocker with no marker that it is blocked. ready=true filters correctly, but the default
	listing is the one you would hand to a person, and it reads as start with #2."*

	The ordering is not the bug and is not changed — newest first is what was asked for. What
	was missing is a row that says why the top one is not the one to start.

	**Neither title contains the word, and that is not incidental.** The first version of this
	test used "The thing that is blocked" and passed with the field removed from the view
	entirely — it was matching the title, not the marker. Falsifying caught it; reading it
	would not have.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Land the migration")
	run("add", "Ship the release")
	run("link", "1", "blocks", "2")

	listed = run("list").output
	rows = {
		row.split()[0].lstrip("#"): row
		for row in listed.splitlines()
		if row.strip().startswith("#")
	}

	assert set(rows) == {"1", "2"}, listed
	assert "blocked" not in rows["1"], rows["1"]

	# The marker is its own cell between the address and the title, not part of either.
	address, marker, title = rows["2"].split(maxsplit=2)

	assert (address, marker, title) == ("#2", "blocked", "Ship the release"), rows["2"]

	# --ready still filters rather than marks, which is the half that already worked.
	assert "#2" not in run("list", "--ready").output


def _marked (listed: str) -> dict[str, str]:
	"""Return each listed row by its ref, without the leading ``#``."""

	return {
		row.split()[0].lstrip("#"): row
		for row in listed.splitlines()
		if row.strip().startswith("#")
	}


def test_a_listing_marks_work_that_is_holding_something_up (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#569``, and it is the mirror of ``#425`` — which is the whole finding.

	`#425` made work that *cannot be started* visible and stopped there. A visitor agent read
	a board where the urgent item was marked ``blocked`` and the five-minute errand holding it
	up carried nothing at all, and said the ranking was lying: the errand was the only thing
	whose completion changed anything. A rule aimed at one direction of a symmetric problem
	never fires for the other.

	**No title contains either marker**, which `#425`'s own docstring records as the trap: its
	first version matched the word in a title and passed with the field removed from the view.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Chase the photographer")
	run("add", "Rewrite the pricing page")
	run("link", "1", "blocks", "2")

	rows = _marked(run("list").output)

	assert set(rows) == {"1", "2"}

	address, marker, rest = rows["1"].split(maxsplit=2)

	# **One word, so the split lands where it reads** (`#913`). This asserted `("#1", "holds",
	# "up  Chase the photographer")` — the mark was two words, so splitting on whitespace cut it
	# in half and half of it arrived attached to the title. That was correct and it hid what the
	# assertion was about.
	assert (address, marker, rest) == ("#1", "blocker", "Chase the photographer"), rows["1"]

	# And the other end still says what it always said.
	assert "blocked" in rows["2"], rows["2"]

	# A script gets both facts as fields rather than one cell with a precedence.
	scripted = {row["ref"]: row for row in json.loads(run("list", "--json").output)}

	assert scripted[1]["blocking"] is True
	assert scripted[1]["blocked"] is False
	assert scripted[2]["blocking"] is False
	assert scripted[2]["blocked"] is True


def test_a_row_that_is_both_says_blocked_because_that_is_what_stops_you (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A middle link in a chain is held up *and* holding something up.

	One column carries both directions, so something has to win, and it is ``blocked``: that
	is the fact deciding whether you can act at all. The other half is not lost — a script
	gets both fields, and ``subroutine show`` lists every link either way.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "First in the chain")
	run("add", "Second in the chain")
	run("add", "Third in the chain")
	run("link", "1", "blocks", "2")
	run("link", "2", "blocks", "3")

	rows = _marked(run("list").output)

	assert "blocked" in rows["2"], rows["2"]
	assert "holds" not in rows["2"], rows["2"]

	# The fact the cell had to drop is still on the scripted row, which is why it is two
	# fields there and one column here.
	scripted = {row["ref"]: row for row in json.loads(run("list", "--json").output)}

	assert scripted[2] == {**scripted[2], "blocked": True, "blocking": True}


def test_finishing_the_held_up_work_clears_the_marker (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Nothing is held up by work that is done, so nothing is holding it up either.

	Without this a shipped release would go on marking everything that ever blocked it, and
	the marker would accumulate rather than decay — the same rule `#425` applies from the
	other end, stated once in `readiness._live_blocks_edge` and read in both directions.

	**The bystander is what makes this able to fail**, and the first version had none. Once
	the held-up item is finished it leaves the listing, and `_column` drops a column with
	fewer than two distinct values — so a single remaining row cannot show a marker whatever
	the code believes, and the assertion held for a reason that had nothing to do with the
	rule. Falsifying caught it: removing the completed-work clause left this green while the
	scripted row said `blocking: true`.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Chase the photographer")
	run("add", "Rewrite the pricing page")
	run("add", "Something else entirely")
	run("link", "1", "blocks", "2")

	assert "blocker" in run("list").output

	run("done", "2")

	assert "blocker" not in run("list").output

	# And the fact itself, not only its rendering — the column would hide a disagreement.
	scripted = {row["ref"]: row for row in json.loads(run("list", "--json").output)}

	assert scripted[1]["blocking"] is False


def test_only_a_blocks_link_marks_a_row_as_holding_something_up (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Every other link type says something about meaning, not about order.

	``relates_to`` is the one that would do it silently, since a specification relates to
	everything in its project.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Chase the photographer")
	run("add", "Rewrite the pricing page")
	run("link", "1", "relates-to", "2")

	assert "blocker" not in run("list").output


def test_a_listing_with_nothing_blocked_shows_no_such_column (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4: a person who has never linked two items never meets the word.

	The column is measured across the page and dropped when every row is empty — the rule the
	kind, started and priority columns already follow. Without this the marker would be a
	permanent extra column on a to-do list, which is exactly what §13.5b exists to prevent.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Buy milk")
	run("add", "Call the dentist")

	assert "blocked" not in run("list").output


def test_add_marks_off_what_it_read_from_the_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#426``, and nothing pinned this format before — so it could regress in silence.

	The echo is `#135`'s confirmation that a sigil was *understood* rather than left in the
	title, and a double space was the whole of what separated the two. An agent reported it as
	genuinely useful and genuinely unreadable, which is a fair description.
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	added = run("add", "Fix the header !4/2 ~2h").output
	line = next(row for row in added.splitlines() if row.startswith("Added"))

	assert line.endswith("(read !4/2 ~2h)"), line
	assert "Fix the header  (read" in line, line

	# §1.4's ordinary case gains nothing: no sigils, no echo, no parentheses.
	assert "(read" not in run("add", "Buy milk").output


def test_add_files_a_description_in_the_same_call (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#424``. A described item used to be two commands, and the second got skipped.

	The skill argues for titles that say the outcome rather than the problem, on the grounds
	that the motivation "belongs in the description — which is one field away". It was not one
	field away on any surface: `#392` gave `subroutine_update` one, which made it a second call
	after the item existed, and `add` had none at all. An agent on a fresh install followed the
	titling advice, skipped the second call six times, and reported that its own titles were
	meaningless without the document it had put everything in.

	**Fixing the surface is what makes that sentence true**, which is why it beat rewording it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Cache the connection roster !3/2", "--description", "Measured at 400ms a call.")

	shown = run("show", "1").output

	assert "Cache the connection roster" in shown
	assert "Measured at 400ms a call." in shown

	# The grammar is untouched by it: a description sits beside the line, never inside it, so
	# the line's own tokens are still read and the title still ends where it did.
	filed = json.loads(run("show", "1", "--json").output)["item"]

	assert filed["title"] == "Cache the connection roster"
	assert (filed["importance"], filed["urgency"]) == (3, 2)


def test_add_without_a_description_sets_none (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Saying nothing means nothing, which is not the same as saying "".

	`create_from_text` merges overrides over the parsed fields, so an empty string passed
	through would be a caller overriding a field they never mentioned — the shape that made
	`estimate` and `tags` unoverridable for a while, one direction reversed.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Buy milk")

	assert json.loads(run("show", "1", "--json").output)["item"]["description"] is None


def test_add_says_what_it_read_out_of_the_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#135``. It filed the work correctly and confirmed nothing, which is the same as not
	being sure it did.

	Written back as the sigils that were typed, because that needs no vocabulary — and because
	§13.5b forbids the words a sentence explaining them would have to use.
	"""

	run("init")
	run("project", "create", "web", "Website")

	printed = run("add", "Fix the header !4/2 ~2h #ops +web").output

	for sigil in ("+web", "!4/2", "~2h", "#ops"):
		assert sigil in printed, f"{sigil} was read and not mentioned:\n{printed}"


def test_add_confirms_a_planned_day_beside_a_deadline (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#673``. The two dates used to be alternatives, and the deadline won.

	So ``on monday by friday`` was answered with Friday alone: Monday had been read and
	stored, and the only evidence was the words having left the title — which is exactly what
	their never having been read looks like. The skill names this line as *the* check, so the
	caller doing as it is told learned nothing.

	**Both renderings are derived from the single-date runs** rather than written out here.
	``_dated`` adds a year only when a bare one would be ambiguous (`#78`), so spelling either
	date in this file would make the test depend on the year it is run in — the trap that
	function's own docstring was written about.
	"""

	run("init")

	planned = run("add", "Sand the door on 2027-03-01").output
	deadline = run("add", "Sand the door by 2027-03-05").output
	together = run("add", "Sand the door on 2027-03-01 by 2027-03-05").output

	day = re.search(r"\(starts ([^,)]+)\)", planned)
	by = re.search(r"\(due ([^,)]+)\)", deadline)

	assert day is not None, f"a planned day alone was not reported:\n{planned}"
	assert by is not None, f"a deadline alone was not reported:\n{deadline}"

	assert f"starts {day.group(1)}" in together, (
		f"the start was dropped once there was a deadline:\n{together}"
	)
	assert f"due {by.group(1)}" in together, f"the deadline was dropped:\n{together}"


def test_an_ordinary_line_is_answered_exactly_as_before (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4. Somebody adding "Buy milk" has typed no grammar and is owed no report of it.

	The whole risk in ``#135`` was making every capture noisier to fix the one that was quiet.
	"""

	run("init")

	assert run("add", "Buy milk").output.splitlines()[0].strip() == "Added: Buy milk"


def test_ready_hides_work_that_cannot_be_started (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#136``. Until this landed, the answer to "what should I work on" was a sorted
	backlog — which is what every other tool offers.

	Deferred work is the half the CLI could already express, through ``--deferred``; blocked
	work is the half it could not express at all.
	"""

	run("init")
	run("add", "Do this one")
	run("add", "Chase it up next week")
	run("defer", "2", "now+7d")

	printed = run("list", "--ready").output

	assert "Do this one" in printed
	assert "Chase it up next week" not in printed, printed


def test_ls_and_list_offer_the_same_flags (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""They share one body, so a flag on one and not the other is a silent divergence.

	``ls`` is the hidden short name for ``list`` and nothing in the help says they differ —
	somebody who learned ``--ready`` on one has every reason to expect it on the other.
	"""

	def flags (command: str) -> set[str]:
		"""Return the long options a command declares."""

		return {
			word.strip(" │")
			for word in run(command, "--help").output.split()
			if word.startswith("--")
		}

	assert flags("list") == flags("ls")


def test_a_document_can_be_written_from_the_cli (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#138``. §5.10's other half, which had no path outside HTTP.

	Read back through ``show``, because the listing spans both kinds (§6.2) and the whole
	claim is that a number names an item whichever kind it turns out to be.
	"""

	run("init")
	run("doc", "create", "Why we dropped the queue", "--type", "decision", "--body", "Because.")

	shown = run("show", "1").output

	assert "Why we dropped the queue" in shown
	assert "Because." in shown
	assert "decision" in shown


def test_a_document_body_can_be_piped_in (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Which is how anybody writes more than a sentence at a terminal, and how an agent does.

	``--body`` wins when both are given: an argument somebody typed is more deliberate than a
	stream they may not have realised was open.
	"""

	run("init")
	run("doc", "create", "Review findings", input="Three findings.\nNone found by reading.\n")

	assert "None found by reading." in run("show", "1").output

	# The claim above, asserted rather than described.
	run("doc", "create", "Typed wins", "--body", "This one.", input="Not this one.\n")

	shown = run("show", "2").output

	assert "This one." in shown
	assert "Not this one." not in shown


def test_writing_a_document_needs_no_type_or_project (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A conclusion arrives before its filing does, so nothing but the title is required."""

	run("init")

	assert "Wrote:" in run("doc", "create", "Just a thought").output


def test_something_added_by_mistake_can_be_taken_off_the_list (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#140``, and the wall was on the surface §1.4 exists to protect.

	Adding "Buy mikl" left you with "Buy mikl" for ever. ``done`` was the only way to make it
	go away, and it is a lie: it says the thing happened.
	"""

	run("init")
	run("add", "Buy mikl")
	run("add", "Call the dentist")

	assert "Deleted: Buy mikl" in run("delete", "1").output

	remaining = run("list").output

	assert "Buy mikl" not in remaining
	assert "Call the dentist" in remaining


def test_the_wrong_thing_deleted_can_be_put_back (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The second commonest mistake after deleting something, and the reason it is soft.

	The suggestion printed by ``delete`` is the command that does it, not a reassurance that it
	could be done — a claim the reader has to trust against one they can run. It carries the
	ref because after this the item is out of every listing, so the number on screen is the
	only way back to it.
	"""

	run("init")
	run("add", "Buy milk")

	assert "subroutine restore 1" in run("delete", "1").output
	assert "Restored: Buy milk" in run("restore", "1").output
	assert "Buy milk" in run("list").output


def test_the_trash_is_a_separate_list_rather_than_a_wider_one (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Nothing in a compact row says whether it is deleted, so a mixed list cannot be read."""

	run("init")
	run("add", "Kept")
	run("add", "Discarded")
	run("delete", "2")

	trash = run("list", "--trash").output

	assert "Discarded" in trash
	assert "Kept" not in trash, trash


def test_a_document_can_be_deleted_and_restored_by_the_same_commands (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One counter serves both kinds (§6.2), and nothing about a number says which it is.

	A `delete` that worked on half the refs would be a surprise nobody could predict from the
	one they were holding.
	"""

	run("init")
	run("doc", "create", "Written by mistake")

	assert "Deleted: Written by mistake" in run("delete", "1").output
	assert "Restored: Written by mistake" in run("restore", "1").output


def test_saying_one_thing_blocks_another_changes_what_is_ready (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#141``'s highest, and the loop it completes.

	``--ready`` reads ``blocks`` links, and nothing outside raw HTTP could make one — so the
	filter shipped in the morning with no way for anybody using the CLI to put anything into
	it.
	"""

	run("init")
	run("add", "Design the schema")
	run("add", "Build the endpoint")

	run("link", "1", "blocks", "2")

	assert "Build the endpoint" not in run("list", "--ready").output

	run("done", "1")

	assert "Build the endpoint" in run("list", "--ready").output


def test_a_deleted_blocker_leaves_the_count_and_stays_on_the_page (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1403`. A milestone counting a deleted blocker can never reach N of N.

	Met on 2026-08-27 building the roadmap: SR#1400 was deleted and the milestone it blocked went
	on reading `0 of 6` with a trashed row among the six. **It was quietly unfinishable**, and
	the reader's natural move is to go looking for work that is not there.

	**`readiness.unblocked` has excluded a deleted blocker since it was written** — the row was
    never actually held up — so this is the display catching up with the rule rather than a
	change to what is startable.

	**The row stays and says so**, which is §12.2a: delete here is reversible and `restore` is
	offered in the confirmation, so removing the line would lose the record that the link exists
	and leave an absence somebody has to infer.
	"""

	run("init")

	for title in ("ROADMAP", "PHASE 1", "PHASE 2", "PHASE 3"):
		run("add", title)

	run("link", "2,3,4", "blocks", "1")
	run("done", "2")

	assert "(1 of 3 blockers done)" in run("show", "1").output

	run("delete", "3")

	shown = run("show", "1", "--tree").output

	assert "(1 of 2 blockers done)" in shown, shown
	assert "What has to happen first (1 of 2 done)" in shown, shown
	assert shown.count("(deleted)") == 2, (
		f"the deleted row is marked in the links section and in the tree:\n{shown}"
	)
	assert shown.count("PHASE 2") == 2, "and it is still on the page in both"

	# **The property the count exists for**: finishing what is left finishes the milestone.
	run("done", "4")

	assert "(2 of 2 blockers done)" in run("show", "1").output

	# **And restoring reverses it**, which is why the link was kept rather than dropped.
	run("restore", "3")

	assert "(2 of 3 blockers done)" in run("show", "1").output


def test_a_plan_can_be_read_in_one_call_rather_than_one_per_item (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1358`, from SR#1290. `show` answered one node at a time.

	The reviewer built 42 links across 28 items and then had no way to look at them — checking
	the order of work would have meant 28 calls to reconstruct what they had just written, so
	**they verified it by reasoning instead**, which is the part worth worrying about.

	**Prerequisites, not dependents**, which is `SR#84`'s model read the way somebody asks it: a
	milestone is an item whose blockers are its contents, so this renders a roadmap as its
	phases and a task as what must happen before it.

	**Indented, and the order is depth-first** — a flat list carrying a depth only reads as a
	tree if the rows arrive in the order somebody looks at them. Written breadth-first first,
	where every level came out together and the indentation described nothing.
	"""

	run("init")

	for title in ("ROADMAP", "PHASE 1", "PHASE 2", "Fix the agenda", "Fix the capture", "Tag it"):
		run("add", title)

	run("link", "2,3", "blocks", "1")
	run("link", "4,5", "blocks", "2")
	run("link", "6", "blocks", "3")

	shown = run("show", "1", "--tree").output

	assert "What has to happen first (0 of 5 done)" in shown, shown

	# **Read from the heading down**, because the page also carries the item's own line and a
	# one-level `Links` section — taking every line with a `#` in it measured those too, which
	# is a harness reading the wrong thing rather than a product doing it.
	after = shown.split("What has to happen first")[1]
	walked = [
		line for line in after.splitlines() if line.strip().startswith("#")
	]

	# **The order is the reading order**, so a phase's own parts follow it rather than every
	# phase arriving before any of their contents.
	titles = [line.split("  ")[-1].strip() for line in walked]

	assert titles == [
		"PHASE 1", "Fix the agenda", "Fix the capture", "PHASE 2", "Tag it"
	], f"the walk is not in reading order:\n{shown}"

	# **And the depth is in the indentation**, which is what makes the shape visible at all.
	def indent (title: str) -> int:
		"""Return how far in a row was drawn."""

		line = next(one for one in walked if one.rstrip().endswith(title))

		return len(line) - len(line.lstrip())

	assert indent("PHASE 1") < indent("Fix the agenda"), shown
	assert indent("PHASE 1") == indent("PHASE 2"), shown


def test_a_finished_part_of_a_plan_is_counted_rather_than_hidden (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1358`. The rollup carries the count and the row stays — the links section's rule.

	Removing a finished row would hide the contents of a finished milestone, which is what
	somebody opened it to see. Decision `SR#102` besides: no information exists only in a
	colour, so the heading says how many.
	"""

	run("init")

	for title in ("ROADMAP", "PHASE 1", "PHASE 2"):
		run("add", title)

	run("link", "2,3", "blocks", "1")
	run("done", "2")

	shown = run("show", "1", "--tree").output

	assert "What has to happen first (1 of 2 done)" in shown, shown
	assert "PHASE 1" in shown, "a finished part was removed rather than counted"


def test_an_item_reached_twice_is_drawn_once_and_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1358`. The shape is a graph, and pretending otherwise doubles the plan.

	Two phases sharing a blocker is ordinary. Drawing its whole subtree under both would say
	there are two of them — and the count on the heading would say so too.
	"""

	run("init")

	for title in ("ROADMAP", "PHASE 1", "PHASE 2", "Shared groundwork"):
		run("add", title)

	run("link", "2,3", "blocks", "1")
	run("link", "4", "blocks", "2,3")

	shown = run("show", "1", "--tree").output
	rows = [
		line for line in shown.split("What has to happen first")[1].splitlines()
		if line.strip().startswith("#")
	]

	assert len([one for one in rows if "Shared groundwork" in one]) == 2, (
		f"a shared blocker is drawn under each thing waiting on it:\n{shown}"
	)

	# **The first drawing carries no mark, and asserting only that the mark *appears* is what
	# let `SR#1410` ship**: `stopped` was keyed by item where *again* is a property of an
	# appearance, so an item at two depths had **both** its drawings marked — including the
	# first. On the real roadmap the very first row read *(shown above)* with nothing above it,
	# and this test was green.
	first, second = [one for one in rows if "Shared groundwork" in one]

	assert "(shown above)" not in first, f"the first drawing claims to be a repeat:\n{shown}"
	assert "(shown above)" in second, f"the second drawing does not say so:\n{shown}"

	# **And a repeat is out of the count**, which is the deleted rows' bargain applied again
	# (`SR#1403`, `SR#1410`): the heading answers *how much of this is left*, one item finished
	# once is finished, and the mark on the row explains the difference between the count and
	# what is drawn. Three distinct items, four drawings.
	assert "What has to happen first (0 of 3 done)" in shown, shown
	assert len(rows) == 4, f"four rows are drawn:\n{shown}"


def test_the_tree_is_absent_from_the_scripted_output_until_it_is_asked_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#349`'s decision one section along — ``null`` for *not asked*, a list for *asked*.

	``[]`` is also what *asked, and nothing blocks this* produces, and a reader of one
	invocation's output cannot tell which flags made it. So the distinction lives in the value
	where they can see it, rather than in the key's presence, which only a reader of the source
	could reason about.
	"""

	run("init")
	run("add", "ROADMAP")
	run("add", "PHASE 1")
	run("link", "2", "blocks", "1")

	assert json.loads(run("show", "1", "--json").output)["tree"] is None

	walked = json.loads(run("show", "1", "--tree", "--json").output)["tree"]

	assert [one["item"]["title"] for one in walked] == ["PHASE 1"]
	assert walked[0]["depth"] == 1

	# **And an item with nothing under it answers with a list**, which is the half that makes
	# the distinction worth having: this is *asked, and nothing blocks it*.
	assert json.loads(run("show", "2", "--tree", "--json").output)["tree"] == []


def test_one_call_makes_a_link_to_each_of_several_items (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1352`, and SR#1290's headline finding.

	*"Filing one task is delightful; filing twenty-two tasks and forty-two links is the same
	delightful interaction repeated sixty-four times."* One realistic project cost 23 creates
	and 37 `blocks` links — a loop in a shell, and sixty round trips through the agent tools,
	where there is no shell to fall back on.

	**One line each rather than a count**, even at twenty. The confusable thing about a link is
	direction, and *made 4 links* reads identically whichever way round they went.
	"""

	run("init")

	for title in ("Ship it", "Changelog", "Tag it", "Announce it"):
		run("add", title)

	made = run("link", "1", "blocks", "2,3,4")

	assert made.output.count("Blocks:") == 3, made.output
	assert "Changelog" in made.output and "Announce it" in made.output, made.output

	shown = run("show", "1").output

	for title in ("Changelog", "Tag it", "Announce it"):
		assert title in shown, f"{title} was not joined:\n{shown}"


def test_a_superseded_item_says_where_the_work_went_and_the_successor_says_what_it_replaced (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`SR#1688`, and this is the argument for a link rather than a column.**

	A document has carried ``supersedes_id`` since the first migration — a unique index so the
	chain cannot fork, settable on both clients and at the terminal — and **226 documents on the
	instance this was found on use it zero times**, because no surface renders either end.
	Driven there: the superseded document says only *superseded*, a dead end, and the one that
	replaced it does not mention it at all. That is `#1684`.

	**A link needed no renderer**, which is what this asserts. The same machinery that draws
	*Blocked by* draws both ends of this, so the reader of a dead item is told where to go and
	the successor says what it absorbed.

	**Both ends, because one end is where the column already fails.** Asserting only the
	successor would pass against a mechanism with exactly the defect this replaces.
	"""

	run("init")
	run("add", "The old way of doing it")
	run("add", "The milestone that absorbed it")

	run("link", "2", "supersedes", "1")

	replaced = run("show", "1").output
	successor = run("show", "2").output

	assert "Superseded by" in replaced, (
		f"the superseded item does not say what replaced it, so it is the dead end a status "
		f"alone already was:\n{replaced}"
	)
	assert "#2" in replaced, f"it says it was superseded and not by what:\n{replaced}"

	assert "Supersedes" in successor, f"the successor does not say what it absorbed:\n{successor}"
	assert "#1" in successor, f"it says it supersedes and not what:\n{successor}"


def test_the_redirect_is_the_first_relation_a_superseded_item_shows (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`SR#1685`'s category decision, asserted where it shows.**

	`#1157` §2's categories are nested by how much a relation binds and `#1535` orders an item's
	links by exactly that, so ``governing`` puts the redirect above the describing relations.
	On a dead item the line saying where the work went is the one worth reading first, and
	``describing`` — which is literally more accurate, since superseding gates nothing — would
	have printed it below everything else.

	**The comparison is against an *incoming* describing link, and the first version of this
	was not.** ``links.order`` sorts by settled, then category, then **incoming before
	outgoing**, then the label — so a redirect compared against *outgoing* describing links is
	put first by the direction key whatever its category is. Re-categorising the relation to
	``describing`` left that version green, which is a mutation passing and means the test was
	carried by a key it was not written about.

	``Duplicated by`` is the comparison because it is incoming and describing, and because it
	sorts **before** ``Superseded by`` on the label — so if the category stopped deciding, the
	two swap and this fails.
	"""

	run("init")
	run("add", "The old way of doing it")
	run("add", "The milestone that absorbed it")
	run("add", "Something else entirely")

	run("link", "2", "supersedes", "1")
	run("link", "3", "duplicates", "1")

	shown = run("show", "1").output
	order = [
		line for line in shown.splitlines()
		if any(word in line for word in ("Superseded by", "Duplicated by"))
	]

	assert len(order) == 2, f"both relations did not render:\n{shown}"
	assert "Superseded by" in order[0], (
		f"the redirect is not the first relation shown, so a reader of a dead item meets "
		f"everything else first:\n{shown}"
	)


def test_several_items_can_block_one_in_a_single_call (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1352`, the side its own example did not cover — and it is the one a plan needs.

	A milestone is an item whose blockers are its contents (`SR#84`), so laying one out is N
	things blocking **one**, which is the *first* position. There is no inverse verb — measured,
	`link 1 blocked-by 2` is refused with *no link type with key 'blocked_by'* — so before this
	a six-part milestone cost six commands.

	**Both sides at once is the cross product**, which is what *each of these blocks each of
	those* says and the only thing it could say. It is what an edge list on one call means,
	which is `SR#1352`'s own second suggestion.
	"""

	run("init")

	for title in ("ROADMAP", "PHASE 1", "PHASE 2", "PHASE 3"):
		run("add", title)

	made = run("link", "2,3,4", "blocks", "1")

	assert made.output.count("Blocks") == 3, made.output

	# **Both ends named once there is more than one source**, because `Blocks: ROADMAP` three
	# times says nothing about which of the three it came from.
	for ref in ("#2", "#3", "#4"):
		assert ref in made.output, made.output

	shown = run("show", "1").output

	assert shown.count("Blocked by") == 3, shown

	# **And a single source still reads as it always did**, which is the ordinary call.
	run("add", "Something else")

	assert run("link", "1", "relates-to", "5").output.startswith("Relates to:")


def test_a_bad_number_among_several_writes_none_of_them (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1352`. Nothing here spans a transaction, so the resolving happens first.

	Each link is its own call — its own HTTP request on a served instance — so a typo in the
	fourth of five would otherwise leave three made, one refused, and no statement of which.
	That is `project rename`'s precedent: count what will happen and name what will break
	before doing any of it.

	**The offending entry is named**, because *'2,nope,4' is not a list of item numbers* sends
	somebody to check all three.
	"""

	run("init")

	for title in ("Ship it", "Changelog", "Tag it"):
		run("add", title)

	refused = run("link", "1", "blocks", "2,nope,3", expect=1)

	assert "'nope'" in refused.output, refused.output
	assert "commas" in refused.output, "the separator is named, since that is what was wrong"

	assert "Blocks" not in run("show", "1").output, (
		"a link was written before the whole list had been read"
	)

	# **And a ref that parses but names nothing is caught in the same pass**, which is the
	# commoner mistake — a number copied from the wrong listing.
	missing = run("link", "1", "blocks", "2,999", expect=1)

	assert "999" in missing.output, missing.output
	assert "Blocks" not in run("show", "1").output, (
		"the readable half of the list was written before the unreadable half was checked"
	)


def test_a_single_number_and_an_address_reach_link_unchanged (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The plural form must accept everything the singular did — `SR#1352`.

	An argument with no comma in it is handed to the resolver **untouched**, so whatever it took
	before it still takes. Written that way rather than routed through the ref parser, which
	reads numbers: a single argument may not be one.

	**A trailing comma is tolerated**, because that is what a list copied out of prose or out of
	an editor's selection carries and it cannot mean anything else.
	"""

	run("init")

	for title in ("Ship it", "Changelog", "Tag it"):
		run("add", title)

	assert "Blocks:" in run("link", "1", "blocks", "2").output
	assert "Blocks:" in run("link", "1", "blocks", "3,").output

	shown = run("show", "1").output

	assert "Changelog" in shown and "Tag it" in shown, shown


def test_one_call_withdraws_a_link_to_each_of_several_items (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1352`'s other half, and the surfaces have to agree about it.

	The agent tool takes several either way round — it is one argument on one tool — so a
	terminal that took several to make and two to undo would be one product disagreeing with
	itself about the same pair of numbers.

	**Undoing a batch is when this is most needed**: a plan laid out the wrong way round is the
	case that produces forty links nobody wants, and one command per link to undo them is the
	friction SR#1290 measured arriving on the way back out.
	"""

	run("init")

	for title in ("Ship it", "Changelog", "Tag it", "Announce"):
		run("add", title)

	run("link", "1", "blocks", "2,3,4")

	undone = run("unlink", "1", "2,3")

	assert undone.output.count("Unlinked:") == 2, undone.output

	shown = run("show", "1").output

	assert "Announce" in shown, "the one not named was withdrawn too"
	assert "Changelog" not in shown and "Tag it" not in shown, shown


def test_withdrawing_a_link_that_is_not_there_leaves_the_others_alone (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1352`. The whole list is checked before any of it is withdrawn.

	**And every missing one is named at once.** Undoing a mistaken batch is exactly when more
	than one of them will already be gone, and a refusal that names the first is a command
	somebody runs five times to learn five things.
	"""

	run("init")

	for title in ("Ship it", "Changelog", "Tag it", "Announce"):
		run("add", title)

	run("link", "1", "blocks", "4")

	refused = run("unlink", "1", "2,3,4", expect=1)

	assert "#2" in refused.output and "#3" in refused.output, (
		f"only some of the missing ones were named:\n{refused.output}"
	)
	assert "Announce" in run("show", "1").output, (
		"the link that was there was withdrawn before the list had been read"
	)


def test_a_link_is_withdrawn_by_naming_the_two_items (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A link's id is a UUID that appears in no listing a person reads.

	Requiring one would make this a command only a script could run, and ``show`` prints the
	two refs — which is what somebody actually has in front of them.
	"""

	run("init")
	run("add", "Blocker")
	run("add", "Blocked")
	run("link", "1", "blocks", "2")

	assert "Unlinked: Blocked" in run("unlink", "1", "2").output
	assert "Build" not in run("show", "1").output
	assert "Blocked" in run("list", "--ready").output


def test_show_counts_the_blockers_that_are_done (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#210`, found by Simon reading ``subroutine show 85``.

	`#84` models a milestone as an item whose blockers are its contents, "presented
	GitHub-style as N of M" — and the count was never built. Every link printed identically,
	so an item with forty-eight *finished* blockers reported forty-eight outstanding ones: the
	thing somebody opens to ask whether a release is ready said the opposite of the truth.

	**Readiness was never wrong**, which is why nothing else caught it. ``--ready`` joins the
	blocker's ``completed_at`` and always had this right; the graph was correct and only the
	rendering was not. So this asserts the *rendering*, and asserts the two agree.
	"""

	run("init")
	run("add", "The milestone")
	run("add", "First part")
	run("add", "Second part")
	run("link", "2", "blocks", "1")
	run("link", "3", "blocks", "1")

	assert "Links  (0 of 2 blockers done)" in run("show", "1").output
	assert "The milestone" not in run("list", "--ready").output, "and it is not startable"

	run("done", "2")

	assert "Links  (1 of 2 blockers done)" in run("show", "1").output

	run("done", "3")

	shown = run("show", "1").output

	assert "Links  (2 of 2 blockers done)" in shown

	# **Still listed, not removed.** The contents of a finished milestone are what it was, and
	# a reader opening it wants to see them — the count is what says they are behind you.
	assert "First part" in shown
	assert "Second part" in shown

	assert "The milestone" in run("list", "--ready").output, "the two now agree"


def test_show_counts_only_blockers_and_not_every_link (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A `relates to` has nothing to be N of, and counting it would make the number a lie."""

	run("init")
	run("add", "The milestone")
	run("add", "A part")
	run("add", "Something similar")
	run("link", "2", "blocks", "1")
	run("link", "1", "relates-to", "3")

	shown = run("show", "1").output

	assert "Links  (0 of 1 blockers done)" in shown
	assert "Something similar" in shown, "the related item is still listed, just not counted"

	# **The other end of the same link gets no count at all.** #2 blocks #1, so from #2's own
	# side that link is something it holds up, not one of its contents — and a heading reading
	# "0 of 1" there would be counting the wrong item's work.
	from_the_blocker = run("show", "2").output

	assert "blockers done" not in from_the_blocker
	assert "Blocks" in from_the_blocker


def test_a_link_that_is_not_there_is_refused_without_naming_a_workspace (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4, in the place it is easiest to miss.

	A refusal is written when something has already gone wrong, so it is the last output
	anybody re-reads for stray vocabulary — and the four-command transcript cannot see it. The
	first version of this said "personal/#1 is not joined to personal/#2" at somebody with one
	workspace.
	"""

	run("init")
	run("add", "One")
	run("add", "Two")

	refused = run("unlink", "1", "2", expect=1).output

	assert "not joined" in refused

	for word in FORBIDDEN:
		assert word not in refused.lower(), refused


@pytest.mark.parametrize(
	("day", "expected"),
	[
		# Inside the window: the year earns nothing and is not printed.
		(datetime.date(2026, 8, 2), "Sun 2 Aug"),
		(datetime.date(2026, 12, 15), "Tue 15 Dec"),
		(datetime.date(2027, 5, 20), "Thu 20 May"),
		# A week overdue is still ordinary, and a year on it would be noise.
		(datetime.date(2026, 7, 24), "Fri 24 Jul"),
		# Outside it, in both directions.
		(datetime.date(2027, 11, 30), "Tue 30 Nov 2027"),
		(datetime.date(2020, 1, 5), "Sun 5 Jan 2020"),
		(datetime.date(2026, 6, 1), "Mon 1 Jun 2026"),
	],
)
def test_a_year_is_printed_only_when_a_bare_date_would_be_ambiguous (
	day: datetime.date, expected: str
) -> None:
	"""``#78``. ``%a %-d %b`` and never a year meant 2027 printed exactly as this November.

	``today`` is injected rather than taken from the clock, because the alternative is a test
	that passes for ten months of the year — written in July, failing in June, about a rule
	that had not changed.
	"""

	rendered = subroutine.cli.personal._dated(day, today=datetime.date(2026, 7, 31))

	assert rendered == expected


def test_the_window_is_narrower_than_a_year_so_a_bare_date_names_one_day () -> None:
	"""**The argument, not the constants.**

	Inside a window shorter than 365 days a rendering like "Tue 30 Nov" can only name one
	date; outside it, it names at least two. Widening it past a year would not be a friendlier
	default, it would be an ambiguous one — so this is what a later tidy-up has to preserve.
	"""

	span = (
		subroutine.cli.personal._A_BARE_DATE_READS_BACK
		+ subroutine.cli.personal._A_BARE_DATE_READS_FORWARD
	)

	assert span < datetime.timedelta(days=365), span


def test_a_deadline_more_than_a_year_away_says_which_year (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""End to end, because the renderer is reached through four different callers."""

	run("init")
	run("add", "Renew the passport by 2027-11-30")

	assert "2027" in run("list").output


def test_you_can_say_you_have_started_something_and_put_it_down_again (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#75``. A person could finish work and put work off and never say they were doing it.

	``in_progress`` was a seeded status reachable only over HTTP — so the one state that
	answers "what am I in the middle of" was the one a person could not set.
	"""

	run("init")
	run("add", "Write the report")

	assert "Started: Write the report" in run("start", "1").output
	assert "Stopped: Write the report" in run("stop", "1").output


def test_starting_and_stopping_survive_a_renamed_status (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1128`. Both sent a status key as a literal, twenty lines from `done`, which resolves.

	**Reaching past the CLI to rename, because nothing can rename a status on any surface yet**
	— that is `#826`, and it is the reason this defect is latent rather than live. It stops
	being latent the day `#826` lands, which is why it is worth a guard before then.

	A status key is data an installation owns (§5.5); the *category* beside it is fixed and is
	what a caller branches on. `done` has never had to care, because it goes through a verb
	route and the server resolves the category.
	"""

	import sqlalchemy
	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.models.vocabulary
	import subroutine.db.session

	run("init")
	run("add", "Write the report")

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			model = subroutine.db.models.vocabulary.Status
			renamed = {"open": "todo_now", "in_progress": "doing"}

			for was, now in renamed.items():
				session.execute(
					sqlalchemy.update(model)
					.where(model.entity_type == "task", model.key == was)
					.values(key=now)
				)

			session.commit()

	finally:
		engine.dispose()

	# Red before the fix: `stop` refused with "There is no task status called 'open' here."
	assert "Started: Write the report" in run("start", "1").output
	assert "Stopped: Write the report" in run("stop", "1").output

	# **And the positive half**, because "did not refuse" is also what a command that silently
	# did nothing produces. The item has to land in a status of the right *category*, under
	# whatever name this workspace now uses for it — `stop` in `todo`, `start` in `in_progress`.
	assert '"status": "todo_now"' in run("show", "1", "--json").output

	run("start", "1")

	assert '"status": "doing"' in run("show", "1", "--json").output


def test_a_workflow_with_nowhere_to_start_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The refusal `#1128` added, driven — because a refusal nothing exercises is the family.

	Reachable the day `#826` lets a workspace delete a status. The message names the *part of
	the workflow* rather than a key, because the reader chose the names and a key this command
	invented would tell them nothing.
	"""

	import sqlalchemy
	import sqlalchemy.orm

	import subroutine.config
	import subroutine.db.models.vocabulary
	import subroutine.db.session

	run("init")
	run("add", "Write the report")

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			model = subroutine.db.models.vocabulary.Status

			# Safe to remove: nothing is in it, which is why this is the one category a
			# workspace could empty without breaking a foreign key.
			session.execute(
				sqlalchemy.delete(model).where(
					model.entity_type == "task", model.category == "in_progress"
				)
			)
			session.commit()

	finally:
		engine.dispose()

	refused = run("start", "1", expect=1)

	assert "nothing to start" in refused.output
	assert "in_progress" in refused.output, "the hint names the part of the workflow"

	# Stopping still works, because its category is untouched — so the refusal is about this
	# workspace's vocabulary rather than about the command being broken.
	assert "Stopped" in run("stop", "1").output


def test_starting_something_is_visible_in_the_list (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**A `start` whose effect is invisible is half a feature.**

	Adding a way to set the status without a way to see it would have moved the gap rather
	than closed it.
	"""

	run("init")
	run("add", "Write the report")
	run("add", "Buy milk")

	assert "doing" not in run("list").output

	run("start", "1")

	listed = run("list").output

	assert "doing" in listed
	assert listed.count("doing") == 1, listed


def test_an_ordinary_list_has_no_column_for_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The same earn-its-place rule the kind, priority and parent columns follow (§14.10).

	A to-do list that annotates every line with an empty cell is one that looks like a
	database, which is the §1.4 leak this whole surface avoids.
	"""

	run("init")
	run("add", "Buy milk")

	before = run("list").output

	run("start", "1")
	run("stop", "1")

	assert run("list").output == before


def test_starting_and_stopping_say_nothing_about_a_status (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§13.5b, and they do not need the word: these are actions that happen to set a field.

	"Started: <title>" is the same shape as "Done: <title>", which is the point.
	"""

	run("init")
	run("add", "Write the report")

	transcript = run("start", "1").output + run("list").output + run("stop", "1").output

	for word in FORBIDDEN:
		assert word not in transcript.lower(), transcript


def test_starting_something_already_finished_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Picking up something already ticked off is nearly always the wrong number."""

	run("init")
	run("add", "Buy milk")
	run("done", "1")

	assert "Already done" in run("start", "1").output


def test_a_refusal_from_start_still_refuses (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**The guard on a name collision `mypy --strict` caught and nothing else would.**

	`stop` is the refusal helper the whole command module is handed, and `def stop` inside that
	scope rebinds it — so every refusal in every command would have called the *command*
	instead. Nothing at runtime would have noticed, because the paths that refuse are the ones
	nobody exercises on a good day.
	"""

	run("init")

	refused = run("start", "99", expect=1)

	assert "no #99" in refused.output


def test_new_work_goes_to_the_project_this_directory_names (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#159`, and the reason "it just works" was not yet true.

	§21.5's adoption procedure creates a project per repository, so an instance that has been
	adopted a few times has many — and until this existed the agent guessed which one from the
	directory name. A guess that is usually right is the worst kind: it misfiles rarely enough
	that nobody is watching.
	"""

	run("init")
	run("project", "create", "web", "Website")

	checkout = tmp_path / "checkout" / "src" / "nav"
	checkout.mkdir(parents=True)

	monkeypatch.chdir(checkout.parent.parent)
	run("use", "--here", "--project", "web")

	# From three directories down, exactly as somebody running a command mid-edit would be.
	monkeypatch.chdir(checkout)

	added = run("add", "Fix the nav")

	assert "in web" in added.output, "a default nobody typed must be said out loud"
	assert "web" in run("show", "1").output


def test_a_project_in_the_line_beats_the_one_in_the_file (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Somebody being explicit about this item beats a file they may not know is there.

	This is the half that keeps the marker from being a trap. A default that could not be
	overridden on the spot would make every `add` in a checkout a decision about the checkout.
	"""

	run("init")
	run("project", "create", "web", "Website")
	run("project", "create", "ops", "Operations")

	monkeypatch.chdir(tmp_path)
	run("use", "--here", "--project", "web")

	added = run("add", "Rotate the certificates +ops")
	shown = run("show", "1").output

	assert "ops" in shown
	assert "web" not in shown

	# **And it does not claim the marker filed it**, which is a separate guard from the one
	# above: the client enforces the rule and the surface reports it, so a message that said
	# "in web" over a task in OPS would be true of nothing and caught by neither.
	assert "in web" not in added.output


def test_work_outside_the_checkout_is_unaffected (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""The property that makes this safe to have: losing it changes nothing already recorded.

	A personal to-do list must not start filing the dentist into a work project because the
	terminal happened to be in a repository — and §1.4 would be broken outright if it did.
	"""

	run("init")
	run("project", "create", "web", "Website")

	inside = tmp_path / "checkout"
	outside = tmp_path / "elsewhere"
	inside.mkdir()
	outside.mkdir()

	monkeypatch.chdir(inside)
	run("use", "--here", "--project", "web")

	monkeypatch.chdir(outside)

	added = run("add", "Call the dentist")

	assert "in web" not in added.output
	assert "web" not in run("show", "1").output


def test_a_marker_naming_a_project_that_does_not_exist_is_refused_when_written (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Checked here, because the alternative is failing on the *next* capture.

	The person who would then have to work out why is not the one who typed this, and the
	message they would get would be about a task rather than about a file.
	"""

	run("init")

	monkeypatch.chdir(tmp_path)

	refused = run("use", "--here", "--project", "nope", expect=1)

	assert "no project" in refused.output.lower()
	assert not (tmp_path / subroutine.directory.FILE_NAME).exists(), (
		"nothing may be written when the thing it names does not exist"
	)


def test_use_reports_the_marker_when_asked_where_it_is (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`subroutine use` is what somebody runs when asking "why is my work going there".

	Reporting the marker only where it acts would answer that question everywhere except
	where it is asked.
	"""

	run("init")
	run("project", "create", "web", "Website")

	monkeypatch.chdir(tmp_path)
	run("use", "--here", "--project", "web")

	assert "web" in run("use").output


def test_project_without_here_is_refused_rather_than_ignored (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A project belongs to a directory, not to the whole machine.

	Silently ignoring the flag would leave somebody believing they had set a default, and
	they would find out from a task filed in the wrong place a week later.
	"""

	run("init")

	refused = run("use", "--project", "web", expect=1)

	assert "--here" in refused.output


def test_a_marker_naming_an_unknown_workspace_is_ignored_not_fatal (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#166`. The one thing a marker must not do is break the program.

	It is advisory context written by a machine into a directory, so a checkout marked for one
	instance must not stop every command working against another — and `--profile` puts a
	second instance one flag away. This was found by *this suite*: the day the project's own
	repository started carrying a marker, 154 tests failed at once.

	It also contradicted the property §13.7a claims for the feature — "losing it costs a
	question, never a different outcome". Having one cost a hard failure.
	"""

	run("init")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	subroutine.directory.write(checkout, workspace="somewhere-else", project="nope")

	monkeypatch.chdir(checkout)

	# Still works, and says why it is not doing what the file asked.
	listed = run("list")

	assert listed.exit_code == 0
	assert "somewhere-else" in listed.output
	assert "Ignoring it" in listed.output

	# And the commands that resolve an item — which is where it used to refuse outright.
	added = run("add", "Still possible")

	assert added.exit_code == 0
	assert run("show", "1").exit_code == 0


def test_a_project_can_be_renamed_and_nothing_recorded_moves (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#176`. The reason given for refusing this had been false for three days.

	Four surfaces said a key is "the first half of every ref the project mints" — §6.2 made a
	ref a bare workspace-scoped integer on 2026-07-29, so a project key is in no ref at all.
	The rule outlived its own reasoning, in four places, checked by nothing.

	The property that matters is the one asserted last: every item keeps its number, because a
	number belongs to the workspace and not to the project.
	"""

	run("init")
	run("project", "create", "st", "Subtask")
	run("add", "Something +st")
	run("add", "Another +st")

	renamed = run("project", "rename", "st", "SR", "--yes")

	assert "sr" in renamed.output

	listed = run("list", "--project", "SR").output

	assert "#1" in listed
	assert "#2" in listed


def test_a_rename_says_what_will_stop_working_before_it_does_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Simon's decision: the old key just stops working, so this has to be said out loud.

	"This will break addresses" is abstract. The count and the three concrete things — the
	address, the capture token, the marker file — are what somebody can actually weigh, which
	is why the project is read before the rename rather than after.
	"""

	run("init")
	run("project", "create", "st", "Subtask")
	run("add", "Something +st")

	refused = run("project", "rename", "st", "SR", expect=1, input="n\n")

	assert "1 item" in refused.output
	assert "+st" in refused.output
	assert "Nothing was renamed" in refused.output

	# And declining really declined.
	assert "st" in run("project", "list").output


def test_a_checkout_still_finds_its_project_after_a_rename (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`#177`, and `#176` without it would have been a data-loss bug shipped on purpose.

	The day somebody renames a project, every `.subroutine` on every machine names a key that
	no longer resolves — and new work falls back to the Inbox. That fallback is right for a
	*deleted* marker and quite wrong for one still sitting there naming something: a file that
	says `ST` and is ignored is worse than no file.
	"""

	import os

	run("init")
	run("project", "create", "st", "Subtask")

	os.chdir(tmp_path)
	run("use", "--here", "--project", "st")
	run("project", "rename", "st", "SR", "--yes")

	added = run("add", "After the rename")

	# It landed in the renamed project, and the file explained itself on the way.
	assert "sr" in added.output
	assert "still says 'st'" in added.output

	assert "After the rename" in run("list", "--project", "SR").output


def test_a_marker_holding_an_old_spelling_of_a_current_key_says_so (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""`#554`. The drift check normalised both sides, so a case difference matched silently.

	`#508` changed the *stored* form of a key to lower case. Every marker written before it
	holds a spelling this program no longer stores, prints or writes anywhere — so the file on
	disk states something no other surface agrees with, and the one mechanism built to notice
	that could not see it, because matching is what it compared. Met on a real instance and
	read as the rename having half-failed.

	Resolution stays case-insensitive, which is what keeps those markers working at all. Only
	the question *does this file agree with us* is exact.
	"""

	run("init")
	run("project", "create", "st", "Subtask")

	os.chdir(tmp_path)
	run("use", "--here", "--project", "st")

	# Exactly what an older program left behind: the id is right and the key beside it is
	# written the way keys used to be stored. Rewritten rather than hand-built, so every other
	# field stays whatever `use --here` really writes.
	marker = tmp_path / subroutine.directory.FILE_NAME
	marker.write_text(
		marker.read_text(encoding="utf-8").replace('project = "st"', 'project = "ST"'),
		encoding="utf-8",
	)

	added = run("add", "Filed from an old checkout")

	assert "'ST'" in added.output, (
		f"the stale spelling is not reported, so the file goes on disagreeing: {added.output}"
	)

	assert "'st'" in added.output, (
		f"and it does not say what the key actually is, which is the half that is actionable: "
		f"{added.output}"
	)

	# It still resolves, which is the half that must not change: those markers predate the
	# rule, and an upgrade that stops old checkouts working is the outage.
	assert "Filed from an old checkout" in run("list", "--project", "st").output

	# And it is not described as a rename. Nothing was renamed, and saying so about two
	# spellings of one key is what made this confusing when somebody met it.
	assert "is now" not in added.output, added.output


def test_a_marker_that_agrees_with_the_instance_says_nothing (
	run: typing.Callable[..., typer.testing.Result], tmp_path: pathlib.Path
) -> None:
	"""The other side of `#554`, and the reason the comparison could not simply be tightened.

	`use --here` normalises what it writes, so a marker this program produced always agrees.
	A check that fired anyway would put a warning under every capture in every checkout, and a
	warning that appears when nothing is wrong stops being read — which would cost more than
	the silence it replaced.
	"""

	run("init")
	run("project", "create", "st", "Subtask")

	os.chdir(tmp_path)

	# Typed in capitals, deliberately: input is case-insensitive and always was (`#508`), so
	# what lands in the file is the stored form rather than what was typed.
	run("use", "--here", "--project", "ST")

	added = run("add", "Filed from a current checkout")

	# The marker really was consulted, which is what stops the rest of this passing vacuously:
	# one that had not been found would produce no warning either, for the wrong reason.
	assert f"from {subroutine.directory.FILE_NAME}" in added.output, added.output

	assert "use --here" not in added.output, (
		f"a marker this program wrote is reported as disagreeing with it: {added.output}"
	)


def test_a_type_can_be_given_when_an_item_is_captured (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#178`, found by tripping over it while filing `#176`.

	`POST /v1/tasks` and `subroutine_add` have accepted a type since they were written; the
	CLI did not, so a person filed everything as a task and corrected it with a second command
	— which is the asymmetry §13.7 and decision `#146` exist to prevent.

	A flag rather than a sigil, for the reason `client.capture` already gives: §6.13's sigils
	are for what somebody types mid-sentence, and "this is a bug" is a classification about the
	sentence rather than part of it.
	"""

	run("init")
	run("add", "Dates render as if this year", "--type", "bug")

	assert "bug" in run("show", "1").output

	# The capture line is untouched by this — the type is not a word in the title.
	assert "Dates render as if this year" in run("list").output


def test_a_type_that_does_not_exist_is_refused_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The refusal is the domain's, so a person and an agent are told the same thing."""

	run("init")

	refused = run("add", "Something", "--type", "banana", expect=1)

	assert "banana" in refused.output


def test_changes_withholds_what_was_only_just_written (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#253`. An empty feed is the first thing somebody meets, so it says so in words.

	**The watermark is set rather than raced** (`#404`). This test used to run `init`, `add`
	and `changes` and assert the feed was empty, which is true only when all three land inside
	one second. It does on a developer's machine. On a loaded CI runner the `init` events fall
	the other side of the boundary and are reported — so the test failed on Python 3.11 and
	passed on 3.12 and 3.13 in the same run, having measured the runner rather than the feed.

	Widening the watermark to an hour makes "everything here is too recent to report" true by
	construction instead of by luck.
	"""

	monkeypatch.setattr(
		subroutine.domain.events, "WATERMARK", datetime.timedelta(hours=1)
	)

	run("init")
	run("add", "Call the dentist before Sunday")

	fresh = run("changes")

	# Withheld — and it says so rather than printing an empty screen.
	assert "Nothing new." in fresh.output


def test_changes_names_what_moved_and_how_to_carry_on (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The feed as a person actually reads it — the coverage `#404` found missing.

	Two things, and the old test's docstring claimed both while asserting neither. That an
	item is named **by its ref and title**, because the view carries those so that no client
	has to resolve ids and a feed of UUIDs is one nobody reads twice. And that the **resume
	number** is printed, because a feed you cannot carry on from is one you read from the
	beginning every time.

	Neither was reachable while the watermark was being waited out rather than set: everything
	a test writes is by definition too recent to appear.
	"""

	monkeypatch.setattr(
		subroutine.domain.events, "WATERMARK", datetime.timedelta(0)
	)

	run("init")
	run("add", "Call the dentist before Sunday")

	moved = run("changes").output

	assert "#1" in moved, "named by its ref"
	assert "Call the dentist" in moved, "and by its title"
	assert "subroutine changes --since" in moved, "and how to carry on from here"


def test_changes_refuses_a_resume_number_it_cannot_honour (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A number belongs to one instance, so ``--since`` needs to know which one.

	With a single connection it is unambiguous and this passes straight through; the refusal
	only appears once a second connection is configured. Asserted here as the *accepting* half,
	so that a later change making it refuse unconditionally is caught.
	"""

	run("init")

	assert run("changes", "--since", "1").exit_code == 0


def test_init_says_what_to_do_when_it_cannot_write_its_directories (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#255`. Simon met this as a traceback, following ``docs/hosting.md`` on a clean server.

	``/var/lib/subroutine`` is created by ``StateDirectory=`` when the service first starts,
	and the service cannot start until ``init`` has run — so on the manual first run the
	directory is absent and ``/var/lib`` is root-owned. What came back was four frames of
	``pathlib.mkdir`` recursion and a bare ``PermissionError``.

	**The guard for it already existed and covered the database directory only**, reached
	through ``settings.sqlite_path``, which is ``None`` on PostgreSQL. So this is written
	against the *configuration* directory specifically: that is the one nothing checked, and
	the one ``ensure_secret_key`` walks into a moment later.

	It names the outermost missing part rather than the leaf, because that is the directory
	somebody can actually create.
	"""

	# **Only the configuration directory is unwritable**, and the other two are fine. Locking
	# all three would let the *data* check fire first — which the old code already had — and
	# the test would pass against the very defect it was written for. That is what the first
	# version of this did.
	locked = tmp_path / "var"
	locked.mkdir()

	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
	monkeypatch.setenv("XDG_CONFIG_HOME", str(locked / "subroutine" / "config"))

	locked.chmod(0o500)

	try:

		result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["init"])

		assert result.exit_code == 1

		# **Asserted on the exception, not on the output.** `CliRunner` captures what was
		# raised rather than letting Typer render it, so "Traceback" and "PermissionError"
		# are absent from `result.output` whether or not the bug is present — two assertions
		# that read like the point of the test and could never fail. What a person actually
		# meets is the rendering of *this*.
		assert not isinstance(result.exception, OSError), result.exception

		assert "Cannot create the configuration directory" in result.output

		# The part somebody can act on, four levels above the one that failed.
		assert f"{locked / 'subroutine'} does not exist" in result.output
		assert "Make it as root" in result.output

	finally:
		# Restored whatever happened, or pytest cannot clean the directory up afterwards.
		locked.chmod(0o700)


def test_init_says_when_the_database_it_used_is_recorded_nowhere (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#265`. The chicken-and-egg that left Simon's service restarting every five seconds.

	`config.toml` does not exist until `init` has run, so a fresh PostgreSQL installation names
	the database in the environment for that one run. It works — and `init` writes only
	`secret_key`, so nothing records where the data went. The variable dies with the shell, the
	unit sets only the XDG paths, and the service comes up configured for SQLite.

	**Writing it to `config.toml` is the wrong fix and is not what this asserts.** A PostgreSQL
	URL routinely carries a password, and a password belongs with the tokens rather than beside
	the connection settings. The value cannot be written for the operator; the operator can be
	told they must write it.

	This docstring used to give the reason as "§12.3a is that this file holds no secrets", which
	is false — it is 0600 and holds `secret_key`, as the sentence three lines above says (`#831`).
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	elsewhere = tmp_path / "elsewhere.db"
	monkeypatch.setenv("SUBROUTINE_DATABASE_URL", f"sqlite:///{elsewhere}")

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["init"])

	assert result.exit_code == 0, result.output
	assert "Ready." in result.output

	assert "came from the environment" in result.output
	assert "will look somewhere else" in result.output
	assert str(subroutine.config.config_file_path()) in result.output


def test_init_is_quiet_when_the_database_is_the_configured_one (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the warning stays out of the ordinary path — §13.5b's first line is one line.

	Without this the test above would pass just as well against a version that warned every
	time, which is the same as not warning at all.
	"""

	assert run("init").output.strip() == 'Ready. Try: subroutine add "something to do"'


def test_a_missing_database_says_which_one_and_why_it_looked_there (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#264`. Simon's service said "run init first" while a populated database sat beside it.

	`has_no_instance_yet` can only answer for SQLite, so the refusal fires **exactly when the
	configuration says SQLite** — which, for somebody who set PostgreSQL up and never recorded
	it, is the whole diagnosis and was the one fact the message withheld. It advised re-running
	the command that had already succeeded.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["serve"])

	assert result.exit_code == 1

	# The database it actually looked at, rather than "here".
	assert "subroutine.db" in result.output

	# And that nobody chose it, which is what separates "never set up" from "misconfigured".
	assert "Nothing has configured 'database_url'" in result.output


def test_init_says_when_it_is_about_to_build_a_second_instance (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#267`. The quiet failure behind the loud one, and the worse of the two.

	Set up on PostgreSQL with the URL in the environment, then run `init` again without it —
	which is what somebody does when a service says "run `subroutine init` first". It reported
	`Ready.`, `db current` reported a healthy schema and `list` reported an empty backlog:
	three confident answers about a database nobody wanted, with the first instance untouched
	and unreachable. The shape of that, to the person it happens to, is "my data has gone".

	The signal is exact rather than heuristic: `config.toml` carries a `secret_key`, which only
	`init` writes, **and** the database it is looking at now is absent — so the earlier run
	used a different one.

	A warning rather than a refusal, because `ensure_secret_key` runs before the database is
	prepared: an `init` that failed part-way leaves a key and no database, and that retry must
	still work. The test below holds that open.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	runner = typer.testing.CliRunner()
	elsewhere = tmp_path / "elsewhere.db"

	monkeypatch.setenv("SUBROUTINE_DATABASE_URL", f"sqlite:///{elsewhere}")

	assert runner.invoke(subroutine.cli.main.app, ["init"]).exit_code == 0
	assert elsewhere.exists()

	# The variable goes with the shell, exactly as it does for a service.
	monkeypatch.delenv("SUBROUTINE_DATABASE_URL")

	again = runner.invoke(subroutine.cli.main.app, ["init"])

	assert again.exit_code == 0
	assert "has run here before" in again.output
	assert "a second, empty one" in again.output

	# Conditional, so that it stays true for the retry case the test below covers.
	assert "If you set an instance up earlier" in again.output

	# Before the work, not after it — a warning under `Ready.` reads as a footnote to success.
	assert again.output.index("has run here before") < again.output.index("Ready.")


def test_init_can_still_be_retried_after_it_failed_part_way (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The flow the warning above must not have become a refusal.

	`ensure_secret_key` writes before `migrate.upgrade` runs, so an `init` that could not reach
	its database leaves a signing key behind and no database at all — which is the same two
	facts the warning fires on. **So the warning fires here too**, and it must still succeed.

	That is why the message is phrased as a condition rather than as a finding: "if you set an
	instance up earlier and it is not there". Written as an assertion it would be false exactly
	when somebody is already recovering from a failure, which is the worst moment to be told
	something untrue about their data.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	runner = typer.testing.CliRunner()

	# A key on disk with nothing else, which is what a part-way failure leaves.
	subroutine.config.ensure_secret_key(subroutine.config.load_settings())

	result = runner.invoke(subroutine.cli.main.app, ["init"])

	assert result.exit_code == 0
	assert "Ready." in result.output


def test_project_move_counts_the_whole_subtree_before_asking (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#246`. The count is the reason this is not a one-liner, so it has to be right.

	`project rename` set the pattern: "this will break addresses" is abstract, and "137 items
	keep their numbers" is something somebody can weigh. The first version of this asked only
	about the named project and reported one item while two were moving — a count that
	undercounts is worse than none, because it is the number somebody says yes to.
	"""

	run("init")
	run("project", "create", "acme", "Acme")
	run("project", "create", "web", "Website")
	run("project", "create", "api", "The API", "--parent", "web")
	run("add", "Fix the nav +web")
	run("add", "Rate limit +api")

	asked = run("project", "move", "web", "--under", "acme", input="n\n", expect=1)

	assert "2 projects move, and 2 items go with them" in asked.output
	assert "Nothing was moved." in asked.output

	# Declining left the tree alone, which is what makes the question a question.
	assert "  web" not in run("project", "list").output


def test_project_move_refuses_to_guess_a_direction (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Neither `--under` nor `--root`, or both, is a refusal rather than a default.

	This is the one project command with no undo, and an omitted destination once meant "move
	to root" — which flattened whole subtrees by accident. The endpoint refuses the same way
	for the same reason; this is the CLI half of that decision.
	"""

	run("init")
	run("project", "create", "web", "Website")

	for arguments in (("project", "move", "web"), ("project", "move", "web", "--root", "--under", "acme")):
		refused = run(*arguments, expect=1)

		assert "Say where to move it." in refused.output


def test_list_sends_a_search_to_the_command_that_searches (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#282``, reported by a Claude Code agent that had learned the MCP surface first.

	It tried ``-q``, ``--search`` and a bare argument, and got three different refusals
	naming neither ``search`` nor each other — and Click's did-you-mean made the middle one
	worse by offering ``--strict``, so the one message that tried to help pointed away from
	the answer. §12.2a: a dead end where a signpost would do.

	Parameterised over all three shapes on purpose. A test covering only the bare argument
	would have passed while ``-q`` still produced "No such option".
	"""

	run("init")
	run("add", "Call the dentist")

	for attempt in (["-q", "dentist"], ["--search", "dentist"], ["dentist"]):
		refused = run("list", *attempt, expect=1)

		assert "subroutine search" in refused.output, f"{attempt} gave: {refused.output!r}"
		assert "dentist" in refused.output, "the refusal repeats what to search for"


def test_the_listing_still_lists_and_its_help_offers_no_words (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The guard on the catcher, which must not become a feature.

	Click renders a positional in the usage line whether or not it is ``hidden``, so the
	interception could easily advertise the thing it refuses — trading one confusion for a
	worse one. And an over-eager catcher would break the ordinary listing entirely.
	"""

	run("init")
	run("add", "Call the dentist")

	assert "Call the dentist" in run("list").output
	assert "Call the dentist" in run("ls").output

	usage = run("list", "--help").output.splitlines()[1]

	assert "[OPTIONS]" in usage
	assert "words" not in usage and "[]" not in usage, f"the usage line offers: {usage!r}"


def _exploding () -> typing.Callable[[], None]:
	"""Return a stand-in for the Typer app that fails the way nothing anticipated."""

	def app () -> None:
		"""Fail."""

		raise RuntimeError("a defect nobody anticipated")

	return app


def test_a_defect_reaches_a_person_as_a_sentence_and_a_file (
	home: pathlib.Path,
	capsys: pytest.CaptureFixture[str],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""``#258``. Typer rendered any unhandled exception as boxed source with a caret.

	§1.4's reader is setting up a to-do list and can act on none of that; §13.5 says an error
	says what to do next. Found as the general case behind `#255`, where only the specific
	path was fixed — the class stayed, so every unhandled ``OSError`` and every genuine
	defect still arrived as a stack.

	**Driven through ``main`` rather than ``CliRunner``, and that is the point.** The runner
	captures what a command raised, so ``result.output`` is empty on a crash and an assertion
	about what a person sees passes against the broken code. That trap has caught this
	project once already and is recorded; this calls the function the console script calls.
	"""

	monkeypatch.setattr(subroutine.cli.main, "app", _exploding())

	with pytest.raises(SystemExit) as ended:
		subroutine.cli.main.main()

	assert ended.value.code == 1

	said = capsys.readouterr().err

	assert "Something went wrong" in said
	assert "Traceback" not in said, "a person is not shown a stack"
	assert subroutine.ISSUES_URL in said

	# **The stack is kept rather than discarded**, which is what makes a report worth asking
	# for — and the sentence names the file, because a report nobody can find is not one.
	written = sorted(
		(subroutine.config.state_home() / subroutine.cli.main.CRASH_DIRECTORY).glob("*.txt")
	)

	assert len(written) == 1

	report = written[0].read_text(encoding="utf-8")

	assert "a defect nobody anticipated" in report
	assert "Traceback" in report
	assert str(written[0]) in said


def test_a_defect_is_still_reported_when_the_report_cannot_be_written (
	home: pathlib.Path,
	capsys: pytest.CaptureFixture[str],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""**A crash handler that crashes replaces a bad message with a worse one.**

	An unwritable state directory is `#255`'s exact condition, and the moment somebody most
	needs a sentence — so a report that cannot be written has to degrade to printing the
	trace, never to a second exception on top of the first.
	"""

	monkeypatch.setattr(subroutine.cli.main, "app", _exploding())
	monkeypatch.setattr(
		subroutine.cli.main, "_crash_report", lambda exception: None
	)

	with pytest.raises(SystemExit) as ended:
		subroutine.cli.main.main()

	assert ended.value.code == 1

	said = capsys.readouterr().err

	assert "Something went wrong" in said

	# With nowhere to keep it, the trace itself is the only copy there will ever be.
	assert "a defect nobody anticipated" in said


def test_a_crash_report_never_carries_a_password_or_a_token (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""**A crash report is a file people are asked to send.**

	The same hazard ``safe_url`` exists for one surface along, and `#189` is this project's
	recorded instance of it going wrong: verifying a document's quoted output put real tokens
	into two published pages. ``db copy --to`` takes a password routinely, and a token on a
	command line is not supposed to happen (§7.4) but must be safe when it does.
	"""

	monkeypatch.setattr(
		sys,
		"argv",
		[
			"subroutine",
			"db",
			"copy",
			"--to",
			"postgresql+psycopg://si:hunter2@db.example.com/subroutine",
			"sr_abc123_deadbeefdeadbeef",
		],
	)

	masked = " ".join(subroutine.cli.main._masked_arguments())

	assert "hunter2" not in masked
	assert "deadbeef" not in masked

	# Masked, not dropped: which command was run is the whole value of recording it, and a
	# host that is not a secret is often the thing that explains the failure.
	assert "db copy" in masked
	assert "db.example.com" in masked


def test_a_document_can_be_revised_from_the_command_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#291``. ``doc create`` wrote one and nothing edited one.

	The gap cost something the hour it was found: a migration runbook written as a document
	changed twice while it was being agreed, and neither change could be folded in — leaving
	a choice between a comment saying "step 5 is different", which is the two-copies problem
	the project exists to avoid, and a runbook wrong in a way somebody discovers while
	following it.
	"""

	run("init")
	run("doc", "create", "What we settled", "--body", "First thoughts.", "--type", "decision")

	run("doc", "edit", "1", "--title", "What we settled, and why")

	shown = run("show", "1").output

	assert "What we settled, and why" in shown
	assert "First thoughts." in shown, "a title change must not touch the body"


def test_a_revision_that_would_lose_somebody_elses_paragraphs_is_refused (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#842`, §8.9, and the stakes are what make it a document rather than a task.

	**`doc edit` is a whole-body replace**, so a lost update here does not take a field — it
	takes every paragraph the other writer added, and leaves no record that they existed. The
	browser has sent `expected_version` on a revision since `SR#761` and argued in its own
	comment that *it matters more here than on a task*; the terminal was the surface that never
	did, and an agent was the second.

	**The version this sends is one the command genuinely showed somebody.** It is read
	seconds or minutes before the write, and on the editor path it is the id of the exact text
	they have been editing — so this is not §8.9 being turned on for a version nobody saw.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "First thoughts.")

	# **The other writer arrives *inside* the editor session, which is the only window there
	# is.** `doc edit --body` reads and writes in the same breath, so a second invocation
	# after the first has finished is not a lost update at all — it is two writes in order,
	# and a test built that way passes against unguarded code. What makes this the real thing
	# is that the interleaving happens between the read and the write of one invocation.
	#
	# **The editor is stubbed here and is a real `sed` subprocess two tests below.** That one
	# owns the round trip — text written out, result read back; this one owns the ordering,
	# and a real editor cannot be made to revise a document halfway through.
	terminal = _NoInput()
	terminal.stdin = _ATerminal()
	monkeypatch.setattr(subroutine.cli.personal, "sys", terminal)

	def meanwhile (_program: typing.Any, _text: str) -> str:
		"""Stand in for the editor, and let somebody else save first.

		``--body`` rather than a pipe for the inner write: the outer command has already been
		given a terminal for stdin, and reaching for one here would be the harness arguing
		with itself rather than anything about the product.
		"""

		run("doc", "edit", "1", "--body", "Their careful paragraphs.")

		return "Mine, written from the old text."

	monkeypatch.setattr(subroutine.cli.personal, "_in_an_editor", meanwhile)

	stale = run("doc", "edit", "1", expect=1)

	assert "changed" in stale.output.lower() or "version" in stale.output.lower(), stale.output
	assert "Their careful paragraphs." in run("show", "1").output, (
		f"the other writer's text was replaced anyway:\n{run('show', '1').output}"
	)


def test_changing_a_documents_title_is_not_guarded_by_a_version (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half, and it is why the rule is *replacing the body* rather than *revising*.

	§8.9 is opt-in because `None` means *did not ask* and never *asked and passed*. Somebody
	running `doc edit 42 --title "…"` has not read the body, is not replacing it, and puts
	none of it at stake — refusing them for a change they did not make would be `SR#755`'s
	quick-status-control argument in reverse, and it would fire on the ordinary act of
	retitling something while a colleague is writing in it.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "First thoughts.")
	run("doc", "edit", "1", input="Somebody else's revision.\n")

	retitled = run("doc", "edit", "1", "--title", "What we settled, and why")

	assert retitled.exit_code == 0, retitled.output

	shown = run("show", "1").output

	assert "What we settled, and why" in shown
	assert "Somebody else's revision." in shown, "a title change must not touch the body"


def test_revising_a_document_reads_a_pipe_like_writing_one_does (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The same three sources ``doc create`` takes, in the same order.

	Piped input is how anybody writes more than a sentence at a terminal, and it is the path
	an agent takes — so ``edit`` reading it differently from ``create`` would be a surface
	disagreeing with itself, which is the family `#282` was.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "Before.")

	run("doc", "edit", "1", input="After, from a pipe.\n")

	assert "After, from a pipe." in run("show", "1").output


def test_revising_a_document_with_nothing_to_change_opens_an_editor (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""What makes it usable for a document of any length, which every one written so far is.

	The editor is a real subprocess on a real temporary file — ``sed`` standing in for one,
	so this asserts the round trip rather than that a function was called. A stub would pass
	against a version that never wrote the current text out or never read the result back,
	which are the two ways this goes wrong.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "Before.")

	monkeypatch.setenv("EDITOR", "sed -i 's/Before/After/'")
	monkeypatch.delenv("VISUAL", raising=False)

	# A terminal, because `CliRunner` supplies a stdin that is not one — and without a
	# terminal `doc edit` takes the pipe rather than the editor, which is the point of the
	# branch rather than an accident of the harness.
	terminal = _NoInput()
	terminal.stdin = _ATerminal()
	monkeypatch.setattr(subroutine.cli.personal, "sys", terminal)

	run("doc", "edit", "1")

	assert "After." in run("show", "1").output


def test_revising_a_document_says_what_to_do_when_no_editor_is_set (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A refusal that can be acted on, rather than a guess at ``vi``.

	Falling back to an editor nobody chose looks helpful until it is not installed, and the
	failure is then about a program the reader never asked for.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "Before.")

	monkeypatch.delenv("EDITOR", raising=False)
	monkeypatch.delenv("VISUAL", raising=False)

	terminal = _NoInput()
	terminal.stdin = _ATerminal()
	monkeypatch.setattr(subroutine.cli.personal, "sys", terminal)

	refused = run("doc", "edit", "1", expect=1)

	assert "editor" in refused.output
	assert "--body" in refused.output


def test_editing_a_task_by_number_says_it_is_a_task (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2c, from the other side. One counter serves both kinds, so ``doc edit 3`` may name
	a task — and "there is no document #3" about something sitting in the listing is the
	answer `#42` was raised to stop being given.
	"""

	run("init")
	run("add", "Call the dentist")

	refused = run("doc", "edit", "1", expect=1)

	assert "task" in refused.output.lower()
	assert "Call the dentist" in refused.output


def test_a_workspace_can_be_renamed_and_everything_keeps_its_number (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#295``. Simon challenged the prohibition and it did not survive being checked.

	The rename itself is the easy half — nothing in the database references a slug, so no ref,
	link or membership moves. The half worth testing is that the *claim* the confirmation
	makes is true: every item keeps its number.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Call the dentist")
	run("add", "Buy milk")

	run("workspace", "rename", "personal", "projects", "--yes")

	listed = run("-w", "projects", "list").output

	assert "Call the dentist" in listed
	assert "Buy milk" in listed
	assert "Call the dentist" in run("-w", "projects", "show", "1").output


def test_renaming_a_workspace_says_what_stops_working_before_it_does_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#176`'s argument, applied one segment earlier in an address.

	"This breaks addresses" is abstract where "this holds 2 items and these three things stop
	working" is something a person can weigh — which is the whole reason the command reads the
	workspace before renaming it rather than renaming and reporting.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Call the dentist")

	refused = run("workspace", "rename", "personal", "projects", input="n\n", expect=1)

	assert "1 item keeps its number" in refused.output, refused.output
	assert "'personal' stops working" in refused.output
	assert "Nothing was renamed." in refused.output

	# And it meant it — the old name still works.
	assert "Call the dentist" in run("-w", "personal", "list").output


def test_a_workspace_can_be_deleted_and_restored_from_a_terminal (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#704`. Its items go out of sight and come back with their numbers intact.

	The claim worth driving is the one the confirmation makes — *out of sight until it is
	restored* — rather than that two commands return zero.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "acme", "Acme")
	run("-w", "acme", "add", "Draft the proposal")

	run("workspace", "delete", "acme", "--yes")

	assert "Draft the proposal" not in run("-w", "personal", "list").output
	assert "acme" in run("workspace", "delete", "acme", input="n\n", expect=1).output

	run("workspace", "restore", "acme")

	assert "Draft the proposal" in run("-w", "acme", "list").output
	assert "Draft the proposal" in run("-w", "acme", "show", "1").output


def test_deleting_a_workspace_says_what_goes_with_it_before_it_does_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#176`'s argument again, and it has more to say here than a rename does.

	A rename keeps everything reachable; this does not. So the confirmation names the count,
	says the short name becomes free, and prints the command that undoes it — because the
	moment somebody is deciding whether to do something like this is the worst possible place
	to make them go and look up how to reverse it.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "acme", "Acme")
	run("-w", "acme", "add", "Draft the proposal")

	refused = run("workspace", "delete", "acme", input="n\n", expect=1)

	assert "1 item keeps its number, out of sight" in refused.output, refused.output
	assert "'acme' becomes free" in refused.output
	assert "subroutine workspace restore acme" in refused.output
	assert "Nothing was deleted." in refused.output

	# And it meant it.
	assert "Draft the proposal" in run("-w", "acme", "list").output


def test_the_only_workspace_cannot_be_deleted_from_a_terminal (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The refusal a person meets first, since one workspace is what ``init`` leaves."""

	run("init", "--workspace", "Personal")

	refused = run("workspace", "delete", "personal", "--yes", expect=1)

	assert "only workspace" in refused.output, refused.output
	assert "Create the workspace that replaces this one first" in refused.output


def test_a_document_can_be_filed_under_a_different_project (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#294`` from the command line, which is where it was needed.

	The gap stranded eleven decision documents in the Inbox — everything this project had
	concluded about itself — because a document's project could be set once and never again.
	"""

	run("init")
	run("project", "create", "docs", "Docs")
	run("doc", "create", "A conclusion", "--body", "Reasoning.")

	run("doc", "edit", "1", "--project", "docs")

	assert "docs" in run("show", "1").output


class _RefusesToBeRead:
	"""Standing in for input that will never arrive and never end."""

	def isatty (self) -> bool:
		"""Report that this is not a terminal, which is what makes reading it a trap."""

		return False

	def read (self) -> str:
		"""Fail loudly rather than blocking, which is what the real thing would do."""

		raise AssertionError("stdin was read when the caller had already said what it wanted")


class _ATerminal(_RefusesToBeRead):
	"""Standing in for a person at a keyboard, so the editor path is reachable in a test."""

	def isatty (self) -> bool:
		"""Report a terminal, which is what sends `doc edit` to the editor."""

		return True


class _NoInput:
	"""``sys``, with a standard input that cannot be read.

	**Patched over the module's own ``sys``, not over ``sys.stdin``.** ``CliRunner`` replaces
	``sys.stdin`` for the duration of an invocation, so a monkeypatch of the real one is
	discarded before the command runs — and a test written that way passes against the defect.
	This proxies everything else through, so nothing else in the command changes behaviour.
	"""

	stdin = _RefusesToBeRead()

	def __getattr__ (self, name: str) -> typing.Any:
		"""Defer to the real module for everything except standard input."""

		return getattr(sys, name)


def test_revising_a_document_does_not_read_stdin_when_it_was_told_what_to_write (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""``#299``. It hung: no terminal and nothing piped is a read that never returns.

	Every script, every CI job and every agent shelling out is in that position, and none of
	them know to send EOF. ``doc create`` short-circuits and carries a comment saying exactly
	why; `#291` copied the shape and lost the ``or``.

	**Asserted as "stdin is never touched", because the obvious test cannot fail.**
	``CliRunner`` supplies an EOF-able stdin, so ``read()`` returns immediately and a
	runner-based test passes against the defect — the same family as asserting on captured
	output. A subprocess with a pipe nobody closes would reproduce it faithfully and hang the
	suite the day it regressed, which is a worse trade than this.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "Before.")

	monkeypatch.setattr(subroutine.cli.personal, "sys", _NoInput())

	run("doc", "edit", "1", "--body", "After.")

	assert "After." in run("show", "1").output

	# The same for every other way of saying what you wanted, since each one takes the branch
	# that would otherwise fall through to the read.
	run("doc", "edit", "1", "--title", "A conclusion, restated")
	run("doc", "edit", "1", "--type", "decision")


def test_renaming_a_project_counts_past_a_page_and_agrees_with_itself (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#296``. The number is the whole point of the prompt, and it saturated at a page.

	`project rename` asked `client.tasks` with no limit and reported `len()`, so
	`default_page_size` capped it: renaming a project of 249 items promised that 50 kept their
	numbers. False in the direction that makes an irreversible operation look *smaller*, in the
	one sentence somebody reads while deciding to do it.

	Sixty items, over the default fifty, because a project smaller than a page cannot show
	this — which is why nothing caught it.
	"""

	run("init", "--workspace", "Personal")
	run("project", "create", "big", "Big")

	for index in range(60):
		run("add", f"item {index} +big")

	refused = run("project", "rename", "big", "HUGE", input="n\n", expect=1)

	assert "60 items keep their numbers" in refused.output, refused.output


def test_the_rename_prompt_agrees_when_there_is_one_item (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#296``'s other half: "1 item keep their numbers" pluralised the noun alone.

	Both rename commands print this sentence and each had its own copy, so the copies could
	disagree — and did, in opposite ways. One helper now, asserted on the command that had it
	wrong.
	"""

	run("init", "--workspace", "Personal")
	run("project", "create", "solo", "Solo")
	run("add", "the only one +solo")

	refused = run("project", "rename", "solo", "ONE", input="n\n", expect=1)

	assert "1 item keeps its number" in refused.output, refused.output
	assert "keep their numbers" not in refused.output


def test_show_caps_the_comments_and_says_how_many_there_are (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#37``, requested by Simon. A reader asking "what is this" should not get a transcript.

	The count is what makes the cap safe: a section that silently printed five of eight would
	be `#33`'s truncation-in-silence, which is the thing this project keeps finding wrong.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Something long-running")

	for index in range(8):
		run("comment", "1", f"comment number {index}")

	shown = run("show", "1").output

	assert "What happened (8, showing 5)" in shown, shown
	assert "comment number 7" in shown
	assert "comment number 2" not in shown


def test_show_prints_every_comment_when_there_are_few (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the ordinary item reads exactly as it did — no count, no cap, nothing to explain.

	Nothing in this instance has more than a handful, so this is the case that matters most
	and the one a change like `#37` most easily breaks.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Something ordinary")
	run("comment", "1", "the only thing that happened")

	shown = run("show", "1").output

	assert "the only thing that happened" in shown
	assert "showing" not in shown
	assert "What happened" in shown


def test_show_says_what_refers_to_an_item (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#144`. The `mention` table is written by every `#42` anybody writes and read by nothing.

	`domain/mentions.backlinks` had no caller and §8.5's ``?include=backlinks`` was honestly
	refused, so *what refers to this?* — the question the whole table exists for — was
	answerable on no surface. `#99`'s justification says a reason written as a comment gets its
	backlink for free, which was true of the data and invisible to every reader.

	**A comment resolves to the item it is on and says so.** A reader sent to #3 who cannot
	find the number in its own prose has been sent to the wrong half of it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "The specification")
	run("add", "Implements it", "--description", "As decided in #1.")
	run("add", "Something else")
	run("comment", "3", "This is the same question as #1.")

	shown = run("show", "1").output

	assert "Referred to by (2)" in shown, f"nothing says what refers to it: {shown}"
	assert "Implements it" in shown
	assert "in a comment" in shown, "a mention in a comment reads as one in the item's prose"


def test_show_says_nothing_about_references_where_there_are_none (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2c's rule that a field nobody set is not printed, applied to a whole heading.

	It is what lets `subroutine show` answer *buy milk* with a number, a title and nothing
	else — and most items on any instance refer to nothing.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Buy milk")

	shown = run("show", "1").output

	assert "Buy milk" in shown, "the probe showed nothing, so it proves nothing"
	assert "Referred to by" not in shown, f"an empty section was printed anyway: {shown}"


def test_releasing_everything_gives_back_only_what_is_held (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1122`. What a session-end hook runs, because a hook has no list of refs.

	The session that would have collected one has stopped, which is the whole reason a harness
	hook is a different kind of channel from every other lever here: it fires whether the agent
	attends or not.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Being worked on")
	run("add", "Nobody has this")
	run("claim", "1")

	freed = run("release", "--all").output

	assert "Released 1" in freed, freed
	assert "Being worked on" in freed
	assert "Nobody has this" not in freed, "it released something nobody was holding"
	assert run("list", "--claimed-by", "me").output.count("#") == 0 or (
		"Being worked on" not in run("list", "--claimed-by", "me").output
	)


def test_releasing_everything_says_nothing_when_there_is_nothing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Which is the ordinary case at the end of a session that finished what it started.

	Finishing hands a claim back by itself (`#1113`), so a hook printing a line every time it
	does nothing would be a hook people turn off — and the point of it is the sessions where
	it has something to do.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Nobody has this")

	assert run("release", "--all").output.strip() == ""


def test_a_number_and_all_together_is_refused (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Because one of them narrows nothing, and guessing which was meant is worse than asking."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something")

	refused = run("release", "1", "--all", expect=1)

	assert "Not both" in refused.output


def test_show_says_what_has_been_checked_and_against_what (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1121`, and §14.1: nothing an agent stores may be invisible to the person.

	**A record, not a proof.** Somebody can say a check passed without having run one, so the
	heading says *recorded* and never *verified* — the value is that it is kept, attributed
	and able to go out of date.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Ship the release")
	run("verify", "1", "--summary", "5,610 passed, 41 skipped", "--tree", "a" * 40)

	shown = run("show", "1").output

	assert "Recorded checks (1)" in shown, shown
	assert "5,610 passed" in shown
	assert "passed" in shown
	assert "aaaaaaa" in shown, f"the tree it ran against is not shown: {shown}"


def test_a_failing_check_is_recorded_and_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The more useful half of the pair, and it must not read as a success."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Ship the release")
	run("verify", "1", "--failed", "--summary", "3 failed in test_agenda")

	shown = run("show", "1").output

	assert "failed" in shown, shown
	assert "3 failed in test_agenda" in shown


def test_recording_outside_a_checkout_says_the_record_cannot_expire (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""§1.4: most machines have no checkout, and a record is still worth keeping there.

	**Said rather than left to be discovered.** A record with no tree cannot go out of date,
	and somebody who believes it can will trust it after the code has moved — which is the
	failure the tree exists to prevent, arriving through the door left open for the machines
	that have none.
	"""

	monkeypatch.chdir(home)
	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Ship the release")
	recorded = run("verify", "1", "--summary", "Checked by hand")

	assert "cannot go out of date" in recorded.output, recorded.output
	assert "no tree" in run("show", "1").output


def test_a_document_is_not_asked_what_has_been_checked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Only a task is checked, so a document's page has no such section and makes no request."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "What we settled", "--type", "decision", "--body", "Because.")

	shown = run("show", "1").output

	assert "What we settled" in shown, "the probe showed nothing, so it proves nothing"
	assert "Recorded checks" not in shown, shown


def test_a_link_in_a_history_reads_as_a_link (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1115`. Every link said *commented*, on both surfaces, in agreement.

	Since `#52` a comment's event names the comment and carries the commented-on item as its
	subject — so `subject_type is not None` looks like a comment marker and is not: links set
	it too, deliberately, so a link event can name the far end and be scoped by it.

	**Both copies of the rule agreed**, which is why nothing caught it. The signature defect
	here is two copies that disagree; these were byte-identical and both wrong, so every
	cross-surface comparison passed over a history claiming a conversation had taken place.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "The first")
	run("add", "The second")
	run("link", "1", "relates-to", "2")

	shown = run("show", "1", "--history").output

	assert "commented" not in shown, f"a link is reported as a conversation: {shown}"
	assert "linked" in shown, shown

	run("unlink", "1", "2")

	assert "unlinked" in run("show", "1", "--history").output

	# **And a real comment still reads as one**, which is what says the fix narrowed the rule
	# rather than turning it off. An absence two behaviours produce is not evidence for either.
	run("comment", "1", "something that actually happened")

	assert "commented" in run("show", "1", "--history").output


def test_the_agenda_opens_with_what_is_waiting_on_you (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1116`. The half that had to come first: a person seeing the question at all.

	The status has been seeded since M1 and used zero times in 925 tasks. Teaching an agent to
	set it before anybody could see one would have built the loop from the end that does not
	close.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Ordinary work")
	run("add", "Which way round should the flag read?")
	run("update", "2", "--status", "needs_input")

	shown = run("agenda").output

	assert "Waiting on you" in shown, shown
	assert "Which way round" in shown
	assert shown.index("Waiting on you") < shown.index("Next"), (
		f"the question is below the work it is holding up: {shown}"
	)


def test_the_agenda_says_nothing_about_waiting_where_nothing_is (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""An empty bucket is dropped, like every other section here.

	A day with nobody waiting on you should not print the words — the absence is the good
	news, and a heading over nothing makes a reader look for what is missing.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Ordinary work")

	shown = run("agenda").output

	assert "Ordinary work" in shown, "the probe showed nothing, so it proves nothing"
	assert "Waiting" not in shown, shown


def test_the_list_narrows_to_what_somebody_is_holding (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1120`. The claim discipline, made visible to whoever is following it.

	Four commands are asked for around every piece of work — claim, start, stop, release — and
	until this the person doing it could not ask the program what they were holding.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Being worked on")
	run("add", "Nobody has this")
	run("claim", "1")

	held = run("list", "--claimed-by", "me").output

	assert "Being worked on" in held
	assert "Nobody has this" not in held, f"the filter did not narrow anything: {held}"

	run("release", "1")

	assert "Being worked on" not in run("list", "--claimed-by", "me").output


def test_the_changes_feed_narrows_to_what_one_account_did (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""*What has it been doing* — the question a person asks about an agent they handed work to.

	`--mine` answers about this machine's own credential, which is the acts you already know
	about. This is the other direction, and it had no command at all.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something happened")

	assert "no such account" not in run("changes", "--by", "si").output.lower()

	# **Reported rather than fatal**, which is `fanout`'s rule and not this command's choice: a
	# connection that cannot answer says so and the others still do, and `--strict` is what
	# makes it stop. The name is in the message, so a typo is visible rather than silent.
	missing = run("changes", "--by", "nobody-here")

	assert "nobody-here" in missing.output, missing.output
	stopped = run("changes", "--by", "nobody-here", "--strict", expect=1)

	assert "nobody-here" in stopped.output


def test_show_says_what_to_read_before_starting (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1119`. The workspace's *what is in force here*, narrowed to one item.

	**Above the links**, because it is the section somebody has to read before doing anything
	and the links are what they read afterwards.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "How dates are written", "--type", "decision", "--body", "Because.")
	run("add", "Rewrite the parser")
	run("link", "1", "documents", "2")

	shown = run("show", "2").output

	assert "Read first" in shown, shown
	assert "How dates are written" in shown
	assert shown.index("Read first") < shown.index("Links"), (
		f"the reading list is below the links it is meant to be read before: {shown}"
	)


def test_show_says_nothing_about_governance_where_nothing_governs (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2c's rule that a field nobody set is not printed, applied to a whole heading.

	This is the section §1.4 is most at risk from: a personal to-do list writes no decisions,
	so a heading that appeared empty would put the word *govern* in front of somebody whose
	list says *buy milk*.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Buy milk")

	shown = run("show", "1").output

	assert "Buy milk" in shown, "the probe showed nothing, so it proves nothing"
	assert "Read first" not in shown, f"an empty section was printed anyway: {shown}"


def test_a_related_decision_is_not_something_to_read_first (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1124` Q2 driven at the terminal, because this is where somebody would notice it.

	*Relates to* means near, and near is not binds. If this section listed it, the heading
	would be a claim the product cannot support, and every later reader would discount it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "How dates are written", "--type", "decision", "--body", "Because.")
	run("add", "Rewrite the parser")
	run("link", "2", "relates-to", "1")

	shown = run("show", "2").output

	assert "Relates to" in shown, f"the link was not made: {shown}"
	assert "Read first" not in shown, shown


def test_show_offers_the_link_the_writing_suggests_and_says_it_is_not_one (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1137`. The evidence, where the decision is made, with one command to act on it.

	**Phrased as a suggestion and printed below the links**, which is the whole of respecting
	the decision underneath: *what governs this* answers from links somebody made, and a
	citation is a reason to think one belongs rather than the thing itself.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "How dates are written", "--type", "decision", "--body", "Because.")
	run("add", "Rewrite the parser", "--description", "Follows #1.")

	shown = run("show", "2").output

	assert "Not linked, but its writing suggests (1)" in shown, shown
	assert "How dates are written" in shown
	assert "this names it" in shown
	assert "subroutine link 1 documents 2" in shown, f"nothing says how to confirm it: {shown}"


def test_the_suggestion_read_from_the_document_offers_the_same_command (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1609`. The tip named the task as the thing that documents the decision.

	**The neighbour above drives the same citation from the task and always passed**, because
	the command was built as *the other end, then this one* and from a task the other end is
	the document. So the guard existed, was correct, and could only ever ask the question from
	the side where a fixed order happens to be right — this project's signature defect, in a
	test written for this feature.

	Confirming the old tip wrote a real edge, and it was invisible where it mattered: *Read
	first* renders a governing link the same way whichever direction it runs, so the work item
	looked perfect and only the decision's own page disagreed.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "How dates are written", "--type", "decision", "--body", "Because.")
	run("add", "Rewrite the parser", "--description", "Follows #1.")

	shown = run("show", "1").output

	assert "Not linked, but its writing suggests (1)" in shown, shown
	assert "it names this" in shown, "the task did the citing, and the evidence says so"
	assert "subroutine link 1 documents 2" in shown, (
		f"the decision governs the task, so it is the source of the offered link: {shown}"
	)
	assert "subroutine link 2 documents 1" not in shown, (
		f"a task does not document a decision: {shown}"
	)


def test_confirming_the_suggestion_makes_it_a_link_and_the_offer_stops (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Driven end to end, because the tip is a promise about a command that has to work."""

	run("init", "--username", "si", "--workspace", "Personal")
	run("doc", "create", "How dates are written", "--type", "decision", "--body", "Because.")
	run("add", "Rewrite the parser", "--description", "Follows #1.")
	run("link", "1", "documents", "2")

	shown = run("show", "2").output

	assert "Documented by" in shown, f"the link was not made: {shown}"
	assert "Not linked, but its writing suggests" not in shown, shown


def test_a_personal_list_never_meets_a_suggestion_about_governance (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§1.4, and this is the section most likely to break it.

	Somebody keeping a to-do list writes no decisions, so there is nothing to propose — but a
	heading that appeared empty, or on an ordinary citation of another task, would put the
	word *governs* in front of a reader whose list says *buy milk*.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Buy milk")
	run("add", "Buy bread", "--description", "Same trip as #1.")

	shown = run("show", "2").output

	assert "Buy bread" in shown, "the probe showed nothing, so it proves nothing"
	assert "suggests" not in shown, f"a suggestion about a task, which cannot govern: {shown}"


def test_the_scripted_item_carries_what_refers_to_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One item, two renderings — `#583`'s defect arriving on a new section.

	A section the rendered path shows and the scripted one omits is exactly what that guard
	was written for, and a new field is when it happens.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "The specification")
	run("add", "Implements it", "--description", "As decided in #1.")

	shown = json.loads(run("show", "1", "--json").output)

	assert [one["ref"] for one in shown["backlinks"]] == [2], shown


def test_show_says_who_wrote_each_comment (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#636`, on the surface where the gap turned out to be.

	The item said the *browser* omitted the author; measured while building it, `#759` had
	already fixed that and the **terminal** was the one printing a date and a body and nothing
	else. A record of what happened with the names cut out is half a record, and it matters
	more than the count of accounts suggests: five of this instance's eight are service
	accounts, so *who wrote this* is the difference between a colleague's note and a machine's.

	**Printed on every line rather than dropped when uniform**, which is where this parts
	company with §12.2a. That rule drops a column saying the same thing on every row because
	the reader sees the whole page and loses nothing; a name cannot be inferred from its own
	absence, so dropping it answers *nobody* rather than *the same person throughout*.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Something ordinary")
	run("comment", "1", "the only thing that happened")

	shown = run("show", "1").output

	assert "the only thing that happened" in shown, "the probe recorded nothing"
	assert "@si" in shown, f"the record does not say who wrote it: {shown}"


def test_a_search_marks_the_word_it_matched (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#103``. The matched-field column says *where*; this saves scanning a long title for it.

	Asserted on the styled spans rather than on the output, because the output is the thing
	that must *not* change: this is a highlight, not an encoding (decision `#102`), so a piped
	run or `NO_COLOR` loses the colour and keeps the answer.
	"""

	line = rich.text.Text()
	subroutine.cli.personal._append_title(line, "cursor jumps when the cursor moves", "CURSOR")

	assert line.plain == "cursor jumps when the cursor moves"
	# Every occurrence, not the first: a title matching twice and marked once reads as though
	# the program found something the reader cannot see.
	assert [(span.start, span.end) for span in line.spans] == [(0, 6), (22, 28)]


def test_a_title_that_looks_like_markup_is_still_printed_rather_than_obeyed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A title is user data, which is why it goes through ``Text`` and never through markup.

	Highlighting by building a marked-up string would reopen exactly that — and the failure is
	invisible until somebody files an item with a bracket in its title.
	"""

	line = rich.text.Text()
	subroutine.cli.personal._append_title(line, "[bold]shout[/bold] about cursor", "cursor")

	assert line.plain == "[bold]shout[/bold] about cursor"
	assert [(span.start, span.end) for span in line.spans] == [(25, 31)]


def test_nothing_is_marked_when_no_search_was_made (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""An ordinary listing is not a search, and must render exactly as it always has."""

	line = rich.text.Text()
	subroutine.cli.personal._append_title(line, "cursor jumps", None)

	assert line.plain == "cursor jumps"
	assert line.spans == []


def test_a_project_listing_survives_a_second_workspace (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#332``. A project belongs to one workspace, and the listing asks them all.

	Latent from the day projects and workspaces both existed, and invisible until an instance
	had two: the loop ran once and could not disagree with itself. The migration in `#288`
	created the second, and `--project` stopped working the same afternoon — the workspace
	without the key raised, the fan-out read that as the *connection* failing, and the rows the
	right workspace had already returned went with it.

	The second workspace is the whole fixture. Everything else here is ordinary.
	"""

	run("init", "--workspace", "Projects")
	run("project", "create", "web", "Website")
	run("add", "Fix the header +web")
	run("workspace", "create", "personal", "Personal")

	listed = run("list", "--project", "web").output

	assert "Fix the header" in listed, listed


def test_a_bad_status_names_the_status_even_when_a_real_project_is_named (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1468`. The listing said a project the caller had just made did not exist.

	**`SR#332`'s per-workspace tolerance was on the task call and not the document one.** A
	project legitimately belongs to one workspace, so the task half has caught an absent project
	and moved on since a second workspace existed; the document half had no such handler, so the
	refusal escaped from there and was reported as though the key were nowhere.

	**It is reachable because tasks and documents resolve in opposite orders**, and both clients
	agree with each other: a task listing resolves the status first, a document listing resolves
	the project first. So in a workspace that holds no such project, ``--status <nonsense>``
	makes the task call raise about the *status* — which falls through instead of skipping the
	workspace — and the document call then ran where the project does not exist.

	**A failed listing exits 0**, because a per-connection failure is reported beside whatever
	did arrive rather than ending the command. Asserting on the exit code would test nothing
	here; the message is the whole subject.

	**What it cost**, from the report: a reader took the message at face value and spent a round
	of calls establishing whether the marker was wrong, the project renamed, or the credential
	unable to read it. The fault was that ``all`` is not a status.
	"""

	run("init", "--workspace", "Projects")
	run("project", "create", "web", "Website")
	run("workspace", "create", "personal", "Personal")

	refused = run("list", "--project", "web", "--status", "all")

	assert "status" in refused.output, refused.output
	assert "no project" not in refused.output, (
		f"a project the caller can list is reported as absent, because the *other* "
		f"workspace answered last:\n{refused.output}"
	)


def test_a_project_that_is_nowhere_is_still_refused_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the refusal has to survive the fix, or a typo becomes an empty list.

	Suppressing the per-workspace refusal unconditionally would answer `--project WBE` with
	"nothing on your list" — the same words as a project that exists and holds nothing, which
	is the one thing the reader is trying to tell apart.
	"""

	run("init", "--workspace", "Projects")
	run("project", "create", "web", "Website")
	run("workspace", "create", "personal", "Personal")

	refused = run("list", "--project", "wbe")

	assert "wbe" in refused.output, refused.output
	assert "no project" in refused.output


def test_a_comment_can_be_taken_back_out (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Item ``#400``. Named by its words, because that is what somebody is looking at.

	A comment has no number of its own and its id is a UUID that appears in nothing a person
	reads — the same reason ``unlink`` names two refs rather than a link id. Requiring one
	would make this a command only a script could run.
	"""

	run("init")
	run("add", "Call the dentist")
	run("comment", "1", "rang, they are closed on Mondays")
	run("comment", "1", "rang again, booked for Thursday")

	gone = run("uncomment", "1", "closed on Mondays")

	assert "Taken out of" in gone.output

	left = run("show", "1").output

	assert "booked for Thursday" in left
	assert "closed on Mondays" not in left


def test_taking_a_comment_out_refuses_rather_than_guessing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Several matches is refused, and the several are deliberately not listed back.

	Listing them would put the reader in the position of choosing by *position*, which is the
	one way of naming things this program does not have — ``done 1`` means ref 1, never the
	first row (§12.2a). So the answer is to be more specific, and the count says how much.
	"""

	run("init")
	run("add", "Fix the parser")
	run("comment", "1", "the parser is wrong")
	run("comment", "1", "the parser is fixed")

	several = run("uncomment", "1", "the parser", expect=1)

	assert "2 comments" in several.output
	assert "Say more of the one you mean" in several.output

	missing = run("uncomment", "1", "nothing says this", expect=1)

	assert "says that" in missing.output

	# Neither refusal took anything with it.
	left = run("show", "1").output

	assert "the parser is wrong" in left
	assert "the parser is fixed" in left


def test_a_stale_marker_falls_back_to_the_stored_context (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Item ``#324``, hit live mid-migration and reproduced here.

	**Its neighbour above passes on a one-workspace instance and that is why nothing caught
	this.** There, dropping the marker's workspace is harmless — the sole-workspace default
	answers immediately after. With two, the drop went all the way to nothing, and a stale
	marker *erased* a perfectly good stored context: ``use --here`` refused, on the line after
	``use projects`` had succeeded.

	The warning said "Ignoring it", and ignoring it is the one thing it did not do. §13.7's
	order has exactly one step left below the marker, and that step is what should answer.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "projects", "Projects")
	run("-w", "projects", "project", "create", "SR", "Subroutine")
	run("use", "projects")

	checkout = tmp_path / "checkout"
	checkout.mkdir()

	# Written by hand rather than through `directory.write`, because a marker made today
	# carries `workspace_id` and is followed through a rename (`#317`). The ones that reach
	# this path are older than that, which is what made the failure look arbitrary.
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "local"\nworkspace = "si"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	settled = run("use", "--here", "--project", "SR")

	assert "which is not on local" in settled.output, "it still says the marker is stale"
	assert "Using 'projects' instead" in settled.output, "and what it used in its place"
	assert "several workspaces" not in settled.output, "which is what it used to refuse with"


def test_the_fallback_does_not_turn_on_how_the_workspace_was_capitalised (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Item ``#418``. `#324`'s fallback applied or did not depending on one keystroke.

	``subroutine use`` stores what was typed — verified: ``context.toml`` holds
	``workspace = "PROJECTS"`` — and ``context.resolve`` normalises both halves before
	anything compares them, with a comment three lines up saying why. ``stored_workspace`` was
	written beside that comment and omitted it, so ``_settled`` compared ``'PROJECTS'`` against
	the canonical ``'projects'``, found no match, and fell back to *"Ignoring it"* — the
	behaviour `#324` had just replaced, with the message `#324` had just replaced.

	**A fix that silently half-applies is worse than no fix**, because the failure is the old
	one and nothing distinguishes them. The test beside this one passes either way: it types
	the slug in lower case, which is what anybody writing the test would do.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "projects", "Projects")
	run("-w", "projects", "project", "create", "SR", "Subroutine")

	# Capitalised on purpose. `Roster.find` and `Identity.workspace` both match
	# case-insensitively, so this resolves and is stored verbatim.
	run("use", "PROJECTS")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "local"\nworkspace = "si"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	settled = run("use", "--here", "--project", "SR")

	assert "Using 'projects' instead" in settled.output
	assert "Ignoring it" not in settled.output


def test_a_stale_marker_with_nothing_to_fall_back_to_still_says_so (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""The other half, without which the fallback could quietly become unconditional.

	A guard that only ever sees a usable stored context cannot tell "fell back correctly" from
	"reported a fallback it did not make". Here there is no stored workspace at all, so the
	old wording is still the right one and the refusal that follows is the honest answer.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "projects", "Projects")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "local"\nworkspace = "si"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	# `add`, not `list`: reads span every reachable workspace by design (§13.7), so only a
	# command that has to choose *where to write* meets this at all.
	refused = run("add", "Something", expect=1)

	assert "Ignoring it" in refused.output
	assert "several workspaces" in refused.output


def test_adopting_a_checkout_takes_the_workspace_from_the_project_it_was_handed (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#1501`, Simon's decision of 2026-08-28: the argument settles it, or names who does.

	``subroutine use --here --project web`` refused for want of a workspace on any connection
	reaching more than one, with no stored context and no marker — which is every fresh broad
	credential, and is the command both plugin skills and the import process prescribe. Two
	separate import runs met it in one day.

	**The mechanism was not the one first reported.** Reading the current workspace does not
	refuse: ``use --here`` with no project succeeds and writes a marker naming the connection
	alone. The refusal came from scoping the *project search*, which asked one workspace and
	had none to ask about — so ``--project`` was consulted and could not be used.

	**Silent while the answer is unambiguous, insistent when it is not**, which is `SR#587`'s
	shape. The three cases here are the whole of the rule, and the last two are what stop this
	passing by guessing.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "projects", "Projects")
	run("-w", "projects", "project", "create", "web", "Web")

	assert "several workspaces" in run("add", "Something", expect=1).output, (
		"the fixture is not ambiguous, so it cannot show anything about resolving one"
	)

	settled = tmp_path / "settled"
	settled.mkdir()
	monkeypatch.chdir(settled)

	adopted = run("use", "--here", "--project", "web")

	assert "several workspaces" not in adopted.output, (
		f"the project names one workspace and the command asked anyway:\n{adopted.output}"
	)

	written = (settled / subroutine.directory.FILE_NAME).read_text(encoding="utf-8")

	assert 'workspace = "projects"' in written, written
	assert 'project = "web"' in written, written

	# **A key that two workspaces hold is refused naming *those two*** — which on a real
	# instance is strictly more than the general refusal can say, because it lists every
	# workspace there is. Every instance seeds an Inbox, so this needs nothing built.
	shared = tmp_path / "shared"
	shared.mkdir()
	monkeypatch.chdir(shared)

	both = run("use", "--here", "--project", "inbox", expect=1)

	assert "personal" in both.output and "projects" in both.output, both.output
	assert not (shared / subroutine.directory.FILE_NAME).exists(), (
		"a refusal left a marker behind"
	)

	# **And a key nothing holds says where it looked.** *There is no project 'web' here* is a
	# complete answer with one workspace and an assertion the reader cannot check with two.
	nowhere = tmp_path / "nowhere"
	nowhere.mkdir()
	monkeypatch.chdir(nowhere)

	missing = run("use", "--here", "--project", "nosuchproject", expect=1)

	assert "personal" in missing.output and "projects" in missing.output, missing.output
	assert not (nowhere / subroutine.directory.FILE_NAME).exists()


def test_a_stored_workspace_that_is_also_gone_is_not_used_as_a_fallback (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""And the fallback is checked against the instance rather than trusted.

	Falling back to a stored slug the connection has never heard of would move the failure one
	step along and change nothing — the same defect wearing the next source's name. So the
	replacement is looked up before it is announced, and when it does not resolve the command
	says what it always said.
	"""

	run("init", "--workspace", "Personal")
	run("workspace", "create", "projects", "Projects")
	run("use", "projects")

	# The stored context now names a workspace that is not there either.
	stored = subroutine.context.file_path()
	stored.write_text(
		'connection = "local"\nworkspace = "long-gone"\n', encoding="utf-8"
	)

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "local"\nworkspace = "si"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	refused = run("add", "Something", expect=1)

	assert "Using 'long-gone'" not in refused.output
	assert "Ignoring it" in refused.output


def test_a_marker_naming_a_connection_that_is_gone_does_not_stop_the_program (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Item ``#409``, end to end. `#166`'s rule, for the half that had no implementation.

	**The neighbour that guards `#166` could not have found this**, and the reason is the one
	`#324` taught: it writes a marker naming an unknown *workspace* on an instance with one
	connection, where the connection half is never in doubt. Reproducing this needs a marker
	whose connection is not in the roster — and with a single connection configured there is
	nothing to fall back *to*, so the interesting case needs two.
	"""

	run("init", "--workspace", "Personal")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "gone"\nworkspace = "personal"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	listed = run("list")

	assert "which is not configured" in listed.output
	assert "Using 'local' instead" in listed.output

	# The point of the whole thing: the commands still work in that directory.
	assert run("add", "Still possible").exit_code == 0
	assert run("show", "1").exit_code == 0


def test_a_marker_for_another_connection_does_not_file_by_project_key (
	run: typing.Callable[..., typer.testing.Result],
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Item ``#414``. `#409` let the connection fall through; the project came along with it.

	``context.resolve`` drops the marker's *workspace* when its connection is not the one that
	answered. Nothing did the same for the project — so ``directory.resolve``'s match-by-key
	fallback, which exists for markers written before `#177` gave them ids, answered with **this
	instance's** project of the same name. Measured live: a checkout marked for one instance
	filed a task into a different one's ``SR``, printing ``Using 'local' instead`` and ``in SR,
	from .subroutine`` one line apart.

	**The project has to exist here for this to test anything.** A marker naming one that is
	absent produces the same output either way, because the fallback then has nothing to find —
	which is exactly why the case went unnoticed: the neighbours above all use a key that is
	not here.

	**Falsified against the original code**: remove ``marker.speaks_for(...)`` from
	``_project_named_by`` and the task is filed ``in SR``.
	"""

	run("init", "--workspace", "Personal")
	run("project", "create", "SR", "Subroutine")

	checkout = tmp_path / "checkout"
	checkout.mkdir()
	(checkout / subroutine.directory.FILE_NAME).write_text(
		'connection = "gone"\nproject = "SR"\n', encoding="utf-8"
	)
	monkeypatch.chdir(checkout)

	added = run("add", "Filed where this connection says")

	assert added.exit_code == 0
	assert "in SR" not in added.output, "the marker names another instance's SR"

	# **And it says which of the two reasons applied** (`#414`). "SR is not on local" would be
	# a refusal asserting a cause the program has not established, and here a false one — SR is
	# on local. It was simply never looked for.
	assert "on gone" in added.output
	assert "going to local" in added.output
	assert "which is not on local" not in added.output


def test_a_connection_named_on_the_command_line_is_still_refused (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the line that keeps `#409`'s leniency where it belongs.

	A typo in ``-c`` acting quietly somewhere else is a worse failure than the one being
	fixed, so the difference between a file and somebody speaking now is asserted on the
	surface as well as in ``context.resolve``.
	"""

	run("init", "--workspace", "Personal")

	refused = run("-c", "gone", "list", expect=1)

	assert "There is no connection called 'gone'" in refused.output


def test_a_type_filter_narrows_both_kinds_rather_than_only_the_tasks (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#501`. **Found by driving the real instance, minutes after the filter was written.**

	The listing spans tasks *and* documents (§6.2), and the first version of `--type` reached
	only the task half — so `--type bug` returned every bug **and every decision, note and
	specification in the workspace**. Narrowing the list widened the part of it nobody had
	filtered, which is worse than not having the flag: the rows that arrive look like an
	answer to the question asked.

	A type is per-entity vocabulary (§5.5), so `bug` is not a document type at all. The right
	answer to "which documents are bugs" is none, not all of them.
	"""

	run("init")
	run("add", "Something broken")
	run("doc", "create", "A conclusion", "--body", "Why", "--type", "decision")

	bugs = run("list", "--type", "bug").output

	assert "Something broken" not in bugs, "nothing was typed as a bug"
	assert "A conclusion" not in bugs, "a decision is not a bug, and must not ride along"

	decisions = run("list", "--type", "decision").output

	assert "A conclusion" in decisions
	assert "Something broken" not in decisions


def test_a_status_only_one_kind_has_still_answers_for_the_other (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#501`. A task status and a document status are different vocabularies (§5.5).

	`active` is a *document* status and no task has one, so asking for it must return the
	documents in force rather than the refusal the task half raised. The first version of this
	skipped the rest of the workspace as soon as tasks refused, which answered a perfectly good
	question with an error.
	"""

	run("init")
	run("add", "An ordinary task")
	run("doc", "create", "Settled", "--body", "Why", "--type", "decision")
	run("doc", "edit", "2", "--status", "active")

	shown = run("list", "--status", "active").output

	assert "Settled" in shown
	assert "An ordinary task" not in shown


def test_a_status_neither_kind_has_is_refused_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#501`. **A typo must not read as an empty list**, which is the whole hazard here.

	Tolerating a key one vocabulary has not got is what makes the test above work; tolerating
	one *neither* has would turn `--status blockd` into "nothing on your list" — the same
	answer as a backlog with nothing in it. `#332`'s lesson, on a second axis.
	"""

	run("init")
	run("add", "An ordinary task")

	refused = run("list", "--status", "blockd")

	assert "blockd" in refused.output
	assert "Nothing on your list" not in refused.output


def test_an_assignee_filter_returns_no_documents_at_all (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#501`. §6.14: a document has an owner rather than a worker, so it has no assignee.

	The same argument `--ready` makes. Including documents would end "everything Simon is
	working on" with every specification in the workspace, which is a longer way of saying the
	filter did not apply.
	"""

	# Named rather than inherited from `getpass.getuser()`: a test that asserts on this
	# machine's login passes here and fails everywhere else, which this suite has paid for.
	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Mine to do @si")
	run("doc", "create", "A conclusion", "--body", "Why", "--type", "decision")

	shown = run("list", "--assignee", "si").output

	assert "Mine to do" in shown, "the filter must find the task it was given"
	assert "A conclusion" not in shown


# **The duplicate-instance guard is no longer a spelling this file can scan for** (`#942`).
# It used to be `merged=False` on `opened()`, and two tests here read the tree for it. The
# guard moved onto the flatten — `World.merging()`, called from `_across` — so which
# commands it stops is now decided by what each one does with the answers rather than by a
# flag, and the only honest way to ask is to run them against two connections naming one
# instance. That is `test_cli_connections.py`, which already starts servers:
# `test_a_duplicate_stops_only_the_reads_that_combine_connections`.


#: How many lines ``cli/personal.register`` may still hold. **A ratchet: it only goes down.**
#:
#: Cold review `#927`'s L-1 measured it at **4,769 lines of a 6,971-line file — 68% — holding
#: ninety nested definitions sharing one closure**, none of them reachable except by running a
#: Typer command. `#943` is taking it apart in stages; this is what stops the next one being
#: undone by the one after.
#:
#: **Lower it when a stage lands. Never raise it.** A new command is a function somewhere else
#: that ``register`` calls, which is the shape this is pushing towards — so needing more room
#: here is the signal, not the exception.
#:
#: **It has now fired on a real change and been paid rather than raised** (`#704`). Two new
#: ``workspace`` commands put the closure 31 lines over, and the remedy was the one this asks
#: for: the whole ``workspace`` group moved to ``_register_workspace``, which is 135 lines out
#: for two in. That is the mechanism working — a feature is what makes somebody do the move.
#:
#: **And a second time, on five lines of help** (`#1136`). That is the more useful data point:
#: the first payment came from a feature big enough to expect it, and this one came from a
#: docstring. ``link`` and ``unlink`` moved to ``_register_links``, 109 lines out for two in.
#:
#: **And a third, on one filter** (`#1120`). The ``user`` group moved to ``_register_users`` —
#: eight commands, 309 lines out for two in, and the largest natural unit that was left.
#:
#: **And a fourth, on one flag** (`#1122`). ``project`` moved to ``_register_projects``, and
#: the new ``setup`` group was written outside the closure to begin with — which is the state
#: this was pushing towards.
#:
#: **And a fifth, on one command** (`#1121`). ``doc`` moved to ``_register_documents``. Five
#: payments, none of them raised, and each cheaper than the last: what a feature pays now is
#: the cost of noticing rather than the cost of designing, and the closure has gone from 4,769
#: lines to under 1,800 without a single stage being planned as one.
#:
#: **And a sixth, on one word** (`SR#1236`). ``done`` learned to say *Marked as past* about
#: something that merely happened, rather than *Done* — four lines of behaviour inside a
#: command. Its whole body left as :func:`subroutine.cli.personal._finished`, which is thirty-
#: five. Six payments, none of them raised, and the pattern has not varied once: the closure is
#: where a command's body is easiest to write and hardest to reach from anywhere else.
#:
#: **And a seventh, on three commands at once** (`SR#1352`, `SR#1358`). ``link`` and ``unlink``
#: grew a second list each and ``show`` grew ``--tree``; the three bodies left as
#: :func:`subroutine.cli.personal._joined`, :func:`~subroutine.cli.personal._unjoined` and
#: :func:`~subroutine.cli.personal._shown_item`. **The ratchet fired first and named the
#: remedy** — the closure was 22 lines over before any of them moved, and the message said
#: where a command's body belongs.
#:
#: **And an eighth, on an option rather than a command** (`SR#1431`). ``changes`` grew
#: ``--filter``, which is sixteen lines and took the closure sixteen over — so the bill a
#: ratchet sends for a *new command* arrives for an option on an existing one too, in smaller
#: instalments and just as certainly. Its body left as
#: :func:`subroutine.cli.personal._what_moved`. **The eighth payment, and the second where the
#: ratchet fired before anything moved**; extracting the body rather than trimming the option is
#: what makes the next option on that command free.
#:
#: **1,567 → 1,500 on 2026-08-31, the ninth payment** (`SR#1696`). ``update`` gained
#: ``--expected-version`` and the ratchet fired fourteen over, which is the instalment above
#: arriving again on the same command. What paid it is the ninety-nine lines that decided
#: *which options were actually named* — fifteen sentinels, each meaning *unset* differently —
#: leaving as :func:`subroutine.cli.personal._named_changes`.
#:
#: **Trimming the new option instead was the available shortcut and is the wrong move**: it
#: buys one option and leaves the next one at the same wall, which is the ratchet being worked
#: around rather than paid.
REGISTER_CEILING = 1_500

#: The floor that stops the ceiling above being met by a scanner that read nothing. Both
#: numbers move together as stages land: lines out of ``register`` become functions here.
#:
#: **145 → 144 on 2026-08-24, and lowering it was the right move rather than a defeat**
#: (`#1187`). ``_field_in_words`` left this module entirely — it moved to
#: :func:`subroutine.views.field_in_words`, because three surfaces render an event and only this
#: one was translating column names. A floor counts functions *here*; a function that leaves for
#: somewhere more than one caller can reach has not gone back into the closure, which is the only
#: thing this number exists to catch.
#: **144 on 2026-08-24, with the ceiling to 1,710** (`#1215`). ``agenda`` gained an option and
#: the closure still shrank: the note about why ``-w`` precedes the command moved into
#: ``_agenda``'s docstring, and ``show_today``'s body left as :func:`_show_today` — which was a
#: correctness fix as well as payment, since calling a Typer command as a plain function hands
#: every unnamed option its ``OptionInfo`` descriptor rather than its default.
#: **145 on 2026-08-24, with the ceiling to 1,697** (`SR#1211`). `update` gained `--remind`, and
#: the closure still shrank by twenty-two: the body that applies the changes left as `_changed`,
#: which needed nothing from the closure that `Program` does not carry. What stayed behind is
#: the part deciding *whether each field was given*, and each of those decides it differently.
#: **147 → 150 on 2026-08-25, with the ceiling to 1,664** (`SR#1236`). Two arrived —
#: ``_finished``, which is ``done``'s body lifted out, and ``_happens``, which answers *is this
#: something that happens to you* for both the agenda's closing tip and ``done``'s wording —
#: and the floor had been standing one under the count for a while, so it moves three. Both
#: numbers moved the right way on a change that *added* behaviour, which is what this pair
#: exists to make ordinary.
#: **150 → 169 on 2026-08-27, with the ceiling holding at 1,626** (`SR#1430`, `SR#1431`). A
#: whole command arrived — ``subroutine journal`` — and the closure did not move, because the
#: five functions it needed were written at module level from the first line and ``move``'s body
#: left to pay for its declaration. **That is the arrangement working as designed rather than
#: being worked around**: the bill for a new command is an extraction, so what is added is paid
#: for instead of accumulated.
MODULE_LEVEL_FLOOR = 190


def _register_span () -> tuple[int, int]:
	"""Return how many lines ``register`` holds and how many functions the module has."""

	source = pathlib.Path(subroutine.cli.personal.__file__).read_text(encoding="utf-8")
	tree = ast.parse(source)

	found = next(
		node
		for node in tree.body
		if isinstance(node, ast.FunctionDef) and node.name == "register"
	)

	return (
		found.end_lineno - found.lineno + 1 if found.end_lineno else 0,
		sum(1 for node in tree.body if isinstance(node, ast.FunctionDef)),
	)


def test_the_personal_command_closure_only_ever_shrinks () -> None:
	"""`#943`, cold review `#927`'s L-1 — the ratchet, not the fix.

	**Two numbers, because either alone is satisfiable the wrong way.** A ceiling on
	``register`` is met by moving code into a second enormous function; a floor on module-level
	functions is met by splitting one in half. Together they say *lines left the closure and
	became things a test can call*.

	The first stage moved the twenty-four definitions that referenced nothing in the closure —
	measured by walking the tree, not by reading — which is 821 lines and no signature change
	anywhere.
	"""

	held, functions = _register_span()

	assert held <= REGISTER_CEILING, (
		f"`register` holds {held} lines against a ceiling of {REGISTER_CEILING}. This ratchet "
		"only goes down: a new command belongs in a function `register` calls, not in the "
		"closure. If a stage of `#943` genuinely made it shorter, lower the ceiling."
	)

	assert functions >= MODULE_LEVEL_FLOOR, (
		f"`cli/personal.py` has {functions} module-level functions against a floor of "
		f"{MODULE_LEVEL_FLOOR}. Something moved back into the closure, or this scan has "
		"stopped reading the tree."
	)


def test_the_helpers_that_left_the_closure_can_be_called_directly () -> None:
	"""The point of the move, driven rather than asserted about.

	**Nested, none of these could be reached without running a Typer command**, which is L-1's
	whole complaint: a helper deciding how a date is worded was testable only through the
	command that printed it. This calls three of them with nothing else set up.
	"""

	assert callable(subroutine.cli.personal._whoami), (
		"`#1034` paid for its option by lifting `whoami`'s body out; if this is gone the "
		"ratchet was met by putting something back rather than by moving it"
	)

	assert subroutine.cli.personal._kept(1) != subroutine.cli.personal._kept(2), (
		"`#296`'s one sentence: the verb and the possessive agree with the count"
	)

	assert "1" in subroutine.cli.personal._kept(1)
	assert "2" in subroutine.cli.personal._kept(2)

	# §13.5b's shape: a row `init` wrote names no item, so the entity type reached the page as
	# `workspace` — one of the seven words a person setting up a to-do list must never meet.
	# Putting that into a reader's words is a pure function of one string, and it was reachable
	# only through the command that printed it.
	assert subroutine.cli.personal._in_this_persons_terms("workspace") == "this list"

	assert subroutine.cli.personal._in_this_persons_terms("nothing_it_knows") == (
		"nothing_it_knows"
	), "an unmapped kind keeps its own name rather than being made one up"


def test_a_helper_speaks_through_the_program_it_was_handed () -> None:
	"""`#943` stage two: the closure has a name, so a test can be the program.

	**This is the thing the object buys and the only honest way to show it.** `_report` names
	every connection that could not be reached and carries on, and it used to be reachable only
	by configuring a broken connection and running a command against it. Here the warning
	channel is a list.

	The behaviour it holds is worth having a direct test for on its own: the command still
	exits 0, because an agenda that refuses to print when one of three servers is down is
	worse than an agenda with a line saying which one.
	"""

	said: list[str] = []

	program = subroutine.cli.personal.Program(
		say=said.append,
		fail=lambda error: pytest.fail(f"nothing here should end the command: {error}"),
		stop=lambda *arguments: pytest.fail("nor stop it"),
		settings=subroutine.config.Settings,
		console=rich.console.Console(),
		warn=said.append,
		mask=lambda text: text,
		selected=subroutine.cli.personal.Selected(),
	)

	broken = subroutine.fanout.Failure(
		connection=subroutine.connections.Connection(name="acme", url="http://nowhere"),
		error=subroutine.errors.NotFound("nothing answered there"),
	)

	subroutine.cli.personal._report(program, _one_connection(), (broken,))

	assert said and "acme" in said[0], said
	assert "nothing answered there" in said[0]

	stranger = subroutine.views.WorkspaceAccess(
		id=uuid.uuid4(),
		slug="acme",
		title="Acme",
		timezone=None,
		role=None,
		permissions=[],
		narrowed_by_credential=False,
	)

	assert subroutine.cli.personal._role(stranger) == "no role", (
		"a superuser reaches a workspace they hold no role in, and that is a real answer"
	)


def test_a_listing_is_narrowed_by_a_date (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``--filter created_at.gte=yesterday`` — item `#815`, and Simon's own question.

	Everything here was created a moment ago, so *today onwards* holds it and *before today*
	holds nothing. That is a weak-looking pair and it is the strongest one available without
	reaching past the CLI to backdate a row: it catches a filter that never reaches the client,
	which is the failure this whole layer is at risk of.
	"""

	run("init")
	run("add", "Ordinary work")

	assert "Ordinary work" in run("list", "--filter", "created_at.gte=today").output
	assert "Ordinary work" not in run("list", "--filter", "created_at.lt=today").output


def test_a_date_a_document_has_not_got_returns_no_documents_rather_than_all_of_them (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
) -> None:
	"""**The failure mode is that narrowing a list makes it longer** (`#815`).

	A document is not scheduled (§6.14), so it has no ``completed_at`` — and the document half
	of this listing is a separate request. If that request simply dropped the filter it could
	not honour, *what did I complete today* would answer with every decision in the workspace
	beside the tasks. That is the ``--type bug`` defect that reached the real instance once
	already, in the direction nobody checks.
	"""

	run("init")
	run("add", "Ordinary work")
	run("done", "1")
	_a_document(home, title="How the thing works", body="It works like this.")

	narrowed = run("list", "--filter", "completed_at.gte=2020-01-01").output

	assert "How the thing works" not in narrowed, "the document half ignored the filter"

	# **And the task is still there**, which is what makes the line above mean anything. An
	# earlier version asserted the absence alone — and a document half that *refused* rather
	# than skipping produces exactly that absence, by failing the whole command. Two opposite
	# behaviours, one passing assertion; found by falsifying, which is the only thing that
	# could have found it.
	assert "Ordinary work" in narrowed, "the listing failed rather than skipping the documents"

	# And a field both kinds have still reaches both, so the rule is about the field rather
	# than about documents.
	shared = run("list", "--filter", "created_at.gte=today").output

	assert "How the thing works" in shared


def test_a_filter_with_no_equals_is_refused_before_anything_is_asked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""One message naming the shape, rather than one refusal per workspace per connection."""

	run("init")

	refused = run("list", "--filter", "yesterday", expect=1).output

	assert "is not a filter" in refused
	assert "created_at.gte=yesterday" in refused, "the refusal did not show the shape"


def test_a_filter_that_names_no_operator_is_refused_rather_than_dropped (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1626`. A name with no operator is not a filter, and was being ignored in silence.

	**The wrong answer was a superset**, which is the whole reason it survived: asking for
	``--filter status=open`` returned the entire listing, so nothing looked broken and a caller
	could compose a report from rows that had never been narrowed.

	**Refused in :func:`subroutine.domain.filtering.parsed`, before a client is chosen**, which
	is what makes both transports agree. The terminal's ``--filter`` is only ever filters, so a
	flat name in it is nobody's — where over HTTP ``status`` is a real query parameter belonging
	to the endpoint, and ``understood`` skips it there on purpose.

	**The two refusals exit differently and that is not this item's doing.** This one is raised
	while the line is being read, before any connection is asked, so it stops the command; a
	field the *registry* does not have is refused per connection by the fan-out, which reports
	it and carries on. Asserted rather than corrected, so that a later change to either is
	visible here.

	The neighbouring shapes are asserted alongside, because a refusal that also turned down
	real filters would pass a test written only about the defect — and ``due_before`` is the
	one that would break, being flat *and* a filter.
	"""

	run("init")
	run("add", "Ordinary work")

	refused = run("list", "--filter", "status=open", expect=1).output

	assert "'status' is not a filter" in refused, refused
	assert "created_at.gte=yesterday" in refused, "the refusal did not show the shape"

	# A dotted name nobody declares keeps its own refusal, which names the field and the
	# vocabulary — a different sentence about a different mistake, and from `understood`.
	unknown = run("list", "--filter", "nonsense.gte=today").output

	assert "is not a field this endpoint can filter on" in unknown, unknown

	# A real filter still narrows, and an alias is still accepted. `due_before` is flat and is
	# a filter, so a rule written about the separator alone would have taken it out.
	assert "Ordinary work" in run("list", "--filter", "created_at.gte=today").output

	alias = run("list", "--filter", "due_before=2099-01-01").output

	assert "is not a filter" not in alias, alias


def test_asking_when_something_was_completed_finds_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`#818`, and it is Simon's second question of `#815`.**

	Finished work is hidden unless a caller says otherwise, and ``completed_at`` is null on
	everything unfinished — so until the two were joined, *what did I complete today* answered
	*nothing on your list* the same minute a task was completed. A plausible, complete, wrong
	answer, and an empty list is the one a person is least likely to doubt.

	The rule already existed one spelling along: naming a finished ``status_category`` implies
	it. This is the same request written differently.
	"""

	run("init")
	run("add", "A finished thing")
	run("add", "An open thing")
	run("done", "1")

	completed = run("list", "--filter", "completed_at.gte=today").output

	assert "A finished thing" in completed

	# **And it widens nothing else.** The implication is carried by the field being asked
	# about, so a filter on `created_at` still hides finished work as it always did — the
	# failure in the other direction would be a backlog that grows every time you ask it a
	# question about dates.
	created = run("list", "--filter", "created_at.gte=today").output

	assert "An open thing" in created
	assert "A finished thing" not in created


def test_a_comment_counts_as_having_worked_on_something (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`--filter touched_at.gte=` — `#815`'s third question, from a terminal.

	The task is created and then commented on, and nothing else touches it. `updated_at` does
	not move for a comment, so the pair below is the difference between *what changed* and
	*what was worked on* — and the second is the question a person asks at the end of a week.
	"""

	run("init")
	run("add", "Ordinary work")
	run("comment", "1", "looked at it")

	assert "Ordinary work" in run("list", "--filter", "touched_at.gte=today").output


def test_claiming_something_is_not_working_on_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A lease is bookkeeping — decision `#817`, and `#726`'s distinction reaching a listing."""

	run("init")
	run("add", "Ordinary work")

	# Created today, so it *is* activity; the point is what claiming adds, which is nothing.
	before = run("list", "--filter", "touched_at.gte=today").output
	run("claim", "1")
	run("release", "1")

	assert run("list", "--filter", "touched_at.gte=today").output == before


def test_the_trash_listing_offers_a_command_that_works_on_its_own_rows (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#693` — found three times, including on a built wheel, before it was fixed.

	`list --trash` printed the generic tip, `subroutine show <ref>`, and `show` does not find a
	deleted item — so the suggestion was wrong for **every row the trash can ever hold**, not
	for an unlucky one. The refusal it earned then said to run plain `list`, which is exactly
	where a deleted item is not.

	**The tip is extracted and run**, rather than matched as a string. A test asserting the word
	`restore` appears would pass on a tip naming a ref that does not exist, or a flag that was
	renamed — and this defect was precisely a command that read correctly and did not work.
	"""

	run("init")
	run("add", "Something to delete")
	run("delete", "1")

	listed = run("list", "--trash").output
	suggested = re.search(r"Tip: (subroutine [^\n—]+)", listed)

	assert suggested, f"the trash listing offered no tip at all:\n{listed}"

	# Run it. `expect=0` is the assertion — the old tip exited 1 with "there is no task #1".
	answered = run(*suggested.group(1).split()[1:])

	assert "Something to delete" in answered.output


def test_a_missing_ref_names_the_trash_as_somewhere_to_look (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The second half of `#693`, and phrased as a condition rather than as a claim.

	The program does not know the item was deleted — it knows it is not here. So the remedy
	says *if you deleted it*, which is `#265`'s rule about refusals that assert a cause they
	have not established.
	"""

	run("init")

	refused = run("show", "99", expect=1).output

	assert "--trash" in refused, "the one other place it could be is not named"
	assert "if you deleted it" in refused, "the refusal asserts a cause it cannot know"


def test_a_document_can_be_tagged_from_the_command_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#819` — `--tag`, repeatable, and the same tags a task uses.

	Driven through `show`, which is where a person actually looks: a field that is stored and
	rendered nowhere is the half of this defect that would survive fixing the other half.
	"""

	run("init")
	run("doc", "create", "Why we chose Preact", "--body", ".", "--tag", "design", "--tag", "web")

	shown = run("show", "1").output

	assert "design" in shown
	assert "web" in shown


def test_a_documents_tags_are_replaced_from_the_command_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And `--tag` given nothing clears them, which is §8.3's null.

	Typer hands an empty list when the flag is absent, so *not asked* and *asked for none* are
	told apart by `None` — the distinction every `PATCH` field here depends on, and the one a
	repeatable flag makes easy to lose.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", ".", "--tag", "draft")
	run("doc", "edit", "1", "--title", "Renamed", "--body", ".")

	assert "draft" in run("show", "1").output, "an untouched edit cleared them"

	run("doc", "edit", "1", "--tag", "settled", "--body", ".")

	shown = run("show", "1").output

	assert "settled" in shown
	assert "draft" not in shown, "the tags were merged rather than replaced"


def test_an_item_in_the_trash_can_be_read_and_says_it_is_in_the_trash (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#700`. ``list --trash`` listed it, ``restore`` worked on it, ``show`` denied it existed.

	Three commands, one item, and one of them saying it is not there. The refusal came from a
	*sub-resource* — ``show`` resolves the item and then asks separately for its links, its
	comments and its children, and two of those three lookups excluded deleted rows locally
	where the HTTP side has included them since `#140`. What reached the reader was the
	sub-resource's message, which reads as the item being gone.

	The other half is that being told nothing is worse than being refused: once it *was*
	shown, it rendered exactly like a live item — so it could be read, acted on, and never
	known to have been deleted.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Bin me")
	run("add", "Keep me")
	run("delete", "1")

	binned = run("show", "1").output

	assert "Bin me" in binned
	assert "deleted" in binned

	# The way back, rather than an invitation to comment on something nobody will read. It is
	# what `list --trash` already offers, so the two agree about what a deleted row is for.
	assert "subroutine restore 1" in binned

	# And a live item is untouched by any of it — the fact is about the trash, not a new column.
	alive = run("show", "2").output

	assert "Keep me" in alive
	assert "deleted" not in alive
	assert "subroutine comment 2" in alive


def test_a_finished_item_in_a_listing_says_so (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`SR#874`, and `SR#873` is what made it reachable.**

	A terminal listing used to show finished work only to somebody who had filtered on
	completion, and they do not need telling. Then a bare `search <ref>` began surfacing
	finished items by design — 548 of the served instance's 721 tasks — and they arrived beside
	open ones looking identical, which is `SR#102`'s rule about a distinction that reads as a
	defect.

	`views.status_is_news` had said this was covered: *"a completion has a better rendering on
	every surface"*. True of `show`, true of the browser's row, and untrue of the one a search
	prints to, where the marks were `doing`, `blocked` and `holds up` and there was no fourth.
	"""

	run("init")
	run("add", "The cursor is decoded wrongly")
	run("add", "Follows on from #1 in some way")
	run("done", "1")

	found = run("search", "1").output

	assert "The cursor is decoded wrongly" in found, "the finished item is the one searched for"
	assert subroutine.cli.personal.FINISHED_MARK in found


def test_an_ordinary_listing_says_nothing_about_finishing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The half that keeps §1.4: the column exists for the rows that need it and no others.

	`SR#12.2a` drops a column no row on the page carries, so somebody who has never completed
	anything never meets the word. Without this the fix above would put a column on every
	listing in the product to say nothing.

	**One row is started and one is not, and that is the whole design of this test.** The first
	version listed two plain tasks and *could not fail*: marking every row finished makes the
	column uniform, and §12.2a drops a uniform column exactly as it drops an empty one — so the
	absence was produced by both the correct behaviour and the mutation, which is two opposite
	behaviours yielding one observation. Found by falsifying, not by reading. A started row
	keeps the column present, so a wrongly-marked open row has somewhere to show up.
	"""

	run("init")
	run("add", "Buy milk")
	run("add", "Buy wine")
	run("start", "1")

	shown = run("list").output

	assert subroutine.cli.personal.STARTED_MARK in shown, "the column has to be on the page"
	assert subroutine.cli.personal.FINISHED_MARK not in shown


def test_a_list_that_includes_deferred_work_puts_it_at_the_bottom (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**`SR#877`, Simon's decision of 2026-08-14.**

	*"The deferred state should contribute to ordering — deferred items appearing last. That
	way they are not invisible, but neither are they confused with non-deferred items."*

	The list is newest first, so a deferred task captured *last* is the one that proves it: it
	leads the ordering on its own merits and has to come out at the bottom anyway. Anything
	else — deferring the first, or the middle — is satisfied by leaving the order alone.

	A document is on the page too, because that is the obstacle this could quietly have failed
	on: `deferred` is a task field, and an order only tasks accept drops documents from a
	merged listing entirely (`SR#782`). It answers *no* and stays.
	"""

	ahead = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()

	run("init")
	run("add", "Buy milk")
	run("doc", "create", "What we decided", "--body", "Prose.")
	run("add", "Renew the passport")
	run("defer", "3", ahead)

	rows = [
		line for line in run("list", "--deferred").output.splitlines() if "#" in line
	]

	assert len(rows) == 3, f"expected three rows, got {rows}"
	assert "Renew the passport" in rows[-1], (
		f"the newest task is deferred and must still come last: {rows}"
	)


def test_a_search_does_not_sink_the_thing_that_was_searched_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#877`'s exception, and `SR#867` is the case that decides it.

	A search is ordered by how well a row answers the question, and an item somebody has put
	off is still the best answer to it — typing a number finds *that* item, and sinking would
	put it below every row that merely mentions the digits. So the leading key is added to a
	list and never to a search, on every backend rather than only where one can rank.

	**The *newest* row is the deferred one, and that is what makes this falsifiable.** The first
	version deferred the oldest, which comes last under a search that sinks and last under one
	that does not — two opposite behaviours producing one observation, so the mutation survived.
	Deferring the row that would otherwise lead is the only arrangement the two disagree about.
	"""

	ahead = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()

	run("init")
	run("add", "Renew the passport")
	run("add", "Passport photographs")
	run("defer", "2", ahead)

	rows = [
		line for line in run("search", "passport", "--deferred").output.splitlines()
		if "#" in line
	]

	assert len(rows) == 2, f"expected both matches, got {rows}"
	assert "#2" in rows[0] and "#1" in rows[1], (
		f"a search stays in the order the instance ranked it: {rows}"
	)


def _one_connection () -> typing.Any:
	"""Return a world with nothing colliding, which is what `_merged` asks about.

	`#942` moved the duplicate-instance refusal onto the flatten, so `_merged` now takes the
	world in order to ask it. Built here rather than mocked, because a stub answering *no
	collision* to anything would make every ordering test below pass against a `_merged`
	that had stopped asking.
	"""

	return subroutine.cli.personal.World(
		roster=subroutine.connections.Roster(connections=(), default="local"),
		current=subroutine.context.Current(connection="local", connection_source="default"),
		reached=(),
		unreachable=(),
		settings=subroutine.config.Settings(),
	)


def _gathered (rows: list[tuple[str, typing.Any]]) -> typing.Any:
	"""Wrap rows as one connection's answer, the shape `_merged` is handed."""

	return subroutine.fanout.Gathered(
		answers=(
			subroutine.fanout.Answer(
				connection=subroutine.connections.Connection(name="here", url=None),
				value=subroutine.cli.personal.Listing(rows=rows),
			),
		),
		failures=(),
	)


class _Row:
	"""The smallest thing the view readers accept: something with the attributes they name.

	`views.Task` requires thirty-four fields and this exercises three of them, so building one
	would be describing a task in order to compare two numbers. `tests/test_ordering.py` uses
	the same shape for the same reason.
	"""

	def __init__ (self, **fields: typing.Any) -> None:
		"""Store whatever the test gave, and nothing it did not."""

		for name, value in fields.items():
			setattr(self, name, value)


def _searched (**scores: float | None) -> list[tuple[str, typing.Any]]:
	"""Return rows carrying the relevance an instance would have sent, oldest written first."""

	return [
		(
			"here",
			_Row(
				ref=index + 1,
				title=name,
				created_at=datetime.datetime(2026, 8, index + 1, tzinfo=datetime.UTC),
				relevance=score,
			),
		)
		for index, (name, score) in enumerate(scores.items())
	]


def test_a_ranked_search_keeps_its_ranking_when_the_terminal_merges () -> None:
	"""**`SR#878`. The server ranked every page and the terminal threw the arrangement away.**

	`_ordering` parses `--order` against the static vocabulary, which has no `relevance` in it —
	that entry is added per request — so a search with no explicit order fell back to
	`-created_at`. Each connection came back correctly ranked and the merge re-sorted the lot
	into newest-first. Measured on the served instance: the API answered `877, 389, 444, 598,
	541` where `subroutine search` answered the same rows strictly newest-first.

	**The rows are what say a search was ranked**, which is the browser's answer to the same
	question (`mergeOrder`) rather than a second copy of the server's rule: a ranked listing
	populates `relevance` on every row and an unranked one leaves it null (`SR#875`).

	**Written oldest-first with the best match oldest**, so newest-first and best-first are
	different lists — otherwise the assertion holds under both behaviours and proves neither.
	"""

	rows = _searched(best=0.9, middling=0.5, worst=0.1)
	order = subroutine.cli.personal._merge_order(None, _gathered(rows))

	assert order[0] == ("relevance", True), f"the merge is not ranked: {order}"

	merged = subroutine.cli.personal._merged(
		_one_connection(), _gathered(rows), order=order
	)

	assert [row[1].title for row in merged] == ["best", "middling", "worst"]


def test_a_listing_that_was_not_ranked_merges_as_it_always_did () -> None:
	"""`SR#878`'s other half: nothing changes for a listing the server did not rank.

	`relevance` is null on every row of one, which is how a client tells the two apart without
	asking `/v1/meta` and inferring what the server would have done.
	"""

	rows = _searched(first=None, second=None, third=None)
	order = subroutine.cli.personal._merge_order(None, _gathered(rows))

	assert order == (("created_at", True), ("ref", False)), f"got {order}"

	merged = subroutine.cli.personal._merged(
		_one_connection(), _gathered(rows), order=order
	)

	assert [row[1].title for row in merged] == ["third", "second", "first"]


def test_an_explicit_order_still_wins_over_the_servers_ranking () -> None:
	"""`SR#878`. A reader who said how they want it arranged gets that, search or no search.

	The same rule the endpoint applies — *"an explicit `?order=` still wins"* — and the same one
	`mergeOrder` applies in the browser.
	"""

	rows = _searched(best=0.9, middling=0.5, worst=0.1)
	order = subroutine.cli.personal._merge_order("title", _gathered(rows))

	assert order == (("title", False), ("ref", False)), f"got {order}"

	merged = subroutine.cli.personal._merged(
		_one_connection(), _gathered(rows), order=order
	)

	assert [row[1].title for row in merged] == ["best", "middling", "worst"]


def test_something_can_be_made_part_of_another_thing_and_taken_back_out (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#44` at the terminal, driven rather than asserted about.

	**Both directions in one test**, because the half that was missing is not the obvious
	one: a subtask could be *created* under a parent since the beginning, and could never be
	moved out again — so a test that only nested something would pass against the defect this
	item was filed for.
	"""

	run("init")
	run("add", "Redo the kitchen")
	run("add", "Choose the tiles")

	made = run("move", "2", "--under", "1")

	assert "part of #1" in made.output

	shown = run("show", "2")

	assert "#1" in shown.output, "the parent has to be visible on the item itself"

	back = run("move", "2", "--top")

	assert "top-level" in back.output


def test_a_move_that_says_nothing_about_where_is_refused (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Neither, or both, is a refusal — never a default.

	`project move` settled this and the endpoint enforces it: guessing between "under
	something" and "to the top" is how a tree gets flattened by somebody who typed one word
	fewer than they meant to.
	"""

	run("init")
	run("add", "Something")

	for arguments in (("move", "1"), ("move", "1", "--under", "1", "--top")):
		refused = run(*arguments, expect=1)

		assert "Say where to move it" in refused.output


def test_a_repeat_can_be_set_precisely_rather_than_only_written_in_a_sentence (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#94`, Simon's direction of 2026-08-16.

	**The grammar can only ever create one.** Reading *"every 14 days"* out of a captured line
	is the fast path and stays it, but a line is typed once — so before this the only way to
	change how something came round, or to stop it, was the API. That is the half a person
	needs most: a repeat somebody set months ago is exactly the thing they later want to move.
	"""

	run("init")

	made = run("add", "Pay the rent by 2026-08-30", "--repeat", "every month on the 30th")

	assert "every month, on the 30th" in made.output

	# **`#2`, not `#1`.** Filing a repeat makes the template first and hands back the
	# occurrence, so the row a person sees is never the number they would guess — which is
	# worth a test knowing, since it is what every surface addresses.
	changed = run("update", "2", "--repeat", "every other tuesday")

	assert changed.exit_code == 0

	shown = run("show", "2")

	assert "every other week, on Tuesday" in shown.output
	assert "on the 30th" not in shown.output, "the old rule should be gone, not beside it"


def test_editing_a_repeat_from_a_script_is_refused_by_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1251`, and it is `SR#299`'s rule rather than a new one.

	The question has to be settled **before** stdin is read rather than by reading it: a
	command that blocked on a prompt would hang for ever in a CI job, in a script, or under an
	agent, on input that is not coming. So with nobody there it refuses, and names the two
	things somebody could have typed.

	**Driven with the real terminal check**, not a substitute — under a runner stdin is a pipe,
	which is exactly the state being tested. The prompt below is the half that needs a seam.
	"""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")

	refused = run("update", "2", "--title", "Morning stand-up", expect=1)

	assert "--just-this-one" in refused.output
	assert "--from-now-on" in refused.output

	# **Never *all*.** Decision `SR#1249` §2: nothing here re-derives a finished occurrence, so
	# there is no past to rewrite and the word would promise something that does not happen.
	assert "all of them" not in refused.output

	for word in FORBIDDEN:
		assert word not in refused.output.lower()

	# **And it stopped before writing anything**, which is the difference between a refusal and
	# a warning about something already done.
	assert "Morning stand-up" not in run("show", "2").output


def test_a_flag_settles_which_occurrences_an_edit_is_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The scripted answer, and the one that proves the edit reached the row that persists.

	`SR#1247`'s defect measured from the terminal: rename the occurrence, complete it, and the
	next one used to come back with the old title. Here the series is asked for, so what comes
	round next is what was typed.
	"""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")

	changed = run("update", "2", "--title", "Morning stand-up", "--from-now-on")

	assert changed.exit_code == 0
	assert "Morning stand-up" in run("show", "2").output

	# **The series itself**, which `SR#1247` made reachable by naming its number on `show`.
	assert "Morning stand-up" in run("show", "1").output


def test_only_one_of_the_two_flags_may_be_given (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Two answers to one question is a mistake worth naming rather than resolving by order."""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")

	refused = run(
		"update", "2", "--title", "Either", "--just-this-one", "--from-now-on", expect=1
	)

	assert "not both" in refused.output


def test_a_terminal_is_asked_which_occurrences_an_edit_is_for (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#1251`, and being asked is the *point* rather than a convenience.

	Decision `SR#1249` §1 is Simon's, overruling a per-field table I proposed: a rule that
	sends a title to every occurrence and a time to one is invisible, because there is no
	surface on which it could be stated where a reader would meet it. A choice made every time
	is a rule nobody has to learn.

	**One thing is substituted and it is named**: whether a terminal is attached. Everything
	below it — the prompt, what the words mean, where the write lands — is the real code, and
	the refusal above is driven with this function untouched. A harness that supplied both
	halves would confirm only the one that was not in doubt.
	"""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")

	monkeypatch.setattr(subroutine.cli.personal, "_a_terminal_is_attached", lambda: True)

	answered = run("update", "2", "--title", "Morning stand-up", input="e\n")

	assert answered.exit_code == 0
	assert "Morning stand-up" in run("show", "1").output, (
		"answering 'every one from now on' left the row that persists alone"
	)


def test_an_ordinary_edit_is_never_asked_which_occurrences_it_is_for (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Most of what anybody edits does not repeat, and the question must cost it nothing.

	**The direction a guard is least likely to be checked in.** A build that asked about every
	edit would pass every test above this one, and would put a prompt in front of somebody
	correcting a typo on a shopping list — which is friction with no decision in it.
	"""

	run("init")
	run("add", "Buy milk")

	monkeypatch.setattr(subroutine.cli.personal, "_a_terminal_is_attached", lambda: True)

	changed = run("update", "1", "--title", "Buy oat milk")

	assert changed.exit_code == 0
	assert "repeat" not in changed.output.lower()


def test_planning_a_repeat_asks_too (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""*This week I'll do the meeting at 3pm instead of 11am* is the case the story is for.

	Simon's own example, and the one only the person can answer — we have no way of knowing
	whether they want every future meeting moved or just the next one. So a *move* has to ask
	as loudly as an edit does; asking on `update` alone would leave the whole point unreachable
	from the command somebody actually types.
	"""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")

	refused = run("plan", "2", "2026-09-04", expect=1)

	assert "--just-this-one" in refused.output

	moved = run("plan", "2", "2026-09-04", "--just-this-one")

	assert moved.exit_code == 0


def test_a_repeat_can_be_stopped_from_the_terminal (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And the work in hand stays, which is the whole difference between stopping and deleting.

	**Reading it back is the assertion that matters** (`#920`): stopping completes the template
	rather than clearing a column, so a row that went on describing the rule would tell somebody
	their stop had not worked when it had.
	"""

	run("init")
	run("add", "Water the plants by 2026-08-20", "--repeat", "every 3 days")

	stopped = run("update", "2", "--repeat", "")

	assert stopped.exit_code == 0

	shown = run("show", "2")

	assert "Water the plants" in shown.output
	assert "every 3 days" not in shown.output


def test_how_a_repeat_is_measured_is_set_and_read_back_at_the_terminal (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#918` at the surface it was found from, and `#920`'s sibling question — is it *news*?

	*Every three days* is two different schedules depending on where it is measured from, so a
	flag that set it and no rendering that said so would be `#251`'s inert control: a written
	value nobody can see. **Only the non-default is said**, on `views.status_is_news`'s rule —
	a schedule anchor is what "every month on the 30th" already sounds like, so naming it would
	put a clause on every repeating row to tell the reader nothing.
	"""

	run("init")
	run(
		"add",
		"Water the plants by 2026-08-20",
		"--repeat",
		"every 3 days",
		"--repeat-from",
		"completion",
	)
	run("add", "Pay the rent by 2026-08-30", "--repeat", "every month on the 30th")

	assert "from when it is done" in run("show", "2").output

	# **The default stays quiet**, which is the half a test asserting only the first would
	# pass without: a rendering that said it always would satisfy the line above and be wrong.
	assert "from when it is done" not in run("show", "4").output

	# And it can be moved without re-sending the rule it qualifies (`#918`).
	run("update", "4", "--repeat-from", "completion")

	moved = run("show", "4")

	assert "from when it is done" in moved.output
	assert "on the 30th" in moved.output, "the rule it qualifies is untouched"


def test_saying_how_a_repeat_is_measured_without_saying_how_often_is_refused (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#918`. And the refusal names no wire field, because at a terminal there is no such flag.

	The service's message describes the *thing* rather than saying *send `recurrence`*, which
	is advice nobody here can follow — `#547`'s defect, met on the surface where the argument
	is spelled `--repeat`. The structured half still carries the field name for a caller that
	wants it.
	"""

	run("init")
	run("add", "Just the once")

	refused = run("update", "1", "--repeat-from", "completion", expect=1)

	assert "does not repeat" in refused.output

	# The *advice* names no wire field. `recurrence_anchor:` still labels the structured half
	# — unquoted, and that is the machine-readable name a script wants — but nothing in the
	# prose tells a person to send something they have no way to type.
	assert "'recurrence'" not in refused.output

	# **An empty anchor is refused too**, unlike every other clearable field: a series always
	# measures from somewhere, so there is no state to clear to — and passing it empty would
	# reach the service as *not given* and answer "Changed" having changed nothing.
	empty = run("update", "1", "--repeat-from", "", expect=1)

	assert "always measured from something" in empty.output


def test_a_title_carrying_terminal_escapes_is_printed_rather_than_obeyed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Rich neutralises its own markup and passes ANSI straight through.

	Measured before this was written: ``ESC[2K`` survives a plain string *and* a
	``rich.text.Text``, where ``BEL`` is dropped by Rich itself. So a title could clear the
	line above it, repaint what was there, or move the cursor — and every listing, every
	agenda and every ``show`` prints titles.

	**Titles arrive from other people**, which is what makes it worth doing: on a shared
	instance the text being printed was written by somebody who is not the reader, and §13.7
	merges an agenda across connections that are not even the same installation.

	Driven through the real commands rather than against the helper, because the listing does
	not print strings — it builds ``rich.text.Text`` and prints that, which is the path a check
	on the string helper would have missed entirely.

	**The row is written round the domain, and since `SR#1555` it has to be** — ``text.fit``
	refuses a control character at the door now, so ``add`` can no longer construct one. That
	is not a reason to retire this: the two guards answer different questions and only one of
	them can reach a row this instance did not write. A merged remote agenda, a row restored
	from another installation and a row written before that refusal existed are all exactly
	this state, and they are the population the paragraph above names. Writing it directly is
	closer to the subject than ``add`` ever was.
	"""

	import sqlalchemy
	import sqlalchemy.orm

	import subroutine.db.session

	run("init")
	run("add", "Buy milk DANGER")

	engine = subroutine.db.session.create_engine(
		subroutine.config.load_settings().database_url
	)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			session.execute(
				sqlalchemy.update(subroutine.db.models.work.Task).values(
					title="Buy milk \x1b[2K\x1b[1;31mDANGER\x1b[0m"
				)
			)
			session.commit()

	finally:
		engine.dispose()

	for command in (("list",), ("agenda",), ("show", "1")):
		printed = run(*command).output

		assert "\x1b[2K" not in printed, f"'{' '.join(command)}' passed an escape through"
		assert "DANGER" in printed, f"'{' '.join(command)}' lost the text around it"

	# And the refusal path, which prints what the caller typed back at them.
	refused = run("show", "\x1b[2K999", expect=1)

	assert "\x1b[2K" not in refused.output


def test_a_change_line_names_no_column_and_no_table (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`subroutine changes` printed the schema at somebody keeping a to-do list.

	Two leaks, both §13.5b's forbidden vocabulary arriving through the one command that
	renders a row it did not shape. The rows ``init`` writes have no item to name, so the line
	fell back to the entity type — ``created  workspace_member``, ``created  workspace`` — and
	a deferred task read ``updated  #2 Water the plants  (snoozed_is_all_day, snoozed_until)``,
	which is two column names for one fact.

	Driven rather than read: these come out of `init` itself and of ordinary commands, so the
	transcript is what a person meets on their second day.
	"""

	# Everything a test writes is younger than the feed's watermark, so without this the
	# transcript is "Nothing new." and the assertions below pass by reading an empty screen.
	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))

	run("init")
	run("add", "Water the plants")
	run("defer", "1", "tomorrow")
	run("start", "1")

	transcript = run("changes").output

	for word in FORBIDDEN:
		assert word not in transcript.lower(), transcript

	assert "when it comes back" in transcript, "the defer is still reported, in words"
	assert "your account" in transcript and "this list" in transcript


def test_every_column_an_event_can_name_reads_as_words (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Derived from the models, because the two that leaked were found by looking at a screen.

	An event's ``changes`` names whatever column the service moved, so the set of things this
	line can print is the set of columns on the three item models. Each is rendered and asked
	whether a person setting up a to-do list would meet a word §13.5b says they must not —
	which is what makes a column added tomorrow fail here rather than on somebody's terminal.

	It also refuses a rendering that is still plainly a database name, because *not forbidden*
	is a lower bar than *readable* and the ``_id`` suffix is the tell.
	"""

	columns = {
		column.name
		for model in (
			subroutine.db.models.work.Task,
			subroutine.db.models.work.Document,
			subroutine.db.models.project.Project,
		)
		for column in model.__table__.columns
	}

	assert len(columns) > 40, f"only {len(columns)} columns were found"

	unreadable = []

	for name in sorted(columns):
		words = subroutine.views.field_in_words(name)

		if (
			any(word in words.lower() for word in FORBIDDEN + OUR_WORD_NOT_THEIRS)
			or words.endswith(("_id", " id"))
		):
			unreadable.append(f"{name} → {words!r}")

	assert not unreadable, (
		"These columns would be printed to somebody keeping a to-do list as they are named in "
		"the database: " + ", ".join(unreadable) + ". Give each a phrase in _A_CHANGE_TO."
	)


def test_a_local_token_bounds_the_work_commands_and_says_what_it_does_not (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``explain scripting`` promised a boundary the command line cannot hold.

	It said that setting ``SUBROUTINE_TOKEN`` locally meant *"the same limits then apply here
	as would apply over the network"*. Measured: a ``--scope task:read`` token really does
	stop ``add``, and ``db backup`` — a complete copy of every workspace — runs anyway,
	because the ``db`` group opens the database directly so that it works when the service
	will not start.

	**The defect is the promise, not the missing check.** Anybody who can run these commands
	can read ``config.toml``, find the database and open it themselves, so no check here could
	make the sentence true. The page says what is true now and names what does hold it: a
	server between them and the file.
	"""

	run("init")

	issued = run("token", "create", "--title", "readonly", "--scope", "task:read").output
	monkeypatch.setenv(
		"SUBROUTINE_TOKEN", next(word for word in issued.split() if word.startswith("sr_"))
	)

	assert "task:write" in run("add", "Nope", expect=1).output, "the work commands do obey it"
	assert run("db", "backup").exit_code == 0, "and the file-level ones cannot"

	said = run("explain", "scripting").output

	assert "db backup" in said, "so the page names the exception"
	assert "same limits" not in said, "rather than the promise it could not keep"


def test_a_listing_says_where_each_item_lives (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#512`, narrowed by decision `#957`: the label is the whole address.

	A key stopped naming one project with `#958`, so a column reading `dist` would say
	nothing about where an item is. What is on the row is what somebody can type back into
	``--project``.
	"""

	run("init", "--username", "si", "--workspace", "projects")
	run("project", "create", "substation", "Substation")
	run("project", "create", "websites", "Websites")
	run("project", "create", "dist", "Packaging", "--parent", "substation")
	run("project", "create", "dist", "Deploys", "--parent", "websites")
	run("add", "Ship the wheel +substation/dist")
	run("add", "Fix the site +websites/dist")

	listed = run("list").output

	assert "substation/dist" in listed
	assert "websites/dist" in listed


def test_a_listing_leaves_out_what_the_request_already_said (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The middle step of the rule: full address, **strip what was asked for**, then §12.2a.

	Somebody who has just typed ``--project substation`` does not need telling that every row
	is in ``substation``; what they want to know is which part of it.
	"""

	run("init", "--username", "si", "--workspace", "projects")
	run("project", "create", "substation", "Substation")
	run("project", "create", "dist", "Packaging", "--parent", "substation")
	run("project", "create", "tools", "Tools", "--parent", "substation")
	run("add", "Ship the wheel +substation/dist")
	run("add", "Sharpen it +substation/tools")

	listed = run("list", "--project", "substation").output

	assert "dist" in listed and "tools" in listed
	assert "substation/" not in listed, "the segment the request named is still on every row"


def test_a_shopping_list_says_nothing_about_projects (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a applied to the **remainder**, which is what keeps §1.4's reader untouched.

	`#512`'s 2026-08-05 decision, unchanged: Simon chose consistency with the uniform-column
	rule over showing a new reader where things go. Everything is in the Inbox, the remainder
	is the same word on every line, and the column does not earn its place.

	**Two rows, because a one-row page has no distinct values to compare** — `_column` drops
	every column on it, so a single "buy milk" would pass this whichever way the rule went.
	"""

	run("init", "--username", "si", "--workspace", "projects")
	run("add", "Buy milk")
	run("add", "Call the dentist")

	listed = run("list").output

	assert "Buy milk" in listed and "Call the dentist" in listed
	assert "inbox" not in listed, "a to-do list is unchanged by this"


class _Filed(typing.NamedTuple):
	"""The one field a project label reads off a row."""

	project_path: str


def test_a_mixed_page_keeps_every_whole_address (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""With no filter there is nothing to strip, so every row says where it is in full."""

	run("init", "--username", "si", "--workspace", "projects")
	run("project", "create", "substation", "Substation")
	run("project", "create", "dist", "Packaging", "--parent", "substation")
	run("add", "Ship the wheel +substation/dist")
	run("add", "Ship the milk")

	listed = run("list").output

	assert "substation/dist" in listed
	assert "inbox" in listed


@pytest.mark.parametrize(
	("path", "within", "expected"),
	[
		("substation/dist", "substation", "dist"),
		("substation", "substation", ""),
		("ui-things/x", "ui", "ui-things/x"),
		("substation/dist", "", "substation/dist"),
		("", "substation", ""),
	],
)
def test_a_label_is_only_shortened_at_a_segment_boundary (
	path: str, within: str, expected: str
) -> None:
	"""**Because ``removeprefix`` on an address is otherwise wrong in general.**

	``--project ui`` against a row in ``ui-things/x`` would leave ``-things/x``, which is not
	an address of anything. Nothing supported reaches that — a filtered listing is narrowed to
	the subtree by the server, so every row really is inside — which is exactly why it is one
	condition rather than a comment saying it cannot happen.

	**This is a unit test and the four above are not**, deliberately: the case it covers is
	unreachable through the command line, so driving one could only ever assert the cases that
	are.
	"""

	# A stand-in rather than a whole `views.Task`, because the cell reads one field and
	# constructing thirty to prove that would say the opposite of what this is testing.
	row = typing.cast(subroutine.views.Task, _Filed(project_path=path))

	assert subroutine.cli.personal._project_cell(row, within) == expected


def test_an_item_says_where_it_lives_when_it_is_read (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A reader who learns an address from a listing looks for it on the item."""

	run("init", "--username", "si", "--workspace", "projects")
	run("project", "create", "substation", "Substation")
	run("project", "create", "dist", "Packaging", "--parent", "substation")
	run("add", "Ship the wheel +substation/dist")

	assert "substation/dist" in run("show", "1").output


def test_a_scripted_row_carries_the_whole_address (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""And is never shortened, because a script has no page to be uniform across.

	The terminal's column is a layout rule; ``--json`` is a contract, and the form it carries
	is the one that goes straight back into ``--project``.
	"""

	run("init", "--username", "si", "--workspace", "projects")
	run("project", "create", "substation", "Substation")
	run("project", "create", "dist", "Packaging", "--parent", "substation")
	run("add", "Ship the wheel +substation/dist")

	rows = json.loads(run("list", "--json", "--project", "substation/dist").output)

	assert [row["project_path"] for row in rows] == ["substation/dist"]


def _agendas (*zones: tuple[str, str]) -> typing.Any:
	"""Wrap one agenda per connection, each counting its day in the zone it was given."""

	return subroutine.fanout.Gathered(
		answers=tuple(
			subroutine.fanout.Answer(
				connection=subroutine.connections.Connection(name=name, url=None),
				value=subroutine.views.Agenda(
					date=datetime.date(2026, 11, 5),
					timezone=zone,
					overdue=[],
					today=[],
					upcoming=[],
					unscheduled=[],
					unscheduled_total=0,
				),
			)
			for name, zone in zones
		),
		failures=(),
	)


def test_connections_counting_different_days_are_said_rather_than_resolved () -> None:
	"""`SR#995`, and the half that replaced sending one zone to everybody.

	The old behaviour made the answer consistent by making it wrong: it sent the **typing
	machine's** zone, so somebody with a work profile on America/New_York and a personal one on
	Europe/London got a third day matching neither, and nothing said so. Each instance resolves
	the reader's own zone now, so the answers can genuinely be about different days — which is
	the truth of the arrangement, and the person is the only one who can settle it.
	"""

	said: list[str] = []
	program = subroutine.cli.personal.Program(
		say=lambda text: pytest.fail(f"this belongs on stderr, not in the agenda: {text}"),
		fail=lambda error: pytest.fail(f"nothing here ends the command: {error}"),
		stop=lambda *arguments: pytest.fail("nor stops it"),
		settings=subroutine.config.Settings,
		console=rich.console.Console(),
		warn=said.append,
		mask=lambda text: text,
		selected=subroutine.cli.personal.Selected(),
	)

	subroutine.cli.personal._report_zones(
		program, _agendas(("work", "America/New_York"), ("personal", "Europe/London"))
	)

	assert said, "a merge of two different days says so"
	assert "America/New_York" in said[0] and "Europe/London" in said[0]
	assert "work" in said[0] and "personal" in said[0], (
		"which connection is in which zone, since the remedy is per account"
	)


def test_work_dated_in_another_zone_is_said_rather_than_left_to_puzzle_over (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1039`. Two correct rules meeting on one line and contradicting each other.

	`SR#773` renders a day-scale date in the **task's** own zone, because re-rendering a day
	through another zone makes it a *different day*. `SR#989` buckets in the **reader's**,
	because a person's agenda is about their own day. So a deadline set for the end of
	somebody's UTC day falls 59 minutes past the end of a London reader's, and the row says
	*due Thu 20 Aug* under a heading that means *not today*.

	**Found by using the product**, on the day a second human first dated something on this
	project's own instance — which is `SR#589`. Fifteen items dated the same day, fourteen
	under Today and one under Next 7 days, every one rendering the same words; I spent an hour
	diagnosing it as a write-path defect before measuring who had set the date.

	Neither rule is reversed here. The disagreement is *said*, which is `_report_zones`'s shape
	one level down: that one reports two connections counting different days, this one reports
	two people in one workspace who are.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Dated somewhere else")
	run("update", "1", "--due", "today", "--timezone", "Australia/Sydney")

	shown = run("agenda").output

	assert "Australia/Sydney" in shown, (
		f"a date set in another zone is rendered in it and bucketed in another, silently: "
		f"{shown}"
	)


def test_work_dated_in_the_readers_own_zone_says_nothing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half, and it is what keeps the line from being noise on every agenda.

	One person, one zone, is the ordinary case and by far the commonest — this instance ran for
	three weeks before it could produce the disagreement at all. A message on every read would
	teach the reader to skim exactly the surface `SR#1005` is about.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "Dated here")
	run("update", "1", "--due", "today")

	shown = run("agenda").output

	assert "Dated here" in shown, "the probe put nothing on the agenda, so it proves nothing"
	assert "your day is" not in shown, f"one person in one zone was warned about it: {shown}"


def test_connections_agreeing_about_the_day_say_nothing () -> None:
	"""The other half, and it is what keeps the line from being noise.

	**Keyed on the zones rather than on the dates**, deliberately: two zones are on the same
	date for part of every day, so a warning keyed on the dates would appear and disappear
	under the reader while nothing changed — `SR#966`'s recorded failure, where a message whose
	trigger is a coincidence reads as a fault in the program.
	"""

	said: list[str] = []
	program = subroutine.cli.personal.Program(
		say=said.append,
		fail=lambda error: pytest.fail(f"nothing here ends the command: {error}"),
		stop=lambda *arguments: pytest.fail("nor stops it"),
		settings=subroutine.config.Settings,
		console=rich.console.Console(),
		warn=said.append,
		mask=lambda text: text,
		selected=subroutine.cli.personal.Selected(),
	)

	subroutine.cli.personal._report_zones(
		program, _agendas(("work", "Europe/London"), ("personal", "Europe/London"))
	)

	assert not said, f"two connections in one zone are one day: {said}"


def test_the_scripted_agenda_says_which_zone_each_connection_counted_in () -> None:
	"""`SR#995`'s other reader. The rendered path says it in words; this is for a script.

	The two scalars beside it are the *first* answer's, which is the whole truth only while
	these agree — so a script merging several instances has to be able to tell, and comparing
	one field is how.
	"""

	said = subroutine.cli.personal._agenda_json(
		_one_connection(), _agendas(("work", "America/New_York"), ("personal", "Europe/London"))
	)

	assert said["timezones"] == {
		"work": "America/New_York", "personal": "Europe/London",
	}
	assert said["timezone"] == "America/New_York", "the scalar is the first answer's, as before"


def test_show_survives_an_instance_that_cannot_answer_what_refers_to_this (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#250`'s skew shape, met within a minute of building `#144` and fixed in the same hour.

	The program and the instance upgrade separately, and **upgrading the program first is the
	ordinary order** — so a `show` that failed outright because one of its five sections is
	newer than the server would break the commonest command over the newest one. Measured
	against the served instance one commit behind: every `subroutine show` answered *There is
	nothing at /v1/tasks/144/backlinks*.

	**A missing route is not a missing item**, and the item was resolved before the section is
	asked for — so a `not_found` here can only be the route.

	Only that one refusal is swallowed. Anything else still reaches the reader, or a broken
	instance would read as an item with nothing referring to it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("add", "The specification")

	def older (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
		"""Answer the way an instance without the route does."""

		raise subroutine.errors.NotFound("There is nothing at /v1/tasks/1/backlinks.")

	monkeypatch.setattr(subroutine.clients.local.Client, "backlinks", older)

	shown = run("show", "1").output

	assert "The specification" in shown, f"one absent section took the whole page: {shown}"
	assert "Referred to by" not in shown

	# **Only that one refusal**, or a broken instance reads as an item nothing refers to. A
	# catch of `SubroutineError` passes the half above and fails here.
	def broken (*_arguments: typing.Any, **_keywords: typing.Any) -> typing.Any:
		"""Answer the way an instance in trouble does."""

		raise subroutine.errors.InternalError("The database is unreachable.")

	monkeypatch.setattr(subroutine.clients.local.Client, "backlinks", broken)

	assert run("show", "1", expect=1).exit_code == 1, (
		"an instance that could not answer was reported as one with nothing to say"
	)


def test_a_document_can_say_which_one_it_replaces (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1144`, at the terminal — the flag is its own code path.

	The client-level guard proves both transports carry it; this proves the option that reaches
	them exists and is wired to the right argument. Driving a command says nothing about its
	flags, which is how a capability comes to be reachable in a library and not by anybody.

	**Both outcomes asserted**, because only one of them can be had by hand: setting the old
	document's status moves it and leaves the chain empty, and *what replaced this* then has no
	answer for ever afterwards.
	"""

	run("init")
	run("doc", "create", "How we deploy", "--body", "First answer.", "--type", "decision")
	run("doc", "create", "How we deploy now", "--body", "Second answer.", "--type", "decision")

	run("doc", "edit", "2", "--supersedes", "1")

	old = run("show", "1").output

	assert "superseded" in old.lower(), f"the replaced decision was not retired:\n{old}"

	# **And an ordinary edit leaves the chain alone**, which is what stops the flag's default
	# quietly superseding something on every revision — the failure this could most easily
	# have introduced.
	run("doc", "create", "Something else", "--type", "decision")
	run("doc", "edit", "3", "--title", "Something else entirely")

	untouched = run("show", "3").output

	assert "superseded" not in untouched.lower(), untouched


def test_revising_a_document_takes_any_of_its_flags_on_its_own (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1201`. `SR#299`'s rule was right and its list was hand-written.

	Standard input is consulted only when nothing else was said, because an empty pipe cannot be
	told from no pipe without blocking. That question was settled against a tuple of four values,
	and two flags were added to this command afterwards — `--tag`, and `--supersedes` in the
	change that found this — so each was refused when used alone.

	**Every flag, not the two that were missing.** A test naming the two would be the same
	hand-written list one layer up, and would pass on the day a third arrives.
	"""

	run("init")
	run("doc", "create", "A conclusion", "--body", "Text.", "--type", "decision")
	run("doc", "create", "Another", "--body", "Text.", "--type", "decision")

	alone = (
		("--title", "Retitled"),
		("--type", "note"),
		("--status", "draft"),
		("--tag", "ops"),
		("--supersedes", "1"),
	)

	for flag, value in alone:
		# Each on its own, with nothing piped — which is what a script, a CI job and an agent
		# shelling out all have, and is the case that was refused.
		result = run("doc", "edit", "2", flag, value)

		assert result.exit_code == 0, (
			f"'doc edit 2 {flag} {value}' alone was refused:\\n{result.output}"
		)

	body = run("show", "2").output

	assert "Text." in body, "a flag-only edit must leave the body alone"


def test_the_hint_for_an_empty_pipe_names_every_flag_that_command_takes () -> None:
	"""The other copy of the same list, and the one a caller actually reads — `SR#1201`.

	It named five options and omitted ``--tag``, so somebody who had just used that flag was
	told to try something else. Derived from the command Typer registered rather than from a
	second list here, which would be the defect wearing a third hat.
	"""

	# **Typed loosely and reached through `get_command`**, which is `tests/test_cli_help`'s
	# recorded lesson: Typer vendors its own click shim, so what comes back is a private
	# `typer._click.core.Command` that is not a `click.Command`. A walk that asked
	# `isinstance(x, click.Group)` once reported clean having read one command in forty-eight.
	node: typing.Any = typer.main.get_command(subroutine.cli.main.app)

	for word in ("doc", "edit"):
		node = node.get_command(click.Context(node, info_name="subroutine"), word)

		assert node is not None, f"'doc edit' is not registered — no {word!r}"

	edit = node
	options = {
		name
		for parameter in edit.params
		for name in getattr(parameter, "opts", ())
		if name.startswith("--")
	}
	# The ref is an argument, and these are about the *output* rather than the content — so
	# offering them to somebody who piped nothing in would be answering a different question.
	# `--no-body` joined them with `#1360`, and having to classify it here is this guard
	# working: a flag that changes nothing must not be advertised as a way to change something.
	options -= {"--json", "--no-body", "--help"}

	assert len(options) >= 6, f"only {len(options)} options were found: {sorted(options)}"

	# **Anchored on the hint itself, not on the message above it.** Slicing from the message
	# caught the comment explaining the hint instead — which mentions two of the flags, so the
	# guard reported the other six missing and would have reported none missing had the comment
	# been longer. A scan over source is only as good as what it is pointed at.
	source = pathlib.Path(subroutine.cli.personal.__file__).read_text(encoding="utf-8")
	start = source.index("Pipe the new text in, or pass")
	hint = source[start : source.index(",\n", start + 200)]

	missing = sorted(name for name in options if name not in hint)

	assert not missing, (
		f"the refusal tells a caller what to pass and does not mention {missing}. "
		f"A flag they may have just used is the one most likely to be missing from it."
	)


def test_a_span_written_as_two_bare_days_is_planned_as_one_pair (
	run: typing.Callable[..., typer.testing.Result],
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#1557`. `plan friday --until monday` on a Saturday finished before it began.

	A bare day means *the soonest such one counting today*, which is right for a single date
	and inverts a span: on a Saturday `friday` is six days off and `monday` is two, so the
	most ordinary way to write a long weekend was refused with *"It cannot finish before it
	starts"* — blaming the reader for an ordering they wrote correctly, and naming neither day
	it had derived.

	**Driven rather than asked, because this surface has its own resolver.** ``_day`` resolves
	both ends here, before any client is called, so the rule cannot be applied where the two
	dates arrive as strings — the domain refuses a bare day name outright and is handed two
	dates that already disagree. A unit test of the rule passes whether or not anything calls
	it; only this says the wiring is there.

	**The clock is pinned**, because the defect moves through the calendar and an unpinned test
	would pass on most days. 29 August 2026 is a Saturday.
	"""

	monkeypatch.setattr(
		subroutine.db.types, "utcnow", lambda: datetime.datetime(2026, 8, 29, 10, 0, tzinfo=datetime.UTC)
	)

	run("init")
	run("add", "A long weekend")

	planned = run("plan", "1", "friday", "--until", "monday")

	assert "Fri 4 Sep" in planned.output, (
		f"the start is not the Friday this was counted from:\n{planned.output}"
	)

	shown = run("show", "1")

	assert "Fri 4 Sep to Mon 7 Sep" in shown.output, (
		f"the Monday was read against today rather than against the Friday:\n{shown.output}"
	)


def test_planning_a_span_on_a_timed_item_refuses_without_advising_the_impossible (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1329`. The refusal was right and its hint named two things this surface cannot do.

	``plan <day> --until <day>`` on a row whose start carries a time reaches ``check_span``'s
	shape refusal, whose hint is *"Give both ends a time, or give both a date with no time."*
	That is good advice **over HTTP** and neither half is reachable from a terminal: nothing
	here writes a time onto an end, and nothing here takes the clock off a start — ``plan`` is
	the only writer of ``starts_at`` and now preserves whatever clock is there, and
	``subroutine update`` has no ``--starts``.

	So a person was told to do one of two things and could do neither, which is `SR#1322`'s own
	finding — *following the advice confirmed the false statement* — met in the first refusal
	written after it.

	**Driven on an ordinary task**, no ``--type event`` and no repeat, because the release note
	described this as affecting *a repeat* and it affects everything with a time on its start.
	"""

	run("init")
	run("add", "Fix the parser on 2026-12-01 at 11:00")

	before = json.loads(run("show", "1", "--json").output)["item"]

	assert before["starts_is_all_day"] is False, "the fixture is not a timed ordinary task"

	refused = run("plan", "1", "2026-12-02", "--until", "2026-12-05", expect=1)

	assert "starts at a time" in refused.output, (
		f"the refusal does not name the thing that is in the way:\n{refused.output}"
	)

	# **The property is about what it does *not* say.** Either sentence sends a reader to a
	# surface they are not on, and the reason to check both is that they fail in opposite
	# directions — one asks for a capability, the other for a removal.
	assert "Give both ends a time" not in refused.output, (
		f"the hint still asks for a timed end, which nothing here can write:\n{refused.output}"
	)
	assert "date with no time" not in refused.output, (
		f"the hint still asks for the clock to come off, which nothing here can do:"
		f"\n{refused.output}"
	)

	after = json.loads(run("show", "1", "--json").output)["item"]

	assert after["starts_at"] == before["starts_at"], "a refused command moved the start anyway"
	assert after["ends_at"] is None, "a refused command set the end anyway"


def test_planning_a_timed_item_confirms_the_o_clock_it_kept (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1330`. The command that stopped destroying a time still would not say it.

	``plan``'s confirmation was the last date rendering in this file going through
	``_render_date``, so it printed *Starts Wed 2 Dec* — **byte for byte what it printed while
	it was throwing the 11:00 away**, which is the silence `SR#1299` was filed about. A fix
	whose output is identical to the defect's teaches nobody that anything changed.

	**And it is written down.** ``--because`` records that sentence as a comment on the item,
	where it outlives the session and is read by whoever asks what happened.
	"""

	run("init")
	run("add", "Doctor's appointment on 2026-12-01 at 11:00", "--type", "event")

	planned = run("plan", "1", "2026-12-02", "--because", "the surgery moved it")

	assert "11:00" in planned.output, (
		f"the confirmation does not say the time the command has just kept:\n{planned.output}"
	)

	shown = run("show", "1").output

	assert "11:00" in shown, f"the recorded reason dropped the o'clock too:\n{shown}"


def test_the_repeat_refusal_points_at_the_occurrence_whatever_the_caller_was_doing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1331`. The hint was ``delete``'s and five verbs raise it.

	``_in_the_trash_too`` is how ``delete``, ``link``, ``discard``, ``undiscard`` and ``move``
	resolve a ref, so a ref naming a recurrence template refuses through one message for all of
	them — and it read *"Stop it with 'subroutine done 2'"*. Somebody drawing a link between
	two items was advised to complete a series.

	Naming the row is right and is `SR#1322`'s improvement; the remedy has to be one that is
	true whatever the caller came to do.
	"""

	run("init")
	run("add", "Water the plants every monday")
	run("add", "Something to link it to")

	# **Found rather than assumed.** A repeat is two rows and which ref each gets is an
	# allocation detail; asserting one here would make this test about that instead.
	rows = {
		ref: json.loads(run("show", str(ref), "--json").output)["item"] for ref in (1, 2, 3)
	}
	series = next(ref for ref, row in rows.items() if row.get("is_template"))
	ordinary = next(
		ref
		for ref, row in rows.items()
		if not row.get("is_template") and row["title"] == "Something to link it to"
	)

	refused = run("link", str(ordinary), "blocks", str(series), expect=1)

	assert "the repeat itself" in refused.output, (
		f"the refusal no longer names what the row is:\n{refused.output}"
	)
	assert "subroutine list" in refused.output, (
		f"nothing points at the row the caller can actually act on:\n{refused.output}"
	)


def test_the_dates_topic_names_the_one_command_that_refuses_a_timestamp (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1332`. ``explain dates`` said the whole list works here, and one row stopped working.

	The page opens *"Everything below works at the command line"* and lists ``a time
	2026-08-01T17:00:00Z``. Since `SR#1299` ``plan`` refuses a written time — deliberately,
	because it names a day — while ``defer`` and ``update --due`` still take one. So the
	vocabulary is no longer uniform across the commands, and this page was the only place
	saying that it is.

	**Driven rather than read.** Asserting the sentence changed would pass against a page that
	says anything at all; what makes this a guard is that both halves of the claim are
	exercised against the real commands, so the page and the product cannot drift apart
	without one of the three assertions failing.
	"""

	run("init")
	run("add", "Fix the parser")

	topic = run("explain", "dates").output

	assert "2026-08-01T17:00:00Z" in topic, "the timestamp row has gone; this guard is stale"
	assert "plan" in topic, (
		f"the dates topic does not say which command refuses a time of day:\n{topic}"
	)

	# The half that still works, so the page is not being made to under-promise instead.
	run("defer", "1", "2026-12-05T17:00:00Z")

	assert json.loads(run("show", "1", "--json").output)["item"]["snoozed_until"] is not None, (
		"'defer' stopped taking a timestamp, so the page's general claim needs re-reading"
	)

	refused = run("plan", "1", "2026-12-05T17:00:00Z", expect=1)

	assert "time of day" in refused.output, (
		f"'plan' no longer refuses a timestamp, so this page's exception is stale:"
		f"\n{refused.output}"
	)


def test_making_a_project_private_says_that_nothing_can_share_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1444`, at the one moment it can be acted on.

	**A private project is visible to its owner and to nobody else, permanently.** §7.3a grants
	sight to holders of a ``project_member`` row, and the only writers are ``projects.create``
	for the owner and ``projects._ensure_member`` when ownership changes — no route, no command,
	no tool adds a second person. Driven on a scratch instance before this was written: a
	colleague's ``project list`` shows the Inbox and not the private project.

	**The flag's own help said "Only its members can see it"**, which is true and reads as an
	invitation to add somebody. It names a set that cannot grow.

	**The remedy is in the sentence rather than after it.** A reader who has just been told a
	thing is invisible needs the way back more than they need the reason, and without it the
	line reads as a refusal of something that in fact succeeded.
	"""

	run("init")

	said = run("project", "create", "secret", "Secret", "--private").output

	assert "Only you can see it" in said
	assert "--public" in said


def test_an_ordinary_project_says_nothing_about_who_can_see_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The other half, and §1.4 is why it is a test rather than an obvious omission.

	A public project is what somebody with a to-do list makes, and a sentence about visibility
	on it would introduce a concept they never asked about — the rule that a field nobody set is
	not printed, applied to a default nobody chose.
	"""

	run("init")

	said = run("project", "create", "web", "Web").output

	assert "Only you can see it" not in said
	assert "Created web" in said


def test_the_line_the_browser_suggests_is_one_a_new_installation_can_run (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1545`. The capture box's own example was a line the product refused.

	`+work` named a project nothing ships. A fresh `init` gives one workspace and one project —
	the Inbox — so *"try: call the dentist tomorrow +work !4/3"* answered *"There is no project
	'work' here"*, on the first screen a new user meets. The refusal is good; being handed a
	failing example by the product itself is not.

	**Driven through the terminal rather than the browser, and that is the point.** Both reach
	one capture grammar, so a fresh install is the cheapest honest place to ask *does this line
	work*. What is being guarded is the **suggestion**, and the suggestion is a string in
	`app.js` — so it is read from there rather than copied, or this becomes two constants that
	agree until one moves.

	**Two assertions, because acceptance is not enough.** Anything the grammar cannot read stays
	in the title verbatim (§6.13 rule 1), so a `+inbox` that stopped resolving would still be
	*added* — with the sigil sitting in the title and nothing failing. The echo is what says the
	project was read, and `SR#1438` is why the echo says it at all.
	"""

	source = (
		pathlib.Path(__file__).resolve().parent.parent
		/ "src" / "subroutine" / "web" / "assets" / "app.js"
	).read_text(encoding="utf-8")

	declared = re.search(r'export const CAPTURE_HINT = "([^"]+)";', source)

	assert declared is not None, "the capture box no longer says what can be typed into it"

	hint = declared.group(1)
	_, _, suggested = hint.partition("try: ")

	assert suggested, f"the placeholder {hint!r} no longer suggests a line to try"

	run("init")

	added = run("add", suggested)

	assert "Added" in added.output, (
		f"the browser suggests {suggested!r} and a fresh installation refuses it:\n"
		f"{added.output}"
	)

	# **The project was read, not merely tolerated.** `(read +inbox …)` is the echo naming what
	# the grammar took; without it the sigil would be sitting in the title of a task that filed
	# perfectly well into the Inbox by default, which looks identical from the exit code.
	assert "read +inbox" in added.output, (
		f"'{suggested}' was accepted but '+inbox' was not read as the project — it is in the "
		f"title instead:\n{added.output}"
	)


def test_every_example_on_the_add_page_is_one_a_new_installation_can_run (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`SR#1556`. Two of six could not run, and they were the two the prose exists to teach.

	`--due` is not an option of `add` at all — a date is written in the captured line, as the
	other examples do — so the rent line exited 2 suggesting `--under` instead. And the plants
	line supplied no date, which `every 3 days` cannot name for itself, so the service refused
	it. Correctly, and with a good message: the fault was the example. Those two sit directly
	above the paragraph that exists to explain them, `--repeat-from schedule` against
	`--repeat-from completion`, so the broken pair was the load-bearing pair on the command a
	new user types first.

	**Read off the command object, never out of rendered help.** `typer.rich_utils` styles an
	option name in parts when it believes it is writing to a terminal, so a scan of the rendered
	page finds `--project` on a laptop and not on a CI runner — `SR#1537`, which cost four wrong
	hypotheses before anybody measured it. The docstring the page is built from has no such
	problem.

	**Driven rather than parsed, which is the whole of it.**
	`test_help_leads_with_examples` already asserts that a page *leads with* examples; that is a
	claim about layout, and both defects here satisfied it perfectly. Nothing anywhere asked
	whether an example works, and neither is findable by reading.

	**Scoped to `add` deliberately.** Sixty-nine help pages carry 158 example lines, and most
	cannot be driven blind: they delete workspaces, remove people, mint credentials or name refs
	that do not exist. Covering them needs a register of what is safe with a reason each, which
	is `SR#1570`. This page is the one a new installation meets first.
	"""

	root = typing.cast(typing.Any, typer.main.get_command(subroutine.cli.main.app))
	page = root.get_command(click.Context(root, info_name="subroutine"), "add")

	examples = [
		line.strip() for line in re.findall(r"^\s*(subroutine .+)$", page.help or "", re.M)
	]

	# **A floor, because a scan that reads nothing reports the same empty list as a clean one.**
	# The regex is over a docstring, so a reformatting that indents differently or a rename of
	# the command would leave this walking an empty page and passing.
	assert len(examples) >= 4, (
		f"only {len(examples)} examples were read off the 'add' page, so this is checking "
		f"almost nothing: {examples}"
	)

	run("init")

	for example in examples:
		arguments = shlex.split(example)

		assert arguments[0] == "subroutine", f"{example!r} is not a command line"

		# `expect=0` is the assertion. The runner reports the exit code, the output and the
		# exception together, which is what tells a reader whether the page or the product is
		# wrong — the rent line failed at argument parsing and the plants line inside the
		# service, and those want opposite fixes.
		run(*arguments[1:])
