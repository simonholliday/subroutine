"""One project in a workspace can be prioritised, and its work rises — `#986`, decision `#982`.

**What is being guarded is a shape rather than a number.** The design turns on three properties
and each is easy to lose while every test stays green:

* **one project per workspace**, held by a single nullable pointer, so choosing B unsets A in the
  same write — that is the anti-spiral mechanism, and a boolean per project would end it;
* **additive inside the band**, so a genuinely urgent item elsewhere keeps its place — the
  requirement is *raise a bar for everyone else*, never *put this project first*;
* **the bonus reaches ranked work only**, because :data:`subroutine.domain.ordering.
  PRIORITISED_BONUS` was measured against the 1-25 product scale.

**Two of these are only visible with the right rows.** A fixture where the prioritised project
already holds the highest-scoring work cannot tell the feature from its absence — which is the
`#982` measurement said out loud: of the top 25 items by rank on the instance this project runs
on, 20 were already in the project most likely to be prioritised. So every ordering test here is
built so that **the bonus is what decides**, and each says which pair of rows it turns on.

**The domain half is here and the surfaces are not.** What each transport reports, and that the
two agree, is in ``tests/test_transport_equivalence.py`` — which is hand-listed, and `#44`'s
``move`` is what going uncovered there looks like. The cost half is in
``tests/test_query_cost.py``, which also carries the correction measuring made to this item: the
correlated spelling is **not** `#856`'s catastrophe.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.ordering
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.domain.workspaces
import subroutine.errors


class World(typing.NamedTuple):
	"""One workspace, three projects and a principal to read them with."""

	session: sqlalchemy.orm.Session
	principal: subroutine.domain.authentication.Principal
	workspace: subroutine.db.models.identity.Workspace
	web: subroutine.db.models.project.Project
	dist: subroutine.db.models.project.Project
	ops: subroutine.db.models.project.Project

	def ranked (self) -> list[str]:
		"""Return every task's title in the order ``-priority_score`` puts them, best first."""

		prefixes = subroutine.domain.scoping.prioritised_paths(
			self.session, self.principal, workspace_ids=[self.workspace.id]
		)
		sortable = subroutine.domain.ordering.prioritising(
			subroutine.domain.ordering.TASK_FIELDS, prefixes=prefixes
		)
		statement = subroutine.domain.scoping.readable_tasks(
			self.principal, workspace_ids=[self.workspace.id]
		).order_by(
			*subroutine.domain.ordering.clauses(
				"-priority_score",
				allowed=sortable,
				default=("-priority_score",),
				tiebreak=subroutine.db.models.work.Task.created_at,
			)
		)

		return [task.title for task in self.session.scalars(statement)]

	def prioritise (self, project: subroutine.db.models.project.Project | None) -> None:
		"""Point the workspace at one project, or at none."""

		subroutine.domain.workspaces.update(
			self.session,
			self.workspace,
			prioritised_project=project,
			actor=self.principal,
		)

	def add (
		self,
		title: str,
		*,
		project: subroutine.db.models.project.Project,
		importance: int | None = None,
		urgency: int | None = None,
	) -> None:
		"""File one task, ranked as the caller says.

		The two axes are named rather than taken as keywords, because §6.3's three states —
		ranked, part-ranked and unranked — are what every ordering test here turns on, and a
		mapping would let a test ask for a state the type checker could not describe.
		"""

		subroutine.domain.tasks.create(
			self.session,
			project=project,
			title=title,
			importance=importance,
			urgency=urgency,
			actor=self.principal,
		)


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""Return a workspace holding ``web``, ``web/dist`` and ``ops``.

	``dist`` is nested because the subtree question is the one a single pointer answers outright,
	and a flat fixture could not tell inheritance from its absence.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Priority"
	)
	principal = subroutine.domain.authentication.Principal(user=setup.user, token=None)

	web = subroutine.domain.projects.create(
		session,
		workspace_id=setup.workspace.id,
		key="web",
		title="Website",
		actor=principal,
	)
	dist = subroutine.domain.projects.create(
		session,
		workspace_id=setup.workspace.id,
		key="dist",
		title="Distribution",
		parent=web,
		actor=principal,
	)
	ops = subroutine.domain.projects.create(
		session,
		workspace_id=setup.workspace.id,
		key="ops",
		title="Operations",
		actor=principal,
	)
	session.flush()

	return World(
		session=session,
		principal=principal,
		workspace=setup.workspace,
		web=web,
		dist=dist,
		ops=ops,
	)


