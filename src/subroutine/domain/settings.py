"""What an installation may configure per workspace and per project — design `#1024`.

**The storage already existed and was already inert, which is what this module is really
about.** ``workspace.settings`` and ``project.settings`` have been JSON columns since the initial
migration, published raw on both views. Only one key was ever written into either —
``visible_status_keys``, put there by a project template — and it was **read nowhere in
``src/``**. So *how do we store settings* was answered long ago; the unanswered question was
**what makes a settings key real**, and until this module nothing did.

**A registry, and a guard that proves something reads each entry.** Everything derives from
:data:`SETTINGS`: what a scope accepts, what a refusal says, what is in force where, and — the
part that matters most — ``tests/test_settings.py`` fails when a declared setting is read by
nothing. That is `#303`'s recorded lesson (*the list was never the control, the guard is*)
applied before the defect rather than after it. Without that clause this module would be the
tenth instance of the declared-and-read-by-nothing family it exists to end.

**Adding a setting is an entry and a default. No migration, no backfill, no data change** —
because unset means *not stated* and the default lives in code. That is the whole extensibility
of the design, and it is a property of *declaring* settings rather than of storing them in JSON.

**Dotted keys, never nested objects.** A flat map merges across scopes trivially; nested objects
need a replace-or-merge rule per key, which is a second registry nobody wants. Grouping is a
rendering concern — a settings screen groups by prefix, and the storage does not need to know
that sections exist.

**Refuse an unknown key on write; ignore one on read.** Asymmetric on purpose. Refusing on write
is `#898`'s decided default one layer in and catches a typo where somebody can still fix it;
ignoring on read is what makes a downgrade survivable, since an instance rolled back to a version
that does not know a key must still serve the entity holding it.

**This registry describes what a *caller* may set. It is not the whole content of those
columns** (`#1030`). ``db/seed.py`` writes ``seed_version`` into ``workspace.settings`` and
reads it back to decide how far a workspace has been seeded — machinery rather than a
preference, in the same map, and described nowhere here. Live project rows also carry keys from
templates that no longer write them.

**So :func:`applied` merging per key is load-bearing for something it was not designed for.**
It was chosen because a caller setting a colour has no business knowing what else is configured;
it is also the only thing stopping that caller wiping the seeder's own record. Making this field
replace wholesale — which is what every *other* field on these entities does, and therefore the
obvious tidy-up — would re-seed every workspace from zero on the next upgrade.
``tests/test_settings.py`` holds it, driven with ``seed_version`` itself.

**A JSON column is replaced, never mutated** (`#42`). SQLAlchemy does not watch inside one, so
``project.settings[key] = value`` is silently never written — and replacing it with an *identical*
dict still marks the row dirty and moves ``updated_at``, so a write happens only when the value
actually changed.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.domain.hierarchy
import subroutine.domain.palette
import subroutine.errors

#: A project's own settings, then those of each ancestor, then the workspace's. Most specific
#: first, which is the order :func:`in_force` walks.
PROJECT = "project"

#: A workspace's, which is the widest scope that exists today.
WORKSPACE = "workspace"

#: Every scope a setting may declare, widest last.
#:
#: **There is no organisation and no user here.** Neither exists as a scope yet — a `User` has no
#: settings column at all (`#904`) and Subroutine has no organisation table — and a scope
#: declared before anything can hold it is the shape this module exists to refuse. Adding one
#: later is an entry in a setting's ``scopes``, which is what makes the ladder extensible without
#: being speculative.
SCOPES: tuple[str, ...] = (PROJECT, WORKSPACE)


class Kind (typing.NamedTuple):
	"""How a setting's values are read, and what a refusal says they should be."""

	#: Returns the value to store, or raises :class:`subroutine.errors.ValidationError` naming
	#: the key. Takes the key so that a refusal talks about the setting rather than the type.
	check: typing.Callable[[typing.Any, str], typing.Any]

	#: What this kind accepts, for a caller listing what an instance can be told.
	describes: str


