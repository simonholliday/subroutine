"""Three places where the terminal's words named something other than what it meant - `SR#5165`.

Two bullets of L-7 of the cold review of 2026-10-17 and NEW-D-2 of its verification, all released:
``plan`` with no day showed the marker that tells *left out* from ``''``, a control character and
all, rather than asking which day; ``update``'s refusal of a change naming nothing listed eight of
its fifteen fields; and ``workspace rename`` asked about a short name it would not store.
"""

import json
import typing

import pytest
import typer.main
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import test_nothing_can_answer
import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

Run = typing.Callable[..., typer.testing.Result]

#: What the cold review counted ``update --help`` offering, in the order it lists them.
UPDATE_FIELDS = [
	"--title", "--description", "--importance", "--urgency", "--estimate", "--remind", "--type",
	"--status", "--project", "--assignee", "--tags", "--due", "--timezone", "--repeat",
	"--repeat-from",
]

#: What ``update``'s list may leave out, written here rather than read from the code under test:
#: each of these says how a change is made or printed rather than what changes.
UPDATE_MODIFIERS = frozenset(
	{"--just-this-one", "--from-now-on", "--expected-version", "--because", "--json"}
)


def _command (*words: str) -> typing.Any:
	"""Return the command these words name, as the program registers it."""

	command: typing.Any = typer.main.get_command(subroutine.cli.main.app)

	for word in words:
		command = command.commands[word]

	return command


def test_plan_with_no_day_asks_which_day (run: Run, piped: None) -> None:
	"""NEW-D-2: the marker was shown, NUL byte and all, as a day this did not understand."""

	run("init")
	run("add", "Call the dentist")

	planned = run("plan", "1", input="2026-11-02\n")

	assert "Which day?" in planned.output, planned.output
	assert "\x00" not in planned.output and "not given" not in planned.output, planned.output

	shown = json.loads(run("show", "1", "--json").output)

	assert "2026-11-02" in json.dumps(shown), shown


def test_plan_with_no_day_and_nothing_to_answer_says_it_needs_one (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Where nothing can answer, the question is the refusal, at once - `SR#5153`'s rule."""

	run("init")
	run("add", "Call the dentist")
	monkeypatch.setattr(subroutine.cli.personal, "_somebody_can_answer", lambda: False)

	refused = run("plan", "1", expect=2)
	said = test_nothing_can_answer._said(refused)

	assert "Which day?" in said and subroutine.cli.personal.NOTHING_CAN_ANSWER in said, said
	assert "\x00" not in refused.output and "not given" not in refused.output, refused.output


def test_update_s_refusal_names_every_field_it_takes (run: Run) -> None:
	"""It named eight of the fifteen, so ``--due``, ``--project`` and ``--assignee`` among the rest
	were never offered; and none of the five that only say how a change is made."""

	run("init")
	run("add", "Call the dentist")

	refused = run("update", "1", expect=1)
	said = test_nothing_can_answer._said(refused)
	offered = said.partition("Name a field: ")[2]

	assert offered == f"{', '.join(UPDATE_FIELDS[:-1])} or {UPDATE_FIELDS[-1]}.", said


@pytest.mark.parametrize(
	("words", "arranged", "named", "unnamed"),
	[
		pytest.param(
			("update",),
			[("add", "Call the dentist")],
			("1",),
			UPDATE_MODIFIERS,
			id="update",
		),
		pytest.param(
			("project", "update"),
			[("project", "create", "web", "Web")],
			("web",),
			frozenset(),
			id="project update",
		),
		pytest.param(("workspace", "update"), [], ("metacortex",), frozenset(), id="workspace update"),
		pytest.param(
			("view", "edit"),
			[("view", "save", "in-flight", "--q", "status:open")],
			("in-flight",),
			frozenset({"--json"}),
			id="view edit",
		),
	],
)
def test_every_refusal_of_nothing_to_change_names_each_option_that_changes_something (
	run: Run,
	words: tuple[str, ...],
	arranged: list[tuple[str, ...]],
	named: tuple[str, ...],
	unnamed: frozenset[str],
) -> None:
	"""`SR#5165`'s siblings: each lists its fields by hand but ``update``'s, and one list was
	wrong. **Each against its own command**, so a field added to any of them is offered."""

	run("init", "--workspace", "metacortex")

	for one in arranged:
		run(*one)

	said = test_nothing_can_answer._said(run(*words, *named, expect=1))
	missing = [
		max(option.opts, key=len)
		for option in _command(*words).params
		if option.param_type_name == "option"
		and not option.hidden
		and max(option.opts, key=len) not in unnamed
		and max(option.opts, key=len) not in said
	]

	assert "Nothing to change" in said, said
	assert not missing, f"{' '.join(words)} does not offer {missing}: {said}"
	assert not [flag for flag in unnamed if flag in said], said


def test_every_modifier_update_leaves_out_is_one_of_its_options () -> None:
	"""A flag renamed out from under the code's set would be offered as a field, and one added to
	it without a reason would go unoffered."""

	options = {flag for option in _command("update").params for flag in option.opts}

	assert subroutine.cli.personal.UPDATE_MODIFIERS == UPDATE_MODIFIERS
	assert not UPDATE_MODIFIERS - options, options


@pytest.mark.parametrize(("typed", "stored"), [(" Matrix ", "matrix"), ("İnbox", "i-nbox")])
def test_renaming_a_workspace_asks_about_the_short_name_it_will_store (
	run: Run, piped: None, typed: str, stored: str
) -> None:
	"""It quoted ``.lower()``, which keeps the spaces and gives the dotted ``i`` of ``İ``.

	**And it is what is stored**, read back from the rename itself.
	"""

	run("init", "--workspace", "metacortex")

	asked = run("workspace", "rename", "metacortex", typed, input="n\n", expect=1)

	assert f"Renaming metacortex to {stored}." in asked.output, asked.output

	renamed = run("workspace", "rename", "metacortex", typed, "--yes")

	assert f"Renamed to {stored} " in renamed.output, renamed.output
