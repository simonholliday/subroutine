"""Fail if anything Subroutine depends on is under a copyleft licence.

Subroutine is AGPL-3.0-or-later and is also offered commercially by agreement (SPEC.md
§2.2a). Both of those rest on being able to grant rights in the whole of the code, and a
copyleft *dependency* takes that away no matter who owns the rest. The dependency that
does it will not arrive as a decision anybody makes — it will arrive inside something
else's requirements, two releases from now, and be discovered during a due-diligence
exercise years later. This catches it on the day it lands.

Walks the runtime dependency closure — the packages that are actually distributed
alongside Subroutine, not the development tools — and reads each one's licence from its
installed metadata. Run by CI; run it by hand with:

    python scripts/check_licences.py
"""

import collections.abc
import importlib.metadata
import sys

import packaging.requirements
import packaging.utils

#: The distribution whose dependencies matter. Its own licence is not in question.
ROOT = "subroutine"

#: Extras to follow. ``postgres`` ships to anyone running the production backend, so its
#: dependencies are as distributed as the base set. ``dev`` deliberately is not: nothing in
#: it is shipped, and a GPL test tool constrains nobody.
EXTRAS = frozenset({"postgres"})

#: Weak copyleft: reaches the file or the library, never the application that imports it.
#: Checked *before* :data:`DENIED`, because "LGPL-3.0-only" contains "gpl-3" and the two
#: answers are not remotely the same — the first is a footnote, the second ends the
#: commercial option. Reported, not fatal, with the caveat in :func:`_report`.
FLAGGED = (
	"lesser general public",
	"lgpl",
	"mozilla public",
	"mpl-",
	"eclipse public",
	"common development and distribution",
)

#: Strong copyleft: would remove the ability to license Subroutine commercially at all.
DENIED = (
	"gnu general public",
	"gnu affero",
	"agpl",
	"gpl-2",
	"gpl-3",
	"gplv2",
	"gplv3",
	"sleepycat",
)

#: Packages whose metadata says nothing useful, with the licence established by reading
#: their repository. A line here is a decision somebody made and can be checked; silence
#: is not. Remove an entry when the package starts declaring its licence properly.
ACKNOWLEDGED: dict[str, str] = {}


def main () -> int:
	"""Report every dependency's licence, and refuse the ones that constrain us."""

	denied: list[str] = []
	flagged: list[str] = []
	unknown: list[str] = []

	for name in sorted(_closure(ROOT)):
		licences = _licences(name)
		shown = ", ".join(licences) if licences else "UNKNOWN"

		print(f"  {name:<24} {shown}")

		verdict = _classify(licences)

		if verdict == "denied":
			denied.append(f"{name} ({shown})")

		elif verdict == "flagged":
			flagged.append(f"{name} ({shown})")

		elif not licences:
			acknowledged = ACKNOWLEDGED.get(packaging.utils.canonicalize_name(name))

			if acknowledged is None:
				unknown.append(name)

	return _report(denied=denied, flagged=flagged, unknown=unknown)


def _classify (licences: list[str]) -> str:
	"""Return ``"denied"``, ``"flagged"`` or ``"permissive"`` for one package's licences.

	Each declared string is judged on its own and the worst verdict wins. Judging a joined
	blob instead is what made "LGPL-3.0-only" read as GPL-3 on this script's first run.
	"""

	verdicts = set()

	for licence in licences:
		lowered = licence.lower()

		# Weak copyleft first: every LGPL spelling contains a GPL spelling inside it.
		if any(term in lowered for term in FLAGGED):
			verdicts.add("flagged")

		elif any(term in lowered for term in DENIED):
			verdicts.add("denied")

	if "denied" in verdicts:
		return "denied"

	if "flagged" in verdicts:
		return "flagged"

	return "permissive"


def _report (*, denied: list[str], flagged: list[str], unknown: list[str]) -> int:
	"""Print the verdict and return the exit status that goes with it."""

	if flagged:
		print("\nWeak copyleft — fine as installed, with one condition:")

		for entry in flagged:
			print(f"  {entry}")

		print(
			"\nThese bind the library, not the application importing it, so a proprietary\n"
			"build may ship alongside them — provided each stays a separately installed\n"
			"package the user could replace. Freezing them into a single-file executable\n"
			"is what would change that. See SPEC.md §2.2a."
		)

	if unknown:
		print("\nNo licence in the package metadata — check these by hand:")

		for entry in unknown:
			print(f"  {entry}")

		print(
			"\nAdd each to ACKNOWLEDGED in this script with the licence you found, so the "
			"next person sees a decision rather than a gap."
		)

	if denied:
		print("\nCopyleft dependencies, which Subroutine cannot be licensed commercially with:")

		for entry in denied:
			print(f"  {entry}")

		print("\nSee SPEC.md §2.2a. Replace the dependency, or accept that AGPL is the only option.")

		return 1

	if unknown:
		return 1

	if flagged:
		print("\nNothing blocks commercial licensing.")

	else:
		print("\nEvery runtime dependency is permissively licensed.")

	return 0


def _closure (root: str) -> set[str]:
	"""Return every package installed alongside ``root``, following its requirements.

	Extras are followed only where :data:`EXTRAS` says so, and environment markers are
	evaluated for the running interpreter — so a dependency that only installs on Windows
	is not reported as a constraint on a Linux build.
	"""

	seen: set[str] = set()
	pending = [(root, frozenset(EXTRAS))]

	while pending:
		name, extras = pending.pop()
		canonical = packaging.utils.canonicalize_name(name)

		if canonical in seen:
			continue

		seen.add(canonical)

		for requirement in _requirements(name, extras):
			pending.append((requirement.name, frozenset(requirement.extras)))

	seen.discard(packaging.utils.canonicalize_name(root))

	return seen


def _requirements (
	name: str, extras: frozenset[str]
) -> collections.abc.Iterator[packaging.requirements.Requirement]:
	"""Yield the requirements of one installed package that apply here."""

	try:
		declared = importlib.metadata.requires(name) or []

	except importlib.metadata.PackageNotFoundError:
		# An unsatisfied optional dependency. Nothing is shipping it, so it constrains
		# nothing — and reporting it as missing would only teach people to ignore this.
		return

	for line in declared:
		requirement = packaging.requirements.Requirement(line)

		if requirement.marker is None:
			yield requirement

			continue

		if requirement.marker.evaluate({"extra": ""}):
			yield requirement

			continue

		for extra in extras:
			if requirement.marker.evaluate({"extra": extra}):
				yield requirement

				break


def _licences (name: str) -> list[str]:
	"""Return every licence a package declares, from any of the three places it may say so."""

	try:
		metadata = importlib.metadata.metadata(name)

	except importlib.metadata.PackageNotFoundError:
		return []

	found: list[str] = []

	# Newer packaging metadata (PEP 639); an SPDX expression when it is present at all.
	for expression in metadata.get_all("License-Expression") or []:
		found.append(str(expression).strip())

	# The free-text field, which is sometimes an SPDX id and sometimes an entire licence
	# pasted in full. Anything that long is prose, not an identifier, and matching against
	# it would flag every package that quotes the GPL in a comparison.
	for declared in metadata.get_all("License") or []:
		text = str(declared).strip()

		if text and len(text) < 200:
			found.append(text)

	# Classifiers, which are the most reliable of the three when they are used at all.
	for classifier in metadata.get_all("Classifier") or []:
		if str(classifier).startswith("License ::"):
			found.append(str(classifier).split("::")[-1].strip())

	return sorted(set(found))


if __name__ == "__main__":
	sys.exit(main())
