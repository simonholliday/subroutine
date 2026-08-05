"""What has been released, and whether moving to it needs a migration — item ``#321``.

**The answer an operator actually needs is not "is there a newer version".** It is whether
upgrading will ask them to stop the service and migrate a database, because that is the
difference between planning ten minutes and discovering them halfway through an install. A
version string on its own is something ``pip index`` already prints.

**Nothing here happens unasked.** §12.4a: a self-hosted tool that phones home uninvited is one
people stop trusting, so this module is reached only by ``subroutine db upgrade --check`` — a
command somebody typed, which is the invitation. There is deliberately **no setting** yet: a
switch governing an automatic check that does not exist would be a control that is declared,
documented and read by nothing, which is this codebase's second signature defect and already
has three instances (`#247`, `#251`, `#303`). It arrives with the thing that reads it.

**The record is published rather than derived from the package index.** PyPI knows a version
and nothing about a schema — measured, 2026-08-04: its JSON carries name, version, summary,
classifiers and URLs, with nowhere a migration head could live. So the project publishes
:data:`DEFAULT_URL`, a small file ``scripts/release.py`` writes from the derivation ``#100``
already makes. A fork changes that constant, or never asks — **there is deliberately no
setting for it** (`#420`). This paragraph said "a fork points the setting elsewhere" for a
day, two paragraphs below the note that a switch nothing reads is this codebase's second
signature defect, which is a decent measure of how easily one gets written.

**No version arithmetic.** Ordering ``0.2.1.dev57`` against ``0.3.0`` correctly needs
``packaging``, which is not a declared dependency, so nothing here compares two version
strings. It does not have to: the published record *is* the order, and the question "does
this move the schema" is answered by comparing two revisions for equality. The one case that
needs neither is the interesting one — a build that is not a published release at all, which
is what `#321` was reported from.
"""

import contextlib
import dataclasses
import typing

import httpx

import subroutine.errors

#: Where the published record lives. Raw rather than the rendered page, and pinned to the
#: default branch: a release is only released once it is on ``main``.
DEFAULT_URL = (
	"https://raw.githubusercontent.com/simonholliday/subroutine/main/docs/releases.json"
)

#: How long to wait. Short on purpose — this is a courtesy on the way to an upgrade, and an
#: operator standing at a terminal should not be made to wait on somebody else's CDN.
TIMEOUT_SECONDS = 5.0


@dataclasses.dataclass(frozen=True)
class Release:
	"""One published release, as the record describes it."""

	version: str
	schema: str
	date: str


@dataclasses.dataclass(frozen=True)
class Standing:
	"""Where a running build stands against what has been published."""

	#: What is running, which is not always what was installed: an editable install reports
	#: the version it was made at (`#234`), and `#321` was found on an instance reporting
	#: ``0.1.5.dev7`` while running something 34 commits later.
	running: str

	#: The schema this build expects, for comparing against a release's.
	schema: str | None

	#: Newest first, exactly as published.
	published: tuple[Release, ...]

	#: Whether this build has ever heard of the newest release's migration — which is what
	#: says *which direction* a difference goes, and it is the difference between "plan an
	#: outage" and "you are already past it". Supplied by the caller rather than looked up
	#: here, so that this module stays a reader of a published file and every case below can
	#: be built without a database. ``subroutine.db.migrate.knows_revision`` answers it.
	knows_the_newest_schema: bool = False

	@property
	def newest (self) -> Release | None:
		"""Return the most recent release, or ``None`` when the record is empty."""

		return self.published[0] if self.published else None

	@property
	def is_a_release (self) -> bool:
		"""Report whether what is running is one of the published versions.

		False for a development build and for an install from a checkout — which is not an
		error and is the state `#321` was reported from. It changes the answer rather than
		invalidating it: how many releases you are behind is unanswerable, and whether the
		newest one expects a different schema is not.
		"""

		return any(release.version == self.running for release in self.published)

	@property
	def behind (self) -> tuple[Release, ...]:
		"""Return the releases published after the one that is running, newest first.

		Empty when what is running is the newest, or when it is not a release at all. Read
		off the record's own order rather than by comparing version strings — the file is
		the order, which is why nothing here needs ``packaging``.
		"""

		if not self.is_a_release:
			return ()

		ahead: list[Release] = []

		for release in self.published:
			if release.version == self.running:
				return tuple(ahead)

			ahead.append(release)

		return ()

	@property
	def schema_standing (self) -> str:
		"""Say how this build's schema relates to the newest release's, in one of three words.

		**Never by comparing version strings**, which would need ``packaging``. Two revisions
		are compared for equality, and where they differ the *direction* comes from whether
		this build has ever heard of the release's revision — the question
		``migrate.knows_revision`` already exists to answer, with the same reasoning: a
		revision this build knows is one it could migrate forward from, so the release is
		behind us; one it has never seen was written later, so a migration is coming.

		**Getting this wrong is the whole cost of the feature.** A development build is
		*ahead* of the newest release, and the first version of this told Simon's own machine
		to "plan for a short outage" when installing that release would have moved his
		database backwards — a confident instruction to do the wrong thing, which is worse
		than the silence it replaced.
		"""

		newest = self.newest

		if newest is None or self.schema is None or newest.schema == self.schema:
			return "same"

		return "behind us" if self.knows_the_newest_schema else "ahead of us"


