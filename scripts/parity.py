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

import dataclasses
import pathlib
import re
import sys
import tempfile
import typing

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
		username="si", status="open", cursor="c", since=1,
	)

	try:
		with tempfile.TemporaryDirectory() as scratch:
			built = test_web._built(pathlib.Path(scratch), test_web._calls(place))

	except Exception as failed:  # pragma: no cover - Node absent or refusing
		return None, f"the builders could not be executed ({failed})"

	shapes = {
		(str(request.get("method", "GET")).upper(), _shape(str(request["path"])))
		for request in built
		if request.get("path")
	}

	return len(shapes), None


def _shape (path: str) -> str:
	"""Return a built path with its identifiers replaced, so two rows of one route are one."""

	segments = []

	for segment in path.split("?")[0].split("/"):
		known = {"w", "p", "l", "dl", "si", "c", "archived", "open"}
		segments.append("{x}" if segment and (segment.isdigit() or segment in known) else segment)

	return "/".join(segments)


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

	return "\n".join(lines) + "\n"


def main () -> int:
	"""Print the report. It answers a question rather than passing or failing."""

	sys.stdout.write(render(measured()))

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
