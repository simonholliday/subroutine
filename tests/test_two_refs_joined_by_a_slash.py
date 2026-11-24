"""Two refs joined by a slash are both mentions, and an upgrade finds them again - `SR#5319`.

The L-2 bullet of the cold review of 2026-11-11 on two refs joined by a slash, decided in `#5285`.
`SR#5146` kept another workspace's address, ``metacortex/#42``, out of the index by refusing every
ref written straight after a slash, so ``#12/#13`` was read as ``#12`` alone, and a backlink an
address made before it stayed until its text was next saved. A ref after a slash is a mention now
only where the slash follows a ref, and a migration finds the mentions of every text holding ``/#``
again.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.migrate
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.mentions
import subroutine.domain.projects
import subroutine.domain.tasks
import test_migrations

#: A migrated database on each backend, bound here so pytest finds it as a fixture.
migrated_url = test_migrations.migrated_url

#: The schema before this item's migration, whose program read ``#12/#13`` as ``#12``.
BEFORE = "507ebc014a86"


def test_two_refs_joined_by_a_slash_are_both_candidates () -> None:
	"""``#12/#13`` names both, beside a third too, and another workspace's address none."""

	candidates = subroutine.domain.mentions.candidates

	assert candidates("Both of #12/#13.") == [12, 13]
	assert candidates("Both of #12/#13, and #14.") == [12, 13, 14]
	assert candidates("#14/#15/#16") == [14, 15, 16]
	assert candidates("Not metacortex/#9/#10, nor work/metacortex/#11.") == []
	assert candidates("A slash alone, /#17, joins it to nothing.") == []


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_upgrade_finds_each_joined_texts_mentions_again (migrated_url: str) -> None:
	"""The migration, with rows in first, on both backends (`#1689`'s rule for a data migration).

	A task's description pastes another workspace's address of the dry cleaning and names the
	shoes and the mirror joined by a slash; at the revision before, its index holds the backlink
	the address made and the shoes alone. A comment says the same, and a deleted comment says it
	too, with no mentions, as deleting it left it. **After the upgrade the live sources name the
	shoes and the mirror, and the deleted comment still names nothing.** A task that holds no
	``/#`` is left as it stood, a row the text does not hold included, since the upgrade reads
	only the texts the old rule could have read wrongly.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)
	mention = typing.cast(sqlalchemy.Table, subroutine.db.models.work.Mention.__table__)

	def targets (source_id: typing.Any) -> set[typing.Any]:
		"""Return the ids one source's mentions name, as the database holds them."""

		with engine.connect() as connection:
			return set(
				connection.execute(
					sqlalchemy.select(mention.c.target_id).where(mention.c.source_id == source_id)
				).scalars()
			)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(
				slug="metacortex", title="Metacortex"
			)

			session.add(workspace)
			subroutine.db.seed.seed_workspace(session, workspace)
			session.flush()

			project = subroutine.domain.projects.create(
				session, workspace_id=workspace.id, key="errands", title="Errands"
			)
			cleaning, shoes, mirror = (
				subroutine.domain.tasks.create(session, project=project, title=title)
				for title in (
					"Pick up the dry cleaning",
					"Collect the shoes from the cobbler",
					"Hang the mirror",
				)
			)
			text = (
				f"Copied from metacortex/#{cleaning.ref}, and both of #{shoes.ref}/#{mirror.ref}."
			)
			pasted = subroutine.domain.tasks.create(
				session, project=project, title="Pack for Zion", description=text
			)
			plain = subroutine.domain.tasks.create(
				session, project=project, title="Return the keys", description=f"See #{shoes.ref}."
			)
			said, unsaid = (
				subroutine.db.models.activity.Comment(
					workspace_id=workspace.id,
					entity_type="task",
					entity_id=pasted.id,
					body=text,
					deleted_at=deleted,
				)
				for deleted in (None, subroutine.db.types.utcnow())
			)

			session.add_all([said, unsaid])
			session.commit()

			workspace_id = workspace.id
			ids = {
				name: item.id
				for name, item in (
					("cleaning", cleaning),
					("shoes", shoes),
					("mirror", mirror),
					("pasted", pasted),
					("plain", plain),
					("said", said),
					("unsaid", unsaid),
				)
			}

		subroutine.db.migrate.downgrade(migrated_url, BEFORE)

		# **The index as the revision before left it**: the address's backlink, the shoes without
		# the mirror, and for the task holding no ``/#`` a row its text does not hold.
		with engine.begin() as connection:
			connection.execute(mention.delete())

			for source_type, source, targeted in (
				("task", "pasted", ("cleaning", "shoes")),
				("comment", "said", ("cleaning", "shoes")),
				("task", "plain", ("mirror",)),
			):
				for target in targeted:
					connection.execute(
						mention.insert().values(
							id=subroutine.db.types.new_uuid(),
							workspace_id=workspace_id,
							source_type=source_type,
							source_id=ids[source],
							target_type="task",
							target_id=ids[target],
							created_at=subroutine.db.types.utcnow(),
						)
					)

		subroutine.db.migrate.upgrade(migrated_url)

		named = {ids["shoes"], ids["mirror"]}

		assert targets(ids["pasted"]) == named, "the task's mentions were not found again"
		assert targets(ids["said"]) == named, "the comment's mentions were not found again"
		assert targets(ids["unsaid"]) == set(), "a deleted comment gained mentions"
		assert targets(ids["plain"]) == {ids["mirror"]}, "a text holding no '/#' was read again"

		subroutine.db.migrate.downgrade(migrated_url, BEFORE)

		assert targets(ids["pasted"]) == named, "going back changed the index"

	finally:
		engine.dispose()
