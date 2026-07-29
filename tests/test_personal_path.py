"""The personal path end to end — SPEC.md §13.5b, run as a test rather than asserted.

The gating criterion is four commands on a fresh installation, and **none of their output
mentioning a workspace, a status, a project, a criterion, a verification, a session or a
claim**. That vocabulary check is the guard on §1.4's progressive-disclosure rule, and it
is meant to fail the first time somebody adds a required field for an agent's benefit.

These run the real CLI against a real database in a temporary XDG home, because the parts
most likely to break are the ones only the wiring exercises: the config file, the state
directory, the local-mode principal, and the numbering that makes ``done 1`` work.
"""

import json
import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.domain.capture
import subroutine.domain.dates

#: SPEC.md §13.5b, verbatim. A person setting up a to-do list has not asked about any of
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

	# Local mode must not pick up a token from whatever shell the suite was started in.
	monkeypatch.delenv("SUBROUTINE_TOKEN", raising=False)
	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	yield tmp_path


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Return a runner for the real CLI, failing loudly on an unexpected non-zero exit."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0, input: str | None = None) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments), input=input)

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


def test_the_four_command_personal_test (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§13.5b: a fresh installation to a completed task, in four commands, no documentation."""

	first = run("init")

	assert first.output.strip() == 'Ready. Try: subroutine add "something to do"'

	second = run("add", "Call the dentist before Sunday")

	assert "Added: Call the dentist" in second.output

	third = run("today")

	assert "Call the dentist" in third.output

	fourth = run("done", "1")

	assert "Done: Call the dentist" in fourth.output

	# And it is gone from the list afterwards, which is the whole point of the fourth
	# command.
	assert "Call the dentist" not in run("today").output


def test_no_command_in_the_personal_path_mentions_the_full_model (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The guard on SPEC.md §1.4, and the one meant to fail when somebody forgets it.

	Every word here names something a person setting up a to-do list has not asked about.
	The moment one appears, the personal path has stopped being a personal path.
	"""

	run("init")
	run("add", "Call the dentist before Sunday")
	run("add", "Buy milk")

	transcript = "\n".join(
		run(*command).output
		for command in (("today",), ("ls",), ("done", "1"), ("plan", "1", "tomorrow"))
	)

	for word in FORBIDDEN:
		assert word not in transcript.lower(), f"the personal path said {word!r}:\n{transcript}"


def test_a_bare_invocation_shows_the_agenda_rather_than_a_help_wall (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""SPEC.md §12.2a: the first thing this tool does unprompted should be useful."""

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
	assert "subroutine today" in run("add", "Buy milk").output
	assert "subroutine done" in run("today").output
	assert "subroutine today" in run("done", "1").output


def test_an_empty_list_says_what_to_do_about_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A blank screen is a dead end; the remedy costs one line."""

	run("init")

	assert 'subroutine add "something to do"' in run("today").output
	assert 'subroutine add "something to do"' in run("ls").output


def test_positions_from_the_last_listing_address_tasks (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The difference between a to-do list you use and one you type identifiers into."""

	run("init")
	run("add", "First")
	run("add", "Second")

	# The numbering is the order printed, so read it back rather than assuming it.
	numbered = {
		line.split(maxsplit=1)[0]: line.split(maxsplit=1)[1].strip()
		for line in run("today").output.splitlines()
		if line.strip()[:1].isdigit()
	}

	assert set(numbered) == {"1", "2"}

	run("done", "2")

	remaining = run("today").output

	assert numbered["2"] not in remaining
	assert numbered["1"] in remaining


def test_a_position_that_was_never_shown_is_refused_with_the_remedy (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A stale or missing listing must not silently address the wrong task."""

	run("init")
	run("add", "Buy milk")

	result = run("done", "9", expect=1)

	assert "subroutine today" in result.output


def test_refs_work_alongside_positions (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Positional addressing is a convenience over refs, which keep working."""

	run("init")
	run("add", "Buy milk")

	ref = run("ls", "--json").output
	name = ref.split('"ref": "')[1].split('"')[0]

	assert "Done: Buy milk" in run("done", name).output


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
	"""``every …`` waits for M7. The words survive, and the user is told why."""

	run("init")

	result = run("add", "Water the plants every monday")

	assert "Water the plants every monday" in result.output
	assert "not supported yet" in result.output


def test_plan_and_defer_move_a_task_between_days (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The two verbs that make an agenda something you steer rather than watch."""

	run("init")
	run("add", "Buy milk")
	run("today")

	assert "Planned:" in run("plan", "1", "tomorrow").output

	run("today")

	hidden = run("defer", "1", "2026-12-01")

	assert "Hidden until" in hidden.output

	# Deferred means hidden: the agenda is empty again.
	assert "Buy milk" not in run("today").output


def test_json_output_carries_enough_to_act_on (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The scripted path and the human path are the same code, so they cannot drift."""

	run("init")
	run("add", "Buy milk")

	document = json.loads(run("today", "--json").output)

	assert document["unscheduled"][0]["title"] == "Buy milk"
	assert document["unscheduled"][0]["ref"]
	assert document["timezone"] == "Europe/London"


def test_a_bad_date_is_refused_with_what_would_have_worked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: errors state the remedy."""

	run("init")
	run("add", "Buy milk")
	run("today")

	result = run("plan", "1", "someday", expect=1)

	assert "tomorrow" in result.output or "2026-08-01" in result.output


def test_help_explains_concepts_not_only_commands (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: users need the model, not just the verbs.

	``--help`` is a vocabulary. This is the grammar, and without it a user who knows every
	flag still does not know that "due Friday" means the end of Friday.
	"""

	listed = run("help")

	for topic in ("dates", "capture", "refs", "scripting"):
		assert topic in listed.output

	assert "deadline" in run("help", "dates").output.lower()
	assert "Nothing is ever lost" in run("help", "capture").output
	assert "SR-42" in run("help", "refs").output


def test_the_help_topics_are_generated_from_the_parsers (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Help that lists a keyword the parser rejects is worse than no help at all.

	Both topics are built from the modules that do the parsing, so this asserts they agree
	rather than asserting a transcription.
	"""

	dates = run("help", "dates").output

	for keyword in subroutine.domain.dates.KEYWORDS:
		assert keyword in dates

	capture = run("help", "capture").output

	for word in subroutine.domain.capture.DEADLINE_WORDS:
		assert word in capture


def test_an_unknown_help_topic_lists_the_real_ones (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: errors state the remedy."""

	result = run("help", "quantum", expect=1)

	assert "dates" in result.output


def test_help_leads_with_examples (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: a flag list teaches vocabulary; an example teaches a sentence.

	Both are needed, in that order — so the worked example must appear before the options
	block, not after it.
	"""

	for command in ("add", "today", "done", "plan"):
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

	assert "\x1b[" not in run("today").output
	assert "\x1b[" not in run("ls").output


def test_a_missing_argument_asks_rather_than_erroring (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a: a required-argument error is a dead end where a question would do."""

	run("init")
	run("add", "Buy milk")
	run("today")

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