def published (url: str = DEFAULT_URL, *, client: httpx.Client | None = None) -> list[Release]:
	"""Fetch the published record, newest first.

	**Every failure is one failure**, deliberately: unreachable, not JSON, the wrong shape,
	an entry missing a field. A reader wants to know that the check could not be made, and
	distinguishing a DNS failure from a truncated file helps nobody standing at a terminal
	about to upgrade something.

	**A client handed in is borrowed, not taken** (`#422`). This closed whatever it was given,
	so a caller reusing one got it shut underneath them — invisible today because only the tests
	pass one, and exactly the kind of thing the second caller discovers rather than the first.
	"""

	try:
		with contextlib.ExitStack() as closing:
			opened = client or closing.enter_context(
				httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
			)
			answered = opened.get(url)
			answered.raise_for_status()
			body = answered.json()

	except (httpx.HTTPError, ValueError) as failure:
		raise subroutine.errors.ServiceUnavailable(
			f"Could not read the list of releases from {url}.",
			hint="Check the machine's network, or upgrade without checking first.",
		) from failure

	return _parsed(body, url)


def _parsed (body: typing.Any, url: str) -> list[Release]:
	"""Turn the fetched document into releases, refusing anything it cannot read.

	**Refused rather than salvaged.** A partially-read record would answer "no migration" for
	an entry it could not parse, which is the one wrong answer that costs somebody an outage
	they did not plan for — so a malformed file is reported as a failed check.
	"""

	rows = body.get("releases") if isinstance(body, dict) else None

	if not isinstance(rows, list):
		raise subroutine.errors.ServiceUnavailable(
			f"{url} is not a list of releases.",
			hint="Check the address, or upgrade without checking first.",
		)

	found = []

	for row in rows:
		if not isinstance(row, dict) or not all(
			isinstance(row.get(field), str) for field in ("version", "schema")
		):
			raise subroutine.errors.ServiceUnavailable(
				f"{url} has an entry this version cannot read.",
				hint="It may have been written by a newer release. Upgrade without checking "
				"first, or read it yourself.",
			)

		stamped = row.get("date")

		found.append(
			Release(
				version=row["version"],
				schema=row["schema"],
				date=stamped if isinstance(stamped, str) else "",
			)
		)

	return found


def describe (standing: Standing) -> list[str]:
	"""Say where this build stands, in the fewest lines that answer the question.

	Three cases, and the third is the one `#321` was reported from. A development build is
	*not* an error state and is not scolded: it is what somebody running from a checkout has,
	and the useful half of the answer — whether the newest release expects a different schema
	— survives perfectly well without knowing how far behind it is.
	"""

	newest = standing.newest

	if newest is None:
		return ["Nothing has been released yet."]

	if standing.running == newest.version:
		return [f"{standing.running} is the newest release."]

	if not standing.is_a_release:
		lines = [
			f"Running {standing.running}, which is not a published release.",
			f"The newest is {newest.version}.",
		]

	else:
		count = len(standing.behind)
		lines = [
			f"Running {standing.running}. {newest.version} is available"
			+ (f", {count} releases newer." if count != 1 else ", one release newer."),
		]

	lines.append(
		{
			"same": "It does not change the database schema.",
			"ahead of us": "It changes the database schema, so plan for a short outage.",
			"behind us": "Its database schema is older than this build's, so it is not an "
			"upgrade. Nothing here downgrades a database.",
		}[standing.schema_standing]
	)

	return lines
