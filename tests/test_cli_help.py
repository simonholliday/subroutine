"""What the terminal help is allowed to say — item ``#170``.

**Help is published output, and it was being written as source.** A clean-room tester found
five leaks; this guard, written from the same class, found thirty-one across fifteen commands
— including two in a docstring written the same morning. That ratio is the argument for the
file: the leaks are individually trivial and collectively the first impression of the product,
and no amount of care catches them one at a time.

Typer renders a command's docstring verbatim into the terminal. So a docstring here is not a
note to the next programmer; it is the page a stranger reads. Reasoning that needs Markdown, a
specification section number or an item reference belongs in a comment above the function,
where it is still beside the code and is not shown to anybody typing ``--help``.
"""

import ast
import inspect
import pathlib
import re
import textwrap
import typing

import click
import pytest
import typer.main
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import subroutine.db.seed

#: What may never appear in a published help string, and why each one matters.
#:
#: ``'ls'`` is the subtle one. It is a *hidden* synonym for ``list`` (§12.2a) — deliberately
#: absent from the command list, because a synonym you can see is a second thing to choose
#: between — so help that names it points at a word the reader has never met.
FORBIDDEN = {
	"Markdown emphasis": re.compile(r"\*\*"),
	"a backtick": re.compile(r"`"),
	"a reference to docs/design.md, which is not published": re.compile(r"SPEC\.md"),
	"a specification section sign": re.compile(r"§"),
	"the hidden synonym 'ls' rather than 'list'": re.compile(r"'ls'"),
}


def _commands () -> typing.Iterator[tuple[str, typing.Any]]:
	"""Yield every command the CLI publishes, by the name a person would type.

	**Walked by asking for the subcommands, not by ``isinstance``.** Typer vendors its own
	click shim, so a ``TyperGroup`` is *not* a ``click.Group`` — an isinstance check silently
	visits the root and nothing else, and this file passed for exactly as long as it took to
	notice it was testing one command. ``list_commands`` is the interface both kinds share.
	"""

	# A stack rather than recursion, so a cycle in a future layout is a hang in one place.
	#
	# **Typed loosely, with the reason written down.** Typer vendors its own click shim, so
	# what `get_command` returns is a `typer._click.core.Command` — a private class that is
	# not a `click.Command` and that Typer exports no name for. Claiming either type here
	# would be a cast asserting something untrue; the walk below only uses the two methods
	# both kinds carry, and the isinstance note above says why it must not test for a class.
	pending: list[tuple[typing.Any, str]] = [
		(typer.main.get_command(subroutine.cli.main.app), "subroutine")
	]

	while pending:
		command, path = pending.pop()

		yield path, command

		if not hasattr(command, "list_commands"):
			continue

		context = click.Context(command, info_name=path)

		for name in command.list_commands(context):
			child = command.get_command(context, name)

			if child is not None:
				pending.append((child, f"{path} {name}"))


def _texts (command: typing.Any) -> typing.Iterator[tuple[str, str]]:
	"""Yield every piece of help text one command publishes, and where it came from."""

	yield "its description", command.help or ""

	for parameter in command.params:
		yield f"the help for {parameter.name}", getattr(parameter, "help", "") or ""


@pytest.mark.parametrize("path,command", list(_commands()), ids=lambda value: str(value))
def test_published_help_is_prose_rather_than_source (
	path: str, command: typing.Any
) -> None:
	"""No command's help may carry markup, a section number or an unpublished name."""

	for where, text in _texts(command):
		for description, pattern in FORBIDDEN.items():
			found = pattern.search(text)

			assert found is None, (
				f"'{path}' has {description} in {where}, around: "
				f"{text[max(found.start() - 40, 0):found.end() + 40]!r}"
			)


#: Help that names *some* of a seeded vocabulary by example rather than offering all of it, and
#: why each is allowed to.
#:
#: The entry goes away when the parameter does, or when its wording stops naming two keys.
BY_EXAMPLE = {
	("subroutine doc create", "status"): (
		"A status is the most freely edited vocabulary there is, and this sentence is about "
		"which one a decision starts in rather than about what may be typed."
	),
}


