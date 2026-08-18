"""One search, three surfaces, compared — `#1010`.

Simon, 2026-08-18, after `#1009` corrected what the surfaces *say* about search: *"we should
ensure that search works the same across all surfaces."* That is `#989`'s requirement — a person
should be able to trust every surface as one consistent guide — asked of search rather than of
the agenda, and `tests/test_agenda_surfaces.py` is the file this is modelled on.

**The divergence is not an ordering, and measuring is what said so.** The same query at the same
moment with ``limit=4``, on this project's own instance:

    terminal   989  906  1001  1010
    agent      525  440   904  1001

One row in four is shared. The cause is ``mcp/tools._listed`` asking for documents at
``limit - len(tasks)``: tasks are fetched first and documents get whatever is left, so at a small
limit a document that ranks above every task is not mis-ordered, it is **absent**.

**The rule was already written down, by the surface that gets it right.** ``cli/personal._listing``
says it in its own docstring — *"Each kind is fetched at the full limit and the merged result is
cut to it, so the cut is made across both rather than allocated between them — twenty documents
must not be able to push every task off a page."* MCP did exactly that, in the other direction.
The browser follows the terminal: it asks each collection for ``PAGE`` and merges with
``inOrder``.

**What is compared is the item set and its order, never the rendering**, which legitimately
differs — the terminal has columns, the browser has chips, an agent gets a flat line.

**And it is the *default* question that is compared.** A surface may offer extra ways to ask,
provided the question it asks when nobody says otherwise is identical everywhere — the rule
``tests/test_agenda_surfaces.py`` states and `#1005` records. So the browser being able to search
finished work, because ``include_completed`` lives in its address, is **not** a divergence: the
default excludes finished work on all three, and a bare ref finds it on all three (`#873`). Read
this before filing the terminal's missing flag as a defect; it is a convenience.

**The fixture traps, and the first is the one that would make this whole file vacuous:**

* **Tasks and documents must interleave.** A fixture whose tasks all sort above its documents
  cannot tell a merged page from a page grouped by kind — both answer identically. So they are
  created alternately, and the assertion below names the kinds so a failure says which.
* **The limit must be smaller than the number of matches**, or nothing is cut and the starvation
  is invisible. It is deliberately smaller than the count of either kind alone.
* **The term must match both kinds**, and it is asserted rather than assumed: a seed that matched
  only tasks would agree on every surface for the wrong reason.
"""

import dataclasses
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
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.documents
import subroutine.domain.tasks
import subroutine.fanout
import subroutine.mcp.tools
import test_web

#: The word every seeded item carries and nothing else does. Long and invented, because a real
#: word risks matching a seeded status, type or project name and turning a comparison of two
#: answers into a comparison of two accidents.
TERM = "quinceberry"

#: Smaller than either kind's share of the matches, which is what makes the cut observable.
#: With four of each seeded, a page of three cannot be filled by one kind without dropping the
#: other entirely — which is the defect this file was written for.
LIMIT = 3


@dataclasses.dataclass(frozen=True)
class Surfaces:
	"""One instance, reached the three ways a person and an agent reach it."""

	session: sqlalchemy.orm.Session
	world: subroutine.cli.personal.World
	client: subroutine.clients.local.Client
	application: typing.Any
	token: str
	seeded: tuple[int, ...]


