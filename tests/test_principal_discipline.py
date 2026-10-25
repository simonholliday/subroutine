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
import typing

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"

#: The names a :class:`~subroutine.domain.authentication.Principal` conventionally goes by, kept as
#: a floor for one held without an annotation. **Not the whole of it** (`SR#5134`, L-11 of the cold
#: review of 2026-10-17): this said every principal in `src` was called one of these, and
#: ``reader``, ``presenter`` and ``found`` hold one too, while ``found`` elsewhere holds a
#: credential read from configuration. No list of names tells those apart, so the scan also takes
#: every name a function annotates as a principal - :func:`_annotated`.
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


#: Every module that asks whether a principal presented no credential at all, and why that is
#: not deciding what it may do (`SR#4567`, decision `#4514`). **Local mode is the named account
#: holding no credential, checked exactly as over HTTP**, so the question may describe a credential
#: and never grant anything: six sites read it as an exemption, and two of them let a
#: ``local_user`` who was only a viewer mint a superuser's credential (S8 of the cold review of
#: 2026-10-05). **Deleting an entry is what closes it**, and a new one is a decision.
LOCAL_ASKED: dict[str, str] = {
	"views.py": (
		"`views.credential` describes the credential presented, and none is the honest answer "
		"for somebody at a terminal - a description, which grants nothing. `#248`, decision "
		"`#4514`."
	),
}


def _names_a_principal (annotation: ast.expr | None) -> bool:
	"""Say whether an annotation is a principal: bare, qualified, quoted, or in a union.

	**What holds one, never what makes or contains one**: ``resolve`` in `api/inprocess.py` is a
	callable returning a principal, which has no ``.token`` to read, so it is not walked into.
	"""

	if isinstance(annotation, ast.Name):
		return annotation.id == "Principal"

	if isinstance(annotation, ast.Attribute):
		return annotation.attr == "Principal"

	if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
		return _names_a_principal(annotation.left) or _names_a_principal(annotation.right)

	if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
		try:
			quoted = ast.parse(annotation.value, mode="eval").body

		except SyntaxError:
			return False

		return _names_a_principal(quoted)

	return False


