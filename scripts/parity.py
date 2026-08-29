"""Where the four surfaces stand, derived on every run rather than remembered.

Decision `SR#1539` is the product goal this measures: *a surface may lack a capability, but it
may never leave somebody stuck*. Equality here is **no dead ends** rather than identical menus —
§21.2's tool budget, §1.4's progressive disclosure and §12.4's recovery property each make
"everything everywhere" the wrong rule, and each is a cost rather than a preference.

**Why this is a program and not a paragraph.** `SR#146` measured the same thing by hand on
2026-08-01 and reported 36 HTTP capabilities, 23 on the client, 24 on the CLI and 11 on MCP.
Derived again three weeks later the figures were 107, 88, 64 and 34: every number was wrong and
nothing anywhere said so. A parity claim is a statement about the *relationship* between four
things that each move independently, so it rots faster than almost anything else written down.

**Everything here is read from the thing itself** — the mounted routers, the client protocol,
the Typer app's package, the MCP catalogue's package, and the browser's request builders
executed in Node. Nothing is a list somebody typed except the excuses, which are the point:
`tests/test_reach.py` holds them, and reporting on them is reporting on the decisions.

**It may import from ``tests``**, which is backwards anywhere it would ship. It does not ship:
``[tool.hatch.build.targets.wheel]`` packages ``src/subroutine`` alone, so this is a developer's
tool sitting beside ``check.py``, and the excuse registers belong with the guard that enforces
them rather than copied here.

Run it for the report; ``tests/test_parity.py`` imports it and asserts the invariants, so the
numbers below are checked rather than merely printed — a runbook is untested code.
"""

import ast
import dataclasses
import pathlib
import re
import sys
import tempfile
import typing

import click
import typer.main

import subroutine.cli.main
import subroutine.mcp.tools

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tests"))

# The line above is what makes this importable: `tests/` is not a package, and the excuse
# registers belong with the guard that enforces them rather than copied here.
import test_reach

#: How a reason says where somebody goes instead — `SR#1539`'s no-dead-ends clause.
#:
#: **A reference to another surface, not a phrase.** Each pattern names a way *out* of the
#: surface being excused: a command or a tool somebody can type, the escape hatch that reaches
#: every route, or the route itself. Checking for prose like "use the terminal" would be a
#: spelling test on somebody's wording; checking for a citable destination is not.
#:
#: **It under-reports rather than over-reports**, which is the safe direction: an excuse that
#: signposts in words this cannot see is counted as silent, so the number is an upper bound on
#: the work and never a claim that a gap has been closed.
WAYS_THROUGH = (
	re.compile(r"`subroutine[ _][a-z]"),
	re.compile(r"subroutine_call_api"),
	re.compile(r"`?(?:GET|POST|PATCH|DELETE) /v1/|`/v1/"),
)

#: The kinds whose excuse owes a signpost, because the capability exists somewhere else.
#:
#: **``administrative``, ``protocol`` and ``tracked`` are deliberately not here.** The first is a
#: boundary — §12.4 wants ``db backup`` to work when the service will not start, and a restore
#: endpoint does not exist at all — and the other two name an item, which is a gap being tracked
#: rather than a route somebody can take today.
#:
#: **These two kinds are each doing two jobs and `SR#1539` says so**: *this surface does not
#: foreground it*, which owes a signpost, and *this surface must not have it*, which is a
#: boundary wearing the wrong label. Splitting them is a decision per entry, so until then this
#: counts both and the count is read as an upper bound.
OWES_A_SIGNPOST = ("budget", "disclosure")


#: MCP tools with no terminal command of the same name, and why — decision `SR#1547`.
#:
#: **Every other one matches exactly**, which is the state worth protecting: `add`, `update`,
#: `done`, `claim`, `link`, `list`, `search`, `show`, `comment`, `project`, `changes`, `journal`
#: and `whoami` are one word each on both surfaces. An agent that drops to a shell types what it
#: already knows.
NOT_A_COMMAND: dict[str, str] = {
	"call_api": (
		"There is no terminal command for it because the terminal *is* the escape hatch: "
		"`subroutine_call_api` exists so an agent can reach a route its fifteen tools do not "
		"cover, and a person at a shell reaches those routes by running the command. A "
		"`subroutine call-api` would be a worse `curl` for somebody who has `curl`."
	),
	"document": (
		"`SR#1549`. The terminal calls this group `doc`, so `subroutine document` is refused "
		"with *Did you mean 'comment'?* — which points at a different kind of record. It runs "
		"against `SR#154`'s own rule that a real word beats an abbreviation, and the fix is "
		"`list`/`ls`'s shape: `document` visible, `doc` a hidden synonym. Deferred past the tag "
		"by `SR#1547` because it is a rename that reaches the skill and `explain`."
	),
}

