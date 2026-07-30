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
import os
import pathlib
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.domain.capture
import subroutine.domain.comments
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
	assert "subroutine today" in run("add", "Buy milk").output
	assert "subroutine done" in run("today").output
	assert "subroutine today" in run("done", "1").output


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
