"""What the browser app ships that we did not write, and under what licence.

**This file exists because ``scripts/check_licences.py`` cannot see any of it.** That script
walks ``importlib.metadata``, which knows about Python distributions and nothing else — so a
JavaScript file copied into this package is a dependency the licence gate is structurally
blind to. `#445` recorded that as the argument against a build step and an npm closure; the
same hole opens for three files as for three hundred, and this is what closes it.

**Vendored rather than fetched from a CDN**, decided with Simon on 2026-08-08. An instance runs
on somebody else's network, sometimes with no route to the public internet at all, and a UI
that goes blank when unpkg is unreachable is a UI that fails for reasons its operator cannot
see or fix. It also means the repository contains everything that was served, which is what
makes "read what you are running" true of the browser half as well as the Python.

Both licences here are permissive and neither one binds the owner the way a copyleft
dependency would (§2.2a). Both nevertheless **require the notice to travel with the code**, and
the minified builds carry no header — so the licence text sits beside each file and
``tests/test_web.py`` fails the build if one goes missing.
"""

import dataclasses
import pathlib

#: Where the copies live, beside this module.
DIRECTORY = pathlib.Path(__file__).resolve().parent / "vendor"

#: Licences a vendored file may carry. Permissive only, and named rather than pattern-matched:
#: "does this string look permissive" is the kind of check that says yes to something nobody
#: read. Adding one is a decision, and §2.2a is the reasoning it has to satisfy.
ALLOWED = frozenset({"MIT", "Apache-2.0", "ISC", "BSD-3-Clause"})


@dataclasses.dataclass(frozen=True)
class Vendored:
	"""One copied file, and everything needed to check or replace it."""

	#: The file, relative to :data:`DIRECTORY`.
	filename: str

	#: What it is upstream, and at what version. The version is here rather than in the
	#: filename because a name carrying a version is a name every import has to be edited for.
	package: str
	version: str

	licence: str

	#: The exact address it was fetched from. A replacement is then a `curl` rather than an
	#: archaeology exercise, which is what makes updating one of these a five-minute job.
	source: str

	#: The licence text, beside the file. Both of these licences require the notice to travel
	#: with the code and both minified builds arrive without one.
	notice: str

	#: What the file must hash to, as ``sha256:<hex>``.
	#:
	#: **Nothing pinned these** (`#927`'s M-29): the catalogue recorded the package, the
	#: version, the licence and the address, and none of that says what arrived. Replacing
	#: ``preact.js`` with arbitrary code passed the entire suite, and ``script-src 'self'``
	#: admits it by definition — the policy's whole argument is that the app loads nothing
	#: from another host, which says nothing about what is *in* the files it does load.
	#:
	#: **The digest of the file as it is served rather than of the upstream download.**
	#: ``phosphor.js`` is not the upstream file at all — it is a handful of path strings
	#: lifted out of a tarball — so a digest of the source would be uncheckable for a quarter
	#: of the catalogue and would answer a different question anyway: what matters is that the
	#: bytes in this repository are the bytes somebody reviewed.
	digest: str


CATALOGUE: tuple[Vendored, ...] = (
	Vendored(
		filename="preact.js",
		package="preact",
		version="10.27.2",
		licence="MIT",
		source="https://unpkg.com/preact@10.27.2/dist/preact.module.js",
		notice="preact.LICENSE",
		digest="sha256:a1cefabf06ec626adcb92731537e1e04fd09a7908e22551bab50540106dc950d",
	),
	Vendored(
		filename="preact-hooks.js",
		package="preact/hooks",
		version="10.27.2",
		licence="MIT",
		source="https://unpkg.com/preact@10.27.2/hooks/dist/hooks.module.js",
		# Ships in the same package as `preact`, so one notice covers both. Named explicitly
		# rather than left blank: a missing notice and a shared one look identical otherwise.
		notice="preact.LICENSE",
		digest="sha256:9295b344df14b5395a612fed63350619d029e91cc2e80e9a2a5f920e38b88972",
	),
	Vendored(
		filename="htm.js",
		package="htm",
		version="3.1.1",
		licence="Apache-2.0",
		source="https://unpkg.com/htm@3.1.1/dist/htm.module.js",
		notice="htm.LICENSE",
		digest="sha256:ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4",
	),
	Vendored(
		filename="phosphor.js",
		package="@phosphor-icons/core",
		version="2.1.1",
		licence="MIT",
		# **Not the file that is served** — unlike the three above, this is a handful of `<path>`
		# strings lifted out of `assets/regular/*.svg` in that tarball, because the package ships
		# 1,512 icons per weight in six weights and an instance needs fifteen. The address is
		# what a replacement starts from; `phosphor.js`'s own comment says what was taken, and
		# is the one place the count lives — a second copy here rotted the day `#925` added one.
		source="https://registry.npmjs.org/@phosphor-icons/core/-/core-2.1.1.tgz",
		notice="phosphor.LICENSE",
		digest="sha256:a84b2378f393f0d00b4eb6529e5ac4d1153d5ce35aae7150bee10908d98a9fd3",
	),
)