#: One concept, one word (`SR#1547`). Variant somebody might write -> the word and the reason.
#:
#: **Scanned over string constants that are not docstrings**, which is the whole trick: a
#: comment explaining the parent rule may say ``subtask`` freely, and a *refusal* may not. What
#: a developer reads and what a user reads are different corpora, and only one of them is this.
#:
#: **A word that is also a published identifier cannot be here.** Scanning for ``todo`` returns
#: eight hits of which seven are the status category *key* — `status_category=todo`, which
#: callers send — so flagging them would be guarding a spelling instead of a thing, and
#: correcting them would break the contract. Terms that double as vocabulary keys are out of
#: scope until each site is decided one at a time, which is `SR#1240`'s territory.
#:
#: **Growing this is a judgement per term rather than a list to type**, and `SR#1547` defers it.
TERMS: dict[str, tuple[str, str]] = {
	"subtask": (
		"sub-task",
		"`SR#1282` made the heading `Sub-tasks` on all three surfaces that draw one, and the "
		"refusal three layers down said `A subtask belongs to…` — one concept, two spellings, "
		"one action apart. `sub-project` was hyphenated everywhere already, so only this "
		"compound was split.",
	),
}


@dataclasses.dataclass(frozen=True)
class Edge:
	"""One surface measured against what it could reach."""

	name: str
	reaches: int
	excused: int

	@property
	def total (self) -> int:
		"""How many capabilities were classified either way."""

		return self.reaches + self.excused


@dataclasses.dataclass(frozen=True)
class Report:
	"""Everything this run measured, so a caller asserts on values rather than on printing."""

	routes: int
	protocol: int
	edges: tuple[Edge, ...]
	by_kind: dict[str, int]

	#: Excuses of a kind that owes a signpost and does not give one, as (register, subject).
	silent: tuple[tuple[str, str], ...]

	#: Distinct route shapes the browser builds, or ``None`` when they could not be executed.
	browser: int | None

	#: Agent tools whose name is a terminal command too, and those whose name is not.
	matched_tools: tuple[str, ...] = ()
	unmatched_tools: tuple[str, ...] = ()

	#: Where a user-facing string uses a word this product spells another way, as
	#: (file and line, the variant, the word it should be).
	misspellings: tuple[tuple[str, str, str], ...] = ()

	#: How many string constants the spelling scan actually read.
	spoken: int = 0

	#: Why the browser was not measured, when it was not.
	browser_absent: str | None = None


def _excuses () -> tuple[tuple[str, typing.Any, str, str], ...]:
	"""Return every exemption as (register, subject, kind, reason), read from the guard."""

	registers = (
		("NOT_REACHED", test_reach.NOT_REACHED),
		("NOT_IN_CLI", test_reach.NOT_IN_CLI),
		("NOT_IN_MCP", test_reach.NOT_IN_MCP),
	)

	return tuple(
		(name, subject, kind, reason)
		for name, register in registers
		for subject, (kind, reason) in register.items()
	)


def signposted (reason: str) -> bool:
	"""Whether a reason names somewhere the reader can actually go instead."""

	return any(pattern.search(reason) for pattern in WAYS_THROUGH)


def browser_routes () -> tuple[int | None, str | None]:
	"""Return how many distinct route shapes the browser builds, by executing its builders.

	**Executed rather than scanned**, because the paths are template literals and a regex over
	JavaScript is a guard with no braces to see by. ``app.js`` has exactly one way out to the
	network — held by ``test_the_app_reaches_the_network_only_through_a_built_request`` — so
	running every exported builder is the whole of what the browser can reach.

	**Degrades to a reason rather than raising.** This needs Node, and a report that cannot be
	produced at all because one surface is unmeasurable is worth less than one that says which
	surface it could not read.
	"""

	try:
		import fastapi

		import test_web
	except ImportError as absent:  # pragma: no cover - a dev environment without the suite
		return None, f"the test helpers could not be imported ({absent})"

	place = test_web.Instance(
		application=typing.cast(fastapi.FastAPI, None), secret="", slug="w", project="p",
		task=1, spare=3, spare_version=1, repeating=4, repeating_version=1, link="l",
		document=2, spare_document=5, document_status="archived", document_link="dl",
		username="si", status="open", cursor="c", document_cursor="d", since=1,
	)

	try:
		with tempfile.TemporaryDirectory() as scratch:
			built = test_web._built(pathlib.Path(scratch), test_web._calls(place))

	except Exception as failed:  # pragma: no cover - Node absent or refusing
		return None, f"the builders could not be executed ({failed})"

	shapes = {
		(str(request.get("method", "GET")).upper(), _shape(str(request["path"]), _stood_in(place)))
		for request in built
		if request.get("path")
	}

	return len(shapes), None


