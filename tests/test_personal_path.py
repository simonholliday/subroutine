"""The personal path end to end — SPEC.md §13.5b, run as a test rather than asserted.

The gating criterion is four commands on a fresh installation, and **none of their output
mentioning a workspace, a status, a project, a criterion, a verification, a session or a
claim**. That vocabulary check is the guard on §1.4's progressive-disclosure rule, and it
is meant to fail the first time somebody adds a required field for an agent's benefit.

These run the real CLI against a real database in a temporary XDG home, because the parts
most likely to break are the ones only the wiring exercises: the config file, the state
directory, the local-mode principal, and the numbering that makes ``done 1`` work.
"""

import datetime
import json
import os
import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import subroutine.domain.capture
import subroutine.domain.comments
import subroutine.domain.dates
import subroutine.errors

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
	assert "Tip: subroutine today" in run("add", "Buy milk").output
	assert "Tip: subroutine done" in run("today").output
	assert "Tip: subroutine today" in run("done", "1").output


def test_a_suggestion_is_marked_as_one_without_relying_on_colour (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#128``, and decision ``#102``: no information exists only in a colour.

	This test is the guard rather than the marker being there, and the distinction matters —
	the version of ``test_every_command_suggests_the_next_one`` above that only looked for
	``"subroutine today"`` passed just as happily on the broken output as on the fixed one,
	which is why the defect survived to be found by somebody reading the README.

	Colour is already gone here: the runner has no terminal, so rich emits none. What is left
	has to be enough on its own, because that is also what a pipe, a log, a screen reader and a
	fenced block in Markdown get.
	"""

	run("init")

	printed = run("add", "Buy milk").output

	assert "\033[" not in printed, "no colour to lean on, which is the point"

	suggestions = [line for line in printed.splitlines() if "subroutine today" in line]

	assert suggestions, printed
	assert all(line.strip().startswith("Tip:") for line in suggestions), printed


def test_an_empty_list_says_what_to_do_about_it (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A blank screen is a dead end; the remedy costs one line."""

	run("init")

	assert 'subroutine add "something to do"' in run("today").output
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
		for line in run("today").output.splitlines()
		if line.strip().startswith("#")
	}

	assert set(shown) == {"#1", "#2"}, "listings print the ref with its sigil"

	# Typed without the sigil, because a shell would eat it (SPEC.md §12.2a).
	run("done", "2")

	remaining = run("today").output

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
	precisely so they can create more (SPEC.md §7.1).
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

	# The confirmation echoes the day that was just set, not the deadline. `_when` prefers
	# a deadline, which is right in a list and wrong here — the user said "tomorrow" and
	# used to be shown Friday.
	assert "Planned for" in run("plan", "1", "tomorrow").output

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
	assert "#7" in run("help", "refs").output


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
	"""The half of SPEC.md §5.10 that had a service and an API and no way to read it."""

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
	"""One ref counter serves both (SPEC.md §6.2), so a reader that only knew tasks was wrong.

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

	assert "task" not in run("today").output.lower()

	_a_typed_task(home, title="The parser drops a token", type_key="bug")

	mixed = run("today").output

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
	"""Simon asked why #5-#8 were not in his list. They were documents (SPEC.md §12.2).

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
	"""SPEC.md §1.4 falling out of a layout rule rather than being enforced by one.

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


def test_a_bare_invocation_says_there_is_more (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""SPEC.md §12.2a's habit, applied to the most likely first thing anybody types.

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

	A bare `subroutine` is somebody arriving; `subroutine today` is somebody who already
	knows what they want. A daily habit should not carry a signpost forever, and the two are
	distinguishable because Typer reports whether a subcommand was invoked.
	"""

	run("init")
	run("add", "buy milk")

	assert "--help" not in run("today").output


