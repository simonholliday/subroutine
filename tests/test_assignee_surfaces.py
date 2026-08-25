"""Who has an item, on every surface that shows one — item ``#1266``.

``#511`` put the assignee's *name* on an item and every surface renders it as ``@username``.
**So this guard is not filling a gap; it is holding one that is already closed** — four
renderers that agree because each was written correctly, with nothing comparing them. That is
``#583``, ``#674`` and ``#992``'s shape: one fact rendered four ways, drifting a field at a
time, each surface individually plausible.

**It matters now rather than in the abstract.** With one account nothing is assigned, so a
surface that quietly stopped labelling an assignee would look identical to a correct one for as
long as this instance is one person's. The moment a second person arrives, a missing label is a
task nobody knows is theirs — and decision ``#1267`` narrows the *agenda* to one person's work,
which makes the label the only thing that says whose a row is on every other view.

**Behavioural rather than a scan.** The question is what a reader is shown, and reading the
source cannot tell a column that is computed from one that is printed — ``#511``'s own column is
dropped or kept depending on the page, which no static check could see.

**What this cannot see, said rather than implied.** There is no chokepoint every item-row
renderer passes through, so :data:`RENDERERS` is named rather than derived and a *fifth* surface
added tomorrow is invisible to it. ``tests/test_agenda_surfaces.py`` has the same blind spot for
the same reason and records it; one known gap shared by two guards is better than two unstated
ones. What would remove it is a single row-rendering seam, which is a larger change than this.

**The population is checked for staleness in both directions**, which is what stops the list
becoming a place to park a surface somebody could not be bothered to drive: every entry must
still render, and every entry must still be reachable.
"""

import datetime
import json
import pathlib
import re
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.cli.personal
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.mcp.tools
import subroutine.views
import test_web

#: The username the fixture assigns work to. Deliberately not a substring of anything else the
#: rows carry — a title, a project key or a status containing it would make every assertion
#: below pass on a surface that had dropped the label entirely.
HOLDER = "jo"

#: One instant, fixed. A surface that renders a date is not what is under test, and a real
#: clock is how `#1245` put a green gate two hours from red.
NOW = datetime.datetime(2026, 8, 26, 9, 0, tzinfo=datetime.UTC)


class Instance(typing.NamedTuple):
	"""One instance holding two items, one of them assigned."""

	world: subroutine.cli.personal.World
	client: subroutine.clients.local.Client
	application: typing.Any
	token: str
	assigned: int
	unassigned: int


@pytest.fixture
def instance (session: sqlalchemy.orm.Session) -> typing.Iterator[Instance]:
	"""Build an instance with one assigned item and one unassigned one.

	**Both, and that is the fixture's whole design.** A page where everything is assigned and a
	page where nothing is are the two states ``#511``'s ``drop_if_uniform=False`` exists to keep
	apart, and either alone would let a surface pass by rendering the column unconditionally or
	never.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Assignees",
		workspace_slug="home",
		timezone="Etc/UTC",
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Assignees"
	)

	actor = subroutine.domain.authentication.Principal(user=setup.user)
	holder = subroutine.domain.users.create(session, actor=actor, username=HOLDER)

	# **A member, because `tasks.assignee_for` will only accept one.** Assignment lists the
	# workspace's members and nothing else, so an account that merely exists cannot be given
	# work — which is the rule, and a fixture that went round it would prove nothing about a
	# row anybody could actually produce.
	subroutine.domain.workspaces.add_member(
		session, setup.workspace, holder, role_key="member", actor=actor
	)

	assigned = subroutine.domain.tasks.create(
		session,
		project=setup.inbox,
		actor=actor,
		title="Draft the announcement",
		assignee_id=holder.id,
		starts=NOW,
	)
	unassigned = subroutine.domain.tasks.create(
		session, project=setup.inbox, actor=actor, title="Order more coffee", starts=NOW
	)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True, default_timezone="Etc/UTC")
	# **Presenting the token rather than relying on local mode**, because there are two accounts
	# here and §12.1a's guess is deliberately refused once there is more than one. It is also
	# the truer arrangement: the person looking is `si`, and the whole question is what `si` is
	# shown about work that is `jo`'s.
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
			assigned=assigned.ref,
			unassigned=unassigned.ref,
		)


def _world (settings: subroutine.config.Settings) -> subroutine.cli.personal.World:
	"""Return a world with one connection and nothing colliding.

	Built rather than stubbed, the way ``tests/test_agenda_surfaces._world`` is: the address a
	row prints comes off this, and a stub answering *no collision* to anything would make the
	rendering pass against a world that had stopped asking.
	"""

	return subroutine.cli.personal.World(
		roster=subroutine.connections.Roster(connections=(), default="local"),
		current=subroutine.context.Current(connection="local", connection_source="default"),
		reached=(),
		unreachable=(),
		settings=settings,
	)


def _terminal (instance: Instance) -> dict[int, str]:
	"""Return what ``subroutine list`` puts on the page, by ref.

	``Columns.measured`` and ``_item_line`` are the terminal's own rendering, reached the way
	``tests/test_agenda_surfaces._terminal`` reaches ``agenda_rows`` — the seam below Typer and
	above the console, so what runs is the code the command runs.

	**Both rows are measured together**, because whether the column appears at all is a
	property of the page rather than of a row (`#511`).
	"""

	rows = [("local", item) for item in instance.client.tasks(order="ref")]
	columns = subroutine.cli.personal.Columns.measured(instance.world, rows)

	return {
		item.ref: subroutine.cli.personal._item_line(
			instance.world, name, item, late=False, columns=columns
		).plain
		for name, item in rows
	}


def _compact (instance: Instance) -> dict[int, str]:
	"""Return ``GET /v1/tasks?format=compact`` — §14.10's line, as a caller receives it."""

	return _by_ref(_asked(instance, "/v1/tasks?format=compact&order=ref")["items"])


