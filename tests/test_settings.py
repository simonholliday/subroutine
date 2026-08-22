"""What an installation may configure, and the guard that keeps an entry honest — `#1025`.

**Read the last test first.** `domain/settings.py` exists because the storage it wraps was
already the declared-and-read-by-nothing defect: two JSON columns, published raw, holding one key
that nothing anywhere consumed. A registry that could hold another such key would be the same
failure with more ceremony, so the guard that proves each entry is *read* is the point of the
module and not a nicety.
"""

import pathlib
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.seed
import subroutine.domain.bootstrap
import subroutine.domain.palette
import subroutine.domain.projects
import subroutine.domain.settings
import subroutine.errors
import subroutine.views

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: How few declared settings would mean the registry has emptied out and the guard below has
#: stopped asking anything. `#405`'s floor: that test reports *offenders*, so an empty registry
#: reports none and reads exactly like a clean one.
FEWEST_SETTINGS = 1


def _declared () -> dict[str, subroutine.domain.settings.Setting]:
	"""Return every setting by the *constant* it is declared as, which is what a reader names.

	Read out of the module rather than from a list beside it, so a setting added without a
	constant — inline in ``SETTINGS``, say — is invisible to the guard and therefore fails the
	count rather than passing quietly.
	"""

	return {
		name: value
		for name, value in vars(subroutine.domain.settings).items()
		if isinstance(value, subroutine.domain.settings.Setting)
	}


def _unread (root: pathlib.Path) -> list[str]:
	"""Return every declared setting whose stated reader does not name it.

	**Takes the tree as an argument** so a synthetic case can be driven through the real
	scanner — `#405`'s rule, after two guards here were found checking a re-implementation of
	their own logic and blind to the walk reading nothing.
	"""

	found = []

	for name, setting in _declared().items():
		reader = root / setting.read_by

		if not reader.exists():
			found.append(f"{setting.key}: {setting.read_by} does not exist")

			continue

		if f"settings.{name}" not in reader.read_text(encoding="utf-8"):
			found.append(f"{setting.key}: {setting.read_by} never names settings.{name}")

	return found


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.Workspace:
	"""A workspace holding ``parent`` and ``parent/child``, so a chain has something to walk."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	parent = subroutine.domain.projects.create(
		session, workspace_id=setup.workspace.id, key="parent", title="Parent"
	)
	subroutine.domain.projects.create(
		session,
		workspace_id=setup.workspace.id,
		key="child",
		title="Child",
		parent=parent,
	)
	session.flush()

	return setup.workspace


def _project (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.project.Project:
	"""Return one of the fixture's projects by key."""

	model = subroutine.db.models.project.Project
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id, model.key == key
		)
	).one()

	return found


def test_a_setting_can_be_set_and_read_back () -> None:
	"""The ordinary case, and the one that proves a value is normalised rather than trusted."""

	stored = subroutine.domain.settings.validated(
		{"appearance.colour": "teal"}, scope=subroutine.domain.settings.PROJECT
	)

	assert stored == {"appearance.colour": "teal"}


def test_a_setting_nothing_declares_is_refused_by_name () -> None:
	"""`#898`'s rule one layer in — a quietly kept typo is a setting nobody will look at again.

	The refusal names the key *and* what the scope does accept, because a closed vocabulary the
	caller cannot see is one they cannot correct against.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.settings.validated(
			{"appearence.colour": "teal"}, scope=subroutine.domain.settings.PROJECT
		)

	reported = raised.value.errors[0]

	assert reported.code == "unknown_field"
	assert "appearence.colour" in reported.message
	assert "appearance.colour" in (reported.hint or ""), "it says what a project does accept"


def test_a_colour_the_palette_does_not_have_is_refused_with_the_whole_palette () -> None:
	"""A closed set, so a rejection that only says no leaves the caller guessing."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.settings.validated(
			{"appearance.colour": "burgundy"}, scope=subroutine.domain.settings.PROJECT
		)

	reported = raised.value.errors[0]

	assert reported.field == "appearance.colour"
	assert "amber" in (reported.hint or ""), "the alternatives are listed"


def test_a_colour_that_is_not_a_name_is_refused_before_the_palette_sees_it () -> None:
	"""A hex is the value somebody will reach for, and it must be turned down as a *kind*.

	`#1023` §4 is the reason: a stored value cannot be rendered on a surface restricted to the
	sixteen ANSI names, and contrast cannot be guaranteed for it in two themes. Refusing it by
	type rather than by lookup is what makes the message say *a colour is given by name*.
	"""

	for wrong in ("#ff0000", 16711680, ["teal"]):
		with pytest.raises(subroutine.errors.ValidationError):
			subroutine.domain.settings.validated(
				{"appearance.colour": wrong}, scope=subroutine.domain.settings.PROJECT
			)