def test_the_two_helps_point_at_each_other (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``--help`` lists the commands; ``help`` explains the concepts. Neither is the whole of it.

	A user who guessed one had no reason to think the other existed. `help` already pointed
	at `--help`; the reverse is the epilog on the application, so whichever a beginner lands
	on names the other.
	"""

	run("init")

	assert "subroutine help" in run("--help").output
	assert "--help" in run("help").output


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
	assert "Renew the passport" not in run("today").output

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
	narrowed = run("list", "--project", "INBOX").output

	assert "Filed nowhere in particular" in narrowed

	# **An unknown key is a failed connection, not a failed command**, because with several
	# connections a project may legitimately exist on one and not another. So it is named on
	# stderr and the command carries on — `--strict` is how a script says it would rather
	# stop, and that is the fan-out's contract rather than anything this flag invented.
	missing = run("list", "--project", "NOSUCH")

	assert "NOSUCH" in missing.output

	# And what it must *not* say is that the list is empty. That reads as "the project exists
	# and has nothing in it", which is the one wrong conclusion available.
	assert "Nothing on your list" not in missing.output

	assert run("list", "--project", "NOSUCH", "--strict", expect=1)


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
	"""

	run("init")
	run("add", "Buy milk")

	refused = run("plan", "1", "next monday", expect=1)

	assert "is not a date this understands" in refused.output
	assert refused.output.count("Write a date as") == 1


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
	`start_at`, so it can make the same choice for itself.
	"""

	run("init")
	run("add", "Do this now")
	run("add", "Renew the passport from 2099-12-01")

	rows = json.loads(run("list", "--json").output)
	titles = {row["title"] for row in rows}

	assert titles == {"Do this now", "Renew the passport"}

	# And the row carries what a script needs to apply the rule itself.
	parked = next(row for row in rows if row["title"] == "Renew the passport")

	assert parked["start_at"] is not None


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

	assert "Parts" in parent
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
	assert "Parts" not in shown


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
	run("project", "create", "WEB", "Website redesign")
	run("add", "Fix the header +WEB")

	assert "Fix the header" in run("list", "--project", "WEB").output


def test_a_project_key_is_refused_rather_than_repaired (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""It is permanent and it becomes part of every address, so a guess is not good enough.

	Case is the exception and is not a repair: ``web`` and ``WEB`` are the same key rather
	than one being fixed into the other, which is why the second of these collides.
	"""

	run("init")

	assert "not a usable key" in run("project", "create", "2FA", "Digits", expect=1).output

	run("project", "create", "WEB", "Website")

	assert "already in use" in run("project", "create", "web", "Again", expect=1).output


def test_the_project_listing_shows_what_is_inside_what (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Ordered by path so a child follows its parent, and indented so that is visible.

	The indentation is the only thing carrying the shape — decision ``#102`` forbids a colour
	being the sole bearer of anything, and a tree drawn in a colour would be exactly that.
	"""

	run("init")
	run("project", "create", "OUTER", "Outer thing")
	run("project", "create", "INNER", "Inner thing", "--parent", "OUTER")

	printed = run("project", "list").output
	rows = [line for line in printed.splitlines() if line.strip()]

	assert any(line.startswith("OUTER") for line in rows)
	assert any(line.startswith("  INNER") for line in rows), printed

	def where (key: str) -> int:
		"""Return which row a project is on."""

		return next(index for index, line in enumerate(rows) if key in line)

	assert where("OUTER") < where("INNER"), printed


def test_add_says_what_it_read_out_of_the_line (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#135``. It filed the work correctly and confirmed nothing, which is the same as not
	being sure it did.

	Written back as the sigils that were typed, because that needs no vocabulary — and because
	§13.5b forbids the words a sentence explaining them would have to use.
	"""

	run("init")
	run("project", "create", "WEB", "Website")

	printed = run("add", "Fix the header !4/2 ~2h #ops +WEB").output

	for sigil in ("+WEB", "!4/2", "~2h", "#ops"):
		assert sigil in printed, f"{sigil} was read and not mentioned:\n{printed}"


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
