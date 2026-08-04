"""Whether a newer release exists, and whether taking it moves the schema — item ``#321``.

**The second half is the feature.** A version string is something ``pip index`` prints; what an
operator cannot get anywhere else is whether upgrading will ask them to stop the service. So
most of this file is about getting that answer right in the three states a machine can be in,
and one of those states is the one the item was reported from — a build that is not a published
release at all.

**Nothing here reaches the network.** Every fetch goes through an ``httpx.MockTransport``, which
is also the only way to test the failures: an unreachable host, a truncated file, a record
written by a later release than the one reading it.
"""

import json
import pathlib
import typing

import httpx
import pytest

import subroutine.db.migrate
import subroutine.errors
import subroutine.releases

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED = ROOT / "docs" / "releases.json"

#: A record with a schema change between two of its entries, so both answers are reachable.
RECORD: dict[str, typing.Any] = {
	"releases": [
		{"version": "0.3.0", "schema": "cccccccccccc", "date": "2026-09-01"},
		{"version": "0.2.1", "schema": "bbbbbbbbbbbb", "date": "2026-08-10"},
		{"version": "0.2.0", "schema": "bbbbbbbbbbbb", "date": "2026-08-02"},
	]
}


def _answering (body: typing.Any, status: int = 200) -> httpx.Client:
	"""Return a client that serves one document, without touching a network."""

	return httpx.Client(
		transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
	)


def _rows () -> tuple[subroutine.releases.Release, ...]:
	"""Return :data:`RECORD` parsed, which is what every case below stands on."""

	return tuple(subroutine.releases.published("https://example.invalid/r.json", client=_answering(RECORD)))


class TestReadingTheRecord:
	"""What ``published`` accepts, and everything it turns down."""

	def test_it_reads_the_releases_newest_first (self) -> None:
		"""The order is the file's, because the file *is* the ordering."""

		assert [release.version for release in _rows()] == ["0.3.0", "0.2.1", "0.2.0"]
		assert _rows()[0].schema == "cccccccccccc"

	@pytest.mark.parametrize(
		"body",
		[
			[],
			{"releases": "0.3.0"},
			{"nothing": "useful"},
			{"releases": [{"version": "0.3.0"}]},
			{"releases": [{"schema": "cccccccccccc"}]},
			{"releases": [{"version": "0.3.0", "schema": 12}]},
		],
		ids=["a list", "not a list of rows", "no releases key", "no schema", "no version", "schema is not a string"],
	)
	def test_a_record_it_cannot_read_is_a_failed_check (self, body: typing.Any) -> None:
		"""**Refused rather than salvaged**, and that is the safety-critical direction.

		A partially-read record would answer "no migration" for the entry it could not parse,
		which is the one wrong answer that costs somebody an outage they did not plan for. A
		check that could not be made is a thing to report; a check that quietly answered from
		half a file is not.
		"""

		with pytest.raises(subroutine.errors.SubroutineError):
			subroutine.releases.published("https://example.invalid/r.json", client=_answering(body))

	def test_an_unreachable_record_is_a_failed_check_and_not_a_crash (self) -> None:
		"""The commonest failure by far, and the one an operator meets on a locked-down box."""

		def refuse (request: httpx.Request) -> httpx.Response:
			raise httpx.ConnectError("no route to host")

		client = httpx.Client(transport=httpx.MockTransport(refuse))

		with pytest.raises(subroutine.errors.SubroutineError) as failed:
			subroutine.releases.published("https://example.invalid/r.json", client=client)

		assert "upgrade without checking first" in (failed.value.hint or "")

	def test_a_refusal_from_the_host_is_a_failed_check (self) -> None:
		"""A 404 is what a moved file looks like, and it must not read as "no releases"."""

		with pytest.raises(subroutine.errors.SubroutineError):
			subroutine.releases.published(
				"https://example.invalid/r.json", client=_answering({}, status=404)
			)