def _stood_in (place: typing.Any) -> set[str]:
	"""Return every value the builders were handed, as it would appear in a path.

	**Read off the instance rather than written out beside it** (`SR#1550`). These are what
	:func:`_shape` has to recognise in a built path in order to collapse ``/tasks/1`` to
	``/tasks/{x}``, and the first version listed them a second time by hand — the pair that
	comes to disagree, in the script whose whole purpose is finding that shape.

	**What the drift would have cost is a wrong number rather than a failure.** New placeholders
	would simply stop being recognised, paths would stop collapsing, and the browser's route
	count would inflate — the report reading as the browser reaching more than it does, with
	nothing anywhere failing.

	The empty ones are dropped because an empty segment is not an identifier, and ``application``
	is not a value a path could carry.

	``_asdict`` rather than ``vars``, because :class:`test_web.Instance` is a ``NamedTuple`` and
	has no ``__dict__`` at all — which is the sort of thing that is only found by running it.
	"""

	return {
		str(value)
		for name, value in place._asdict().items()
		if name != "application" and isinstance(value, str | int) and str(value)
	}


def _shape (path: str, stood_in: set[str]) -> str:
	"""Return a built path with its identifiers replaced, so two rows of one route are one.

	Takes what to recognise as an argument for `SR#405`'s reason: a scanner that cannot be
	handed its subject can only ever be tested against itself.
	"""

	segments = []

	for segment in path.split("?")[0].split("/"):
		stands_for_one = segment and (segment.isdigit() or segment in stood_in)
		segments.append("{x}" if stands_for_one else segment)

	return "/".join(segments)


def commands () -> set[str]:
	"""Return every command word the terminal offers, at any depth.

	Walked rather than listed for `SR#405`'s reason, and typed loosely for
	``tests/test_cli_help.py``'s: Typer vendors its own click shim, so what ``get_command``
	returns is a private class that is not a ``click.Command`` and that Typer exports no name
	for. Only the two methods both kinds carry are used.
	"""

	found: set[str] = set()

	def walk (node: typing.Any, path: str) -> None:
		"""Add this command's word, then everything under it."""

		if path:
			found.add(path.split(" ")[0])

		if not hasattr(node, "list_commands"):
			return

		context = click.Context(node, info_name=path or "subroutine")

		for name in node.list_commands(context):
			child = node.get_command(context, name)

			if child is not None:
				walk(child, f"{path} {name}".strip())

	walk(typer.main.get_command(subroutine.cli.main.app), "")

	return found


def tool_names () -> set[str]:
	"""Return every agent tool's name with the product prefix taken off.

	The catalogue binds each tool to a connection it never consults to describe itself, so
	nothing here needs an instance — the schemas and the names are static.
	"""

	tools = subroutine.mcp.tools.catalogue(typing.cast(typing.Any, None))

	return {tool.name.removeprefix("subroutine_") for tool in tools}


def spoken (tree: ast.Module) -> typing.Iterator[tuple[int, str]]:
	"""Yield every string constant in this module that is **not** a docstring.

	**The docstrings are the point of the exclusion** (`SR#1547`). A comment or a docstring is
	what a developer reads about the product; a string constant is, near enough, what somebody
	using it reads. ``subtask`` explaining the parent rule in prose is fine, and ``subtask`` in
	a refusal is the defect — so a scan that could not tell them apart would either miss the
	second or forbid the first.

	Takes the tree rather than a path, so a synthetic offender can be fed to it (`SR#405`).
	"""

	held = {
		id(node.body[0].value)
		for node in ast.walk(tree)
		if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
		and getattr(node, "body", None)
		and isinstance(node.body[0], ast.Expr)
		and isinstance(node.body[0].value, ast.Constant)
		and isinstance(node.body[0].value.value, str)
	}

	for node in ast.walk(tree):
		if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in held:
			yield node.lineno, node.value