def _agent (instance: Instance) -> dict[int, str]:
	"""Return ``subroutine_list`` as the lines a model is handed.

	Read off the line rather than off the call, for the reason ``test_agenda_surfaces`` gives:
	a model is handed text and has nothing else to go on.
	"""

	said = subroutine.mcp.tools._listed(instance.client, {"order": "ref"})

	return _by_ref([line for line in said.splitlines() if line.startswith("#")])


def _browser (instance: Instance, tmp_path: pathlib.Path) -> dict[int, str]:
	"""Return the marks the page draws against each row, by running the served ``app.js``.

	The app's own decision rather than a Python restatement of it — ``marks`` is one function
	and the list, the board and the agenda all go through it, so driving it once covers the
	browser's three views.
	"""

	answer = _asked(instance, "/v1/tasks?order=ref")
	drawn = test_web._ran(tmp_path, f"""
		import * as app from "{test_web._staged(tmp_path).as_uri()}";

		const items = {json.dumps(answer)}.items;

		process.stdout.write(JSON.stringify(Object.fromEntries(
			items.map((item) => [item.ref, app.marks(item, true).map((m) => m.text).join(" ")])
		)));
	""")

	return {int(ref): text for ref, text in drawn.items()}


def _asked (instance: Instance, path: str) -> dict[str, typing.Any]:
	"""Ask this instance for a listing over HTTP, the way any client does."""

	answer = api_support.call(
		instance.application,
		"GET",
		path,
		headers={"Authorization": f"Bearer {instance.token}"},
	)

	assert answer.status_code == 200, answer.text

	return typing.cast(dict[str, typing.Any], answer.json())


def _by_ref (lines: typing.Sequence[str]) -> dict[int, str]:
	"""Key rendered lines by the ref each one opens with."""

	found = {}

	for line in lines:
		match = re.match(r"#(\d+)\b", line)

		assert match is not None, f"a rendered row does not open with an address: {line}"

		found[int(match.group(1))] = line

	return found


#: Every surface that renders an item to somebody, and how to ask it.
#:
#: **Named rather than derived, because there is no seam every one of them passes through** —
#: the module docstring says what that costs. Each entry is here because a *reader* meets its
#: output: a person at a terminal, a client of the HTTP API, a model reading lines, a person
#: looking at a page.
RENDERERS: dict[str, typing.Callable[[Instance, pathlib.Path], dict[int, str]]] = {
	"the terminal": lambda instance, _tmp: _terminal(instance),
	"the compact listing": lambda instance, _tmp: _compact(instance),
	"an agent": lambda instance, _tmp: _agent(instance),
	"the browser": _browser,
}


@pytest.mark.parametrize("surface", sorted(RENDERERS))
def test_every_surface_that_shows_an_item_names_its_assignee (
	instance: Instance, tmp_path: pathlib.Path, surface: str
) -> None:
	"""`#1266`. Four renderers agree, and until now nothing compared them.

	**The label is what says whose a row is on every view except the agenda**, once decision
	`#1267` narrows that one to a person. A surface that stopped rendering it would look
	perfectly correct on a one-account instance, which is what this instance is until the day
	it is not.
	"""

	shown = RENDERERS[surface](instance, tmp_path)
	row = shown.get(instance.assigned)

	assert row is not None, f"{surface} did not render the assigned item at all: {shown}"
	assert f"@{HOLDER}" in row, f"{surface} does not say who has this: {row!r}"


@pytest.mark.parametrize("surface", sorted(RENDERERS))
def test_no_surface_puts_an_assignee_on_work_nobody_has (
	instance: Instance, tmp_path: pathlib.Path, surface: str
) -> None:
	"""The other half, and without it a surface passes by labelling every row.

	Unassigned is the ordinary state — the whole backlog on this instance — and a sigil against
	it is worse than a missing one: it reads as a person's name that happens to be blank.
	"""

	shown = RENDERERS[surface](instance, tmp_path)
	row = shown.get(instance.unassigned)

	assert row is not None, f"{surface} did not render the unassigned item at all: {shown}"
	assert "@" not in row, f"{surface} put an assignee on work nobody has: {row!r}"


def test_every_named_surface_still_renders_something (
	instance: Instance, tmp_path: pathlib.Path
) -> None:
	"""An entry that has stopped rendering makes both tests above vacuous rather than red.

	``in`` over an empty string is false, so a broken driver fails the first test in a way that
	reads as *the surface dropped the label* — the diagnosis one step wrong, on the guard whose
	whole job is to say which surface. This separates them: a driver that returns nothing is
	reported as a driver that returns nothing.
	"""

	wanted = {instance.assigned, instance.unassigned}
	short = {
		name: sorted(RENDERERS[name](instance, tmp_path))
		for name in sorted(RENDERERS)
		if not wanted <= set(RENDERERS[name](instance, tmp_path))
	}

	assert not short, f"these surfaces did not render both rows: {short}"


def test_the_named_surfaces_are_the_ones_this_project_has () -> None:
	"""A floor, because a list is only as good as somebody remembering to add to it.

	It cannot catch a fifth surface — nothing here can, and the module docstring says so — but
	it does catch the list being emptied or a driver being deleted, which is how a named
	population usually fails rather than by falling behind.
	"""

	assert len(RENDERERS) >= 4, (
		f"only {len(RENDERERS)} surfaces are driven, and this project has four that render an "
		f"item to somebody: a terminal, the compact listing, an agent and a browser"
	)