def test_null_clears_a_setting_rather_than_storing_it () -> None:
	"""*Not stated* is what a wider scope shows through, so clearing has to remove the key.

	Storing ``None`` would make a project's own settings say *this project has no colour*, which
	reads identically and inherits nothing — the difference between the two is the whole of the
	inheritance rule.
	"""

	assert (
		subroutine.domain.settings.validated(
			{"appearance.colour": None}, scope=subroutine.domain.settings.PROJECT
		)
		== {}
	)


def test_a_value_is_taken_from_the_most_specific_scope_that_has_one () -> None:
	"""The chain, as a pure function, before any tree is involved."""

	setting = subroutine.domain.settings.COLOUR

	assert (
		subroutine.domain.settings.in_force(
			setting, stored=[{"appearance.colour": "amber"}, {"appearance.colour": "teal"}]
		)
		== "amber"
	)

	assert (
		subroutine.domain.settings.in_force(setting, stored=[{}, {"appearance.colour": "teal"}])
		== "teal"
	)

	assert subroutine.domain.settings.in_force(setting, stored=[{}, {}]) is None


def test_a_key_nothing_declares_is_ignored_on_the_way_out () -> None:
	"""The read half of the asymmetry, and it is what makes a downgrade survivable.

	An instance rolled back to a version that has never heard of a key must still serve the
	entity holding it. Writing is where a typo is caught, because that is where somebody can
	still fix it.
	"""

	assert (
		subroutine.domain.settings.in_force(
			subroutine.domain.settings.COLOUR, stored=[{"appearance.gradient": "sunset"}]
		)
		is None
	)