def test_no_source_file_writes_a_seeded_vocabulary_out_by_hand () -> None:
	"""`#1240`. The list was typed in six places and every one had to be edited by hand.

	**Derived from the seeds, so it cannot be satisfied by editing this test.** The pattern is
	built by joining the keys the way somebody writing prose joins them, in the seeded order —
	which is exactly the string that was in two ``--help`` texts, two tool schemas and two model
	docstrings, and that was wrong in all six the day decision `#1235` added ``event``.

	``db/seed.py`` is the one place allowed to contain it, because that is where the keys are
	declared and the joining function lives.

	**What this does not catch, and the item says so**: a list that names the seeds correctly is
	still silent about a type a workspace added or renamed itself (`#1129`). Removing the copies
	turns six unknown gaps into one known one; it does not close it.
	"""

	# **An absolute root, because a relative one reads nothing here.** ``pathlib.Path("src")``
	# resolves against pytest's working directory, and the first version of this scan walked an
	# empty match set and passed with a literal restored — a mutation that passed, which is the
	# defect this whole item is about. ``tests/test_references.py`` uses the same anchor for the
	# same reason.
	root = pathlib.Path(__file__).resolve().parent.parent / "src"
	offenders = []
	scanned = 0

	for entity_type in ("task", "document"):
		spelled = subroutine.db.seed.named_types(entity_type)

		for path in sorted(root.rglob("*.py")):
			if path.name == "seed.py":
				continue

			scanned += 1
			text = path.read_text(encoding="utf-8")

			for number, line in enumerate(text.splitlines(), start=1):
				if spelled in line or spelled.replace(", ", " ") in line:
					offenders.append(f"  {path.name}:{number}: {line.strip()[:96]}")

	# The floor beside the assertion, for the reason above: an empty offender list means
	# nothing to report only if something was read.
	assert scanned > 200, scanned

	assert not offenders, (
		"a source file writes out a vocabulary the seeds already declare, so it goes stale the "
		"next time a seed set lands. Build it with subroutine.db.seed.named_types.\n"
		+ "\n".join(offenders)
	)


def _vocabularies () -> dict[str, frozenset[str]]:
	"""Every vocabulary a workspace is seeded with, by a name a failure can print.

	Read off the seeds rather than listed, because a list of what the seeds contain is the
	second copy this guard exists to refuse.
	"""

	found: dict[str, set[str]] = {
		"link type": {one.key for one in subroutine.db.seed.LINK_TYPES}
	}

	for kind in subroutine.db.seed.SEEDED_ITEM_TYPES:
		found.setdefault(f"{kind.entity_type} type", set()).add(kind.key)

	for status in subroutine.db.seed._STATUSES:
		found.setdefault(f"{status.entity_type} status", set()).add(status.key)

	return {name: frozenset(keys) for name, keys in found.items()}


def _named (text: str, keys: typing.Iterable[str]) -> frozenset[str]:
	"""Which of these keys the text names, in either spelling.

	Hyphens read better at a command line than the underscores the seeds use, and both are
	accepted on input, so both count here.
	"""

	spelled = text.replace("-", "_")

	return frozenset(
		key
		for key in keys
		if re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", spelled) is not None
	)


@pytest.mark.parametrize("path,command", list(_commands()), ids=lambda value: str(value))
def test_an_argument_that_lists_a_vocabulary_lists_all_of_it (
	path: str, command: typing.Any
) -> None:
	"""An offer of *some* of a vocabulary reads as an offer of all of it.

	`#1136`, measured: ``link``'s ``relation`` named four of five link types and left out
	``documents`` — the one that says a decision governs a piece of work, and the one the
	whole *what governs this* feature is built on. The instance held **42 of 1,002** links of
	that type, which reads as indiscipline and is not: an agent or a person reads the help,
	sees four, and picks the nearest of the four.

	**Arguments only, not descriptions.** A description is prose and uses these words as
	words — ``link``'s own says what ``blocks`` and ``documents`` are *for* without offering
	either. An argument's help is where the offer is made, so it is where completeness is a
	promise. `#821` fixed the same defect on the MCP tool, where the answer was to name none
	of them and point at the vocabulary instead; a terminal reader is better served by the
	five they almost certainly have, and the refusal names the real ones when they are not.
	"""

	vocabularies = _vocabularies()

	for parameter in command.params:
		text = getattr(parameter, "help", "") or ""
		named = {name: _named(text, keys) for name, keys in vocabularies.items()}

		if any(found == vocabularies[name] for name, found in named.items()):
			continue

		for name, found in named.items():
			if len(found) < 2 or (path, parameter.name) in BY_EXAMPLE:
				continue

			raise AssertionError(
				f"'{path}' offers {sorted(found)} of the {name} vocabulary in the help for "
				f"{parameter.name}, and leaves out "
				f"{sorted(vocabularies[name] - found)}"
			)