@pytest.fixture
def surfaces (session: sqlalchemy.orm.Session) -> typing.Iterator[Surfaces]:
	"""Build a backlog where one word matches both kinds, alternately."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Searching"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Searching"
	)

	seeded = _seed(session, project=setup.inbox)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True)
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"), settings, session_factory=factory
	)

	with client:
		yield Surfaces(
			session=session,
			world=_world(settings, client),
			client=client,
			application=api_support.build_app(factory),
			token=issued.value.get_secret_value(),
			seeded=seeded,
		)


def _seed (
	session: sqlalchemy.orm.Session, *, project: subroutine.db.models.project.Project
) -> tuple[int, ...]:
	"""Create four tasks and four documents matching :data:`TERM`, alternately.

	**Alternately, and that is the whole fixture.** The default order is ``-created_at``, so
	creating them in turn puts the two kinds through each other — and a page grouped by kind is
	then a different page rather than the same one differently arranged. Seeded the other way
	round, every assertion here would pass against the defect.
	"""

	made: list[int] = []

	for number in range(4):
		made.append(subroutine.domain.tasks.create(
			session, project=project, title=f"{TERM} task {number}"
		).ref)
		made.append(subroutine.domain.documents.create(
			session, project=project, title=f"{TERM} document {number}"
		).ref)

	return tuple(made)


def _world (
	settings: subroutine.config.Settings, client: subroutine.clients.local.Client
) -> subroutine.cli.personal.World:
	"""Return a world holding this one instance, which is what ``_listing`` asks.

	**One connection, unlike ``tests/test_agenda_surfaces.py``'s**, and the difference is
	deliberate: that file needs two because ``_across`` concatenates and a merge across
	connections is what it is about. Here the merge under test is between the two *kinds*, which
	one connection answers — and a second would add a second ref space to a comparison whose
	whole subject is which refs came back in which order.
	"""

	return subroutine.cli.personal.World(
		roster=subroutine.connections.Roster(connections=(), default="local"),
		current=subroutine.context.Current(connection="local", connection_source="default"),
		reached=(
			subroutine.cli.personal.Reached(client=client, identity=client.identity()),
		),
		unreachable=(),
		settings=settings,
	)


def _terminal (surfaces: Surfaces, *, limit: int) -> list[int]:
	"""Return the refs ``subroutine search`` puts on the page, in the order it prints them.

	The three calls ``_listed`` makes, rather than a restatement of them: a search does not sink
	(`#877`), so the order goes out unchanged and comes back through ``_merge_order``.
	"""

	gathered = subroutine.cli.personal._listing(
		surfaces.world, limit=limit, strict=True, order=None, q=TERM
	)
	rows = subroutine.cli.personal._merged(
		surfaces.world,
		gathered,
		order=subroutine.cli.personal._merge_order(None, gathered),
	)

	return [item.ref for _name, item in rows][:limit]


def _agent (surfaces: Surfaces, *, limit: int) -> list[int]:
	"""Return the refs an agent is handed, read off the lines it actually reads."""

	said = subroutine.mcp.tools._searched(surfaces.client, {"q": TERM, "limit": limit})
	found = []

	for line in said.splitlines():
		match = re.match(r"#(\d+)\b", line)

		if match is not None:
			found.append(int(match.group(1)))

	return found


def _browser (surfaces: Surfaces, tmp_path: pathlib.Path, *, limit: int) -> list[int]:
	"""Return the refs the page renders, by asking the way the page asks and merging its way.

	Both collections at the full limit and then ``inOrder``, which is what ``load`` does — so
	this is the browser's own decision rather than a Python restatement of it.
	"""

	answers = {}

	for kind in ("tasks", "documents"):
		answer = api_support.call(
			surfaces.application,
			"GET",
			f"/v1/{kind}?limit={limit}&q={TERM}",
			headers={"Authorization": f"Bearer {surfaces.token}"},
		)

		assert answer.status_code == 200, answer.text

		answers[kind] = answer.json()["items"]

	merged = test_web._ran(tmp_path, f"""
		import * as app from "{test_web._staged(tmp_path).as_uri()}";

		const rows = [
			...{json.dumps(answers["tasks"])},
			...{json.dumps(answers["documents"])},
		];

		process.stdout.write(JSON.stringify(
			app.inOrder(rows, app.mergeOrder({{}}, rows)).map((row) => row.ref)
		));
	""")

	return list(merged)[:limit]


def test_the_seed_matches_both_kinds_and_puts_them_through_each_other (
	surfaces: Surfaces,
) -> None:
	"""The fixture can show the difference, asserted rather than assumed.

	**A seed matching only tasks would agree on every surface for the wrong reason**, and a seed
	whose kinds do not interleave cannot tell a merged page from a grouped one. Both are the
	shape that makes a comparison vacuous, so both are checked before anything is compared.
	"""

	found = _terminal(surfaces, limit=100)
	kinds = [
		"task" if ref in _refs_of(surfaces, "task") else "document" for ref in found
	]

	assert len(found) == len(surfaces.seeded), (
		f"the term matched {len(found)} of {len(surfaces.seeded)} seeded items"
	)
	assert len(set(kinds)) == 2, f"only one kind matched, so nothing here can disagree: {kinds}"
	assert kinds != sorted(kinds, key=lambda kind: kind != kinds[0]), (
		f"the kinds do not interleave, so a grouped page reads the same as a merged one: {kinds}"
	)


def test_every_surface_names_the_same_items_in_the_same_order (
	surfaces: Surfaces, tmp_path: pathlib.Path
) -> None:
	"""One question, one moment, three answers that have to be the one answer.

	Compared as a *sequence* rather than a set, because the order is the product: a search is
	ranked, and two surfaces agreeing on which items matched while disagreeing about which
	answered best is the divergence rather than the absence of one.
	"""

	terminal = _terminal(surfaces, limit=100)
	agent = _agent(surfaces, limit=100)
	browser = _browser(surfaces, tmp_path, limit=100)

	assert agent == terminal, (
		f"an agent and the terminal answered one question differently:\n"
		f"  terminal  {terminal}\n  agent     {agent}"
	)
	assert browser == terminal, (
		f"the browser and the terminal answered one question differently:\n"
		f"  terminal  {terminal}\n  browser   {browser}"
	)


def test_a_page_is_cut_across_both_kinds_rather_than_allocated_between_them (
	surfaces: Surfaces, tmp_path: pathlib.Path
) -> None:
	"""`#1010`'s sharp half, and the rule is `cli/personal._listing`'s own words.

	*"Each kind is fetched at the full limit and the merged result is cut to it, so the cut is
	made across both rather than allocated between them — twenty documents must not be able to
	push every task off a page."* An agent did the reverse: tasks were fetched first and
	documents asked for ``limit - len(tasks)``, so a document that ranked above every task was
	absent rather than late.

	**Asserted on a page smaller than either kind's share**, or there is nothing to cut and this
	passes against the defect.
	"""

	terminal = _terminal(surfaces, limit=LIMIT)
	agent = _agent(surfaces, limit=LIMIT)
	browser = _browser(surfaces, tmp_path, limit=LIMIT)

	assert len(terminal) == LIMIT, f"the fixture did not fill a page of {LIMIT}: {terminal}"

	assert agent == terminal, (
		f"a page of {LIMIT} was allocated between the kinds rather than cut across them:\n"
		f"  terminal  {terminal}\n  agent     {agent}"
	)
	assert browser == terminal, (
		f"the browser cut a page of {LIMIT} differently:\n"
		f"  terminal  {terminal}\n  browser   {browser}"
	)


def _refs_of (surfaces: Surfaces, kind: str) -> set[int]:
	"""Return the refs of one kind, for naming what a failure actually found."""

	rows = (
		surfaces.client.tasks(q=TERM, limit=100)
		if kind == "task"
		else surfaces.client.documents(q=TERM, limit=100)
	)

	return {row.ref for row in rows}