def test_a_project_takes_the_nearest_ancestors_colour (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""Simon's requirement of 2026-08-19, walked upwards through a real tree.

	    this project -> its parent -> ... -> the workspace -> no colour

	**Every step is driven and each one changes an answer**, which is what a precedence test has
	to do — a level whose value equals the level below it is indistinguishable from a level that
	was never consulted, and that is how a chain test passes over a chain with a step missing.
	"""

	parent = _project(session, world.id, "parent")
	child = _project(session, world.id, "child")
	wanted = [parent.id, child.id]

	def resolved () -> dict[uuid.UUID, str | None]:
		"""Ask what colour each project has in force right now."""

		return subroutine.domain.settings.for_projects(
			session, subroutine.domain.settings.COLOUR, wanted
		)

	# Nothing set anywhere: no colour, rather than a default nobody chose.
	assert resolved() == {parent.id: None, child.id: None}

	# The workspace alone, which every project in it inherits.
	world.settings = {"appearance.colour": "slate"}
	session.flush()

	assert resolved() == {parent.id: "slate", child.id: "slate"}

	# A project's own beats it, and carries down to what is under it.
	parent.settings = {"appearance.colour": "teal"}
	session.flush()

	assert resolved() == {parent.id: "teal", child.id: "teal"}

	# And the child's own beats its parent's, which is the step a two-level tree cannot show.
	child.settings = {"appearance.colour": "amber"}
	session.flush()

	assert resolved() == {parent.id: "teal", child.id: "amber"}


def test_resolving_a_page_of_projects_costs_a_fixed_number_of_queries (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""`#39`'s N+1, on the one field that is rendered on every line.

	**Counted rather than timed**, which is `#961`'s recorded rule: on a fixture holding three
	projects a per-row walk is too fast to measure, so what is asserted is that ten projects cost
	what two do. The number itself is deliberately not pinned — it is the *growth* that is the
	defect.
	"""

	wanted = []

	for index in range(10):
		made = subroutine.domain.projects.create(
			session, workspace_id=world.id, key=f"p{index}", title=f"P{index}"
		)
		wanted.append(made.id)

	session.flush()

	counted = []

	def watch (*_args: object, **_kwargs: object) -> None:
		"""Count one statement."""

		counted.append(1)

	sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", watch)

	try:
		counted.clear()
		subroutine.domain.settings.for_projects(
			session, subroutine.domain.settings.COLOUR, wanted[:2]
		)
		few = len(counted)

		counted.clear()
		subroutine.domain.settings.for_projects(
			session, subroutine.domain.settings.COLOUR, wanted
		)
		many = len(counted)

	finally:
		sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", watch)

	assert few > 0, "the fixture is not exercising the query path at all"
	assert many == few, (
		f"resolving ten projects cost {many} queries where two cost {few} — this walks per row"
	)


def test_two_settings_cost_one_walk_rather_than_two (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#1072`. The growth test above is satisfied by a constant overhead, and this is it.

	:class:`subroutine.views.Vocabulary` needs two settings on **every** task, document and
	agenda listing, and asked twice — three queries each, six per page — under a comment saying
	the second was *"resolved up the same chain and in the same two queries … so the walk is
	shared with the colour rather than repeated"*. It was not shared, and a sentence claiming a
	property the code does not have is what stops the next reader counting.

	**Counted rather than timed**, like its neighbour, and asserting *the same as one* rather
	than a number: what is wrong is paying per setting, not the size of the walk.
	"""

	wanted = []

	for index in range(4):
		made = subroutine.domain.projects.create(
			session, workspace_id=world.id, key=f"q{index}", title=f"Q{index}"
		)
		wanted.append(made.id)

	session.flush()

	counted: list[int] = []

	def watch (*_args: object, **_kwargs: object) -> None:
		"""Count one statement."""

		counted.append(1)

	sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", watch)

	try:
		counted.clear()
		subroutine.domain.settings.several_for_projects(
			session, [subroutine.domain.settings.COLOUR], wanted
		)
		one = len(counted)

		counted.clear()
		both = subroutine.domain.settings.several_for_projects(
			session,
			[subroutine.domain.settings.COLOUR, subroutine.domain.settings.HIDDEN_STATUSES],
			wanted,
		)
		two = len(counted)

	finally:
		sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", watch)

	assert one > 0, "the fixture is not exercising the query path at all"
	assert two == one, (
		f"two settings cost {two} queries where one costs {one} — the walk is being repeated "
		f"per setting rather than shared"
	)

	assert set(both) == {
		subroutine.domain.settings.COLOUR.key,
		subroutine.domain.settings.HIDDEN_STATUSES.key,
	}


def test_a_page_of_rows_resolves_both_of_its_settings_once (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""Driven through the thing that actually pays it, which is the whole reason it matters.

	The test above proves the domain can do it in one walk; this proves the caller does. A
	function that batches and a caller that calls it twice is the same cost with a better
	docstring — and that is precisely the state `SR#1072` found.
	"""

	made = [
		subroutine.domain.projects.create(
			session, workspace_id=world.id, key=f"r{index}", title=f"R{index}"
		)
		for index in range(4)
	]
	session.flush()

	counted: list[str] = []

	def watch (
		_connection: object,
		_cursor: object,
		statement: str,
		*_rest: object,
		**_kwargs: object,
	) -> None:
		"""Keep the SQL of one statement, so the assertion can say which ones it means."""

		counted.append(statement)

	sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", watch)

	try:
		subroutine.views.Vocabulary.for_projects(session, made)

	finally:
		sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", watch)

	settings_reads = [one for one in counted if "settings" in one.lower()]

	assert settings_reads, "the fixture is not reaching the settings walk at all"
	assert len(settings_reads) <= 3, (
		f"building a page's vocabulary read stored settings {len(settings_reads)} times:\n"
		+ "\n".join(settings_reads)
	)


def test_asking_about_no_projects_asks_the_database_nothing () -> None:
	"""The empty page, which every batch loader here answers without a round trip."""

	assert (
		subroutine.domain.settings.for_projects(
			None,  # type: ignore[arg-type]
			subroutine.domain.settings.COLOUR,
			[],
		)
		== {}
	)


def test_every_declared_setting_is_read_by_something () -> None:
	"""**The point of the registry, and the reason this module exists at all.**

	``workspace.settings`` and ``project.settings`` have been JSON columns since the initial
	migration. One key was ever written into either — ``visible_status_keys``, put there by a
	project template — and it was read **nowhere** in ``src/``: stored, published on two views,
	seeded by three templates, and consumed by nothing. Ninth instance of the
	declared-and-read-by-nothing family.

	**A registry that could hold another one would be that failure with more ceremony.** So an
	entry names where its value is consumed, and this fails when that file does not name it —
	`#303`'s recorded lesson, *the list was never the control, the guard is*, applied before the
	defect rather than after it.

	**It asks whether the constant is named, not whether the key string appears.** A reader
	going through ``settings.COLOUR`` is one a rename cannot silently detach; a reader matching
	``"appearance.colour"`` as a literal is a second copy of the key, which is the thing the
	registry exists to prevent.
	"""

	assert len(_declared()) >= FEWEST_SETTINGS, (
		f"only {len(_declared())} settings are declared, fewer than the {FEWEST_SETTINGS} "
		f"expected — this has stopped reading the registry, and no offenders reads exactly "
		f"like a clean one"
	)

	unread = _unread(ROOT)

	assert not unread, (
		"a setting is declared and nothing reads it, which is the defect this registry "
		"exists to end:\n  " + "\n  ".join(unread)
	)


def test_the_reader_guard_can_see_a_setting_nothing_reads (tmp_path: pathlib.Path) -> None:
	"""And the guard above is driven through its own entry point, against a tree that lacks it.

	A check written from the same assumption as the thing it checks is this repository's
	most-repeated finding. Pointing the real scanner at an empty tree makes every declared
	setting unread, which is the failure it exists to report — so a version that read nothing,
	or compared nothing, fails here.
	"""

	assert _unread(tmp_path), (
		"the scanner reported no offenders against a tree holding none of the readers, so it "
		"is not reading what it claims to read"
	)


def test_every_setting_is_offered_at_a_scope_that_exists () -> None:
	"""A scope declared before anything can hold it is the shape this module refuses.

	There is no organisation and no user scope — neither exists — so a setting naming one would
	be a chain step nothing could ever fill, resolving to the default for ever while looking
	configurable.
	"""

	wrong = {
		setting.key: sorted(set(setting.scopes) - set(subroutine.domain.settings.SCOPES))
		for setting in subroutine.domain.settings.SETTINGS.values()
		if set(setting.scopes) - set(subroutine.domain.settings.SCOPES)
	}

	assert not wrong, f"these settings name a scope that does not exist: {wrong}"


def test_every_setting_is_reachable_from_the_scope_registry () -> None:
	"""``offered`` is what a form and a refusal both read, so it must see every entry."""

	reachable = set()

	for scope in subroutine.domain.settings.SCOPES:
		reachable |= set(subroutine.domain.settings.offered(scope))

	assert reachable == set(subroutine.domain.settings.SETTINGS), (
		"a setting is declared and offered at no scope, so nothing can ever set it"
	)


def test_a_write_keeps_the_keys_it_was_not_told_about () -> None:
	"""`#1030`. The property standing between setting a colour and re-seeding every workspace.

	**`workspace.settings` holds machinery as well as preferences.** `db/seed.py` writes
	``seed_version`` there and `_applied_version` reads it to decide how far a workspace has
	been seeded — treating an absent or unreadable value as *nothing has been applied*, and
	starting over. It is in the same JSON column as the settings a person sets, and this
	registry does not describe it.

	**So the per-key merge is load-bearing for something it was not designed for.** It was
	chosen because a caller setting a colour has no business knowing what else is configured;
	it also happens to be the only thing stopping that caller wiping the seeder's own record.

	**Measured on the live instance the day `#1025` shipped**, which is why this exists: five
	projects were written to, and every one of them still carried ``visible_status_keys`` and
	four carried ``require_verification_to_complete`` — a key removed from the templates by
	`#133` and still in the data. Replace semantics would have deleted three keys from each,
	silently, in a write about a colour.

	**Driven with the real key rather than an invented one**, so the case names the thing that
	would actually break. The obvious tidy-up — making this field replace like every other field
	on these entities does — passes the whole suite without this.
	"""

	held = {
		subroutine.db.seed.SEED_VERSION_KEY: 4,
		"visible_status_keys": ["open", "done"],
	}

	after = subroutine.domain.settings.applied(
		held, {"appearance.colour": "teal"}, scope=subroutine.domain.settings.WORKSPACE
	)

	assert after[subroutine.db.seed.SEED_VERSION_KEY] == 4, (
		"a write about a colour dropped the seeder's own record, so the next upgrade re-seeds "
		"this workspace from zero"
	)
	assert after["visible_status_keys"] == ["open", "done"], (
		"a write dropped a key this build no longer declares — which is every key on every "
		"project row created before it"
	)
	assert after["appearance.colour"] == "teal", "and the thing actually being set is set"

	# **Clearing one key leaves the others**, which is the same property from the other side and
	# is the case a replace would also get wrong — by clearing everything rather than one thing.
	cleared = subroutine.domain.settings.applied(
		after, {"appearance.colour": None}, scope=subroutine.domain.settings.WORKSPACE
	)

	assert "appearance.colour" not in cleared, "clearing has to remove the key, not null it"
	assert cleared[subroutine.db.seed.SEED_VERSION_KEY] == 4, "and take nothing else with it"


def test_a_list_of_statuses_is_stored_sorted_and_without_repeats () -> None:
	"""A deny-list is a set wearing a list's clothes, so the stored form is canonical.

	It matters more here than it usually would: replacing a JSON column with an *equal* dict
	still marks the row dirty and moves ``updated_at`` (`#42`), so two spellings of one set
	would make writing the same thing twice look like a change.
	"""

	stored = subroutine.domain.settings.validated(
		{"statuses.hidden": ["done", "blocked", "done", " blocked "]},
		scope=subroutine.domain.settings.PROJECT,
	)

	assert stored == {"statuses.hidden": ["blocked", "done"]}


def test_a_status_list_that_is_not_a_list_is_refused_by_name () -> None:
	"""One key sent as a bare word, which is the likeliest thing a hand-written call does."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.settings.validated(
			{"statuses.hidden": "blocked"}, scope=subroutine.domain.settings.PROJECT
		)

	reported = raised.value.errors[0]

	assert reported.field == "statuses.hidden"
	assert "blocked" in (reported.hint or ""), "the shape is shown rather than described"


def test_a_status_list_holding_something_that_is_not_a_word_is_refused () -> None:
	"""The entry is named, because a list of five with one wrong is a needle in a haystack."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.settings.validated(
			{"statuses.hidden": ["blocked", 7]}, scope=subroutine.domain.settings.PROJECT
		)

	assert "7" in raised.value.errors[0].message


def test_a_status_this_workspace_does_not_have_is_refused_with_the_ones_it_does (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""The half a :class:`Kind` structurally cannot check, and the reason it exists.

	A ``Kind`` is a pure declaration with no session, so it can say *this is a list of words* and
	never *these words are statuses here*. Without the second, ``statuses.hidden = ["blockd"]``
	hides nothing, silently, and looks exactly like a setting that did not take — the
	declared-and-read-by-nothing family (`#303`) one level in, at the value rather than the key.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.settings.verified(
			session, world.id, {"statuses.hidden": ["blockd"]}
		)

	reported = raised.value.errors[0]

	assert reported.field == "statuses.hidden"
	assert "blockd" in reported.message
	assert "blocked" in (reported.hint or ""), "it says which statuses this workspace has"


def test_a_setting_with_no_workspace_check_of_its_own_is_left_alone (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""Most settings are their own whole rule, and :func:`verified` must be silent for those.

	Falsified the other way round on purpose: a colour that the palette *would* refuse is passed
	here and accepted, which proves this step is asking each setting rather than re-running
	validation over everything.
	"""

	subroutine.domain.settings.verified(session, world.id, {"appearance.colour": "burgundy"})


def test_a_project_takes_the_nearest_ancestors_hidden_statuses (
	world: subroutine.db.models.identity.Workspace, session: sqlalchemy.orm.Session
) -> None:
	"""The same chain the colour walks, on the registry's second entry — `#1029`.

	Driven rather than assumed to follow, because :func:`for_projects` takes the setting as an
	argument and a chain that worked for one value and not another would be invisible from the
	colour's own test.

	**Every step changes an answer.** A level whose value equals the level below it cannot be
	told from a level nothing consulted, which is how a precedence test passes over a missing
	step — `#815`'s recorded trap, met on §6.5's timezone chain.
	"""

	parent = _project(session, world.id, "parent")
	child = _project(session, world.id, "child")
	wanted = [parent.id, child.id]

	def resolved () -> dict[uuid.UUID, object]:
		"""Ask what each project hides right now."""

		return subroutine.domain.settings.for_projects(
			session, subroutine.domain.settings.HIDDEN_STATUSES, wanted
		)

	# Nothing set: everything is offered, which is what an unconfigured instance does today.
	assert resolved() == {parent.id: (), child.id: ()}

	world.settings = {"statuses.hidden": ["needs_input"]}
	session.flush()

	assert resolved() == {parent.id: ["needs_input"], child.id: ["needs_input"]}

	parent.settings = {"statuses.hidden": ["blocked"]}
	session.flush()

	assert resolved() == {parent.id: ["blocked"], child.id: ["blocked"]}

	# **Replaced, not merged, between scopes.** The nearest ancestor that says anything says all
	# of it — which is `in_force`'s rule for every setting, and is why a project wanting its
	# parent's list plus one more writes both. Merging up a tree would make *offer this after
	# all* unsayable from anywhere below wherever it was hidden.
	child.settings = {"statuses.hidden": []}
	session.flush()

	assert resolved() == {parent.id: ["blocked"], child.id: []}, (
		"an empty list is a project saying it offers everything, not a project saying nothing"
	)