def test_nothing_is_excused_from_that_which_no_longer_needs_it () -> None:
	"""An excuse for a parameter that has stopped naming two keys is a decision about nothing."""

	vocabularies = _vocabularies()
	parameters = {
		(path, parameter.name): getattr(parameter, "help", "") or ""
		for path, command in _commands()
		for parameter in command.params
	}

	for where, reason in BY_EXAMPLE.items():
		assert where in parameters, f"{where} is excused and is not a parameter: {reason}"

		text = parameters[where]

		assert any(
			len(_named(text, keys)) >= 2 and _named(text, keys) != keys
			for keys in vocabularies.values()
		), f"{where} no longer names part of a vocabulary, so its excuse can go"


def test_no_command_advertises_a_sentinel_as_a_default () -> None:
	"""A value that exists to mean "not given" must never be shown as one you may pass.

	``update --help`` said ``How much it matters, 1-5. [default: -1]``, which invites
	``--importance -1`` and answers "Nothing to change."; the string sentinels printed their
	own escape character as ``[default:  not given]``. ``serve`` advertised ``[default: 0]``
	for a port, which is not a port.

	Rendered rather than read off the parameters, because the defect is in what Typer *prints*
	— the declarations were all perfectly sensible.
	"""

	runner = typer.testing.CliRunner()

	for path, _command in _commands():
		arguments = [*path.split()[1:], "--help"]
		rendered = runner.invoke(subroutine.cli.main.app, arguments).output

		assert subroutine.cli.personal.UNGIVEN not in rendered, path
		assert "not given" not in rendered, path
		assert "[default: -1]" not in rendered, path
		assert "[default: 0]" not in rendered, path


def test_the_walk_reaches_every_command_the_app_registers () -> None:
	"""The half a parametrised test cannot assert about itself — item ``#405``.

	**This file's first version walked the tree with ``isinstance(x, click.Group)``.** Typer
	vendors its own click shim, so a ``TyperGroup`` is not one: the walk visited the root,
	yielded a single command, and every check below passed. It was testing one command out of
	forty-eight and reporting a clean run.

	A parametrisation is structurally unable to notice that, because "no cases failed" and "one
	case ran" read the same. So the walk is compared against what the app says it registers —
	derived rather than counted, so a command added tomorrow is covered without anybody
	remembering.
	"""

	walked = {path for path, _ in _commands()}
	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	missing = {name for name in registered if f"subroutine {name}" not in walked}

	assert not missing, f"the walk never reached {sorted(missing)}"

	# And a floor, because every name above could be reached while the *groups* went
	# unopened — which is a smaller version of the same defect, and the one that would hide
	# `token create` and `db backup`.
	assert len(walked) > 40, f"the walk reached {len(walked)} commands, which is too few"


#: Where the program's own advice is written.
#:
#: `#1004`. ``cli/personal`` holds ``_suggest`` and every caller of it; ``cli/main`` holds the
#: two that go through the public ``suggest``. Read as a directory rather than as two names, so
#: a third module growing a tip is covered without anybody remembering this file.
ADVICE = pathlib.Path(subroutine.cli.main.__file__).parent

#: The functions that print a tip, and which argument carries the command.
#:
#: ``_suggest`` takes a console first because most callers have one; ``suggest`` is the public
#: face for the two that do not. Two positions rather than one is the cost of that, and it is
#: cheaper than a guard that reads the wrong argument in silence.
SUGGESTERS = {"_suggest": 1, "suggest": 0}