def _one_colour (value: typing.Any, key: str) -> str:
	"""Read a value that must name one of :data:`subroutine.domain.palette.NAMES`."""

	if not isinstance(value, str):
		raise subroutine.errors.ValidationError(
			f"{key} is a colour's name, not {type(value).__name__}.",
			errors=[
				subroutine.errors.FieldError(
					field=key,
					code="invalid_field_value",
					message="A colour is given by name.",
					hint=f"One of: {', '.join(subroutine.domain.palette.NAMES)}.",
				)
			],
		)

	return subroutine.domain.palette.refuse_unknown(value, key=key)


#: A colour from the closed palette, by name.
A_COLOUR = Kind(check=_one_colour, describes="one of the palette's colour names")


def _some_status_keys (value: typing.Any, key: str) -> list[str]:
	"""Read a value that must be a list of status keys, and return it canonically.

	**Sorted and deduplicated, because this is a set wearing a list's clothes.** A deny-list has
	no order to preserve, and a canonical form is what stops ``["done", "blocked"]`` and
	``["blocked", "done"]`` being two different stored values — which matters here more than it
	usually would, since replacing a JSON column with an *equal* dict still marks the row dirty
	and moves ``updated_at`` (`#42`).

	**The keys are not checked against the workspace's vocabulary here**, because a
	:class:`Kind` is a pure declaration with no session. That check exists and lives in
	:attr:`Setting.verify`, which the services call where the workspace is in hand.
	"""

	if not isinstance(value, list):
		raise subroutine.errors.ValidationError(
			f"{key} is a list of status keys, not {type(value).__name__}.",
			errors=[
				subroutine.errors.FieldError(
					field=key,
					code="invalid_field_value",
					message="Statuses are given as a list of their keys.",
					hint='For example: ["blocked", "needs_input"].',
				)
			],
		)

	for one in value:
		if not isinstance(one, str) or not one.strip():
			raise subroutine.errors.ValidationError(
				f"{key} holds a status key that is not a word.",
				errors=[
					subroutine.errors.FieldError(
						field=key,
						code="invalid_field_value",
						message=f"{one!r} is not a status key.",
						hint="Each entry is the key of a status, such as 'blocked'.",
					)
				],
			)

	return sorted({one.strip() for one in value})


#: Some of a workspace's status keys, in no meaningful order.
SOME_STATUS_KEYS = Kind(check=_some_status_keys, describes="a list of status keys")


class Setting (typing.NamedTuple):
	"""One thing an installation may configure, and everything anybody needs to know about it.

	**The single source of truth for this setting.** Validation on write, what ``/v1/meta``
	could publish, what a settings form would render, which permission gates it and — through
	:attr:`read_by` — the guard that proves it is not another inert control. Six things derived
	from one entry rather than six lists that have to agree.
	"""

	#: Dotted, and grouped by prefix — ``appearance.colour``.
	key: str

	#: Where it may be set, most specific first. A setting with one scope has no chain at all
	#: and resolving it is a dictionary lookup.
	scopes: tuple[str, ...]

	#: How a value is read and refused.
	kind: Kind

	#: What an unset setting reads as, at every scope. ``None`` means *not stated*, which is
	#: what lets a wider scope show through — and it is why no scope but the last may carry a
	#: real default. Migration ``233f898a2bee`` exists because ``workspace.timezone`` was
	#: ``NOT NULL DEFAULT 'UTC'``, which shadowed the instance and left a step nothing could
	#: reach.
	default: typing.Any

	#: One line, in the reader's terms, for a form or an ``explain`` topic.
	summary: str

	#: Where the value is consumed, as a path under ``src/``. **Read by the guard**, which
	#: fails when this file does not name the setting — so an entry nothing uses cannot sit
	#: here looking deliberate.
	read_by: str

	#: The verb needed to write it, or ``None`` for the scope's ordinary one — ``project:write``
	#: for a project setting, ``workspace:write`` for a workspace's (Simon's decision of
	#: 2026-08-19, recorded on `#1024`). A setting that grants a *capability* rather than
	#: choosing an appearance may want a stronger check, and this is where it says so.
	permission: str | None = None

	#: A second check, run where the workspace is known — or ``None`` where the kind is the
	#: whole rule.
	#:
	#: **A :class:`Kind` is a pure declaration and cannot query**, which is what keeps this
	#: registry readable; a setting whose values name *this workspace's own vocabulary* needs a
	#: session to be checked at all. Rather than giving every kind a session it does not want,
	#: the setting says so here and :func:`verified` runs it from the services.
	#:
	#: **It exists because the alternative failure is silent.** A deny-list entry that matches
	#: no status hides nothing and looks exactly like a setting that did not take — the
	#: declared-and-read-by-nothing family (`#303`) one level in, at the value rather than at
	#: the key.
	verify: (
		typing.Callable[[sqlalchemy.orm.Session, uuid.UUID, typing.Any], None] | None
	) = None


