"""When a view has shown you less than there is, every surface says so — item ``#1543``.

Decision ``#2266`` is what this implements, and it is worth reading before changing anything
here: the shape below is its conclusion rather than a convenient arrangement.

**Four correct implementations, and until now nothing compared them.** ``has_more`` in the
envelope, *…and more* in the terminal, *More matched than are shown* in the tools, *There are
more* in the browser — each individually plausible, each with its own test, drifting a field at
a time. That is ``#583``, ``#674``, ``#992`` and ``#1266``'s shape, and it is the reason this
file exists on a property that is currently *satisfied* everywhere.

**The rule is not "the same words", and cannot be.** These idioms differ in what they can
honestly state, and every difference is argued at its own site: the board can only say *there
are more* because the rows never arrived; the links section says *5 of 18* because every row did
arrive and was counted locally; *…and more* declines a number because an exact remainder needs a
second scan of the whole result, which is the trade §8.4's ``include_total`` already declines.
Requiring one wording would force a surface either to invent a figure or to pay for a scan, and
both are worse than the drift being prevented.

So the checkable claim is ``#1543``'s three points, unchanged:

1. A view that had to stop **says so**.
2. It **names or offers the way to the rest**.
3. A view that did **not** stop says nothing — a permanent *there may be more* is §12.2a's
   column that says the same thing on every row, wearing a sentence.

**Three kinds, because there are three different questions.** *Cut* is a limit; *withheld* is
rows removed by a rule the reader can undo; *folded* is a section collapsed over rows that are
all present. A surface may legitimately have no instance of one, and :data:`REGISTER` is where
that is written down rather than inferred from an absence.

**Scope is a property of an entry, and the board is what proves it is needed.** ``#1790``'s own
argument is that a notice at the foot of a board is not reached by a glance at one column, so
*cut at page scope* and *cut at section scope* are two disclosures rather than one rendering of
one. Measured: the column notice and ``#1845``'s heading ``+`` **are** one derivation read twice
— ``tally`` reads the same ``held(column)`` the notice reads — while the board's footer is a
separate statement about the page.

**The cut class is driven; the other two are classified and not yet driven.** That is decision
``#2266``'s staging and its reason: shipping the crisp property first makes the rest *visibly*
unclassified rather than silently absent, and only a known gap can be finished.

**What this cannot see, said rather than implied.** There is no chokepoint every listing
renderer passes through, so :data:`REGISTER` names its surfaces rather than deriving them and a
*fifth* one added tomorrow is invisible to it. ``tests/test_assignee_surfaces.py`` and
``tests/test_agenda_surfaces.py`` have the same blind spot for the same reason and both record
it; **one known gap shared by three guards is better than three unstated ones.** What would
remove it is a single listing seam, which is a larger change than this.
"""

import pathlib
import typing
import uuid

import pytest
import sqlalchemy.orm
import typer.testing

import api_support
import subroutine.cli.main
import subroutine.cli.personal
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.mcp.tools
import subroutine.views
import test_web

#: The three questions a reader can be answering when a view holds something back. They are not
#: interchangeable: *cut* is undone by asking for more, *withheld* by lifting a rule, *folded*
#: by opening a section over rows that were never missing.
CUT = "cut"
WITHHELD = "withheld"
FOLDED = "folded"

KINDS = (CUT, WITHHELD, FOLDED)

#: Where a disclosure sits. An entry may carry both — see the module docstring on the board.
PAGE = "page"
SECTION = "section"

#: Every surface a reader meets a listing on. **Named rather than derived**; the docstring says
#: what that costs and which two other guards share the cost.
SURFACES = ("HTTP", "MCP", "the browser", "the terminal")

#: How many rows the fixture holds, and the limit it asks for. Two apart rather than one, so a
#: surface cannot pass by treating *exactly one page* as the boundary — ``#1071`` records that
#: inference being wrong in both directions on the tools.
ROWS = 5
LIMIT = 2