#: How few tips would mean this has stopped reading the source.
#:
#: `#405`'s floor, and it does real work here: the scan below reports offenders, so a scan that
#: reads nothing reports none and is indistinguishable from a clean tree. Around forty at the
#: time of writing, measured with ``grep -c '_suggest(' src/subroutine/cli/personal.py``.
FEWEST_TIPS = 30


def _leading_literal (node: ast.expr) -> str | None:
	"""Return the literal head of a command string, or ``None`` where there is not one.

	An f-string is the common case — ``f"subroutine changes --since {last}"`` — and its head is
	a plain constant, which is where the command name always is. Anything else is skipped
	rather than guessed at, and the floor above is what stops skipping everything reading as
	success.
	"""

	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value

	if isinstance(node, ast.JoinedStr) and node.values:
		first = node.values[0]

		if isinstance(first, ast.Constant) and isinstance(first.value, str):
			return first.value

	return None


def _tips () -> list[tuple[str, int, str]]:
	"""Return every suggested command line, as ``(file, line, text)``.

	Read off the source rather than by running the commands, because a tip is printed on one
	branch of one command and driving all of them is a different and much larger test.
	"""

	found: list[tuple[str, int, str]] = []

	for path in sorted(ADVICE.glob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"))

		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue

			# `_suggest(...)` and `personal.suggest(...)` are both reached, because the second
			# is how `cli/main` calls it and its two tips are as published as any other.
			name = (
				node.func.id
				if isinstance(node.func, ast.Name)
				else node.func.attr
				if isinstance(node.func, ast.Attribute)
				else None
			)

			if name not in SUGGESTERS:
				continue

			where = SUGGESTERS[name]
			given = node.args[where] if len(node.args) > where else None

			for keyword in node.keywords:
				if keyword.arg == "command":
					given = keyword.value

			if given is None:
				continue

			text = _leading_literal(given)

			if text is not None:
				found.append((path.name, node.lineno, text))

	return found


def _signpost (command: typing.Any) -> bool:
	"""Say whether a command exists only to report that it has moved.

	**Derived from what the function does, not from what it is called.** `#509`'s signposts —
	``subroutine today`` and ``subroutine upgrade`` — print where something went and end in
	``raise typer.Exit(2)``, so they cannot succeed on any path. A body whose last statement is
	a ``raise`` is exactly that and nothing else, measured: it names those two and no others.

	**``hidden`` is the wrong question here**, which is worth writing down because the neighbour
	guard in ``tests/test_packaging.py`` asks it and is right to. There a workflow demonstrates
	the path a stranger is shown, so *offered* is the test. A tip is the opposite — §1.4 hides
	``use``, ``claim`` and ``connections`` from the general help precisely so they can be
	revealed at the moment they are useful, and a tip is that reveal. Refusing hidden commands
	here would fail the mechanism for doing progressive disclosure correctly.
	"""

	callback = getattr(command, "callback", None)

	if callback is None:
		return False

	try:
		source = textwrap.dedent(inspect.getsource(callback))

	except (OSError, TypeError):
		return False

	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.FunctionDef) and node.name == callback.__name__:
			return bool(node.body) and isinstance(node.body[-1], ast.Raise)

	return False


