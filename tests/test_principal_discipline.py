"""Nothing asks a principal about its token when it means to ask about its authority.

**The failure this exists for has already happened once, and cost the whole of `#248`.**
``Principal.token is None`` meant "no credential at all" everywhere, because for a year there
was only one kind of credential and the two questions had the same answer. The moment a second
arrived, six sites went on answering the old question — and every one of them failed *open*,
including the guard against a credential minting a wider credential.

So the rule is not "never read ``.token``" — reporting which token was used, and attributing an
event to it, are both correct and are both here. The rule is that **each site is written down
with the question it is really asking**, so that a third credential type (§20.2's calendar
feed, `#514`'s OAuth client) is a re-read of five short reasons rather than a search.

Modelled on `tests/test_actor_discipline.py`, and on `#405`'s rule for the whole family: the
scanner takes the tree as an argument so a synthetic offender can be fed through the real code
rather than through a copy of its rule.
"""

import ast
import pathlib

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"

#: The names a :class:`~subroutine.domain.authentication.Principal` goes by in this codebase.
#: **Measured rather than assumed** — every Principal-typed local in `src` is called one of
#: these, and the things that are *not* principals but do have a ``.token`` are called
#: ``resolved``, ``minted``, ``found`` and ``self``, so none of them collides.
PRINCIPAL_NAMES = frozenset({"principal", "actor"})

#: Where the module holding the class itself lives. Its own reads are the definition of the
#: property rather than a use of it, so listing them would be listing the answer.
DEFINITION = "domain/authentication.py"

#: Every module that asks a principal about its token, and the question it is really asking.
#: **Deleting an entry is what closes it**, and a new one is a decision somebody takes.
ASKING: dict[str, str] = {
	"views.py": (
		"`views.credential` describes the credential presented, and a token is one of the "
		"two kinds it can be. It asks `is_local` first and handles a session before it gets "
		"here, so this read means *an API token specifically* — which is what the branch "
		"below it renders. `#248`."
	),
	"domain/local.py": (
		"`describe` says how somebody is acting, for `doctor` and `--verbose`. Three "
		"credentials, three sentences, and this is the first of three branches rather than "
		"an absence standing in for one. `#248`."
	),
	"domain/events.py": (
		"`actor_token_id` is a foreign key to `api_token`, so this is asking for a row in "
		"that table and null is the honest answer for anything else. That a browser "
		"session's events therefore carry no credential id is a real gap and belongs to "
		"`#158`'s question — *what did I do* — rather than to this one."
	),
	"api/sessions.py": (
		"Signing out names what the caller is holding instead, so this asks "
		"*which kind of credential is this* in order to write a useful hint. `#248`."
	),
}


def _asking (root: pathlib.Path) -> dict[str, list[int]]:
	"""Return every module that reads a principal's token, with the lines it does it on.

	Takes the tree as an argument so that a synthetic offender goes through this function
	rather than through a second copy of its rule — `#405`, whose lesson is that a guard
	tested against a re-implementation leaves the *scan* unchecked, which is the half that
	fails silently.
	"""

	found: dict[str, list[int]] = {}

	for path in sorted(root.rglob("*.py")):
		where = path.relative_to(root).as_posix()

		if where == DEFINITION:
			continue

		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Attribute)
				and node.attr == "token"
				and isinstance(node.value, ast.Name)
				and node.value.id in PRINCIPAL_NAMES
			):
				found.setdefault(where, []).append(node.lineno)

	return found


def test_every_module_asking_a_principal_for_its_token_says_what_it_means () -> None:
	"""A site reading `.token` and meaning "no credential" is how `#248` happened."""

	unexplained = sorted(set(_asking(SOURCE)) - set(ASKING))

	assert not unexplained, (
		f"{unexplained} ask a principal for its token and nothing says which question that "
		f"is. Add a reason to ASKING, or ask `is_local` / `credential_prefix` / `expires_at` "
		f"instead — those are the three questions this was standing in for."
	)


def test_the_list_names_only_modules_that_still_ask () -> None:
	"""An entry whose site has gone reads as a considered decision and is a fossil.

	The two-directional check `#405` went round the repository adding: without it, a module
	that stopped reading `.token` leaves a written reason behind, and the next reader takes
	it as evidence that somebody has thought about a thing nobody is doing.
	"""

	asking = _asking(SOURCE)
	gone = sorted(name for name in ASKING if name not in asking)

	assert not gone, (
		f"{gone} no longer ask a principal for its token, so their entries in ASKING are "
		f"stale. Delete them."
	)


def test_every_reason_is_a_reason () -> None:
	"""Each entry cites the item or the section that would settle it."""

	for name, reason in ASKING.items():
		assert len(reason) > 60, f"ASKING[{name!r}] is too short to be a reason"
		assert "`#" in reason or "§" in reason, (
			f"ASKING[{name!r}] names neither an item nor a specification section"
		)


def test_the_scan_finds_a_synthetic_offender (tmp_path: pathlib.Path) -> None:
	"""Fed a defect through its own entry point, the scanner reports it.

	**Written from the real defect rather than from an invented one**: this is the exact
	shape `_refuse_amplification` carried, an early return granting the caller everything
	because the absence of a token was read as the absence of a credential.
	"""

	offender = tmp_path / "somewhere.py"
	offender.write_text(
		"def check (actor):\n\tif actor.token is None:\n\t\treturn\n", encoding="utf-8"
	)

	assert _asking(tmp_path) == {"somewhere.py": [2]}


def test_the_scan_leaves_a_different_token_alone (tmp_path: pathlib.Path) -> None:
	"""The other direction, and the reason this is scoped by name rather than by attribute.

	`clients/http.py` and `mcp/relay.py` both read `resolved.token`, which is a *credential
	resolved from configuration* and has nothing to do with a principal. A guard that caught
	those would be one people learn to add exclusions to, which is how an excuse list stops
	being read.
	"""

	innocent = tmp_path / "elsewhere.py"
	innocent.write_text(
		"def send (resolved, minted):\n"
		"\tif resolved.token is None:\n"
		"\t\treturn None\n"
		"\n"
		"\treturn minted.token\n",
		encoding="utf-8",
	)

	assert _asking(tmp_path) == {}


def test_the_scan_reads_the_real_tree () -> None:
	"""A floor, because a walk that reads nothing makes every check above pass vacuously.

	`#405`'s two-directional property gives most scanners this for free — a scan reading
	nothing fails the *stale* test — and it is asserted anyway, because "nothing was found"
	and "nothing was looked at" are the two states this file must never confuse.
	"""

	assert _asking(SOURCE), "the walk found no reads at all, so it is measuring nothing"


@pytest.mark.parametrize("name", sorted(ASKING))
def test_each_named_module_exists (name: str) -> None:
	"""An entry naming a module that has been renamed is an exemption nobody notices."""

	assert (SOURCE / name).is_file(), f"ASKING names {name!r}, which is not a module"
