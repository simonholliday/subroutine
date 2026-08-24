"""A field added to a response model since the last release carries a default — `#482`.

``tests/test_compatibility.py`` states this rule and holds captured bodies for **one** endpoint,
``/v1/me``. That is a guard checking the shape it was written from, and it has now let the same
defect through three times: `#345` twice on 2026-08-03, and again on 2026-08-04 when
``assigned_by_id`` and ``responsible_user_id`` were added to ``views.Task`` and ``views.User``
without defaults. The suite was green — 2,677 passing on both backends — and the next command
against the served instance said:

    hpz2g4 answered, but not as a Subroutine instance:
    Task could not be read from its response (assigned_by_id: Field required).

**Captured bodies do not generalise.** One per view means capturing from a running older
instance every time a model is added, which nobody will do, and they rot the moment a field is
legitimately added. So this is structural instead: read the models as they were at the last
release and compare.

**Why the models rather than a list of names.** ``test_compatibility`` already asserts that a
field required *from the start* is still refused, and that is right — a blanket "everything
optional" would be wrong, and would let a genuinely broken body through. Only a **new** field
has to be defaulted, and only a diff against the last release can tell the two apart.

Scope is ``subroutine.views``, measured rather than assumed: every ``model_validate`` on
``clients/http.py`` goes through ``_parsed``, and every model handed to it comes from that
module.
"""

import ast
import pathlib
import subprocess
import typing

import pydantic
import pytest

import subroutine.db.seed
import subroutine.domain.links
import subroutine.domain.readiness
import subroutine.views