#: What a workspace or a project may be marked with, and the first entry in this registry.
#:
#: **`#102` was amended for this**, by Simon on 2026-08-19: colour may carry identity provided
#: the identity is a bounded unordered category, is redundant with a word already on the row, and
#: is stored as a name. All three hold — the palette is closed, the project's name is already on
#: every row under `#1018`'s ``address`` family, and this stores a name.
COLOUR = Setting(
	key="appearance.colour",
	scopes=(PROJECT, WORKSPACE),
	kind=A_COLOUR,
	default=None,
	summary="The colour this project's work is marked with, inherited by anything under it.",
	read_by="src/subroutine/views.py",
)


def _these_statuses_exist (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	value: typing.Any,
) -> None:
	"""Refuse a status key this workspace does not have, naming the ones it does."""

	model = subroutine.db.models.vocabulary.Status
	known = set(
		session.scalars(
			sqlalchemy.select(model.key).where(model.workspace_id == workspace_id)
		).all()
	)
	missing = [one for one in value if one not in known]

	if not missing:
		return

	raise subroutine.errors.ValidationError(
		f"This workspace has no status keyed {missing[0]!r}.",
		errors=[
			subroutine.errors.FieldError(
				field=HIDDEN_STATUSES.key,
				code="invalid_field_value",
				message=f"No such status: {', '.join(sorted(missing))}.",
				hint=f"This workspace has: {', '.join(sorted(known))}.",
			)
		],
	)


#: The statuses a project does not offer, inherited by anything under it.
#:
#: **A deny-list rather than an allow-list**, which is `#826`'s asymmetry applied to
#: configuration: a status added to a workspace later appears everywhere by default, where an
#: allow-list would leave it silently missing from every project that had ever configured one —
#: and an absence is the mistake nobody notices. Unset and empty mean the same harmless thing,
#: so nothing needs backfilling.
#:
#: **Keys rather than ids, declining `#916`'s precedent deliberately.** That item stored
#: item-type ids because a calendar feed *"has no reader to complain when it silently stops
#: matching"*; this has one, since a hidden status reappearing in a dropdown is visible the same
#: day. And a status cannot be renamed or added at all today (`#826`) — there is no write
#: endpoint and ``db/seed.py`` is the only thing that builds one — so the hazard is unreachable
#: rather than merely unlikely. What keys buy is a value that is readable in the JSON, typeable
#: at the command line, and diagnosable by looking at it, which is what every other status
#: reference on the wire already is. The obligation that leaves is written onto `#826`: if
#: statuses ever become writable, a rename must rewrite these lists.
#:
#: **It narrows what is *offered* and refuses no write** (Simon, 2026-08-20). A preference, not
#: a permission — so it cannot break a script, an import, or an agent that read the vocabulary
#: last week, and turning it off has no data consequence because nothing was refused while it
#: was on. The cost is named rather than hidden: a project's configured set is a suggestion, and
#: nothing downstream may assume it is complete.
HIDDEN_STATUSES = Setting(
	key="statuses.hidden",
	scopes=(PROJECT, WORKSPACE),
	kind=SOME_STATUS_KEYS,
	# An empty *tuple*, because a default is shared by every caller that falls back to it and a
	# mutable one would be a single list handed to every unconfigured project on the page. The
	# stored value is a list either way; nothing here compares the two by type.
	default=(),
	summary="Statuses this project does not offer when somebody sets one.",
	read_by="src/subroutine/views.py",
	verify=_these_statuses_exist,
)