def _annotated (function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
	"""Return the names one function's own annotations say hold a principal - `SR#5134`."""

	arguments = function.args
	every = [
		*arguments.posonlyargs,
		*arguments.args,
		*arguments.kwonlyargs,
		*(one for one in (arguments.vararg, arguments.kwarg) if one is not None),
	]
	named = {one.arg for one in every if _names_a_principal(one.annotation)}

	for node in ast.walk(function):
		if (
			isinstance(node, ast.AnnAssign)
			and isinstance(node.target, ast.Name)
			and _names_a_principal(node.annotation)
		):
			named.add(node.target.id)

	return frozenset(named)


def _reads (
	node: ast.AST, attribute: str, names: frozenset[str]
) -> typing.TypeGuard[ast.Attribute]:
	"""Say whether this node reads ``attribute`` of a name in ``names``."""

	return (
		isinstance(node, ast.Attribute)
		and node.attr == attribute
		and isinstance(node.value, ast.Name)
		and node.value.id in names
	)


def _asking (root: pathlib.Path, attribute: str = "token") -> dict[str, list[int]]:
	"""Return every module that reads ``attribute`` of a principal, with the lines it does it on.

	Takes the tree as an argument so that a synthetic offender goes through this function
	rather than through a second copy of its rule — `#405`, whose lesson is that a guard
	tested against a re-implementation leaves the *scan* unchecked, which is the half that
	fails silently.
	"""

	found: dict[str, list[int]] = {}

	for path in sorted(root.rglob("*.py")):
		where = path.relative_to(root).as_posix()

		# **The definition's own reads are the answer for a token, and not for this** (`SR#4567`): two of
		# the six exemptions lived in the module that defines ``is_local``.
		if where == DEFINITION and attribute == "token":
			continue

		tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
		lines = {
			node.lineno for node in ast.walk(tree) if _reads(node, attribute, PRINCIPAL_NAMES)
		}

		# **And within each function, every name it annotates as a principal** (`SR#5134`), read
		# wherever that function's body reaches, a closure inside it included.
		for function in ast.walk(tree):
			if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
				named = _annotated(function)

				if named:
					lines |= {
						node.lineno for node in ast.walk(function) if _reads(node, attribute, named)
					}

		if lines:
			found[where] = sorted(lines)

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


def test_the_scan_finds_a_principal_by_its_annotation (tmp_path: pathlib.Path) -> None:
	"""`SR#5134`: a principal called neither name is found by what it is annotated as.

	``reader`` and ``presenter`` hold principals in `src`, and so does ``found`` in two places,
	where elsewhere it holds a credential read from configuration - so a name decides nothing. A
	quoted annotation in a union with ``None`` is found too, and neither an unannotated ``found``
	nor a callable that makes a principal is.
	"""

	offender = tmp_path / "somewhere.py"
	offender.write_text(
		"import subroutine.domain.authentication\n"
		"\n"
		"Principal = subroutine.domain.authentication.Principal\n"
		"\n"
		"def check (reader: Principal) -> None:\n"
		"\tif reader.token is None:\n"
		"\t\treturn\n"
		"\n"
		"def other (found: 'subroutine.domain.authentication.Principal | None') -> object:\n"
		"\treturn found.token\n"
		"\n"
		"def configured (found):\n"
		"\treturn found.token\n"
		"\n"
		"def made (resolve: 'typing.Callable[[], Principal]') -> object:\n"
		"\treturn resolve.token\n",
		encoding="utf-8",
	)

	assert _asking(tmp_path) == {"somewhere.py": [6, 10]}


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


def test_nothing_but_a_description_asks_whether_a_principal_is_local () -> None:
	"""`SR#4567`, decision `#4514`: nothing branches on the terminal to decide what it may do.

	Local mode is the configured account presenting no credential, so every check runs for it as
	for that account over HTTP. A read of ``is_local`` anywhere but a description is an exemption
	coming back.
	"""

	unexplained = sorted(set(_asking(SOURCE, "is_local")) - set(LOCAL_ASKED))

	assert not unexplained, (
		f"{unexplained} ask whether a principal presented no credential. Local mode is checked "
		f"as over HTTP (decision `#4514`), so decide by the account's permissions instead, or "
		f"add a reason to LOCAL_ASKED if this only describes the credential."
	)


def test_the_local_list_names_only_modules_that_still_ask () -> None:
	"""The stale direction, as for :data:`ASKING`: a reason whose site has gone is a fossil."""

	asking = _asking(SOURCE, "is_local")
	gone = sorted(name for name in LOCAL_ASKED if name not in asking)

	assert not gone, f"{gone} no longer ask whether a principal is local. Delete them."


def test_the_local_scan_reads_the_module_that_defines_it (tmp_path: pathlib.Path) -> None:
	"""Two of the six exemptions were in ``domain/authentication.py``, so the scan does not skip it."""

	defining = tmp_path / "domain"
	defining.mkdir()
	(defining / "authentication.py").write_text(
		"def issue (actor):\n\tif actor.is_local:\n\t\treturn\n", encoding="utf-8"
	)

	assert _asking(tmp_path, "is_local") == {"domain/authentication.py": [2]}


def test_the_local_scan_finds_the_exemption_it_was_written_for (tmp_path: pathlib.Path) -> None:
	"""Fed the shape the six exemptions had, through the real scanner, it reports it."""

	offender = tmp_path / "somewhere.py"
	offender.write_text(
		"def check (actor):\n\tif actor.is_local and not actor.user.is_service_account:\n"
		"\t\treturn\n",
		encoding="utf-8",
	)

	assert _asking(tmp_path, "is_local") == {"somewhere.py": [2]}
