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

Every licence here is permissive and none binds the owner the way a copyleft dependency
would (§2.2a). All of them nevertheless **require the notice to travel with the file**, and a
minified build and a drawing carry no header — so the licence text sits beside each file and
``tests/test_web.py`` fails the build if one goes missing.

**Not all of it is code** (`#2864`, `#2865`). The app's mark is a drawing somebody else made
and its headings are set in somebody else's font, and both are vendored for the same reasons:
an instance serves them rather than fetching them, the repository holds what was served, and
the licence gate can see an SVG and a woff2 no better than it can see a JavaScript file.
"""

import dataclasses
import pathlib

#: Where the copies live, beside this module.
DIRECTORY = pathlib.Path(__file__).resolve().parent / "vendor"

#: Licences a vendored file may carry. Permissive only, and named rather than pattern-matched:
#: "does this string look permissive" is the kind of check that says yes to something nobody
#: read. Adding one is a decision, and §2.2a is the reasoning it has to satisfy.
#:
#: **`OFL-1.1` joined it on 2026-09-17** (`#2865`), for the font the headings are set in. §2.2a's
#: question is whether a dependency binds the owner where our own licence does not, and the SIL
#: Open Font Licence answers no: it governs the font files, never the program that renders with
#: them, and what it asks of somebody redistributing them is that the notice travels and that
#: they are not sold on their own - which is this file's whole arrangement already. Its one
#: other rule is a Reserved Font Name, so a *modified* copy may not keep the name; the files
#: here are unmodified.
ALLOWED = frozenset({"MIT", "Apache-2.0", "ISC", "BSD-3-Clause", "OFL-1.1"})


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
		digest="sha256:a4b9cb64160e0ed7aa82a88d0b3c1bbda5d3d8cc8768f44c5a2f35d35485250b",
	),
	Vendored(
		# **The app's mark** (`#2864`), and the one file here that is not code. Simon chose it for
		# Subroutine on 2026-09-17 from the set the other products draw theirs from, and
		# `scripts/marks.py` draws every icon this app serves from this file - so the shape is
		# written down once and `assets/favicon.md` says what is made of it.
		filename="kanban.svg",
		package="lucide-static",
		version="1.47.0",
		licence="ISC",
		source="https://registry.npmjs.org/lucide-static/-/lucide-static-1.47.0.tgz",
		# Both notices the package ships: Lucide's own ISC, and the MIT of the Feather icons
		# some of Lucide's are derived from (`#1940`). This is not one of those, and the licence
		# travels whole anyway.
		notice="lucide.LICENSE",
		digest="sha256:0048f2a541eb657e0557146d3cf070c8e513901a2dc67e2fb9e07d429071a0f3",
	),
	Vendored(
		# **The face headings are set in** (`#2865`), and the weight the wordmark uses is the one
		# below. Both are the Latin subset Fontsource ships, which is what the branding was chosen
		# in; between them they are 29.3 KB, fetched once by a reader and then cached.
		filename="lexend-latin-400-normal.woff2",
		package="@fontsource/lexend",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/lexend/-/lexend-5.3.0.tgz",
		notice="lexend.LICENSE",
		digest="sha256:0601e0a909219a542cdd581e3f8f1ff8fb208978cbc9bca1c90df02fd8062bb1",
	),
	Vendored(
		# **The weight a row's title asks for** (`#2871`). Without it `font-weight: 500` draws
		# the 400 file - measured, not assumed - so every title in a list, an agenda, a board
		# and the journal would have lost the weight that separates it from the meta beneath.
		filename="lexend-latin-500-normal.woff2",
		package="@fontsource/lexend",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/lexend/-/lexend-5.3.0.tgz",
		notice="lexend.LICENSE",
		digest="sha256:27cf5288dd5129fb2f93d230e4e9df7684222d732767284b142801c274ce95b0",
	),
	Vendored(
		filename="lexend-latin-600-normal.woff2",
		package="@fontsource/lexend",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/lexend/-/lexend-5.3.0.tgz",
		# One notice covers all three faces, as `preact.LICENSE` covers two files: named on each
		# rather than left blank, since a shared notice and a missing one look the same from here.
		notice="lexend.LICENSE",
		digest="sha256:c3c291158a48c5172383ec8febb21ca64075b7c9d8413553683b254a247efdda",
	),
	Vendored(
		# **The face a reader reads, at the weight the page reads it** (`#2877`, `#2873`). Inter at
		# its usual 400 carries as much ink as the serif that read heavy, so the body is set at 300
		# here as it was there. **The notice is `rsms/inter`'s own `LICENSE.txt`**, not the
		# repackager's: Fontsource's published `LICENSE` is a template naming Google Inc. whatever
		# the font (`#2868`).
		filename="inter-latin-300-normal.woff2",
		package="@fontsource/inter",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/inter/-/inter-5.3.0.tgz",
		notice="inter.LICENSE",
		digest="sha256:be0276550393a72b94d673505567dceba801511d5e1ca5a87793190dc5d5a6ca",
	),
	Vendored(
		# Kept although almost nothing declares it (`#2873`): the explicit 400s sit inside contexts
		# declaring 600, and a missing weight is substituted in silence (`#2871`).
		filename="inter-latin-400-normal.woff2",
		package="@fontsource/inter",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/inter/-/inter-5.3.0.tgz",
		notice="inter.LICENSE",
		digest="sha256:8909904ab6c872eb994093482a88a28eca2cd95912d7b6fecd72103b0dc07edc",
	),
	Vendored(
		filename="inter-latin-500-normal.woff2",
		package="@fontsource/inter",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/inter/-/inter-5.3.0.tgz",
		notice="inter.LICENSE",
		digest="sha256:f3779f1efccc4bdcdf9c0a02ab95bf6bd092ed09c48c08cedc725889edd1d19f",
	),
	Vendored(
		filename="inter-latin-600-normal.woff2",
		package="@fontsource/inter",
		version="5.3.0",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/inter/-/inter-5.3.0.tgz",
		notice="inter.LICENSE",
		digest="sha256:f9a06e79cd3a2a20951c0f0e28f66dd0e6d3fda73911d640a2125c8fcb78f21a",
	),
	Vendored(
		# **The face code and the item number are set in** (`#2877`), back exactly as `#2868`
		# reviewed it - the same package, version and digests, recovered from that commit rather
		# than fetched again. 600 is here because `**`code`**` renders `strong > code` and a `th` is
		# 600: without a real bold face a browser smears the regular one, and says nothing.
		filename="jetbrains-mono-latin-400-normal.woff2",
		package="@fontsource/jetbrains-mono",
		version="5.2.5",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/jetbrains-mono/-/jetbrains-mono-5.2.5.tgz",
		notice="jetbrains-mono.LICENSE",
		digest="sha256:14425ba9c695763c1547f48a206b7aa60350a33ae23de09f0407877f3fcd89eb",
	),
	Vendored(
		filename="jetbrains-mono-latin-600-normal.woff2",
		package="@fontsource/jetbrains-mono",
		version="5.2.5",
		licence="OFL-1.1",
		source="https://registry.npmjs.org/@fontsource/jetbrains-mono/-/jetbrains-mono-5.2.5.tgz",
		notice="jetbrains-mono.LICENSE",
		digest="sha256:400c6bfda18d5d14acad1c15d6dcb9f8e13c015e7286317e0b9a482539bef147",
	),
)