def test_prioritising_one_project_unsets_whatever_was_prioritised_before (
	world: World,
) -> None:
	"""**The radio-button semantic, and the whole design rests on it** (decision ``#982`` §6).

	Simon's question was *"how would we stop this spiralling?"*: a per-project dial has an
	equilibrium indistinguishable from having no feature at all, reached by locally rational
	moves, because every boost is a silent demotion of everything untouched. One nullable pointer
	makes the accumulation structurally impossible — there is no N to accumulate — and this is
	the assertion that says so.

	It would pass equally against a boolean on ``project`` *while nothing set two of them*, which
	is exactly how ``is_inbox`` is safe today and why decision ``#982`` says not to copy it. So
	the second half matters: B is prioritised **and** A is not, in one write nobody had to
	remember to make.
	"""

	world.prioritise(world.web)

	assert world.workspace.prioritised_project_id == world.web.id

	world.prioritise(world.ops)

	assert world.workspace.prioritised_project_id == world.ops.id, "choosing B must take effect"
	assert world.workspace.prioritised_project_id != world.web.id, (
		"and A must stop being prioritised in the same write — a second prioritised project is "
		"the state this design exists to make unreachable"
	)


def test_clearing_it_leaves_nothing_prioritised (world: World) -> None:
	"""``None`` is a value here rather than an absence (§8.3)."""

	world.prioritise(world.web)
	world.prioritise(None)

	assert world.workspace.prioritised_project_id is None


def test_the_prioritised_project_outranks_a_higher_score_elsewhere (world: World) -> None:
	"""The feature, on the one pair of rows that can tell it from nothing.

	``ops`` scores 16 and ``web`` scores 15, so without the bonus ``ops`` comes first and with it
	``web`` does — 15 + 3 = 18. **Chosen so that neither the tiebreak nor the existing order can
	produce the answer**: `#982` measured 20 of the top 25 items already sitting in the project
	most likely to be prioritised, which is a fixture in which this feature is invisible.
	"""

	world.add("Rotate the certificates", project=world.ops, importance=4, urgency=4)
	world.add("Rewrite the home page", project=world.web, importance=5, urgency=3)

	assert world.ranked() == ["Rotate the certificates", "Rewrite the home page"], (
		"the fixture must start with the *other* project's work on top, or this test cannot "
		"tell the bonus from the arrangement that was already there"
	)

	world.prioritise(world.web)

	assert world.ranked() == ["Rewrite the home page", "Rotate the certificates"]


def test_an_emergency_in_another_project_still_comes_first (world: World) -> None:
	"""**The requirement, quoted**: *"only an urgent or important item from any other project
	would appear in my agenda"* — which is *raise a bar*, never *put this project first*.

	This is what refuted decision ``#982``'s option (a), project priority as the primary sort
	key: under it the favoured project's *worst* item outranked a five-alarm emergency for ever,
	and the best non-Subroutine item fell to position 142 of 167. Measured at ``+3`` the
	emergency stays at position 1, which is the requirement and therefore the test.

	It is also what a multiplier fails at. a threefold multiplier put the same ``!5/5`` at position 43.
	"""

	world.add("Everything is on fire", project=world.ops, importance=5, urgency=5)
	world.add("Rewrite the home page", project=world.web, importance=5, urgency=4)

	world.prioritise(world.web)

	assert world.ranked()[0] == "Everything is on fire", (
		"a prioritised project must not bury an emergency elsewhere — that is the option the "
		"requirement itself rules out"
	)


def test_the_whole_subtree_inherits (world: World) -> None:
	"""Prioritising a parent raises what is filed underneath it (decision ``#982`` §7).

	**A single pointer is what makes the hierarchy question disappear**: there is only ever one
	prioritised project, so nothing compounds down a chain and no depth rule is needed. Simon's
	worry was compounding, and the answer is that there is nothing to compound with.
	"""

	world.add("Rotate the certificates", project=world.ops, importance=4, urgency=4)
	world.add("Ship the release", project=world.dist, importance=5, urgency=3)

	world.prioritise(world.web)

	assert world.ranked() == ["Ship the release", "Rotate the certificates"], (
		"work in web/dist must rise when web is prioritised"
	)


