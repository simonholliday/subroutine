"""Which copies of this software are involved in one call — item ``#381``.

A session reaches Subroutine through as many as three installations that upgrade separately,
and until this module existed nothing anywhere reported any of them:

- the **plugin**, a copy of ``plugins/subroutine`` that the editor caches under its version
  and hands to the session — the skill, the configuration fields and ``.mcp.json``;
- the **program**, whatever ``subroutine`` resolves to on the machine, which is what actually
  serves the tools and answers a command line;
- the **instance**, the process on the far end of a connection, with a database at some
  migration behind or ahead of what that process expects.

Refreshing any one of them says nothing about the others, and on 2026-08-03 every pair of
them disagreed at some point in the same day. The expensive part was never the disagreement;
it was that **an agent cannot tell a capability that does not exist from one its program is
too old to have**, so "does this work?" degrades into testing every call by hand.

**Nothing here raises.** A version that cannot be determined is reported as ``None`` and
rendered as nothing at all, because this is the module a reader consults *when something is
already wrong* — a diagnostic that fails on the machine it is diagnosing is worse than no
diagnostic. That is the same rule :func:`subroutine.api.problems._accepted_field_names`
follows, and for the same reason.
"""

import json
import os
import pathlib

import subroutine

#: The editor tells a plugin's own processes where its cached copy lives, and the MCP server
#: is one of those. **Measured rather than assumed** (2026-08-03): the environment of a
#: running ``subroutine mcp`` launched by the Claude Code plugin carried
#: ``CLAUDE_PLUGIN_ROOT=/home/si/.claude/plugins/cache/subroutine/subroutine/0.1.1`` — while
#: the manifest in the tree said ``0.2.2`` and the program was a newer editable install, which
#: is `#381`'s scenario in one line of ``/proc``.
#:
#: Not a documented contract of ours, so it is consulted and never required: unset simply
#: means this process was not started by a plugin, which is true of every command line.
PLUGIN_ROOT = "CLAUDE_PLUGIN_ROOT"

#: Where a plugin keeps its manifest, relative to that root.
MANIFEST = pathlib.Path(".claude-plugin") / "plugin.json"


def program () -> str:
	"""Report the version of the program answering, as it was installed.

	Since ``#234`` this comes from the tag the package was built at, so an *editable* install
	made before a tag goes on reporting the older number until ``pip install -e .`` is run
	again. That is a real property of the machine rather than a defect to paper over, and
	reporting it is the point: a number that lags is exactly what a reader is trying to find.
	"""

	return subroutine.__version__


def ordered (version: str) -> tuple[int, ...] | None:
	"""Return a version as numbers to compare, or ``None`` when it cannot be compared.

	**Compare when it is unambiguous, decline when it is not** — item ``#417``. Three plain
	numbers order exactly one way; anything else does not, and a pre-release suffix is where
	the defensible answers multiply. ``0.2.1.dev73+g29ddffa34`` against ``0.2.4`` needs
	``packaging`` to get right, which is not a declared dependency and would be a fourteenth
	one bought for a single diagnostic line — so this returns ``None`` and every caller says
	nothing rather than guessing.

	``scripts/release.py`` has run on this rule since `#396` and now reads it from here.
	Two orderings of one thing is how the reach and the write set came apart in `#413`, and a
	release refusal disagreeing with a diagnostic about which version leads would be the same
	shape with worse consequences.
	"""

	parts = version.split(".")

	if len(parts) != 3 or not all(part.isdigit() for part in parts):
		return None

	return tuple(int(part) for part in parts)


def plugin () -> str | None:
	"""Report the version of the plugin that started this process, if one did.

	``None`` covers every way of not knowing, and they are not worth telling apart here: a
	command line, an MCP server started by hand, a plugin whose manifest has moved, an editor
	that names the variable something else. Each means "no plugin version to compare", and a
	reader who needs to know why looks at the same place either way.

	**The manifest is the authority, not the directory name.** The editor happens to key its
	cache by version today, so the path ends in the number — but that is its cache layout and
	not a promise to us, and a guard reading a version out of a path is one that reports
	confident nonsense the day the layout changes.
	"""

	root = os.environ.get(PLUGIN_ROOT)

	if not root:
		return None

	try:
		manifest = json.loads((pathlib.Path(root) / MANIFEST).read_text(encoding="utf-8"))

	except (OSError, ValueError):
		return None

	if not isinstance(manifest, dict):
		return None

	version = manifest.get("version")

	return version if isinstance(version, str) else None


#: What a caller says about itself, so an instance can tell a stale one from a current one.
#:
#: **`#839`'s client half, and it ships before anything reads it on purpose.** A plugin is a
#: *cache key* — Claude Code keeps an installed copy under its version — so the thing that goes
#: stale is the copy on somebody's machine. A fix that taught only the *server* to notice would
#: be useless for exactly the population it is for: a stale caller is running old client code by
#: definition, so whatever is emitted today is what a future instance has to work with.
#:
#: **Measured before it was written** (`#839`, and `sandbox/#10` from the far side): nothing
#: reached the server at all. ``User-Agent`` carries ``API_VERSION`` — the *contract* version,
#: ``1.0`` — and `#539` moved the tool implementations server-side, so ``plugin()`` below
#: reads ``CLAUDE_PLUGIN_ROOT`` in a process the plugin did not start. Both halves of `#381`'s
#: three-way check were therefore inert on this transport, and the skill tells a reader that
#: silence after the version line means nothing is wrong.
#:
#: **Nothing consumes these yet and that is not the same as an inert control** (`#303`). An
#: inert control claims to enforce something and does not; this transmits a fact so that a later
#: release can. Both relays are driven against the real application and asserted to send them,
#: so deleting them fails rather than going quiet — which is what a control nothing reads could
#: not say for itself.
#:
#: A missing ``Subroutine-Plugin`` means *no plugin contributed this connection* — a bare CLI,
#: or a script. A missing ``Subroutine-Program`` means *nothing of ours is running here*, which
#: is the ``subroutine-remote`` shape, where the editor posts straight to ``/mcp`` and the only
#: version that exists is a literal in the manifest.
#: What is running on the caller's own machine, and which cached copy of the plugin started it.
#: Named here rather than spelled at each relay, because a header written twice is two strings
#: that can drift and only one of them is ever read.
PROGRAM_HEADER = "Subroutine-Program"
PLUGIN_HEADER = "Subroutine-Plugin"


def calling () -> dict[str, str]:
	"""Return the headers naming what is making this request, omitting what cannot be known."""

	said = {PROGRAM_HEADER: program()}
	cached = plugin()

	if cached is not None:
		said[PLUGIN_HEADER] = cached

	return said
