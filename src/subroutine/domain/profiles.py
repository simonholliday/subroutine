"""Setting a credential up is choosing what it is *for* — decision ``#370``, item ``#372``.

Four named scenarios, each a shorthand for a combination of the parts a credential already
has: which projects it can see, which of those it may change, and which verbs it carries.

**A profile is a shorthand for a decision, never a capability of its own.** Everything one
produces can be typed out by naming the parts, and this module says so by *expanding* into
those parts rather than by taking a path of its own through the issuing code. That matters
more than the convenience: a preset that could express something the flags cannot would be a
second permission model, and this project has already paid for having one rule in two places.

**And the feature is really the refusal.** ``--profile observer --write SR`` is not a
narrower observer, it is two different intentions in one command, and a program that silently
picked one would be handing somebody a credential that does not do what they just said. Each
profile therefore declares what it accepts, and anything else is turned down by name with the
alternative stated.

The default when no profile is named is unchanged and stays unchanged: the contributor role,
optionally pinned to a workspace, with no project scope. That is what the one live agent
already has, and a default that moved would re-scope every credential anybody creates from
habit.
"""

import dataclasses
import enum

import subroutine.errors
import subroutine.permissions


class Reach(enum.Enum):
	"""What a profile says about the projects a credential can see at all."""

	#: Exactly one project, and everything filed underneath it.
	ONE_PROJECT = "one project"

	#: One or more, named. The point of the profile is that some are read-only.
	SOME_PROJECTS = "one or more projects"

	#: Whichever projects are named, or the whole workspace when none is.
	PROJECTS_OR_THE_WORKSPACE = "any projects named, or the whole workspace"

	#: The workspace, with no project restriction at all.
	THE_WORKSPACE = "the whole workspace"


class Writes(enum.Enum):
	"""What a profile says about where a credential may change things."""

	#: Everywhere it can reach. ``project_write_scope`` stays null, which is what every
	#: credential issued before `#371` means.
	EVERYTHING_IT_REACHES = "everything it can reach"

	#: A named subset of the reach. This is the arrangement `#371` was built for.
	SOME_OF_ITS_REACH = "only the projects named with '--write'"

	#: Nothing, enforced by the *scopes* rather than by an empty write set — an empty list is
	#: refused at issue, deliberately, because "given as empty" is something the caller has
	#: not said clearly enough to act on.
	NOTHING = "nothing at all"


@dataclasses.dataclass(frozen=True)
class Profile:
	"""One named scenario, and everything it decides."""

	#: What somebody types after ``--profile``.
	key: str

	#: One line, printed in ``--help`` and quoted back in a refusal. Says what the credential
	#: is *for*, because that is the thing being chosen.
	summary: str

	reach: Reach
	writes: Writes

	#: The verbs the credential is narrowed to. Empty means **no narrowing** (§7.3) — the
	#: owner's role decides — which is the right answer for three of the four.
	scopes: frozenset[str] = frozenset()

	#: Whether ``--workspace`` has to be given. Only true where the profile's whole meaning
	#: is "this workspace and no other".
	needs_a_workspace: bool = False


#: The four, in the order they appear in decision `#370`'s table.
#:
#: **Data in one place, checked by a test**, for the reason every allow-list here is: the
#: alternative is four branches in a command that nobody can compare at a glance, and the
#: comparison is the whole point — somebody choosing a profile is choosing between them.
CATALOGUE: tuple[Profile, ...] = (
	Profile(
		key="worker",
		summary="owns one project and everything under it",
		reach=Reach.ONE_PROJECT,
		writes=Writes.EVERYTHING_IT_REACHES,
	),
	Profile(
		key="collaborator",
		summary="reads related work for context, writes only its own project",
		reach=Reach.SOME_PROJECTS,
		writes=Writes.SOME_OF_ITS_REACH,
	),
	Profile(
		key="observer",
		summary="reports on work and changes nothing",
		reach=Reach.PROJECTS_OR_THE_WORKSPACE,
		writes=Writes.NOTHING,
		scopes=subroutine.permissions.READS,
	),
	Profile(
		key="colleague",
		summary="another person, working in one workspace as they would in their own",
		reach=Reach.THE_WORKSPACE,
		writes=Writes.EVERYTHING_IT_REACHES,
		needs_a_workspace=True,
	),
)

BY_KEY: dict[str, Profile] = {profile.key: profile for profile in CATALOGUE}


@dataclasses.dataclass(frozen=True)
class Shape:
	"""The arguments a profile expands into, ready to be issued."""

	scopes: list[str]
	projects: list[str] | None
	writes: list[str] | None


def named (key: str) -> Profile:
	"""Return the profile of that name, or refuse with the ones that exist.

	Listing the alternatives is not politeness. A refusal naming only what was wrong leaves
	the reader to go and find a list, and the list is four items long.
	"""

	found = BY_KEY.get(key)

	if found is not None:
		return found

	raise subroutine.errors.ValidationError(
		f"There is no {key!r} profile.",
		errors=[
			subroutine.errors.FieldError(
				field="profile",
				code="invalid_field_value",
				message=f"{key!r} is not one of the profiles.",
				hint=_alternatives(),
			)
		],
	)