#: Every setting this build recognises, by key.
SETTINGS: dict[str, Setting] = {
	COLOUR.key: COLOUR,
	HIDDEN_STATUSES.key: HIDDEN_STATUSES,
}


def verified (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	stored: dict[str, typing.Any],
) -> None:
	"""Run every workspace-aware check the stored settings ask for.

	Called by the services once the map to store is assembled, because that is where a session
	and a workspace are both in hand. Silent for a setting whose :class:`Kind` is the whole rule,
	which is most of them.
	"""

	for key, value in stored.items():
		found = SETTINGS.get(key)

		if found is not None and found.verify is not None:
			found.verify(session, workspace_id, value)


def offered (scope: str) -> dict[str, Setting]:
	"""Return the settings that may be set at one scope."""

	return {key: found for key, found in SETTINGS.items() if scope in found.scopes}


def _no_such_setting (key: str, scope: str) -> subroutine.errors.ValidationError:
	"""Refuse a key nothing declares, naming what this scope does accept."""

	available = sorted(offered(scope))

	return subroutine.errors.ValidationError(
		f"{key!r} is not a setting a {scope} has.",
		errors=[
			subroutine.errors.FieldError(
				field="settings",
				code="unknown_field",
				message=f"Unknown setting {key!r}.",
				hint=(
					f"A {scope} accepts: {', '.join(available)}."
					if available
					else f"A {scope} has no settings."
				),
			)
		],
	)


def validated (given: dict[str, typing.Any], *, scope: str) -> dict[str, typing.Any]:
	"""Return what to store, refusing an unknown key and reading every value.

	**An unknown key is refused rather than kept**, which is `#898`'s rule one layer in: a
	listing that quietly ignores ``creatd_at.gte`` is the failure that module exists for, and a
	settings map that quietly keeps ``appearence.colour`` is the same failure somewhere a person
	will not look again for months.

	**``None`` clears**, at every key, and is not passed to the kind — *not stated* is a value
	every setting accepts by construction, and it is what a caller sends to fall back to a wider
	scope.
	"""

	stored = {}

	for key, value in given.items():
		found = SETTINGS.get(key)

		if found is None or scope not in found.scopes:
			raise _no_such_setting(key, scope)

		if value is None:
			continue

		stored[key] = found.kind.check(value, key)

	return stored


def applied (
	existing: dict[str, typing.Any] | None,
	given: dict[str, typing.Any],
	*,
	scope: str,
) -> dict[str, typing.Any]:
	"""Return the map to store, merging what was asked into what is already there.

	**Per key, not per map, and this is a departure from §8.3 worth its own sentence.** Every
	other field on these entities replaces wholesale: a caller sending ``tags`` replaces the
	tags. Settings do not, because a caller setting a colour has no business knowing what else
	is configured — replace semantics would make *set the colour* silently clear every other
	setting on the project, and the second setting this registry gains is where somebody would
	find out.

	So the map is a namespace rather than a value: **a key not mentioned is untouched, a key
	sent as ``None`` is cleared, and a key sent with a value is set.** That is §8.3's own rule
	applied one level down, at the granularity a caller actually addresses.

	**Cleared rather than stored as null**, because *not stated* is what lets a wider scope show
	through. A stored ``None`` would mean *this project has no colour*, which reads identically
	and inherits nothing — and the difference between those two is the whole of the inheritance
	rule.
	"""

	checked = validated(given, scope=scope)
	stored = dict(existing or {})

	for key in given:
		if key in checked:
			stored[key] = checked[key]

		else:
			stored.pop(key, None)

	return stored