class Entry(typing.NamedTuple):
	"""What one surface does about one kind of holding-back."""

	#: Where the disclosure sits, empty when there is none to place.
	scopes: tuple[str, ...]
	#: What it is called, or ``None`` where this surface has no instance of the kind — or has
	#: one nobody has classified yet.
	idiom: str | None
	#: Why, in prose. Required on every entry, because an absence is the thing most easily
	#: mistaken for a gap and an unwritten reason cannot be argued with.
	why: str


#: Every ``(surface, kind)`` and what that surface does about it — decision ``#2266``.
#:
#: **Complete by derivation**: :func:`test_the_register_classifies_every_surface_against_every_kind`
#: builds the product of :data:`SURFACES` and :data:`KINDS` and refuses a gap or a stranger, so
#: adding either forces a written answer rather than allowing a silent one.
REGISTER: dict[tuple[str, str], Entry] = {
	("HTTP", CUT): Entry(
		(PAGE,),
		"has_more, with next_cursor",
		"The envelope carries the flag and the way on together, which is why it is the "
		"reference the other three are compared against rather than one of the four.",
	),
	("HTTP", WITHHELD): Entry(
		(),
		None,
		"A filter is the caller's own parameter, so nothing is withheld by a rule they did "
		"not state. `ready` and `to_act_on` narrow because they were asked for, and the "
		"request that produced the page is the disclosure.",
	),
	("HTTP", FOLDED): Entry(
		(),
		None,
		"Nothing is collapsed in a JSON envelope. Folding is a reading affordance and this "
		"surface has no reader to afford it to — a boundary rather than a gap.",
	),
	("MCP", CUT): Entry(
		(PAGE,),
		"More matched than are shown",
		"One sentence naming both remedies — raise the limit, or narrow with project or "
		"filter — because a model is handed text and has nothing else to go on. No figure, "
		"because `has_more` is a flag and counting costs §8.4's second scan.",
	),
	("MCP", WITHHELD): Entry(
		(PAGE,),
		"held_back",
		"`#1610`'s other half, on the surface it matters most to: `ready` is the call an "
		"agent makes with no other context, so *there is nothing to do* and *all of it is "
		"waiting on something above it* are otherwise the same page. A count here where the "
		"cut is a flag, because that set is bounded by work under blocked ancestors.",
	),
	("MCP", FOLDED): Entry(
		(),
		None,
		"A model is handed one flat body. The agenda's buckets are deliberately flattened for "
		"this surface — a reader paying for five headings over five rows — so there is no "
		"section to collapse. A boundary.",
	),
	("the terminal", CUT): Entry(
		(PAGE,),
		"…and more",
		"A flag and the literal command that widens it, assembled from the arguments this "
		"listing was given so the way on is the reader's own query rather than a generic "
		"hint. It declines a count deliberately.",
	),
	("the terminal", WITHHELD): Entry(
		(PAGE,),
		"put off, and what readiness held back",
		"Two counts rather than a flag, and affordable for one reason: each set is small by "
		"construction — the parked work, and the work under blocked ancestors — so neither is "
		"a second scan of the result. `#72`, `#73` and `#1610`.",
	),
	("the terminal", FOLDED): Entry(
		(),
		None,
		"Nothing on this surface collapses. The agenda's buckets are printed in full and a "
		"listing is one sequence; a reader scrolls rather than opens. A boundary.",
	),
	("the browser", CUT): Entry(
		(PAGE, SECTION),
		"There are more, and a column's own notice",
		"Both scopes, and this is the pair that put scope in the register. `#1790` gave every "
		"board column its own allowance and notice because a footer four columns wide is not "
		"reached by a glance at one column; `#1845` put the same derivation on the heading. "
		"Measured: the notice and the heading `+` are one derivation read twice, and the "
		"board's footer is a third statement at page scope.",
	),
	("the browser", WITHHELD): Entry(
		(PAGE,),
		"the narrowed bar",
		"`#1020` and `#2173`. Every narrowing says so in a sentence that also offers the way "
		"back — including the collapse, which is worded *hiding* rather than *showing* "
		"because it takes rows away where every other line narrows to something.",
	),
	("the browser", FOLDED): Entry(
		(SECTION,),
		"a shut column's heading tally",
		"The only surface with anything folded. A shut column still states what it holds, so "
		"a reader can tell *nothing left* from *not looking* without opening it.",
	),
}