ROOT = subprocess.run(
	["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
).stdout.strip()

#: Where the shared response models live. One path, because that is what the client parses.
VIEWS = "src/subroutine/views.py"

#: The fewest classes and fields a healthy read of the old file finds. A structural diff is
#: satisfied most comfortably by reading nothing at all — every field is then "not new" — so the
#: floor is what stops a broken parse reading as a clean bill of health.
LEAST_CLASSES = 10
LEAST_FIELDS = 60


def last_release () -> str | None:
	"""Return the most recent tag that is not this commit, or ``None`` if there is none.

	The same question ``scripts/check_release_notes.py`` asks to compare migration heads, so the
	mechanism is one this repository already relies on rather than a new dependency on git.

	**"That is not this commit" is `#895`, and without it this guard is inert on the one commit
	where it matters.** ``scripts/release.py`` commits and then tags, so on a release commit
	``git describe`` resolves to a tag pointing at ``HEAD`` — and the comparison below reads the
	current file, diffs it against itself and finds nothing new. Measured on ``8138be5``, the
	``v0.7.1`` release commit: both sides hashed to ``20a4b91e``.

	**The release is the only moment a client can be a whole version behind the instance**, which
	is what `#345` and `#482` are about — two fields added to ``/v1/me`` as *required*, and a
	client one commit ahead refusing the instance outright. So the guard was live on every
	ordinary commit and asleep on the one that ships, and nobody would ever have seen it fail to
	fail: the next ordinary commit resolves the tag properly and it works again.

	``HEAD^`` rather than filtering the tag list, because it asks the question directly — *what
	was released before whatever this is* — and it is the same answer on an ordinary commit,
	where nothing points at ``HEAD`` anyway.
	"""

	pointing = subprocess.run(
		["git", "tag", "--points-at", "HEAD"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)
	start = "HEAD^" if pointing.stdout.strip() else "HEAD"

	found = subprocess.run(
		["git", "describe", "--tags", "--abbrev=0", start],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	return found.stdout.strip() or None


def _commit (reference: str) -> str:
	"""Return the commit a reference names, for comparing two of them."""

	return subprocess.run(
		["git", "rev-parse", f"{reference}^{{commit}}"],
		cwd=ROOT, capture_output=True, text=True, check=True,
	).stdout.strip()


def _source_at (tag: str, path: str) -> str | None:
	"""Return a file as it was at ``tag``, or ``None`` if it was not there."""

	found = subprocess.run(
		["git", "show", f"{tag}:{path}"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	return found.stdout if found.returncode == 0 else None


def _base_name (node: ast.expr) -> str | None:
	"""Return the class a base expression names, through a subscript if there is one.

	``class Changes(Collection[Event])`` is an ``ast.Subscript``, which has no ``.id`` — so a
	comprehension keeping only ``ast.Name`` drops it, and the class resolves to its own declared
	fields with everything it inherits missing (`#1125`). That reported ``Changes.items`` and
	``Changes.page`` as added since ``v0.7.6`` when both had been there all along.

	**It went unseen for two releases and neither gap was an accident of writing it.** `Changes`
	is the only subscripted base in the file and it was written *inside* the `v0.7.6` cycle, so
	before that the comparison took the "the whole model is new" branch and never looked; and the
	comparison itself steps back on a release commit (`#895`), so the first ordinary commit after
	the tag is the first moment both halves are awake.
	"""

	if isinstance(node, ast.Name):
		return node.id

	# `Collection[Event]` — the subscript is the parameter, and the base is what it is on.
	if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
		return node.value.id

	return None


def fields_at (source: str) -> dict[str, set[str]]:
	"""Return every class in ``source`` and the field names it carries, inheritance included.

	**Parsed rather than imported.** Importing a module from an older release means resolving
	that release's imports inside this one's interpreter, which is a much larger promise than
	this check needs — and a check that can break on an unrelated refactor is one that gets
	switched off.

	Inheritance is resolved because ``model_fields`` on the live side includes it: ``Me``
	extends ``User``, so comparing declared-only against inherited-too would report every
	inherited field as new and fail on the first run.
	"""

	tree = ast.parse(source)
	declared: dict[str, set[str]] = {}
	bases: dict[str, list[str]] = {}

	for node in tree.body:
		if not isinstance(node, ast.ClassDef):
			continue

		declared[node.name] = {
			entry.target.id
			for entry in node.body
			if isinstance(entry, ast.AnnAssign) and isinstance(entry.target, ast.Name)
		}
		bases[node.name] = [named for named in map(_base_name, node.bases) if named]

	def resolved (name: str, seen: frozenset[str] = frozenset()) -> set[str]:
		"""Return a class's fields plus everything it inherits from classes in this file."""

		if name in seen:
			return set()

		found = set(declared.get(name, set()))

		for base in bases.get(name, []):
			found |= resolved(base, seen | {name})

		return found

	return {name: resolved(name) for name in declared}


def annotations_at (source: str) -> dict[str, dict[str, str]]:
	"""Return every class in ``source`` and the annotation each of its fields carries.

	:func:`fields_at`'s sibling, and the two are separate because they answer different
	questions: that one asks *which keys does this shape have*, and this asks *what is at each
	key*. A shape can gain a required key without gaining a key name — by the model at an
	existing field changing to a different model — which is `#1155`, and is invisible to a
	comparison that only knows names.

	Inheritance is resolved the same way and for the same reason, with the subclass winning:
	a field redeclared on a subclass is what that subclass sends.
	"""

	tree = ast.parse(source)
	declared: dict[str, dict[str, str]] = {}
	bases: dict[str, list[str]] = {}

	for node in tree.body:
		if not isinstance(node, ast.ClassDef):
			continue

		declared[node.name] = {
			entry.target.id: ast.unparse(entry.annotation)
			for entry in node.body
			if isinstance(entry, ast.AnnAssign) and isinstance(entry.target, ast.Name)
		}
		bases[node.name] = [named for named in map(_base_name, node.bases) if named]

	def resolved (name: str, seen: frozenset[str] = frozenset()) -> dict[str, str]:
		"""Return a class's annotations plus everything it inherits, the subclass winning."""

		if name in seen:
			return {}

		found: dict[str, str] = {}

		for base in bases.get(name, []):
			found |= resolved(base, seen | {name})

		return found | declared.get(name, {})

	return {name: resolved(name) for name in declared}


def _classes_named (annotation: str, known: typing.Container[str]) -> set[str]:
	"""Return the model names an annotation mentions.

	``dict[str, list[ItemType]]`` names ``ItemType``; ``str`` names nothing. Parsed rather than
	pattern-matched, because an annotation is an expression and the interesting ones here are
	all nested inside subscripts.
	"""

	try:
		tree = ast.parse(annotation, mode="eval")

	except SyntaxError:
		return set()

	return {
		node.id
		for node in ast.walk(tree)
		if isinstance(node, ast.Name) and node.id in known
	}


def response_models () -> dict[str, type[pydantic.BaseModel]]:
	"""Return the response models this client parses, as they are now."""

	return {
		name: value
		for name, value in vars(subroutine.views).items()
		if isinstance(value, type)
		and issubclass(value, pydantic.BaseModel)
		and value.__module__ == subroutine.views.__name__
	}


@pytest.fixture(scope="module")
def before () -> dict[str, set[str]]:
	"""Return the view models as they were at the most recent release."""

	tag = last_release()

	if tag is None:
		pytest.skip("no tag in this checkout, so there is no released shape to compare against")

	# **The tag must be a different commit, or this compares a file with itself** (`#895`).
	# `last_release` takes care of that and this is what stops it going back: a skip would be
	# the wrong answer, because "nothing is new" and "nothing was compared" are the two readings
	# this whole file exists to keep apart.
	assert _commit(tag) != _commit("HEAD"), (
		f"{tag} is this commit, so the comparison would diff {VIEWS} against itself and find "
		f"nothing new — which is what a release commit looked like before `#895`"
	)

	source = _source_at(tag, VIEWS)

	if source is None:
		pytest.skip(f"{VIEWS} did not exist at {tag}")

	return fields_at(source)


@pytest.fixture(scope="module")
def annotated_before () -> dict[str, dict[str, str]]:
	"""Return what each view field was annotated as at the most recent release.

	The same git read as :func:`before`, parsed the other way — see :func:`annotations_at` for
	why the two questions are not one.
	"""

	tag = last_release()

	if tag is None:
		pytest.skip("no tag in this checkout, so there is no released shape to compare against")

	assert _commit(tag) != _commit("HEAD"), (
		f"{tag} is this commit, so the comparison would diff {VIEWS} against itself"
	)

	source = _source_at(tag, VIEWS)

	if source is None:
		pytest.skip(f"{VIEWS} did not exist at {tag}")

	return annotations_at(source)


def test_a_generic_base_is_still_a_base () -> None:
	"""`#1125` — a subscripted base was dropped, so its fields read as newly added.

	Driven with a source string rather than through the release comparison, because that
	comparison only runs when the checkout has a tag *behind* `HEAD` (`#895`) — so a test
	depending on it would pass vacuously in exactly the state this defect hid in.

	The real case is `class Changes(Collection[Event])`, the only subscripted base in
	``views.py``, written inside the `v0.7.6` cycle. Before that release the comparison took
	the "the whole model is new" branch; on the release commit it stepped back; and the first
	ordinary commit after the tag is where it finally fired, two releases after the shape
	arrived.
	"""

	source = (
		"class Page:\n"
		"\tlimit: int\n"
		"class Collection:\n"
		"\titems: list[str]\n"
		"\tpage: Page\n"
		"class Changes(Collection[Event]):\n"
		"\tcovers: list[str]\n"
	)

	found = fields_at(source)

	assert found["Changes"] == {"covers", "items", "page"}, (
		"a base reached through a subscript is still a base, and its fields are inherited"
	)

	# The plain case must keep working, because the fix widens what counts as a base.
	assert found["Collection"] == {"items", "page"}


def test_a_field_added_since_the_last_release_carries_a_default (
	before: dict[str, set[str]],
) -> None:
	"""The rule `#345` states and nothing enforced beyond one endpoint.

	An instance one release behind sends a body without the field. Required, and this client
	refuses that instance outright rather than reading the rest of what it said — which is not a
	degraded mode, it is the CLI reporting that the server is not a Subroutine instance.
	"""

	offenders: list[str] = []

	for name, model in response_models().items():
		if name not in before:
			# The whole model is new, so no older instance sends this shape at all and there is
			# nothing for a missing field to break.
			continue

		for field, info in model.model_fields.items():
			if field not in before[name] and info.is_required():
				offenders.append(f"{name}.{field}")

	assert not offenders, (
		f"added since {last_release()} and required: {', '.join(sorted(offenders))}. An "
		f"instance one release behind sends a body without these, and this client would refuse "
		f"it outright. Give each a default — `= None` — so an older body still parses (`#345`)."
	)


def test_a_field_that_changed_model_did_not_gain_a_required_key (
	before: dict[str, set[str]],
	annotated_before: dict[str, dict[str, str]],
) -> None:
	"""`#1155`. The hole the class-by-class diff above leaves, and the fourth time this bit.

	That check exempts a model that did not exist at the last release, on the ground that *no
	older instance sends this shape at all*. **True of a class name and false of a response
	position.** ``Vocabulary.item_types`` was a list of ``Named`` and is now a list of
	``ItemType``: the class is new, the place it sits in the body is not, and an instance one
	release behind sends exactly that shape. `#1134` made its ``category`` required, the suite
	stayed green, and the next command against the served instance said

	    hpz2g4 answered, but not as a Subroutine instance:
	    Meta could not be read from its response (item_types.document.0.category: Field required).

	**The three cases, worked through, and only one is uncovered.** A *new* field that is
	required is caught by the check above, by the field. A *new* field that is optional is never
	parsed at all, because an older body omits it. A **pre-existing** field whose annotation
	changed is this one.

	So the rule: what the new model requires must be something the model it replaced already
	sent. Anything else is a key an older instance has no way to know about.
	"""

	now = annotations_at(pathlib.Path(ROOT, VIEWS).read_text(encoding="utf-8"))
	models = response_models()
	offenders: list[str] = []
	compared = 0

	for name, fields in now.items():
		if name not in annotated_before:
			continue

		for field, annotation in fields.items():
			was = annotated_before[name].get(field)

			if was is None or was == annotation:
				continue

			compared += 1
			# What the field used to be able to send, which is the whole of what an older
			# instance can be relied on to put there.
			sent = set().union(*(before.get(one, set()) for one in _classes_named(was, before)))

			for arrived in _classes_named(annotation, models) - set(annotated_before):
				offenders.extend(
					f"{name}.{field} -> {arrived}.{key}"
					for key, info in models[arrived].model_fields.items()
					if info.is_required() and key not in sent
				)

	assert not offenders, (
		f"a field that already existed now names a model that did not, and that model requires "
		f"keys the old one never sent: {', '.join(sorted(offenders))}. An instance one release "
		f"behind sends the old shape, and this client would refuse it outright. Give each a "
		f"default (`#345`)."
	)

	# **Not an assertion about the count**, because zero is the ordinary and correct answer: most
	# releases change no annotation at all. It is printed so a run that compared nothing is
	# visible to somebody reading the output rather than being indistinguishable from a pass.
	print(f"annotations compared: {compared}")


def test_the_comparison_actually_read_the_old_models (
	before: dict[str, set[str]],
) -> None:
	"""A diff against nothing finds no new fields and passes, which is the failure to prevent.

	The floor is not a substitute for falsifying the check — a walk that reads *most* things
	satisfies it happily — but it is what catches the walk that reads none, which is the one
	an unrelated refactor produces.
	"""

	assert len(before) >= LEAST_CLASSES, (
		f"read {len(before)} classes from {VIEWS} at {last_release()}, expected at least "
		f"{LEAST_CLASSES} — has the parse stopped reaching them?"
	)

	assert sum(len(names) for names in before.values()) >= LEAST_FIELDS


def test_the_models_it_compares_are_the_ones_the_client_parses () -> None:
	"""Scope, pinned. Every model `clients/http.py` validates comes from ``views``.

	Worth asserting rather than trusting the docstring: the day somebody parses a response into
	a model defined in ``api/`` is the day this guard stops covering the thing it is named for,
	and nothing else would say so.
	"""

	source = pathlib.Path(ROOT, "src/subroutine/clients/http.py")
	parsed = ast.parse(source.read_text(encoding="utf-8"))
	outside: list[str] = []

	for node in ast.walk(parsed):
		if not isinstance(node, ast.Attribute) or node.attr != "model_validate":
			continue

		named = ast.unparse(node.value)

		if named != "model" and not named.startswith("subroutine.views."):
			outside.append(named)

	assert not outside, (
		f"{', '.join(sorted(set(outside)))} is parsed from a response and is not a view, so "
		f"this guard does not cover it. Either move it into `views` or widen the scope here."
	)


def test_every_model_it_checks_is_reachable_from_the_live_module () -> None:
	"""The live half needs a floor of its own, for the same reason the historic half does."""

	found = response_models()

	assert len(found) >= LEAST_CLASSES
	assert "Task" in found and "User" in found, (
		"the two models that carried this defect on 2026-08-04 must be in scope"
	)


def test_a_new_required_field_would_be_caught (before: dict[str, set[str]]) -> None:
	"""Feed the real comparison a synthetic offender, rather than trusting it in the abstract.

	`#405`: a guard is tested by putting a defect through its own entry point. Here that means
	a model that *was* in the last release gaining a required field — which is exactly what
	`assigned_by_id` did, and what nothing noticed.
	"""

	planted = dict(before)
	subject = "Task"

	assert subject in planted, "the fixture must know this model, or the case proves nothing"

	planted[subject] = planted[subject] - {"title"}
	offenders = [
		f"{subject}.{field}"
		for field, info in response_models()[subject].model_fields.items()
		if field not in planted[subject] and info.is_required()
	]

	assert offenders == [f"{subject}.title"], (
		f"the comparison did not notice a required field missing from the old shape: {offenders}"
	)


def test_a_new_optional_field_is_accepted (before: dict[str, set[str]]) -> None:
	"""The other side of the same case, so the rule is not simply "refuse everything new"."""

	planted = dict(before)
	planted["Task"] = planted["Task"] - {"assigned_by_id"}

	offenders = [
		field
		for field, info in response_models()["Task"].model_fields.items()
		if field not in planted["Task"] and info.is_required()
	]

	assert offenders == [], (
		"a field added *with* a default was reported, so this refuses ordinary additions"
	)


#: A body as an instance one release behind sends it: the seeded ``blocks`` relation, and no
#: ``link_category`` key at all. Written out rather than captured, because capturing needs a
#: running 0.7.6 and `#482`'s whole argument is that nobody will do that every time.
A_LINK_FROM_BEFORE_CATEGORIES = {
	"id": "01a03300-0000-7000-8000-000000000001",
	"link_type": "blocks",
	"label": "Blocks",
	"direction": "outgoing",
	"other": {
		"id": "01a03300-0000-7000-8000-000000000002",
		"ref": 7,
		"title": "The thing in the way",
		"entity_type": "task",
		"status": "open",
		"status_category": "open",
		"status_is_default": True,
	},
}


def test_an_absent_category_is_read_as_the_key_meant_before_there_were_any () -> None:
	"""`#1168`. **Defaulted is not enough on a field somebody branches on.**

	`#482`'s rule — every new field carries a default — answers *will a newer client refuse an
	older instance*. It has nothing to say about *will a newer client misread one*, and
	``link_category`` is the first field where that matters: three surfaces compare it to
	``gating``, so an absent value read as the empty string read as *nothing is holding this
	up*. The *N of M blockers done* rollup vanished from the terminal, from an agent's ``show``
	and from the browser, on exactly the item a milestone is read off.

	**The rule this adds, for the next field:** a default that drives a branch has to be
	checked for *which way it fails*. `36a9602` did that deliberately for ``type_is_default``
	and chose the direction that fails as noise — an older body reads as *nothing here is the
	default*, so a type is printed that need not have been. This one failed as **loss**, which
	is the direction that cannot be spotted by reading the output.
	"""

	link = subroutine.views.Link.model_validate(A_LINK_FROM_BEFORE_CATEGORIES)

	assert link.link_category == subroutine.domain.readiness.GATING, (
		"a blocker from an instance that predates categories still holds work up"
	)

	# The three surfaces are unchanged and still compare to `gating`; this is the value they
	# now get. Asserting the comparison rather than the string is what makes that explicit.
	assert (link.link_category == subroutine.domain.readiness.GATING) is (
		A_LINK_FROM_BEFORE_CATEGORIES["link_type"] == "blocks"
	), "and it agrees with the rule that older instance runs"


def test_a_key_from_before_categories_that_nothing_recognises_stays_unstated () -> None:
	"""The honest half: a relation somebody added by hand cannot be classified from here.

	It is left ``None`` rather than guessed at, and ``None`` is not any category — so it holds
	nothing up, which is what that instance's own pre-category rule did with it too. The point
	of asserting it is that ``None`` and ``""`` are different claims: *nobody said* against
	*somebody said nothing*.
	"""

	invented = dict(A_LINK_FROM_BEFORE_CATEGORIES, link_type="holds_up")
	link = subroutine.views.Link.model_validate(invented)

	assert link.link_category is None
	assert link.link_category != subroutine.domain.readiness.GATING


def test_a_category_the_server_did_state_is_never_overwritten () -> None:
	"""A current instance decides, and the fill must not second-guess it.

	This is the case a workspace that has re-categorised ``blocks`` depends on: the server
	says ``describing``, and a client that "corrected" it from the key would put `#1156` back.
	"""

	said = dict(A_LINK_FROM_BEFORE_CATEGORIES, link_category="describing")

	assert subroutine.views.Link.model_validate(said).link_category == "describing"


def test_the_fill_is_derived_from_the_seed_rather_than_written_out_beside_it () -> None:
	"""``BEFORE_CATEGORIES`` must stay a derivation, because a copy is what rots.

	The tempting shape here was a frozen table of five pairs — *what these keys meant then*, as
	against the seed's *what they mean now*. That distinction is real and it buys a second copy
	of a rule, which is this codebase's signature defect; the seed is already this program's
	statement of what a standard relation key means.

	So the guard is not "the two agree" — they cannot disagree — but that the derivation is
	still a derivation and still covers the relation the whole defect was about.
	"""

	seeded = {one.key: one.category for one in subroutine.db.seed.LINK_TYPES}

	assert seeded == subroutine.domain.links.BEFORE_CATEGORIES, (
		"this map is meant to *be* the seed's categories; a hand-written copy would need a "
		"guard saying the two agree, and two copies that agree are invisible until one stops"
	)
	assert (
		subroutine.domain.links.BEFORE_CATEGORIES["blocks"]
		== subroutine.domain.readiness.GATING
	), "and the relation `#1168` was about is in it"


#: A listing as an instance one release behind describes it: the two keys named after what the
#: lists *contain*, and neither of the keys named after the parameter that consumes them.
#: Written out for the reason above — capturing needs a running 0.8.0.
A_LISTING_FROM_BEFORE_THE_RENAME = {
	"path": "/v1/tasks",
	"filters": ["project", "status"],
	"sortable": ["created_at", "due_at"],
	"selectable": ["ref", "title"],
	"formats": ["json", "table"],
}

#: The same listing as an instance one release *ahead* describes it, once the deprecated pair
#: has been dropped. This one is a prediction rather than a record, and it is the only version
#: of this client that can ever be taught to read it.
A_LISTING_FROM_AFTER_THE_REMOVAL = {
	"path": "/v1/tasks",
	"filters": ["project", "status"],
	"order": ["created_at", "due_at"],
	"fields": ["ref", "title"],
	"formats": ["json", "table"],
}


def test_an_older_instances_listing_still_says_what_can_be_ordered_and_selected () -> None:
	"""The same lesson as the category above, on the release that renamed two published keys.

	``order`` and ``fields`` have to be **defaulted** — a required field would make a 0.8.1
	client refuse every 0.8.0 instance outright, which is `#345` in its worst direction. But a
	default answers *will it refuse* and says nothing about *will it misread*, and empty is the
	most misleading value these two could take: a client reads it as *this listing sorts by
	nothing* about an instance advertising thirteen names, and the discovery endpoint that
	exists to save a round trip has cost one instead.
	"""

	listing = subroutine.views.Listing.model_validate(A_LISTING_FROM_BEFORE_THE_RENAME)

	assert listing.order == ["created_at", "due_at"], (
		"a 0.8.0 instance says what it can sort by under the old key, and the caller asked "
		"the question rather than the key"
	)
	assert listing.fields == ["ref", "title"]


def test_a_newer_instances_listing_still_answers_the_deprecated_names () -> None:
	"""The half that can only be written now, because this is the client that ships with it.

	0.9.0 drops ``sortable`` and ``selectable``. A 0.8.1 client reading one would then have the
	identical silence one release later, in the direction nobody is watching — so the fill runs
	both ways and the deprecated names keep answering for as long as this model declares them.
	"""

	listing = subroutine.views.Listing.model_validate(A_LISTING_FROM_AFTER_THE_REMOVAL)

	assert listing.sortable == ["created_at", "due_at"]
	assert listing.selectable == ["ref", "title"]


def test_a_listing_that_offers_nothing_is_not_given_something () -> None:
	"""Empty is ambiguous here and the ambiguity is deliberately not resolved.

	*The instance did not say* and *there is nothing to sort by* are different claims, and
	``None`` would tell them apart — at the cost of publishing ``array | null`` for a
	distinction that changes no outcome. It changes none because a listing genuinely offering
	nothing sends **both** names empty, so the fill has nothing to copy either way.
	"""

	bare = subroutine.views.Listing.model_validate(
		{"path": "/v1/nothing", "filters": [], "formats": []}
	)

	assert bare.order == [] and bare.sortable == []
	assert bare.fields == [] and bare.selectable == []


def test_what_the_instance_did_state_is_never_overwritten () -> None:
	"""The instance decides, and a fill that second-guessed it would be a fabrication.

	The case that would break: an instance whose two lists legitimately differ. Nothing builds
	one today — ``meta`` publishes one value under each pair of names — but the fill must be a
	repair for silence rather than a rule about what the pair contains, or it becomes the
	second copy this rename was spent removing.
	"""

	disagreeing = dict(
		A_LISTING_FROM_BEFORE_THE_RENAME, order=["title"], fields=["ref"]
	)
	listing = subroutine.views.Listing.model_validate(disagreeing)

	assert listing.order == ["title"], "the new key was stated and must stand"
	assert listing.sortable == ["created_at", "due_at"], "and so must the old one"


def test_unparsed_models_do_not_silently_widen_the_scope () -> None:
	"""``fields_at`` must find the classes it is given, on source this test controls."""

	found = fields_at(
		"import pydantic\n"
		"class Base(pydantic.BaseModel):\n"
		"\tone: int\n"
		"class Child(Base):\n"
		"\ttwo: str\n"
	)

	assert found["Base"] == {"one"}
	assert found["Child"] == {"one", "two"}, "inheritance must be resolved, or every inherited field reads as new"