def expand (
	profile: Profile,
	*,
	projects: list[str],
	writes: list[str],
	scopes: list[str],
	workspace: str | None,
) -> Shape:
	"""Turn a chosen profile and what was typed beside it into concrete arguments.

	Every refusal here is about a *contradiction* rather than about a value being wrong on its
	own — ``--write`` is a perfectly good flag, and it means something the observer profile
	has just said it does not do. So each names both halves and what to do instead.

	The relationship between the two project lists — that a write set lies inside the reach —
	is deliberately **not** checked here. It is checked where the ids are canonical, by
	:func:`subroutine.domain.authentication.issue_token`, and a second copy of that rule is
	exactly what this module's docstring argues against.
	"""

	# **Uniform, so there is nothing to remember.** Three of the four profiles leave scopes
	# alone, so `--scope` beside them would merely narrow further and could be allowed — and
	# then `observer` would be the one exception, which is a rule somebody has to learn before
	# they can use any of them. A profile *is* the scope decision; wanting a different one is
	# wanting the parts.
	if scopes:
		_contradiction(
			profile,
			field="scope",
			said="'--scope'",
			because="already decides which verbs the credential carries",
			instead="leave '--profile' off and name the scopes, projects and writes yourself",
		)

	if profile.needs_a_workspace and not workspace:
		raise subroutine.errors.ValidationError(
			f"The {profile.key!r} profile needs a workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="workspace",
					code="missing_field",
					message=f"{profile.key!r} means one workspace and no other, so it has to "
					f"name which.",
					hint="Add '--workspace <name>'. 'subroutine list' prints the names beside "
					"each item when there is more than one.",
				)
			],
		)

	_check_the_reach(profile, projects)
	_check_the_writes(profile, writes)

	return Shape(
		scopes=sorted(profile.scopes),
		projects=list(projects) or None,
		writes=list(writes) or None,
	)


def _check_the_reach (profile: Profile, projects: list[str]) -> None:
	"""Refuse a set of projects the profile cannot mean."""

	if profile.reach is Reach.ONE_PROJECT and len(projects) != 1:
		_wrong_number(
			profile,
			wanted="exactly one '--project'",
			given=len(projects),
			instead="use '--profile collaborator' to read several and write one of them",
		)

	if profile.reach is Reach.SOME_PROJECTS and not projects:
		_wrong_number(
			profile,
			wanted="at least one '--project'",
			given=0,
			instead="use '--profile worker' for an agent that owns a single project",
		)

	if profile.reach is Reach.THE_WORKSPACE and projects:
		_contradiction(
			profile,
			field="project",
			said="'--project'",
			because="reaches the whole workspace",
			instead="use '--profile worker' or '--profile collaborator' to bound it to "
			"projects",
		)


def _check_the_writes (profile: Profile, writes: list[str]) -> None:
	"""Refuse a write set the profile cannot mean."""

	if profile.writes is Writes.EVERYTHING_IT_REACHES and writes:
		_contradiction(
			profile,
			field="write",
			said="'--write'",
			because="writes everywhere it can reach, which is already what you asked for",
			instead="use '--profile collaborator' to read more than it can write",
		)

	if profile.writes is Writes.NOTHING and writes:
		_contradiction(
			profile,
			field="write",
			said="'--write'",
			because="changes nothing at all",
			instead="use '--profile collaborator' for an agent that reads widely and writes "
			"in one place",
		)

	if profile.writes is Writes.SOME_OF_ITS_REACH and not writes:
		_wrong_number(
			profile,
			wanted="at least one '--write'",
			given=0,
			instead="use '--profile worker' if it should write everywhere it can reach",
		)


def _contradiction (
	profile: Profile, *, field: str, said: str, because: str, instead: str
) -> None:
	"""Refuse two intentions in one command, naming both of them."""

	raise subroutine.errors.ValidationError(
		f"{said} does not go with the {profile.key!r} profile.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{profile.key!r} {because}.",
				hint=f"Either drop {said}, or {instead}.",
			)
		],
	)


def _wrong_number (profile: Profile, *, wanted: str, given: int, instead: str) -> None:
	"""Refuse a count the profile cannot work with."""

	counted = "none" if given == 0 else str(given)

	raise subroutine.errors.ValidationError(
		f"The {profile.key!r} profile wants {wanted}.",
		errors=[
			subroutine.errors.FieldError(
				field="project",
				code="invalid_field_value",
				message=f"{profile.key!r} is {_described(profile)}, and {counted} was given.",
				hint=f"Name {wanted}, or {instead}.",
			)
		],
	)


def _described (profile: Profile) -> str:
	"""Say what a profile reaches and what it may change, in one clause."""

	return f"{profile.reach.value}, writing {profile.writes.value}"


def _alternatives () -> str:
	"""List every profile with its one line, for a refusal that names no valid choice."""

	return "The profiles are: " + "; ".join(
		f"{profile.key} — {profile.summary}" for profile in CATALOGUE
	)


def catalogue_help () -> str:
	"""Describe every profile, for the ``--profile`` flag's own help text.

	Built from :data:`CATALOGUE` rather than written out beside it, so a profile added later
	appears in ``--help`` without anybody remembering to say so. The same argument
	``docs/errors.md`` is generated for.
	"""

	return "What the credential is for. " + "; ".join(
		f"{profile.key}: {profile.summary}" for profile in CATALOGUE
	)
