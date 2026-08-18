"""One agenda, three surfaces, compared — `#992`.

Simon, 2026-08-18: a person working across Claude Code, the browser and the terminal *"should
not get different results from each. They should be able to trust all surfaces as a consistent
guide to what to work on next."* Decision `#989` is the record.

**The function is already one.** ``domain/agenda.build`` is a single implementation every
surface reaches, so nothing here is about the domain: the divergence is entirely in what each
surface *asks for* and what it does to the answer *afterwards*. Nothing compared those, which
is exactly why they drifted — ``tests/test_web.py`` ties the terminal's section list to the
browser's, so the *labels* could not disagree and the *contents* were free to.

This is `#583` / `#674`'s shape asked of a third surface: one fact rendered three ways with
nothing comparing them.

**What is compared is the item set and its order, per bucket, from one instance at one
moment** — never the rendering, which legitimately differs. The terminal has columns, the
browser has chips, an agent gets a flat line.

**The fixture is where this guard could go blind, and every trap below is a recorded one:**

* **One workspace cannot see the tiebreak.** The server breaks ties on ``created_at`` and the
  terminal breaks them on ``ref``; refs are allocated per workspace, so those agree until the
  agenda spans two. Two are seeded, with the earlier-created task holding the *higher* ref.
* **One connection cannot see the merge**, because ``_across`` concatenates.
* **UTC cannot see the timezone divergence.** The instance is ``Australia/Sydney`` and the
  typing machine is ``Europe/London``, which is `#532`'s and `#773`'s own choice: Sydney's
  abbreviations are never zone names, and it is far enough east that the two are on different
  dates at the instant these run against.
* **An empty ``upcoming`` makes the horizon difference invisible**, which is exactly how `#985`
  survived. Something is seeded inside the week and something outside it.
* **A real clock makes the timezone assertion pass for part of the day**, which is `#737`'s
  shape — a fixture that passes because of *when* it ran. The instant is fixed.

**Five of these are ``xfail(strict=True)`` and each names the item that removes the mark.**
The guard is built *second* rather than last, deliberately (`#990`), so it is written against
the divergence as it stands and each remaining fix turns one assertion green. ``strict`` is
what makes that a ratchet rather than a note: the day a fix lands, the mark itself fails until
somebody takes it off, so the guard cannot be left describing a defect that has gone.
"""

import contextlib
import dataclasses
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
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.domain.workspaces
import subroutine.fanout
import subroutine.mcp.tools
import subroutine.views
import test_web

#: The zone the *instance* keeps, and therefore the one §6.5's chain resolves to for a reader
#: whose account says nothing. Sydney because its abbreviations are never zone names (`#532`)
#: and because it is far enough east to be on a different date from London for half the day.
INSTANCE_ZONE = "Australia/Sydney"

#: The zone the *typing machine* is in, which is what ``subroutine agenda`` currently sends as
#: an explicit override (`#995`). Different from the instance's on purpose.
MACHINE_ZONE = "Europe/London"

#: One instant, fixed, so which day this is about never depends on when the suite ran. In
#: Sydney this is 2026-11-05 02:30; in London it is still 2026-11-04.
MOMENT = datetime.datetime(2026, 11, 4, 15, 30, tzinfo=datetime.UTC)

#: The day :data:`MOMENT` falls on where the instance lives. Every seeded date is written
#: relative to this, so the arrangement reads as a day rather than as a list of dates.
TODAY = datetime.date(2026, 11, 5)

#: The buckets, taken from the terminal's section list rather than written out again, so a
#: sixth one is compared the day somebody adds it.
BUCKETS = tuple(field for _heading, field, _late in subroutine.cli.personal.AGENDA_SECTIONS)


@dataclasses.dataclass(frozen=True)
class Surfaces:
	"""One instance, reached the three ways a person and an agent reach it."""

	session: sqlalchemy.orm.Session
	world: subroutine.cli.personal.World
	client: subroutine.clients.local.Client
	application: typing.Any
	token: str

	def gathered (
		self, **asked: typing.Any
	) -> subroutine.fanout.Gathered[subroutine.views.Agenda]:
		"""Ask this instance for an agenda the way ``fanout`` hands one to the terminal."""

		return subroutine.fanout.gather(
			[self.client], lambda client: client.agenda(**asked), strict=True
		)


