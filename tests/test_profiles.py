"""Named credential profiles — decision ``#370``, item ``#372``.

**The feature under test is the refusal**, not the preset. Expanding ``--profile worker`` into
one project and no write set saves a little typing; turning down ``--profile observer --write
WEB`` is what stops somebody being handed a credential that does not do what they just said,
and finding out weeks later when an agent writes something nobody expected it to.

Two properties hold the module together and each has a test that fails when it stops holding:

- **Nothing a profile does is unreachable by naming the parts.** A preset able to express
  something the flags cannot would be a second permission model beside the first.
- **Every profile in the catalogue works.** The data is easy to extend and the refusals are
  where the thinking is, so a fifth profile that nobody ever expanded is the likely mistake.
"""

import pytest

import subroutine.domain.profiles
import subroutine.errors
import subroutine.permissions

#: One set of arguments each profile *accepts*, so that adding a profile without a working
#: expansion fails rather than being covered by nothing. Keyed by profile so a new entry is
#: required rather than optional — the last test in this file is what enforces that.
CANONICAL: dict[str, dict[str, list[str] | str | None]] = {
	"worker": {"projects": ["WEB"], "writes": [], "workspace": None},
	"collaborator": {"projects": ["SR", "WEB"], "writes": ["WEB"], "workspace": None},
	"observer": {"projects": [], "writes": [], "workspace": None},
	"colleague": {"projects": [], "writes": [], "workspace": "acme"},
}


def _expand (key: str, **overrides: object) -> subroutine.domain.profiles.Shape:
	"""Expand one profile with its canonical arguments, changing only what a test names."""

	arguments = {**CANONICAL[key], "scopes": [], **overrides}

	return subroutine.domain.profiles.expand(
		subroutine.domain.profiles.named(key),
		projects=arguments["projects"],  # type: ignore[arg-type]
		writes=arguments["writes"],  # type: ignore[arg-type]
		scopes=arguments["scopes"],  # type: ignore[arg-type]
		workspace=arguments["workspace"],  # type: ignore[arg-type]
	)


class TestTheCatalogue:
	"""What the four profiles are, and what stops a fifth being half-added."""

	def test_every_key_is_distinct (self) -> None:
		"""Two profiles of one name would make ``BY_KEY`` silently drop one."""

		keys = [profile.key for profile in subroutine.domain.profiles.CATALOGUE]

		assert len(keys) == len(set(keys))
		assert len(subroutine.domain.profiles.BY_KEY) == len(keys)

	def test_every_scope_a_profile_names_is_a_real_permission (self) -> None:
		"""A typo here would issue a credential narrowed to a verb nothing ever checks.

		Which is *worse* than a refusal: an unknown scope narrows the credential to nothing
		it can actually use, so the failure arrives as "this agent cannot do anything" rather
		than as "that is not a permission".
		"""

		for profile in subroutine.domain.profiles.CATALOGUE:
			assert profile.scopes <= subroutine.permissions.WORKSPACE_LEVEL, profile.key

	def test_every_profile_is_listed_in_the_help_text (self) -> None:
		"""``--help`` is built from the catalogue, so a new profile appears without editing it.

		The same argument ``docs/errors.md`` is generated for: a list written out beside the
		data it describes is a second copy, and second copies here go stale.
		"""

		described = subroutine.domain.profiles.catalogue_help()

		for profile in subroutine.domain.profiles.CATALOGUE:
			assert profile.key in described
			assert profile.summary in described

	def test_an_unknown_profile_is_refused_with_the_ones_that_exist (self) -> None:
		"""A refusal naming only what was wrong sends the reader to find a four-item list."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			subroutine.domain.profiles.named("supervisor")

		hint = refused.value.errors[0].hint or ""

		for profile in subroutine.domain.profiles.CATALOGUE:
			assert profile.key in hint

	@pytest.mark.parametrize("key", sorted(CANONICAL))
	def test_each_profile_expands (self, key: str) -> None:
		"""Every profile has arguments it accepts, and produces something usable from them."""

		shape = _expand(key)

		assert shape.scopes == sorted(subroutine.domain.profiles.BY_KEY[key].scopes)

	def test_the_canonical_arguments_cover_every_profile (self) -> None:
		"""**The guard on the guard.** A profile added with no case here is tested by nothing.

		Written the way `#141`'s exemptions should have been: the thing that closes the entry
		is deleting it, and the thing that opens one is adding a profile. Neither is optional.
		"""

		assert set(CANONICAL) == set(subroutine.domain.profiles.BY_KEY)

	def test_a_profile_produces_nothing_the_flags_could_not (self) -> None:
		"""The property that keeps a profile a shorthand rather than a capability.

		``Shape`` carries exactly the three arguments ``token create`` already takes, so this
		is checked by its shape rather than by a comparison somebody has to maintain: if a
		profile could ever set something else, there would be a fourth field here to set it
		with.
		"""

		fields = set(subroutine.domain.profiles.Shape.__dataclass_fields__)

		assert fields == {"scopes", "projects", "writes"}


class TestWorker:
	"""One project and everything under it, writing everywhere it reaches."""

	def test_it_takes_one_project (self) -> None:
		"""The reach is the project, and the write set stays null — its whole reach."""

		shape = _expand("worker")

		assert shape.projects == ["WEB"]
		assert shape.writes is None
		assert shape.scopes == []

	def test_naming_no_project_is_refused (self) -> None:
		"""A worker with no project is an unbounded agent, which is the default already."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("worker", projects=[])

		assert "exactly one" in str(refused.value)

	def test_naming_two_projects_is_refused_and_points_at_collaborator (self) -> None:
		"""The refusal names the profile that *does* mean this, because somebody wants it."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("worker", projects=["WEB", "API"])

		assert "collaborator" in (refused.value.errors[0].hint or "")

	def test_a_write_set_is_refused (self) -> None:
		"""``--write`` beside a worker is two intentions: it already writes where it reaches."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("worker", writes=["WEB"])

		assert "'--write'" in str(refused.value)