def in_force (setting: Setting, *, stored: typing.Sequence[dict[str, typing.Any]]) -> typing.Any:
	"""Return the value that applies, given each scope's own settings most specific first.

	**Only the scopes a setting declares**, assembled by the caller — which is what stops §6.5's
	four-level shape being copied onto settings that have one real level. `#904` made that
	correction and it is the reason this takes a sequence rather than a workspace and a project:
	a chain is the caller's to build, and most settings do not have one.

	**An unknown key in a stored map is ignored here**, which is the read half of the asymmetry
	in this module's docstring: an instance rolled back to a version that has never heard of a
	key must still serve the entity holding it.
	"""

	for holder in stored:
		if setting.key in holder:
			return holder[setting.key]

	return setting.default


def for_projects (
	session: sqlalchemy.orm.Session,
	setting: Setting,
	ids: typing.Collection[uuid.UUID],
) -> dict[uuid.UUID, typing.Any]:
	"""Return the value in force for each of these projects, looking upwards.

	    this project -> its parent -> ... -> the workspace -> the default

	**Two queries for a whole page, never one per row.** ``project.path`` already carries every
	ancestor's id, so the ancestors are looked up rather than walked — the shape
	:func:`subroutine.domain.projects.paths_for` uses, and for the same reason: a colour is
	rendered on every line, so the per-row version is `#39`'s N+1 on the one column that is
	always there.

	**Resolved here rather than in each client**, which corrects `#1023` §7. A row carries
	``project_path``, so a browser genuinely *could* walk it — but knowing which ancestor holds a
	value means holding every project's settings and repeating the walk in three surfaces.
	`#925`'s rule settles it: when a client would need a copy of a rule to render a field,
	publish the rendering instead.

	**The workspace is derived per project rather than passed in**, which is not merely
	convenient: the agenda spans workspaces (`#989`), so a page's projects do not share one. A
	single workspace argument would have given every row on that page the first workspace's
	colour — a plausible, complete, wrong answer, and one nobody would notice on a
	single-workspace instance.

	**Deliberately not narrowed by scoping, and that discloses nothing.** Visibility inherits
	*down* a project tree — §7.3a hides a project when it or any ancestor is private without a
	membership — so anybody who can see a row can already see every ancestor this reads. The
	argument is ``paths_for``'s, unchanged.
	"""

	if not ids:
		return {}

	model = subroutine.db.models.project.Project

	# `.tuples().all()` rather than the `Result` itself: a bare `dict(session.execute(...))`
	# raises, because a `Result` has a `.keys()` method and `dict` therefore treats it as a
	# mapping. A recorded trap here, met once as a ruff C416 suggestion applied to working code.
	rows = (
		session.execute(
			sqlalchemy.select(model.id, model.path, model.workspace_id).where(
				model.id.in_(set(ids))
			)
		)
		.tuples()
		.all()
	)
	ancestry = {
		uuid.UUID(segment)
		for _identity, path, _space in rows
		for segment in subroutine.domain.hierarchy.path_segments(path)
	}

	held = (
		dict(
			session.execute(
				sqlalchemy.select(model.id, model.settings).where(model.id.in_(ancestry))
			)
			.tuples()
			.all()
		)
		if ancestry
		else {}
	)

	# Every workspace the page touches, in one query for the same reason the ancestors are.
	space = subroutine.db.models.identity.Workspace
	spaces = dict(
		session.execute(
			sqlalchemy.select(space.id, space.settings).where(
				space.id.in_({identity for _row, _path, identity in rows})
			)
		)
		.tuples()
		.all()
	)

	resolved = {}

	for identity, path, workspace_id in rows:
		# **Deepest first, which is what "the nearest ancestor" means.** `path` is written
		# root-first and includes the project's own id, so the chain is that list reversed —
		# and the workspace is the last link after it.
		chain = [
			dict(held.get(uuid.UUID(segment)) or {})
			for segment in reversed(subroutine.domain.hierarchy.path_segments(path))
		]
		resolved[identity] = in_force(
			setting, stored=[*chain, dict(spaces.get(workspace_id) or {})]
		)

	return resolved