class TestWhereABuildStands:
	"""The three states a machine can be in, and the direction of the schema difference."""

	def test_running_the_newest_release_says_so_and_stops (self) -> None:
		"""One line. There is nothing else worth saying to somebody who is up to date."""

		standing = subroutine.releases.Standing(
			running="0.3.0", schema="cccccccccccc", published=_rows()
		)

		assert subroutine.releases.describe(standing) == ["0.3.0 is the newest release."]

	def test_being_behind_counts_the_releases_and_names_the_schema_change (self) -> None:
		"""Counted off the record's order rather than by comparing version strings."""

		standing = subroutine.releases.Standing(
			running="0.2.0", schema="bbbbbbbbbbbb", published=_rows()
		)

		assert [release.version for release in standing.behind] == ["0.3.0", "0.2.1"]

		said = subroutine.releases.describe(standing)

		assert "2 releases newer" in said[0]
		assert said[1] == "It changes the database schema, so plan for a short outage."

	def test_one_release_behind_is_written_as_one (self) -> None:
		""""1 releases newer" is the sort of thing nobody proof-reads until a stranger does."""

		standing = subroutine.releases.Standing(
			running="0.2.1", schema="bbbbbbbbbbbb", published=_rows()
		)

		assert "one release newer" in subroutine.releases.describe(standing)[0]

	def test_a_release_that_changes_nothing_says_that_too (self) -> None:
		"""The answer that lets somebody upgrade without arranging anything."""

		record = {"releases": [dict(RECORD["releases"][1]), dict(RECORD["releases"][2])]}
		rows = tuple(
			subroutine.releases.published(
				"https://example.invalid/r.json", client=_answering(record)
			)
		)
		standing = subroutine.releases.Standing(
			running="0.2.0", schema="bbbbbbbbbbbb", published=rows
		)

		assert subroutine.releases.describe(standing)[1] == (
			"It does not change the database schema."
		)

	def test_a_development_build_is_not_treated_as_a_release (self) -> None:
		"""`#321`'s own case: an instance reporting a version PyPI has never heard of.

		Not an error and not scolded — it is what an editable install or a checkout has, and
		the useful half of the answer survives without knowing how far behind it is.
		"""

		standing = subroutine.releases.Standing(
			running="0.2.1.dev57+gfd89a79", schema="cccccccccccc", published=_rows()
		)

		assert not standing.is_a_release
		assert standing.behind == (), "how far behind is unanswerable, so it is not answered"
		assert "not a published release" in subroutine.releases.describe(standing)[0]

	def test_a_build_ahead_of_the_newest_release_is_not_told_to_plan_an_outage (self) -> None:
		"""**The mistake the first version of this made, on Simon's own machine.**

		A development build is *ahead* of the newest release, so its schema differs and the
		difference points backwards. Reporting that as "plan for a short outage" is a
		confident instruction to install something that would move the database the wrong
		way — worse than the silence it replaced, which is the whole reason this is asserted
		rather than assumed.

		The direction comes from whether this build has heard of the release's revision, which
		is what ``migrate.knows_revision`` already exists to answer.
		"""

		standing = subroutine.releases.Standing(
			running="0.4.0.dev1",
			schema="dddddddddddd",
			published=_rows(),
			knows_the_newest_schema=True,
		)

		assert standing.schema_standing == "behind us"

		said = subroutine.releases.describe(standing)

		assert "plan for a short outage" not in " ".join(said)
		assert "not an upgrade" in said[-1]

	def test_an_unknown_revision_means_a_migration_is_coming (self) -> None:
		"""And the other direction, which is the ordinary one for somebody who is behind."""

		standing = subroutine.releases.Standing(
			running="0.2.0",
			schema="bbbbbbbbbbbb",
			published=_rows(),
			knows_the_newest_schema=False,
		)

		assert standing.schema_standing == "ahead of us"

	def test_an_empty_record_says_nothing_has_been_released (self) -> None:
		"""A fork before its first release, which must not read as an error."""

		standing = subroutine.releases.Standing(running="0.1.0", schema="a", published=())

		assert subroutine.releases.describe(standing) == ["Nothing has been released yet."]


class TestThePublishedRecord:
	"""The file this repository actually ships, which is the one a stranger will read."""

	def test_it_parses_with_the_reader_that_will_read_it (self) -> None:
		"""**Through ``published`` rather than ``json.load``**, which is the point.

		A record that parses as JSON and not as releases would be discovered by an operator
		checking for an upgrade, which is the worst moment. The reader is strict on purpose,
		so the file has to satisfy it here.
		"""

		body = json.loads(PUBLISHED.read_text(encoding="utf-8"))
		rows = subroutine.releases.published(
			"https://example.invalid/r.json", client=_answering(body)
		)

		assert rows, "the shipped record has no releases in it"

	def test_every_recorded_schema_is_one_this_build_has_heard_of (self) -> None:
		"""Nothing in the past may name a revision this build does not know.

		A release's schema head is always an ancestor of the current one, so failing this
		means either the record was hand-edited or a migration was deleted from the history —
		and the second would make ``upgrade`` unable to migrate anybody forward from that
		release. Cheap to check, and it is the sort of thing that is discovered years later.
		"""

		body = json.loads(PUBLISHED.read_text(encoding="utf-8"))

		for row in body["releases"]:
			assert subroutine.db.migrate.knows_revision(row["schema"]), (
				f"{PUBLISHED.name} records {row['version']} at schema {row['schema']}, "
				f"which this build has never heard of"
			)

	def test_no_version_is_recorded_twice (self) -> None:
		"""A duplicate would make "how far behind" count one of them twice."""

		versions = [row["version"] for row in json.loads(PUBLISHED.read_text(encoding="utf-8"))["releases"]]

		assert len(versions) == len(set(versions))