class TestCollaborator:
	"""Reads a related tree, writes one part of it. The arrangement `#371` was built for."""

	def test_it_carries_both_lists (self) -> None:
		"""Reach and write set are separate, which is the whole of decision ``#370``."""

		shape = _expand("collaborator")

		assert shape.projects == ["SR", "WEB"]
		assert shape.writes == ["WEB"]

	def test_naming_no_write_set_is_refused (self) -> None:
		"""Without one it is a worker with several projects, and should say so.

		Left alone, a null write set means "everywhere it reaches" — so a collaborator that
		forgot ``--write`` would be issued as the widest credential in the table under the
		name of the narrowest.
		"""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("collaborator", writes=[])

		assert "at least one '--write'" in str(refused.value)

	def test_naming_no_project_is_refused (self) -> None:
		"""There is nothing for a write set to be a subset of."""

		with pytest.raises(subroutine.errors.ValidationError):
			_expand("collaborator", projects=[])


class TestObserver:
	"""Reports on work and changes nothing."""

	def test_it_is_narrowed_to_the_reading_verbs (self) -> None:
		"""**Scopes, not an empty write set.** An empty list is refused at issue, deliberately.

		``project_write_scope = []`` would be "given as empty", which the issuing code turns
		down because it is something the caller has not said clearly enough to act on. Read-only
		scopes say the same thing in the vocabulary that already exists — and they also stop a
		write that has no project at all, which a write scope could not.
		"""

		shape = _expand("observer")

		assert set(shape.scopes) == subroutine.permissions.READS
		assert shape.writes is None

	def test_it_may_be_left_at_the_whole_workspace (self) -> None:
		"""A reporting agent usually wants everything, and naming nothing is how to say so."""

		assert _expand("observer").projects is None

	def test_it_may_be_bounded_to_projects (self) -> None:
		"""And a review of one tree is a real case, so the reach is optional rather than fixed."""

		assert _expand("observer", projects=["SR"]).projects == ["SR"]

	def test_a_write_set_is_refused (self) -> None:
		"""The contradiction this file exists for: it is not a narrower observer."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("observer", writes=["SR"])

		assert "changes nothing at all" in str(refused.value.errors[0].message)


class TestColleague:
	"""A second person, in one workspace, working as they would in their own."""

	def test_it_pins_a_workspace_and_nothing_else (self) -> None:
		"""No project scope and no scope narrowing: their membership decides what they may do.

		The role in decision ``#370``'s table describes the *scenario* — somebody added to a
		workspace as a member — rather than something this profile sets. Membership is granted
		by ``user add``, and a credential cannot widen what a role allows (§7.3).
		"""

		shape = _expand("colleague")

		assert shape.projects is None
		assert shape.writes is None
		assert shape.scopes == []

	def test_it_needs_a_workspace (self) -> None:
		"""Its whole meaning is "this one and no other", so it has to say which."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("colleague", workspace=None)

		assert refused.value.errors[0].field == "workspace"

	def test_naming_a_project_is_refused (self) -> None:
		"""Two answers to "what does it reach": the workspace, and these projects."""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand("colleague", projects=["WEB"])

		assert "'--project'" in str(refused.value)


class TestScopesBesideAProfile:
	"""One rule, applied to all four, because an exception is a thing to learn first."""

	@pytest.mark.parametrize("key", sorted(CANONICAL))
	def test_naming_a_scope_as_well_is_refused (self, key: str) -> None:
		"""Uniform on purpose.

		Three of the four leave scopes alone, so ``--scope`` beside them would merely narrow
		further and could be allowed — and then ``observer`` would be the single exception,
		which is a rule somebody has to learn before they can use any of them. A profile *is*
		the scope decision.
		"""

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			_expand(key, scopes=[subroutine.permissions.TASK_READ])

		assert "name the scopes, projects and writes yourself" in (
			refused.value.errors[0].hint or ""
		)