def test_only_the_project_itself_is_named_as_prioritised (world: World) -> None:
	"""Its subtree inherits the *bonus* and does not inherit the *label*.

	Marking children as prioritised would read as four prioritised projects, which is the state
	this design makes impossible — so the two halves are deliberately different, and that
	difference is the thing a later "tidy-up" would collapse.
	"""

	world.prioritise(world.web)

	named = subroutine.domain.projects.prioritised_addresses(
		world.session, world.principal, workspace_ids=[world.workspace.id]
	)

	assert named == {world.workspace.id: "web"}


def test_unranked_work_is_unaffected (world: World) -> None:
	"""There is nothing to add to a null, and that is a limit rather than a defect.

	`#857`'s trap recurring, recorded on decision ``#982`` before it was built: *nobody writes
	`!4/3` on buy milk*, so this feature does nothing at all for a list that is mostly
	unassessed. Asserted rather than assumed, because a later change that "helpfully" gave
	unranked work a band would put a shopping list above assessed work.
	"""

	world.add("Buy milk", project=world.web)
	world.add("Rotate the certificates", project=world.ops, importance=1, urgency=1)

	world.prioritise(world.web)

	assert world.ranked() == ["Rotate the certificates", "Buy milk"], (
		"unranked work sorts last however its project is ranked"
	)


def test_part_ranked_work_is_unaffected (world: World) -> None:
	"""**The bonus reaches the ranked band only, and this is the decision** — see
	:func:`subroutine.domain.ordering.ranking`.

	:data:`subroutine.domain.ordering.PRIORITISED_BONUS` was measured against the product scale,
	1 to 25, where 3 is about half a step of one axis. The part-ranked band runs 1 to 5, where
	the same 3 is 60% of full scale — enough that a ``!1`` in the prioritised project would
	outrank a ``!4`` everywhere else, which is in miniature the *favoured project's worst beats
	everybody's best* failure decision ``#982`` turned down. Two constants would fix it and
	would be the beginning of the dial this design declines.

	``!1`` here and ``!4`` there is exactly that pair, so this test **fails against the other
	choice** — which is what makes the decision one clause and one red test to reverse.
	"""

	world.add("Tidy the wiki", project=world.web, importance=1)
	world.add("Rotate the certificates", project=world.ops, importance=4)

	world.prioritise(world.web)

	assert world.ranked() == ["Rotate the certificates", "Tidy the wiki"], (
		"a part-ranked item in the prioritised project must not overtake a better-assessed one "
		"elsewhere; the bonus is sized for the 1-25 scale and this band runs 1-5"
	)


def test_a_project_in_another_workspace_cannot_be_prioritised (
	session: sqlalchemy.orm.Session, world: World
) -> None:
	"""A pointer across a tenancy boundary is a scoping hole, not a curiosity.

	Every caller resolves the project through :mod:`subroutine.domain.selection`, which is
	workspace-scoped, so this refusal is defence behind that rather than the only guard. It stays
	for `#916`'s reason: a check that cannot be reached is cheaper than one nobody wrote, and
	what it would let through is one workspace's focus deciding another's ordering.
	"""

	elsewhere = subroutine.domain.workspaces.create(
		session,
		slug=f"other-{uuid.uuid4().hex[:6]}",
		title="Somewhere else",
		owner=world.principal.user,
		actor=world.principal,
	)
	theirs = subroutine.domain.projects.create(
		session,
		workspace_id=elsewhere.id,
		key="theirs",
		title="Theirs",
		actor=world.principal,
	)
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		world.prioritise(theirs)

	assert "another workspace" in str(refused.value)
	assert world.workspace.prioritised_project_id is None, "and nothing is written"


def test_deleting_the_project_stops_it_being_prioritised (world: World) -> None:
	"""``ON DELETE SET NULL``, so the state cannot outlive its subject.

	A pointer at a row that has gone is a workspace claiming a focus nobody can name, and the
	constraint is what makes that unreachable rather than a thing to remember to tidy up.
	"""

	world.prioritise(world.ops)
	world.session.flush()

	world.session.delete(world.ops)
	world.session.flush()
	world.session.refresh(world.workspace)

	assert world.workspace.prioritised_project_id is None