#: The kinds this file drives against a real instance, as opposed to classifying in prose.
#:
#: **`CUT` alone, and that is staged rather than partial** — decision ``#2266``. All four
#: surfaces already satisfy it, so populating it first buys a working guard immediately and
#: leaves the other two kinds *visibly* unclassified. A known gap can be finished; an unknown
#: one cannot.
DRIVEN = (CUT,)


class Instance(typing.NamedTuple):
	"""One instance holding more rows than a page will show."""

	world: subroutine.cli.personal.World
	client: subroutine.clients.local.Client
	application: typing.Any
	token: str


@pytest.fixture
def instance (session: sqlalchemy.orm.Session) -> typing.Iterator[Instance]:
	"""Build an instance with :data:`ROWS` tasks in it."""

	setup = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Truncation",
		workspace_slug="home",
		timezone="Etc/UTC",
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Truncation"
	)

	actor = subroutine.domain.authentication.Principal(user=setup.user)

	for number in range(ROWS):
		subroutine.domain.tasks.create(
			session, project=setup.inbox, actor=actor, title=f"Thing number {number}"
		)

	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True, default_timezone="Etc/UTC")
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		settings,
		session_factory=factory,
		token=issued.value.get_secret_value(),
	)

	with client:
		yield Instance(
			world=_world(settings),
			client=client,
			application=api_support.build_app(factory),
			token=issued.value.get_secret_value(),
		)


def _world (settings: subroutine.config.Settings) -> subroutine.cli.personal.World:
	"""The terminal's view of one local connection."""

	return subroutine.cli.personal.World(
		roster=subroutine.connections.Roster(connections=(), default="local"),
		current=subroutine.context.Current(connection="local", connection_source="default"),
		reached=(),
		unreachable=(),
		settings=settings,
	)


def _http (instance: Instance, limit: int, _tmp: pathlib.Path) -> str:
	"""Return the envelope's own account of whether it stopped."""

	answer = api_support.call(
		instance.application,
		"GET",
		f"/v1/tasks?order=ref&limit={limit}",
		headers={"Authorization": f"Bearer {instance.token}"},
	)

	assert answer.status_code == 200, answer.text

	page = answer.json()["page"]

	# **The cursor's *presence* rather than its name**, and the difference is the whole
	# assertion. Interpolating `next_cursor={value}` puts the field name in the string whether
	# or not a cursor came back, so the check would be reading this driver's own f-string —
	# measured, by withholding the cursor in `api/tasks.py` and watching the test pass. HTTP is
	# the one surface with no prose to read, so the claim has to be rendered into some, and
	# rendering the field name is not rendering the claim.
	return "; ".join((
		f"has_more={page['has_more']}",
		"a cursor is offered" if page["next_cursor"] else "no cursor is offered",
	))


def _agent (instance: Instance, limit: int, _tmp: pathlib.Path) -> str:
	"""Return ``subroutine_list`` as the body a model is handed."""

	return subroutine.mcp.tools._listed(instance.client, {"order": "ref", "limit": limit})


