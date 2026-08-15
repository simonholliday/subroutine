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


CATALOGUE: tuple[Vendored, ...] = (
	Vendored(
		filename="preact.js",
		package="preact",
		version="10.27.2",
		licence="MIT",
		source="https://unpkg.com/preact@10.27.2/dist/preact.module.js",
		notice="preact.LICENSE",
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
	),
	Vendored(
		filename="htm.js",
		package="htm",
		version="3.1.1",
		licence="Apache-2.0",
		source="https://unpkg.com/htm@3.1.1/dist/htm.module.js",
		notice="htm.LICENSE",
	),
	Vendored(
		filename="phosphor.js",
		package="@phosphor-icons/core",
		version="2.1.1",
		licence="MIT",
		# **Not the file that is served** — unlike the three above, this is fourteen `<path>`
		# strings lifted out of `assets/regular/*.svg` in that tarball, because the package ships
		# 1,512 icons per weight in six weights and an instance needs fourteen. The address is
		# what a replacement starts from; `phosphor.js`'s own comment says what was taken.
		source="https://registry.npmjs.org/@phosphor-icons/core/-/core-2.1.1.tgz",
		notice="phosphor.LICENSE",
	),
)
