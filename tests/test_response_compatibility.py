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

import pydantic
import pytest

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
	"""Return the most recent tag, or ``None`` in a checkout that has none.

	The same question ``scripts/check_release_notes.py`` asks to compare migration heads, so the
	mechanism is one this repository already relies on rather than a new dependency on git.
	"""

	found = subprocess.run(
		["git", "describe", "--tags", "--abbrev=0"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	return found.stdout.strip() or None


def _source_at (tag: str, path: str) -> str | None:
	"""Return a file as it was at ``tag``, or ``None`` if it was not there."""

	found = subprocess.run(
		["git", "show", f"{tag}:{path}"],
		cwd=ROOT, capture_output=True, text=True, check=False,
	)

	return found.stdout if found.returncode == 0 else None


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
		bases[node.name] = [
			entry.id for entry in node.bases if isinstance(entry, ast.Name)
		]

	def resolved (name: str, seen: frozenset[str] = frozenset()) -> set[str]:
		"""Return a class's fields plus everything it inherits from classes in this file."""

		if name in seen:
			return set()

		found = set(declared.get(name, set()))

		for base in bases.get(name, []):
			found |= resolved(base, seen | {name})

		return found

	return {name: resolved(name) for name in declared}


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

	source = _source_at(tag, VIEWS)

	if source is None:
		pytest.skip(f"{VIEWS} did not exist at {tag}")

	return fields_at(source)


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