def _terminal (_instance: Instance, limit: int, _tmp: pathlib.Path) -> str:
	"""Return what ``subroutine list`` prints, by running the real command.

	**The whole command rather than the seam under it**, unlike the three drivers above and
	unlike ``tests/test_assignee_surfaces``. The sentence this file is about is assembled
	*inside* ``_listed`` — the two lines beside it, ``_say_parked`` and ``_say_held_back``, are
	extracted helpers and this one is not — so driving a seam would leave the branch that
	decides whether to print at all untested, which on this surface is the half that has
	actually broken (`#646`: the list stopped dead at fifty and said nothing).

	**Its own instance, and that is sound rather than a compromise.** The claim is per surface —
	*when this view stopped, did it say so and name the way on* — so the four need not share
	rows. ``tests/conftest.py`` gives every test an empty XDG home, which is what makes running
	the real CLI here safe.
	"""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str) -> typer.testing.Result:
		"""Run one command and refuse to read the output of a command that failed."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == 0, f"{arguments}: {result.output}\n{result.exception!r}"

		return result

	invoke("init")

	for number in range(ROWS):
		invoke("add", f"Thing number {number}")

	return invoke("list", "--limit", str(limit)).output


def _browser (instance: Instance, limit: int, tmp_path: pathlib.Path) -> str:
	"""Return what the listing draws under the rows, by running the served ``app.js``.

	The app's own component rather than a Python restatement of it, and ``Listing`` is where
	both the sentence and the control live.
	"""

	answer = api_support.call(
		instance.application,
		"GET",
		f"/v1/tasks?order=ref&limit={limit}",
		headers={"Authorization": f"Bearer {instance.token}"},
	)

	assert answer.status_code == 200, answer.text

	body = answer.json()
	page = body["page"]

	# `more` is what `App` hands the component: the cursor per kind, or null where there is
	# nothing behind it. Built here the way the app builds it, from the same envelope.
	more = {"tasks": page["next_cursor"] if page["has_more"] else None, "documents": None}

	return test_web._rendered(tmp_path, {"Listing": {
		"items": body["items"],
		"more": more,
	}})["Listing"]


#: How to ask each surface what it says about a page that was cut. **One entry per surface in
#: :data:`SURFACES`**, held to that by the coverage test below.
DRIVERS: dict[str, typing.Callable[[Instance, int, pathlib.Path], str]] = {
	"HTTP": _http,
	"MCP": _agent,
	"the browser": _browser,
	"the terminal": _terminal,
}

#: What each surface must be saying once it has stopped, and the way on it must offer with it.
#:
#: **Two assertions per surface rather than one**, which is ``#1543``'s whole point: a flag with
#: no route is a reader told they are stuck. The phrases are the surfaces' own words, so a
#: rewording is a decision that comes past this file rather than a silent change.
SAYS: dict[str, tuple[str, str]] = {
	"HTTP": ("has_more=True", "a cursor is offered"),
	"MCP": ("More matched than are shown", "Raise limit"),
	"the browser": ("There are more", "Show more"),
	"the terminal": ("…and more", "to see further"),
}


def test_the_register_classifies_every_surface_against_every_kind () -> None:
	"""No combination is left to be inferred from an absence — decision ``#2266``.

	**Derived rather than counted**, so adding a surface or a kind forces a written answer. The
	failure this is written against is the one ``#1543`` describes: a guard whose name implies
	it covers a population it was never given.
	"""

	wanted = {(surface, kind) for surface in SURFACES for kind in KINDS}

	missing = sorted(wanted - set(REGISTER))
	strangers = sorted(set(REGISTER) - wanted)

	assert not missing, (
		f"{missing} have no entry, so what those surfaces do about that kind of holding-back "
		f"is decided by nothing. Say what they do, or write down why the kind cannot arise."
	)
	assert not strangers, (
		f"{strangers} are registered and name no surface or kind this file knows. Rename them, "
		f"or add the surface to SURFACES so it is driven as well as described."
	)


def test_every_entry_gives_a_reason_and_places_what_it_claims () -> None:
	"""An entry is prose or it is nothing, and a named idiom has to sit somewhere.

	**Both directions.** An idiom with no scope cannot be found by a reader; a scope with no
	idiom claims a disclosure that does not exist. The pair is what stops the register becoming
	a place to park a surface nobody looked at.
	"""

	thin = sorted(key for key, entry in REGISTER.items() if len(entry.why) < 60)

	assert not thin, (
		f"{thin} have a reason too short to be one. This register's value is the argument, not "
		f"the classification — every other excuse list here is held to the same floor."
	)

	unplaced = sorted(key for key, entry in REGISTER.items() if entry.idiom and not entry.scopes)
	unclaimed = sorted(
		key for key, entry in REGISTER.items() if entry.scopes and not entry.idiom
	)

	assert not unplaced, f"{unplaced} name a disclosure and never say where a reader meets it"
	assert not unclaimed, f"{unclaimed} claim a place for a disclosure they do not name"

	badly = sorted(
		key
		for key, entry in REGISTER.items()
		if set(entry.scopes) - {PAGE, SECTION}
	)

	assert not badly, f"{badly} sit somewhere this file has no name for: {badly}"


def test_the_driven_kinds_have_a_driver_for_every_surface () -> None:
	"""``DRIVEN`` is a promise, and this is what makes it one.

	A kind named as driven whose surfaces are only *described* is the failure ``#1539`` records
	twice over: a control that is specified, documented and inert. Every surface must be asked a
	real question about every driven kind, or the kind is not driven.
	"""

	assert set(DRIVERS) == set(SURFACES), (
		f"the drivers and the surfaces disagree: {sorted(set(DRIVERS) ^ set(SURFACES))}. Every "
		f"surface named here is one a reader meets, so every one of them is asked."
	)

	assert set(SAYS) == set(SURFACES), (
		f"the expected wordings and the surfaces disagree: {sorted(set(SAYS) ^ set(SURFACES))}"
	)

	silent = sorted(
		(surface, kind)
		for surface in SURFACES
		for kind in DRIVEN
		if REGISTER[(surface, kind)].idiom is None
	)

	assert not silent, (
		f"{silent} are in a driven kind and disclose nothing. A surface that holds rows back "
		f"and says so nowhere is exactly what this file exists to refuse."
	)


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_every_surface_says_so_and_names_the_way_on_when_a_page_was_cut (
	instance: Instance, tmp_path: pathlib.Path, surface: str
) -> None:
	"""``#1543`` points 1 and 2, on all four at once.

	**Two assertions, because a flag without a route is the worse half.** A reader told their
	view is limited and not told how to widen it has been informed of a dead end, which is
	exactly what decision ``#1539`` forbids — *a surface may lack a capability, but it may never
	leave somebody stuck*.
	"""

	said = DRIVERS[surface](instance, LIMIT, tmp_path)
	discloses, way_on = SAYS[surface]

	assert discloses in said, (
		f"{surface} showed {LIMIT} of {ROWS} rows and did not say it had stopped. A listing "
		f"that quietly omits things stops supporting the inference refs exist for — that *not "
		f"in the list* means *not in the system*:\n{said}"
	)

	assert way_on in said, (
		f"{surface} said it had stopped and did not name the way to the rest, which leaves a "
		f"reader stuck where the other three do not:\n{said}"
	)


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_no_surface_says_it_stopped_when_it_did_not (
	instance: Instance, tmp_path: pathlib.Path, surface: str
) -> None:
	"""``#1543`` point 3, and it is what stops the fix being a banner.

	A permanent *there may be more* is §12.2a's column that says the same thing on every row,
	wearing a sentence — and a reader learns to ignore a line that is only sometimes true, which
	costs the disclosure above its whole value.
	"""

	said = DRIVERS[surface](instance, ROWS + 1, tmp_path)
	discloses, _way_on = SAYS[surface]

	assert discloses not in said, (
		f"{surface} showed every row there is and still said it had stopped. A notice that is "
		f"always there is one nobody reads:\n{said}"
	)