def _unreachable (line: str) -> str | None:
	"""Return why a suggested command line names nothing, or ``None`` where it is fine.

	**Walked against the tree rather than matched against a list of names**, because the two
	failures are different: a word that names no subcommand of a group is a broken tip, and a
	word after a leaf command is an argument and is none of this guard's business.
	``subroutine project prioritise web`` is the case that decides it — ``web`` is a project,
	and a flat check either refuses it or stops looking one word too early.
	"""

	words = line.split()

	if not words or words[0] != "subroutine":
		return None

	# **Typed loosely, for the reason `_commands` above writes out**: Typer vendors its own
	# click shim, so what `get_command` returns is a private `typer._click.core.Command` that
	# is not a `click.Command` and that Typer exports no name for. Only `list_commands` and
	# `get_command` are used, which both kinds carry.
	node: typing.Any = typer.main.get_command(subroutine.cli.main.app)
	path = "subroutine"

	for word in words[1:]:
		# A flag, an argument, or an f-string's brace: the command name is over. Anything
		# quoted stops it too, which is what keeps `add "something to do"` out.
		if not re.fullmatch(r"[a-z][a-z0-9-]*", word):
			break

		if not hasattr(node, "list_commands"):
			# A leaf, so this word is an argument to it rather than a subcommand.
			break

		context = click.Context(node, info_name=path)
		child = node.get_command(context, word)

		if child is None:
			return f"{path!r} has no {word!r}"

		node = child
		path = f"{path} {word}"

	if _signpost(node):
		return f"{path!r} is a signpost that always fails"

	return None


def test_every_command_the_program_suggests_is_one_it_can_run () -> None:
	"""`#1004`. The tips are the most-read advice in the product and nothing checked them.

	§12.2a's habit is that a command ends by naming the next one, so somebody following the
	program is following a list of string literals. The same guard already exists twice pointed
	elsewhere — ``tests/test_plugin.py`` over the skill's prose, ``tests/test_documentation.py``
	over the README — both written because a page naming a renamed command is worse than one
	saying less: the reader cannot tell a typo of theirs from a promise of ours (`#134`, `#136`,
	`#138`). The program's own mouth was the surface with no such check.

	**What it must catch, and what it must not.** A tip naming a command that has gone, and a
	tip naming one of `#509`'s signposts — which is what `#1011` found in CI, where a workflow
	ran ``subroutine today`` for five pushes after it stopped working. What it must *not* catch
	is a tip naming a hidden command: ``use``, ``claim`` and ``connections`` are hidden from the
	general help so they can be revealed when they become relevant, and a tip is how that
	reveal happens. **This item's own falsification said to use ``subroutine today`` "which was
	removed" — it was not**, and building the neighbour guard in ``tests/test_packaging.py``
	found that out the expensive way, by passing against the live defect.

	Arguments are deliberately not checked. Flags used not to be either, on the grounds that
	the false-positive rate was unknown — it was measured for `#1264` and is eleven, all
	nameable, so :func:`test_every_flag_this_program_prints_is_one_it_accepts` below does that
	half now.
	"""

	tips = _tips()

	assert len(tips) >= FEWEST_TIPS, (
		f"only {len(tips)} tips were found, which is fewer than the {FEWEST_TIPS} that exist "
		f"— this has stopped reading the source, and no offenders reads exactly like a clean "
		f"tree"
	)

	broken = [
		f"{name}:{line} suggests {text!r} — {why}"
		for name, line, text in tips
		if (why := _unreachable(text)) is not None
	]

	assert not broken, "the program suggests commands it cannot run:\n  " + "\n  ".join(broken)


#: The ``--flag`` spellings in this source that are not options of ours, and why each is there.
#:
#: `#1264`. Every entry is a flag *named in a string* that no command declares, and the rule is
#: that a legitimate one is not addressed to somebody at a terminal here — it belongs to another
#: program, or it is a word about a colour, or it is a spelling being discussed rather than
#: offered. Keyed by the file as well as the flag, because ``--quiet`` and ``--file`` are
#: ordinary words: excusing them everywhere would wave through the next real mistake.
NOT_OUR_OPTIONS = {
	"db/backup.py::--dbname": "psql's, passed to it",
	"db/backup.py::--file": "psql's, passed to it",
	# Named in prose rather than passed: `SR#1554` records why the plain format is the vector
	# and what replaces it, and a docstring that could not name the flag would be arguing
	# about something it may not spell.
	"db/backup.py::--format": "pg_dump's, named in the argument for changing to it",
	"db/backup.py::--no-owner": "pg_dump's, passed to it",
	"db/backup.py::--no-privileges": "pg_dump's, passed to it",
	"db/backup.py::--no-psqlrc": "psql's, passed to it",
	"db/backup.py::--quiet": "psql's, passed to it",
	"db/backup.py::--set": "psql's, passed to it",
	"db/backup.py::--single-transaction": "psql's, passed to it",
	"domain/palette.py::--accent": "the name of a colour role, not an option",
	"domain/palette.py::--warn": "the name of a colour role, not an option",
	"cli/personal.py::--show-status": "a spelling weighed in a comment and not taken",
	"domain/capture.py::--projects": "the record of a flag that never existed, which is the "
	"defect this guard is for",
}