@pytest.fixture
def surfaces (
	session: sqlalchemy.orm.Session,
) -> typing.Iterator[Surfaces]:
	"""Build a day with something in every bucket, across two workspaces."""

	setup = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Surfaces",
		workspace_slug="home",
		timezone=INSTANCE_ZONE,
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Surfaces"
	)

	work = subroutine.domain.workspaces.create(
		session,
		slug="work",
		title="Work",
		owner=setup.user,
		timezone=INSTANCE_ZONE,
	)
	elsewhere = subroutine.domain.bootstrap.inbox_for(session, work)

	assert elsewhere is not None, "a workspace is created with an inbox (`#301`)"

	_seed(session, home=setup.inbox, work=elsewhere, space=work)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(
		dev_mode=True, default_timezone=MACHINE_ZONE
	)
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"), settings, session_factory=factory
	)

	with client:
		yield Surfaces(
			session=session,
			world=_world(settings),
			client=client,
			application=api_support.build_app(factory),
			token=issued.value.get_secret_value(),
		)


def test_the_sections_cover_every_bucket_the_agenda_carries () -> None:
	"""A sixth bucket is compared the day it exists, rather than the day somebody notices.

	Derived rather than listed, which is the whole reason :data:`BUCKETS` reads the terminal's
	section list. This is the other half: a bucket that reaches ``views.Agenda`` and never
	reaches the sections would be invisible to every comparison below.
	"""

	carried = {
		name
		for name, field in subroutine.views.Agenda.model_fields.items()
		if typing.get_origin(field.annotation) is list
	}

	assert carried == set(BUCKETS), (
		f"the agenda carries {sorted(carried)} and the sections name {sorted(BUCKETS)}"
	)


@pytest.mark.xfail(strict=True, reason="`#995` — the terminal sends the typing machine's zone")
def test_every_surface_asks_the_agenda_for_the_same_day_in_the_same_zone (
	surfaces: Surfaces, tmp_path: pathlib.Path
) -> None:
	"""Decision `#989`: the reader's own timezone decides the buckets, on every surface.

	**Which day the whole answer is about** is a larger inconsistency than any ordering, and
	nothing reports it. The terminal fills §6.5's ``explicit`` slot with the *typing machine's*
	zone — which on a mismatched pair is a third answer matching neither instance — where the
	browser deliberately refuses to, with the reason written into ``agendaRequest``.
	"""

	asked = subroutine.cli.personal.agenda_asked(
		surfaces.world, workspace=None, now=MOMENT
	)
	browser = _browser_asked(tmp_path)
	agent = subroutine.mcp.tools._agenda_asked({})

	said = {
		"the terminal": (asked.get("date"), asked.get("timezone")),
		"the browser": (browser.get("date"), browser.get("timezone")),
		"an agent": (agent.get("date"), agent.get("timezone")),
	}

	assert set(said.values()) == {(None, None)}, (
		f"the day and the zone are the chain's to decide, and {said} is three answers"
	)


@pytest.mark.xfail(strict=True, reason="`#991` — an agent asks for no look-ahead at all")
def test_every_surface_asks_for_the_same_look_ahead (
	surfaces: Surfaces, tmp_path: pathlib.Path
) -> None:
	"""`#985` and `#991`: a bucket nobody asks for is empty, and looks like a quiet week.

	``GET /v1/agenda`` omits ``upcoming`` unless asked, so a surface rendering a heading for it
	and never requesting it shows nothing however much is due — which is not a rendering fault
	and is invisible to every test that hands the renderer its own input.
	"""

	said = {
		"the terminal": subroutine.cli.personal.agenda_asked(
			surfaces.world, workspace=None, now=MOMENT
		).get("horizon_days"),
		"the browser": _browser_asked(tmp_path).get("horizon_days"),
		"an agent": subroutine.mcp.tools._agenda_asked({}).get("horizon_days"),
	}

	assert len(set(said.values())) == 1, f"three look-aheads, not one: {said}"