def misspelled (root: pathlib.Path) -> tuple[list[tuple[str, str, str]], int]:
	"""Return where a user-facing string uses a variant of a term, and how much was read.

	**Both halves returned, because a scan that reports only offenders answers the same empty
	list when it is clean and when it is blind.** The count is what a floor can be put under.
	"""

	found: list[tuple[str, str, str]] = []
	read = 0

	for path in sorted(root.rglob("*.py")):
		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for line, text in spoken(tree):
			read += 1

			for variant, (word, _reason) in TERMS.items():
				if re.search(rf"\b{re.escape(variant)}s?\b", text, re.I):
					found.append((f"{path}:{line}", variant, word))

	return found, read


def measured () -> Report:
	"""Return where the four surfaces stand, read from their own sources."""

	protocol = test_reach._protocol()
	routes = test_reach._mutating() | test_reach._reading()
	in_cli = protocol & test_reach._called_in("cli")
	in_mcp = protocol & test_reach._called_in("mcp")

	by_kind: dict[str, int] = dict.fromkeys(test_reach.KINDS, 0)
	silent = []

	for register, subject, kind, reason in _excuses():
		by_kind[kind] = by_kind.get(kind, 0) + 1

		if kind in OWES_A_SIGNPOST and not signposted(reason):
			silent.append((register, str(subject)))

	count, absent = browser_routes()
	offered = commands()
	tools = tool_names()
	wrong, read = misspelled(pathlib.Path(__file__).resolve().parent.parent / "src")

	return Report(
		routes=len(routes),
		protocol=len(protocol),
		edges=(
			Edge("client protocol", len(routes) - len(test_reach.NOT_REACHED),
			     len(test_reach.NOT_REACHED)),
			Edge("terminal", len(in_cli), len(test_reach.NOT_IN_CLI)),
			Edge("agent tools", len(in_mcp), len(test_reach.NOT_IN_MCP)),
		),
		by_kind=by_kind,
		silent=tuple(sorted(silent)),
		browser=count,
		matched_tools=tuple(sorted(tools & offered)),
		unmatched_tools=tuple(sorted(tools - offered)),
		misspellings=tuple(sorted(wrong)),
		spoken=read,
		browser_absent=absent,
	)


def render (report: Report) -> str:
	"""Return the report as somebody reads it."""

	lines = [
		"Where the four surfaces stand — decision SR#1539",
		"",
		f"  HTTP is the reference: {report.routes} routes mounted.",
		"",
		f"  {'surface':18} {'reaches':>8} {'excused':>8} {'unclassified':>13}",
	]

	for edge in report.edges:
		lines.append(f"  {edge.name:18} {edge.reaches:8} {edge.excused:8} {0:13}")

	if report.browser is None:
		lines.append(f"  {'browser':18} {'not measured':>8} — {report.browser_absent}")

	else:
		lines.append(
			f"  {'browser':18} {report.browser:8} {'—':>8} {'not modelled':>13}"
		)

	lines += [
		"",
		"  Excuses, by the constraint each claims:",
		"",
	]

	for kind in sorted(report.by_kind):
		lines.append(f"    {kind:16} {report.by_kind[kind]:4}")

	owed = sum(report.by_kind.get(kind, 0) for kind in OWES_A_SIGNPOST)

	lines += [
		"",
		f"  Of the {owed} excuses that owe a signpost, {len(report.silent)} name no way through.",
		"  (An upper bound: a signpost written in words this cannot cite reads as silent.)",
		"",
	]

	for register, subject in report.silent:
		lines.append(f"    {register:14} {subject}")

	lines += [
		"",
		"Whether the four surfaces use one word — decision SR#1547",
		"",
		f"  {len(report.matched_tools)} of "
		f"{len(report.matched_tools) + len(report.unmatched_tools)} agent tools are named "
		f"exactly as a terminal command is.",
		"",
	]

	for name in report.unmatched_tools:
		excused = "excused" if name in NOT_A_COMMAND else "UNEXPLAINED"
		lines.append(f"    {name:14} no command of this name — {excused}")

	lines += [
		"",
		f"  {report.spoken} user-facing strings read, "
		f"{len(report.misspellings)} using a word we spell another way.",
		"",
	]

	for where, variant, word in report.misspellings:
		lines.append(f"    {where}  {variant!r} -> {word!r}")

	return "\n".join(lines) + "\n"


def main () -> int:
	"""Print the report. It answers a question rather than passing or failing."""

	sys.stdout.write(render(measured()))

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
