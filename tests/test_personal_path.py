"""The personal path end to end — SPEC.md §13.5b, run as a test rather than asserted.

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
import sys
import typing

import pytest
import rich.text
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.config
import subroutine.context
import subroutine.directory
import subroutine.domain.capture
import subroutine.domain.comments
import subroutine.domain.dates
import subroutine.domain.events
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
	[("list",), ("today",), ("add", "Buy milk"), ("show", "1")],
	ids=["list", "today", "add", "show"],
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
	assert "changed importance" in shown
	assert "commented" in shown, "a comment must reach the history — that is what #52 built"


def test_the_history_is_in_the_json_whether_or_not_it_was_asked_for (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A key that appears only with a flag makes a script test for the key rather than read it.

	"Absent" and "nothing has happened" would then be the same shape for two different facts,
	which is the `due_at: null` mistake `_as_json` already avoids for documents.
	"""

	run("init")
	run("add", "Fix the parser")

	assert json.loads(run("show", "1", "--json").output)["history"] == []
	assert json.loads(run("show", "1", "--history", "--json").output)["history"]


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

	document = json.loads(run("today", "--json").output)

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


#: A field the terminal row shows and the scripted row deliberately does not carry under that
#: name, and what a script gets instead. Same rule as the registers in
#: ``test_api_writability.py``, and for the same reason: "the JSON does not have it" describes
#: the code rather than giving a reason, and `#820` is what happens when nothing checks one.
RENDERED_ONLY: dict[str, str] = {
	"estimate_human": (
		"Carried as `estimate_minutes`. §6.4's grammar is a rendering — a script handed '2h' "
		"has to parse the terminal's prose back into the number it was made from."
	),
	"description": (
		"`_match_cell` reads it to say *why* a search matched, and a listing row carries "
		"neither body — §14.10 makes response size a first-order cost, and the whole "
		"description on every row of a search is the opposite of that. What a scripted "
		"search cannot see is the match *reason*, which is `#840`."
	),
	"body": "The same, for a document.",
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

	assert "Planned for" in run("plan", "1", "friday").output
	assert "Planned for" in run("plan", "1", "next friday").output
	assert "Hidden until" in run("defer", "1", "monday").output

	# Abbreviations too — they are in the same table `explain dates` prints.
	assert "Planned for" in run("plan", "1", "fri").output


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

	printed = _sections(run("today").output)

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

	printed = _sections(run("today").output)

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

	printed = _sections(run("today").output)

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

	printed = _sections(run("today").output)

	assert printed["In progress"] == ["2"], printed
	assert printed["Next"] == ["1"], printed

	# And the heading is dropped entirely when nothing is started, like every other bucket.
	run("stop", "2")

	assert "In progress" not in run("today").output


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

	scripted = json.loads(run("today", "--json").output)

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

	assert (address, marker, rest) == ("#1", "holds", "up  Chase the photographer"), rows["1"]

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

	assert "holds up" in run("list").output

	run("done", "2")

	assert "holds up" not in run("list").output

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

	assert "holds up" not in run("list").output


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

	day = re.search(r"\(for ([^,)]+)\)", planned)
	by = re.search(r"\(due ([^,)]+)\)", deadline)

	assert day is not None, f"a planned day alone was not reported:\n{planned}"
	assert by is not None, f"a deadline alone was not reported:\n{deadline}"

	assert f"for {day.group(1)}" in together, (
		f"the planned day was dropped once there was a deadline:\n{together}"
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


#: The commands that open every connection and do *not* run the duplicate-instance guard,
#: each with the reason it cannot count one instance twice (`#327`). Adding a fourth is
#: meant to be an act: the guard exists so a merged read does not double-count, and getting
#: that wrong in the permissive direction is silent.
UNMERGED_COMMANDS = {
	"_listed": "list and ls group by connection, one heading each, so a duplicate is shown "
	"twice rather than counted twice",
	"show": "one address resolved in one context, so there is nothing to combine",
	"whoami": "one line per connection, which is the command somebody runs to find out "
	"their configuration is ambiguous",
}


def _commands_skipping_the_duplicate_guard () -> dict[str, str]:
	"""Return every command that opens the world with the duplicate check turned off.

	Reads the tree rather than a list, so a synthetic offender reaches the real scan — the
	shape `#405` settled after two guards here were found checking a copy of their own rule.
	"""

	source = pathlib.Path(subroutine.cli.personal.__file__).read_text(encoding="utf-8")
	found: dict[str, str] = {}
	current = ""

	for line in source.split("\n"):
		named = re.match(r"\tdef (\w+) ?\(", line)

		if named is not None:
			current = named.group(1)

		if "merged=False" in line and "def " not in line:
			found[current] = line.strip()

	return found


def test_only_the_commands_that_report_per_connection_skip_the_duplicate_guard () -> None:
	"""`#327`: the guard was applied to every command and belongs to the merge.

	`fanout.refuse_duplicate_instances` stops a *merged* read counting one instance twice.
	It ran wherever the world was opened, so it also refused `whoami` — which prints a line
	per connection and combines nothing — and `list`, which groups by connection and would
	have shown the collision plainly. During the 2026-08-03 migration that made it impossible
	to verify a copy while the original still existed, which is why `#288`'s steps had to move.

	**This is the ratchet, not the fix.** `today` merges into buckets and still refuses, so
	`#337`'s conclusion survives: per-connection identity remains unusable for the operator
	because their agenda is a merged read.
	"""

	found = _commands_skipping_the_duplicate_guard()

	assert set(found) == set(UNMERGED_COMMANDS), (
		"a command started or stopped skipping the duplicate-instance guard. Each one is a "
		"claim that it cannot count an instance twice — add it to UNMERGED_COMMANDS with the "
		"reason, or leave the guard on.\n"
		f"in the code: {sorted(found)}\nrecorded here: {sorted(UNMERGED_COMMANDS)}"
	)


def test_the_command_that_merges_does_not_skip_the_duplicate_guard () -> None:
	"""The other half, and the one that makes the list above safe to shorten.

	A scan that read nothing would make the test above pass with an empty set on both sides,
	so this names the command whose whole design is merging (§13.7 — the dentist and the
	stand-up belong in one list) and fails if it ever appears in the skipping set.
	"""

	assert "today" not in _commands_skipping_the_duplicate_guard(), (
		"`today` merges across connections by design, so it is the one read that genuinely "
		"double-counts when two connections are the same instance"
	)

	assert _commands_skipping_the_duplicate_guard(), (
		"the scan found nothing at all, so it is not measuring the tree"
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