@pytest.mark.xfail(strict=True, reason="`#991`, `#993`, `#995` — all three divergences land here")
def test_every_surface_names_the_same_work_in_the_same_order (
	surfaces: Surfaces, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The claim `#990` is about: one instance, one moment, one answer.

	Compared per bucket and in order, because both halves have diverged for different reasons —
	the contents by what each surface asks for, the order by what each does afterwards.
	"""

	with _at(monkeypatch, MOMENT):
		terminal = _terminal(surfaces)
		browser = _browser(surfaces, tmp_path)
		agent = _agent(surfaces)

	assert terminal == browser, _difference("the terminal", terminal, "the browser", browser)

	# An agent reads one flat list rather than five headings (`#989`), so its claim is the
	# concatenation in bucket order — which is what the terminal prints down the page.
	flat = [ref for bucket in BUCKETS for ref in terminal[bucket]]

	assert [ref for ref, _bucket in agent] == flat, (
		f"an agent is told {[ref for ref, _bucket in agent]} where the page says {flat}"
	)


@pytest.mark.xfail(strict=True, reason="`#993` — `--json` never calls `_in_order`")
def test_the_scripted_agenda_is_ordered_like_the_one_a_person_reads (
	surfaces: Surfaces, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#993`'s third half: ``--json`` calls ``_across`` and never ``_in_order``.

	On one connection the two coincide, which is why nothing has caught it. Driven against a
	world holding two so the concatenation is visible — the defect ``_render``'s own comment
	records as fixed for the rendered path, still live on the scripted one.
	"""

	with _at(monkeypatch, MOMENT):
		asked = subroutine.cli.personal.agenda_asked(
			surfaces.world, workspace=None, now=MOMENT
		)
		gathered = _twice(surfaces.gathered(**asked))

		rendered = {
			bucket: [task.ref for _name, task in rows]
			for bucket, rows in subroutine.cli.personal.agenda_rows(
				surfaces.world, gathered
			).items()
		}
		scripted = subroutine.cli.personal._agenda_json(surfaces.world, gathered)

	said = {bucket: [row["ref"] for row in scripted[bucket]] for bucket in BUCKETS}

	assert said == rendered, _difference("--json", said, "the page", rendered)


@pytest.mark.xfail(strict=True, reason="`#991` — an agent's rows carry no bucket at all")
def test_every_agenda_row_an_agent_reads_says_which_bucket_it_is_in (
	surfaces: Surfaces, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#989`'s condition on full parity, and the measurement that decided it.

	The rows carry no bucket label at all today, so overdue is distinguishable from due-today
	only by comparing dates — and a backlog row would be distinguishable from a commitment only
	by the absence of a deadline. **The label is what earns the bytes**: without it, flat parity
	would be worse than the drop it replaces.
	"""

	with _at(monkeypatch, MOMENT):
		agent = _agent(surfaces)
		terminal = _terminal(surfaces)

	assert agent, "the seeded day is not empty"

	wanted = [(ref, bucket) for bucket in BUCKETS for ref in terminal[bucket]]

	assert agent == wanted, (
		"a row says which bucket it is in, and says the same one the page does —\n\t"
		f"an agent reads {agent}\n\tthe page says {wanted}"
	)


def _seed (
	session: sqlalchemy.orm.Session,
	*,
	home: subroutine.db.models.project.Project,
	work: subroutine.db.models.project.Project,
	space: subroutine.db.models.identity.Workspace,
) -> None:
	"""Fill every bucket, and arrange the two orderings to disagree where they can.

	**Each tie is built so that the earlier-created task carries the *higher* ref**, because
	that is the only arrangement the two rules disagree about: the server breaks a tie on
	``created_at`` ascending and the terminal breaks it on ``ref``. A fixture where the two
	happen to agree passes against both, which is how a fixture stops being a guard.

	The same trick is played on ``upcoming``, where the server's second key is ``starts_at``
	and the terminal's is the ref again — so the task starting *soonest* is given the higher
	number.

	**Refs are made globally unique**, by starting the second workspace's counter high. They
	are unique per workspace by design (§6.2), so two workspaces both holding a ``#2`` would
	make every message below ambiguous about which item it meant — and a comparison that reads
	the same either way is one that cannot report what it found.
	"""

	space.next_ref_number = 100
	session.flush()

	made = [
		subroutine.domain.tasks.create(
			session,
			project=home,
			title="Renew the passport",
			due=TODAY - datetime.timedelta(days=4),
			timezone=INSTANCE_ZONE,
		),
		subroutine.domain.tasks.create(
			session, project=home, title="Pay the rent", due=TODAY, timezone=INSTANCE_ZONE
		),
		subroutine.domain.tasks.create(
			session, project=home, title="Draft the report", status_key="in_progress"
		),
		# Later than the one below it and numbered lower, which is what separates
		# *soonest first* from *lowest ref first*.
		subroutine.domain.tasks.create(
			session,
			project=home,
			title="Team offsite",
			starts=TODAY + datetime.timedelta(days=5),
			timezone=INSTANCE_ZONE,
		),
		subroutine.domain.tasks.create(
			session,
			project=work,
			title="Dentist",
			starts=TODAY + datetime.timedelta(days=2),
			timezone=INSTANCE_ZONE,
		),
		# The tie: same rank, created first, numbered last.
		subroutine.domain.tasks.create(
			session, project=work, title="Write the spec", importance=3, urgency=3
		),
		subroutine.domain.tasks.create(
			session, project=home, title="Tidy the shed", importance=3, urgency=3
		),
	]

	# Written rather than left to the clock, so a tie is a property of the fixture rather than
	# of how fast the machine ran. Seconds apart, in creation order.
	for index, task in enumerate(made):
		task.created_at = MOMENT - datetime.timedelta(days=30, seconds=len(made) - index)

	session.flush()


def _world (settings: subroutine.config.Settings) -> subroutine.cli.personal.World:
	"""Return a world with nothing colliding, which is what the flatten asks about.

	Built rather than stubbed: a stub answering *no collision* to anything would make every
	ordering below pass against a merge that had stopped asking (`#942`).
	"""

	return subroutine.cli.personal.World(
		roster=subroutine.connections.Roster(connections=(), default="local"),
		current=subroutine.context.Current(connection="local", connection_source="default"),
		reached=(),
		unreachable=(),
		settings=settings,
	)


def _twice (
	gathered: subroutine.fanout.Gathered[subroutine.views.Agenda],
) -> subroutine.fanout.Gathered[subroutine.views.Agenda]:
	"""Return one instance's answer as though two connections had given it.

	**A fixture with one connection cannot see the merge**, because ``_across`` concatenates
	and one sorted run is already in order. The second name is a different connection carrying
	the same rows, which is enough to make a concatenation visible and does not pretend to be
	a second instance — ``refuse_duplicate_instances`` is the thing that would object, and this
	world holds no collision to raise.
	"""

	answer = gathered.answers[0]

	return subroutine.fanout.Gathered(
		answers=(
			answer,
			subroutine.fanout.Answer(
				connection=subroutine.connections.Connection(name="work", url=None),
				value=answer.value,
			),
		),
		failures=(),
	)


def _terminal (surfaces: Surfaces) -> dict[str, list[int]]:
	"""Return what ``subroutine agenda`` puts on the page, bucket by bucket."""

	asked = subroutine.cli.personal.agenda_asked(surfaces.world, workspace=None, now=MOMENT)
	rows = subroutine.cli.personal.agenda_rows(surfaces.world, surfaces.gathered(**asked))

	return {bucket: [task.ref for _name, task in rows[bucket]] for bucket in BUCKETS}


def _browser (surfaces: Surfaces, tmp_path: pathlib.Path) -> dict[str, list[int]]:
	"""Return what the page renders, by asking for it the way the page does.

	The request comes from ``agendaRequest()`` and the grouping from ``agendaBuckets``, both
	run in Node against the served ``app.js`` — so this is the browser's own decision rather
	than a Python restatement of it.
	"""

	asked = _browser_request(tmp_path)
	answer = api_support.call(
		surfaces.application,
		asked["method"],
		f"/v1{asked['path']}",
		headers={"Authorization": f"Bearer {surfaces.token}"},
	)

	assert answer.status_code == 200, answer.text

	grouped = test_web._ran(tmp_path, f"""
		import * as app from "{test_web._staged(tmp_path).as_uri()}";

		const agenda = {json.dumps(answer.json())};

		process.stdout.write(JSON.stringify(app.agendaBuckets(agenda, [])));
	""")
	found = {
		bucket["key"]: [item["ref"] for item in bucket["items"]] for bucket in grouped
	}

	# `agendaBuckets` drops an empty bucket rather than printing a heading over nothing, so
	# an absent one is the page saying there is nothing in it.
	return {bucket: found.get(bucket, []) for bucket in BUCKETS}


def _agent (surfaces: Surfaces) -> list[tuple[int, str | None]]:
	"""Return what an agent is given: each ref, and which bucket the row says it is in.

	**Read off the line rather than off the call**, because the line is the surface — a model
	is handed text and has nothing else to go on. The bucket is whichever cell names one, and
	it comes before the status so a workspace that renames a status to one of these words
	cannot be read as a bucket (§5.5 makes that vocabulary theirs).
	"""

	found: list[tuple[int, str | None]] = []

	for line in _agent_lines(surfaces):
		match = re.match(r"#(\d+)\b", line)

		assert match is not None, f"every row opens with an address: {line}"

		cells = [cell.strip() for cell in line.split("  ") if cell.strip()]
		found.append((
			int(match.group(1)),
			next((cell for cell in cells if cell in BUCKETS), None),
		))

	return found


def _agent_lines (surfaces: Surfaces) -> list[str]:
	"""Return ``subroutine_list(today=true)`` as the lines a model reads."""

	said = subroutine.mcp.tools._listed(surfaces.client, {"today": True})

	return [line for line in said.splitlines() if line.startswith("#")]


def _browser_asked (tmp_path: pathlib.Path) -> dict[str, typing.Any]:
	"""Return the browser's request as the parameters the other two name."""

	asked = _browser_request(tmp_path)
	_path, _, query = asked["path"].partition("?")
	given = dict(
		pair.split("=", 1) for pair in query.split("&") if pair
	)

	return {
		"date": given.get("date"),
		"timezone": given.get("timezone"),
		"horizon_days": None if "horizon_days" not in given else int(given["horizon_days"]),
		"workspace": given.get("workspace_id"),
	}


def _browser_request (tmp_path: pathlib.Path) -> dict[str, typing.Any]:
	"""Return what ``agendaRequest()`` asks for, run in Node against the served module."""

	answer = test_web._ran(tmp_path, f"""
		import * as app from "{test_web._staged(tmp_path).as_uri()}";

		process.stdout.write(JSON.stringify(app.agendaRequest()));
	""")

	return typing.cast(dict[str, typing.Any], answer)


@contextlib.contextmanager
def _at (monkeypatch: pytest.MonkeyPatch, moment: datetime.datetime) -> typing.Iterator[None]:
	"""Hold the clock still for the duration of one comparison.

	**Around the reads and not around the seeding**, because the rows need distinct creation
	instants and the buckets need one moment. A guard that let the real clock decide which day
	this was would agree with itself for part of every day whatever the surfaces did, which is
	the shape `#737` records.
	"""

	monkeypatch.setattr(subroutine.db.types, "utcnow", lambda: moment)

	try:
		yield

	finally:
		monkeypatch.undo()


def _difference (
	one: str, first: dict[str, list[int]], other: str, second: dict[str, list[int]]
) -> str:
	"""Name the buckets that disagree, rather than printing two dictionaries."""

	said = [
		f"{bucket}: {one} says {first.get(bucket)}, {other} says {second.get(bucket)}"
		for bucket in BUCKETS
		if first.get(bucket) != second.get(bucket)
	]

	return "one agenda, two answers —\n\t" + "\n\t".join(said)