#: What a flag looks like in prose. Lower case with interior hyphens, which is every option
#: this program has; a ``--`` on its own or followed by anything else is not one.
FLAG = re.compile(r"--[a-z][a-z0-9-]*")


def _declared_options () -> set[str]:
	"""Return every long option any command accepts, by walking the app.

	Derived from the same walk the rest of this file uses, so a command added tomorrow brings
	its flags with it. ``--help`` is added by hand: Typer attaches it when a command is
	rendered rather than declaring it as a parameter, so it is real everywhere and appears in
	no ``params`` list.
	"""

	found = {"--help"}

	for _path, command in _commands():
		for parameter in command.params:
			for spelling in (
				*getattr(parameter, "opts", ()),
				*getattr(parameter, "secondary_opts", ()),
			):
				if spelling.startswith("--"):
					found.add(spelling)

	return found


def _flags_named_in_source () -> dict[str, list[tuple[str, int]]]:
	"""Return every ``--flag`` written into a string under ``src``, and where.

	Read from the syntax tree rather than by grepping the text, so a flag in a comment — which
	nobody but a programmer reads — is not mistaken for one the program says out loud.
	"""

	root = pathlib.Path(subroutine.cli.main.__file__).parent.parent
	found: dict[str, list[tuple[str, int]]] = {}

	for path in sorted(root.rglob("*.py")):
		where = path.relative_to(root).as_posix()

		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
				continue

			for spelling in FLAG.findall(node.value):
				found.setdefault(spelling, []).append((where, node.lineno))

	return found


def test_every_flag_this_program_prints_is_one_it_accepts () -> None:
	"""`#1264`. Three of these have shipped and only somebody typing one has ever found them.

	``require_secret_key`` offered ``--dev-mode``, which was never built; ``restore``'s help
	said ``subroutine list --deleted``, where the flag is ``--trash`` and Typer's *did you
	mean* then pointed at ``--deferred``; and ``domain/capture.py`` carries a comment about a
	third, ``subroutine list --projects``, *"caught by running it"*.

	That is the whole detection history: running it. A message naming a flag that does not
	exist fires at the moment somebody has no other information, and what they reasonably
	conclude from *no such option* is that the rest of the message is stale too.
	"""

	declared = _declared_options()

	assert len(declared) > 50, (
		f"only {len(declared)} options were found, which is too few — the walk has stopped "
		f"reading the app, and every flag would then look wrong rather than none"
	)

	named = _flags_named_in_source()

	assert named, "no flags were found in any string, so this asserts nothing"

	broken = [
		f"{where}:{line} names {spelling!r}, which no command accepts"
		for spelling, sites in sorted(named.items())
		if spelling not in declared
		for where, line in sites
		if f"{where}::{spelling}" not in NOT_OUR_OPTIONS
	]

	assert not broken, "the program names flags it does not have:\n  " + "\n  ".join(broken)


def test_no_excused_flag_has_quietly_become_ours_or_gone_away () -> None:
	"""An entry that is no longer needed is a considered decision nobody made.

	Two ways one expires: the string moves or is deleted, and the excuse then describes
	nothing; or we grow a real option by that name in that file, at which point the entry is
	hiding a check rather than explaining one.
	"""

	declared = _declared_options()
	named = _flags_named_in_source()

	stale = []

	for entry, why in sorted(NOT_OUR_OPTIONS.items()):
		where, spelling = entry.split("::")

		if not any(site == where for site, _line in named.get(spelling, [])):
			stale.append(f"{entry} is excused as {why!r} and is not written there any more")

		elif spelling in declared:
			stale.append(f"{entry} is excused as {why!r} and is now an option this program has")

	assert not stale, "excuses that have expired:\n  " + "\n  ".join(stale)
