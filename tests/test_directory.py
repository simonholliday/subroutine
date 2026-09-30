"""What a checkout belongs to — docs/design.md §13.7a, item ``#159``.

The question an agent could not answer, and the reason "it just works" was not yet true: on an
instance with one project it is free, and §21.5's adoption procedure *produces* instances with
many.
"""

import pathlib
import typing
import uuid

import pytest

import subroutine.context
import subroutine.directory


def _write (directory: pathlib.Path, body: str) -> pathlib.Path:
	"""Put a marker in ``directory`` verbatim, including a broken one."""

	directory.mkdir(parents=True, exist_ok=True)
	path = directory / subroutine.directory.FILE_NAME
	path.write_text(body, encoding="utf-8")

	return path


def test_a_marker_is_found_from_a_subdirectory (tmp_path: pathlib.Path) -> None:
	"""Walking up is the whole mechanism, and it is the one git uses for the same reason.

	People run commands from wherever they happen to be; an agent is started from wherever
	its client chose. A marker that only worked in the directory holding it would be a
	setting that works when you do not need it.
	"""

	_write(tmp_path, 'project = "web"\n')
	deep = tmp_path / "src" / "components" / "nav"
	deep.mkdir(parents=True)

	found = subroutine.directory.find(deep)

	assert found is not None
	assert found.project == "web"


def test_the_nearest_marker_wins_and_the_walk_stops (tmp_path: pathlib.Path) -> None:
	"""A repository inside another is rare and deliberate when it happens.

	Merging the two would produce a context neither file states, which is worse than the one
	the closer file asked for.
	"""

	_write(tmp_path, 'project = "outer"\nworkspace = "si"\n')
	inner = tmp_path / "vendor" / "thing"
	_write(inner, 'project = "inner"\n')

	found = subroutine.directory.find(inner)

	assert found is not None
	assert found.project == "inner"
	assert found.workspace is None, "the outer file's keys must not leak into the inner one"


def test_no_marker_anywhere_is_not_an_error (tmp_path: pathlib.Path) -> None:
	"""Most directories are not a checkout of anything, and that is the ordinary case."""

	assert subroutine.directory.find(tmp_path) is None


def test_a_marker_that_cannot_be_read_is_treated_as_absent (tmp_path: pathlib.Path) -> None:
	"""The same rule `context.read` argues for, and for the same reason.

	Losing this file is meant to cost a question, so refusing to run because of a stray
	character in one would be a worse outcome than the one it protects against — and it would
	arrive as a broken `subroutine add` rather than as anything a reader could connect to a
	file they may not know exists.
	"""

	_write(tmp_path, "project = not quoted\n")

	assert subroutine.directory.find(tmp_path) is None


def test_a_directory_this_account_cannot_look_inside_holds_no_marker (
	tmp_path: pathlib.Path,
) -> None:
	"""A real denial on a real directory, which is how `#1255` was met.

	The service account was asked for a token from a shell sitting in the operator's own home
	directory. It could not stat inside it, ``is_file`` raised, and a command that issues a
	credential answered with a traceback.

	Skipped rather than faked for a process that cannot be refused anything, since the
	condition genuinely does not exist for one — and the sibling test below covers the same
	code on every machine.
	"""

	closed = tmp_path / "closed"
	_write(closed, 'project = "web"\n')
	closed.chmod(0o000)

	try:
		try:
			(closed / subroutine.directory.FILE_NAME).is_file()

		except PermissionError:
			pass

		else:
			pytest.skip("this process can read a directory with no permissions on it")

		assert subroutine.directory.find(closed) is None

	finally:
		closed.chmod(0o700)


def test_a_marker_that_is_not_utf_8_is_read_as_none (tmp_path: pathlib.Path) -> None:
	"""`SR#3942`, L-10 of the cold review of 2026-09-28: reading one raised, and nothing caught it.

	A marker that cannot be read is absent, by this module's own rule, and one that is not UTF-8
	raised as neither of the two failures caught, so ``subroutine mcp`` died at its first message
	and the terminal at every command. **Read as no marker.**
	"""

	marked = tmp_path / "marked"
	marked.mkdir()
	(marked / subroutine.directory.FILE_NAME).write_bytes(b'project = "caf\xe9"\n')

	assert subroutine.directory.find(marked) is None


def test_a_working_directory_that_has_gone_holds_no_marker (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#3942`: ``Path.cwd`` raises once the directory it names has been deleted.

	The terminal, the agent tools and the relay each looked for a marker from there first, and
	died. **No marker**, as a directory nobody can look inside holds none.
	"""

	gone = tmp_path / "gone"
	gone.mkdir()
	monkeypatch.chdir(gone)
	gone.rmdir()

	assert subroutine.directory.find() is None


def test_an_unreadable_directory_does_not_stop_the_walk (
	monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
	"""The marker above an unreadable directory is still found.

	Continuing rather than giving up is the half a denial at the innermost directory cannot
	show, since there is nothing above it to find. Patched at the call that raised, so the
	real walk runs — and it runs everywhere, including as a process nothing can refuse.
	"""

	_write(tmp_path, 'project = "web"\n')

	deep = tmp_path / "closed"
	deep.mkdir()

	refused = deep / subroutine.directory.FILE_NAME
	real = pathlib.Path.is_file

	def denied (self: pathlib.Path) -> bool:
		"""Refuse the one candidate, and answer for every other path as usual."""

		if self == refused:
			raise PermissionError(13, "Permission denied", str(self))

		return real(self)

	monkeypatch.setattr(pathlib.Path, "is_file", denied)

	found = subroutine.directory.find(deep)

	assert found is not None
	assert found.project == "web"


def test_a_marker_holding_nothing_useful_is_absent (tmp_path: pathlib.Path) -> None:
	"""An empty file, or one holding only keys this does not read, says nothing."""

	_write(tmp_path, '# just a comment\nunrelated = "value"\n')

	assert subroutine.directory.find(tmp_path) is None


def test_what_is_written_is_what_is_read_back (tmp_path: pathlib.Path) -> None:
	"""The round trip, so the writer and the parser cannot drift apart."""

	subroutine.directory.write(
		tmp_path, connection="work", workspace="acme", project="web"
	)

	found = subroutine.directory.find(tmp_path)

	assert found is not None
	assert (found.connection, found.workspace, found.project) == ("work", "acme", "web")


def test_a_written_marker_explains_itself (tmp_path: pathlib.Path) -> None:
	"""Unlike everything else this program writes, somebody who did not run the command reads it.

	It lands in a repository, so the next person to meet it is doing a code review — and a
	file of bare keys with no statement of what deleting it costs is one they will either
	leave alone forever or remove without knowing.
	"""

	path = subroutine.directory.write(tmp_path, project="web")
	written = path.read_text(encoding="utf-8")

	assert "Safe to delete" in written
	assert "subroutine use --here" in written


@pytest.mark.parametrize("key", ["connection", "workspace", "project"])
def test_only_the_keys_it_declares_are_read (tmp_path: pathlib.Path, key: str) -> None:
	"""Each key on its own, so a partial marker is a partial answer rather than a refusal."""

	_write(tmp_path, f'{key} = "value"\n')

	found = subroutine.directory.find(tmp_path)

	assert found is not None
	assert getattr(found, key) == "value"


def test_a_marker_beats_the_stored_context_and_loses_to_the_environment (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""§13.7a's ordering, which is the only one that works.

	A marker describes *this checkout*, so it must beat a machine-global `subroutine use` —
	two repositories open at once is the case the whole thing exists for. A flag or an
	exported variable is somebody saying something *now*, and must beat a file three
	directories up that they may have forgotten is there.
	"""

	marker = subroutine.directory.Marker(path=tmp_path, workspace="fromfile")

	assert subroutine.context._first(
		(None, subroutine.context.FROM_FLAG),
		(marker.workspace, subroutine.context.FROM_DIRECTORY),
		("fromstored", subroutine.context.FROM_STORED),
	) == ("fromfile", subroutine.context.FROM_DIRECTORY)

	assert subroutine.context._first(
		("fromflag", subroutine.context.FROM_FLAG),
		(marker.workspace, subroutine.context.FROM_DIRECTORY),
	) == ("fromflag", subroutine.context.FROM_FLAG)


def test_a_marker_records_the_project_id_beside_the_key (tmp_path: pathlib.Path) -> None:
	"""`#177`. The id is what survives a rename; the key is what a person can recognise.

	This file's own docstring argued for a key *because* §5.2 forbade renaming one. `#176`
	removed that clause, so the argument's middle third is gone and the other two survive —
	which is why both are written rather than one replacing the other.
	"""

	subroutine.directory.write(
		tmp_path, project="SR", project_id="0f9c1234-0000-0000-0000-000000000000"
	)

	found = subroutine.directory.find(tmp_path)

	assert found is not None
	assert found.project_id == "0f9c1234-0000-0000-0000-000000000000"

	# **Read back exactly as written, and `#508` did not change that.** A marker is a file
	# somebody may have hand-edited; normalising it on the way in would make what the file says
	# and what this reports two different things. It resolves either way, because every lookup
	# normalises what it is given — which is why markers written when keys were upper-cased go
	# on working after the change.
	assert found.project == "SR"

	# And it still reads as something rather than as a pair of opaque values.
	assert "# SR" in (tmp_path / subroutine.directory.FILE_NAME).read_text(encoding="utf-8")


def test_a_marker_written_before_ids_existed_still_works (tmp_path: pathlib.Path) -> None:
	"""Every marker on disk today names a key and no id — including this repository's own.

	An upgrade that made those stop working would be the outage, not the fix.
	"""

	_write(tmp_path, 'project = "SR"\n')

	found = subroutine.directory.find(tmp_path)

	assert found is not None
	assert found.project == "SR"
	assert found.project_id is None


class _Row(typing.NamedTuple):
	"""The fields `resolve` reads, standing in for a project as a client reports it.

	``parent_id`` arrived with `#957`, which made a key unique among its siblings rather than
	in its workspace — so what a marker resolves to is the whole address, and composing one
	means walking up.
	"""

	id: uuid.UUID
	key: str
	parent_id: uuid.UUID | None = None


def test_a_marker_follows_a_renamed_project_by_id (tmp_path: pathlib.Path) -> None:
	"""`#177`, and `#232` is why it is asserted here rather than only in the CLI.

	The id is the half that survives a rename, so a marker written before one must go on
	naming the same project under its new key. `subroutine_add` never did this at all — it
	passed the marker's key to the server unresolved — and the CLI's copy of the matching was
	the only one, which is what moved it into `directory`.
	"""

	moved = uuid.uuid4()
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="OLD", project_id=str(moved)
	)

	assert subroutine.directory.resolve(marker, [_Row(moved, "NEW")]) == "NEW"


def test_a_marker_written_before_ids_still_resolves_by_key (tmp_path: pathlib.Path) -> None:
	"""Every marker written before `#177` carries a key and no id, including this repository's.

	Case-insensitively, because a key is stored uppercase and a person editing this file by
	hand will not always type it that way.
	"""

	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="web"
	)

	assert subroutine.directory.resolve(marker, [_Row(uuid.uuid4(), "web")]) == "web"


def test_a_marker_speaks_only_for_the_connection_it_names (tmp_path: pathlib.Path) -> None:
	"""Item ``#414``. A marker names one instance, and its project is true only there.

	The defect this closes needed two things that are both ordinary: a marker whose connection
	is gone or overridden, and a project key two instances share — ``SR``, ``WEB``, ``API``,
	``DOCS`` are exactly the kind. ``resolve``'s match-by-key fallback then filed work into a
	*different* instance's project of the same name, under a warning saying the connection had
	been ignored.

	A marker naming no connection speaks for whichever one answers, which is every marker
	written before §13.7 and is what keeps them working.
	"""

	named = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, connection="work", project="SR"
	)

	assert named.speaks_for("work")
	assert not named.speaks_for("local")

	# Case-insensitively, because `Roster.find` matches that way — otherwise one connection
	# spelled two ways would be two connections here and one everywhere else.
	assert named.speaks_for("work")

	silent = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="SR"
	)

	assert silent.speaks_for("anything at all")


def test_a_marker_naming_nothing_here_resolves_to_nothing (tmp_path: pathlib.Path) -> None:
	"""`#166`: ``None`` is an answer, and the caller's job is to carry on having heard it.

	Both halves stale — a key that is not here and an id that is not either — because a
	marker for somebody else's instance is exactly the case this has to survive, and it is
	the case committing the file into a shared repository produces.
	"""

	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME,
		project="elsewhere",
		project_id=str(uuid.uuid4()),
	)

	assert subroutine.directory.resolve(marker, [_Row(uuid.uuid4(), "web")]) is None


class _Space(typing.NamedTuple):
	"""The two fields `resolve_workspace` reads, standing in for a workspace."""

	id: uuid.UUID
	slug: str


def test_a_marker_follows_a_renamed_workspace_by_id (tmp_path: pathlib.Path) -> None:
	"""`#317`: the same durability as a project key, one level up.

	A workspace could not be renamed when this file was designed, so its slug was durable by
	construction and recording only the name was correct. `#295` made renaming possible and
	did not carry `#177`'s answer across — so every marked checkout printed "names workspace
	'x', which is not on local" on every command afterwards, about nothing, since `project_id`
	went on filing the work in the right place.
	"""

	moved = uuid.uuid4()
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME,
		workspace="personal",
		workspace_id=str(moved),
	)

	assert subroutine.directory.resolve_workspace(marker, [_Space(moved, "projects")]) == (
		"projects"
	)


def test_a_marker_written_before_workspace_ids_still_resolves_by_slug (
	tmp_path: pathlib.Path,
) -> None:
	"""Every marker written before `#317` carries a slug and no id, including this repository's.

	In whatever case it was written, since `SR#3893`, as the program reads a workspace everywhere
	else: a marker naming ``Projects`` was ignored where the workspace is stored as ``projects``.
	"""

	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, workspace="si"
	)

	assert subroutine.directory.resolve_workspace(marker, [_Space(uuid.uuid4(), "si")]) == "si"


def test_a_marker_naming_a_workspace_that_is_not_here_resolves_to_nothing (
	tmp_path: pathlib.Path,
) -> None:
	"""`#166` again: the warning and the carrying-on are what a stale marker must produce."""

	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME,
		workspace="elsewhere",
		workspace_id=str(uuid.uuid4()),
	)

	assert subroutine.directory.resolve_workspace(marker, [_Space(uuid.uuid4(), "si")]) is None


def test_a_marker_resolves_to_a_whole_address (tmp_path: pathlib.Path) -> None:
	"""Decision `#957`: a key stopped being unique, so a key is no longer an answer.

	The marker records an id, which is what survives a rename — and then hands back a *name*
	for the caller to send. A bare key handed back may name a different project by the time it
	is sent, silently, into a listing nobody is watching. That is `#414`'s failure and the fix
	is the same one: say the thing that can only mean one project.
	"""

	parent = uuid.uuid4()
	child = uuid.uuid4()
	rows = [_Row(parent, "substation"), _Row(child, "dist", parent)]
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="dist", project_id=str(child)
	)

	assert subroutine.directory.resolve(marker, rows) == "substation/dist"


def test_a_marker_written_before_addresses_still_resolves (tmp_path: pathlib.Path) -> None:
	"""Every marker in every checkout holds a bare key, including this repository's.

	It resolves by id, so what it holds in ``project`` matters only for the fallback and for
	the sentence saying the file is out of date — which is a suggestion to run ``use --here``,
	not a refusal.
	"""

	parent = uuid.uuid4()
	child = uuid.uuid4()
	rows = [_Row(parent, "substation"), _Row(child, "dist", parent)]
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="dist"
	)

	assert subroutine.directory.resolve(marker, rows) == "substation/dist"


def test_a_marker_holding_an_address_resolves_by_it (tmp_path: pathlib.Path) -> None:
	"""Which is what ``use --here --project`` writes now, and the half with no id to fall on.

	Both spellings have to match, or a marker whose project was deleted and remade — a new id,
	the same address — would stop resolving for a reason nobody could see.
	"""

	parent = uuid.uuid4()
	child = uuid.uuid4()
	rows = [_Row(parent, "substation"), _Row(child, "dist", parent)]
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="substation/dist"
	)

	assert subroutine.directory.resolve(marker, rows) == "substation/dist"


def test_a_bare_key_names_the_one_project_that_has_it_or_none (tmp_path: pathlib.Path) -> None:
	"""`SR#3894`, M-6 of the cold review of 2026-09-28: the first row carrying the key won.

	With ``alpha``, ``alpha/web`` and a root ``web``, a marker's bare ``web`` meant whichever was
	made last, and with only ``alpha/web`` and ``beta/web`` one of them, in silence. **The whole
	address first, over every row, then a bare key only where one project has it**; a key several
	share names nothing, and :func:`subroutine.directory.ambiguous` says which they are.
	"""

	alpha, beta = uuid.uuid4(), uuid.uuid4()
	root = _Row(uuid.uuid4(), "web")
	nested = [_Row(alpha, "alpha"), _Row(uuid.uuid4(), "web", alpha)]
	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, project="web"
	)

	for rows in ([*nested, root], [root, *nested]):
		assert subroutine.directory.resolve(marker, rows) == "web", rows

	shared = [
		_Row(alpha, "alpha"),
		_Row(uuid.uuid4(), "web", alpha),
		_Row(beta, "beta"),
		_Row(uuid.uuid4(), "web", beta),
	]

	assert subroutine.directory.resolve(marker, shared) is None
	assert subroutine.directory.ambiguous(marker, shared) == ["alpha/web", "beta/web"]
	assert subroutine.directory.ambiguous(marker, nested) == []


def test_a_marker_names_a_workspace_in_whatever_case_it_was_written (tmp_path: pathlib.Path) -> None:
	"""`SR#3893`, M-5 (a): ``workspace = "Projects"`` named nothing, and the tools ignored it."""

	marker = subroutine.directory.Marker(
		path=tmp_path / subroutine.directory.FILE_NAME, workspace="Projects"
	)

	assert subroutine.directory.resolve_workspace(
		marker, [_Space(uuid.uuid4(), "projects")]
	) == "projects"


@pytest.mark.parametrize(
	("written", "stored", "found"),
	[("My Team", "my-team", True), ("Maße", "masse", False), ("MAßE", "maße", True)],
)
def test_a_marker_names_a_workspace_as_its_name_is_stored (
	tmp_path: pathlib.Path, written: str, stored: str, found: bool
) -> None:
	"""`SR#4022`, L-5 (5) of the cold review of 2026-09-30: ``casefold`` is not the stored rule.

	*My Team* missed ``my-team``, and *Maße* matched ``masse``, a workspace of its own. **Compared
	by the rule that stores a workspace's name.**
	"""

	marker = subroutine.directory.Marker(path=tmp_path / subroutine.directory.FILE_NAME, workspace=written)
	answered = subroutine.directory.resolve_workspace(marker, [_Space(uuid.uuid4(), stored)])

	assert answered == (stored if found else None), answered


def test_composing_an_address_terminates_when_a_parent_is_absent (
	tmp_path: pathlib.Path,
) -> None:
	"""**A partial address is a knowingly poor answer, and the alternative is worse.**

	Composing from ``parent_id`` needs every ancestor in the rows it was handed, and what it
	returns when one is missing is a *suffix* — which may resolve, and may resolve somewhere
	else. That is stated rather than defended: no supported path produces it, because the
	callers pass a whole workspace's projects and the same code already assumes that listing
	is complete (`_subtree` walks it in one forward pass). The alternative here is raising
	inside a routine that runs while somebody is capturing a task.

	What removes it is `#512`, where the server publishes the address and no client composes
	one. Asserted so that a walk which looped or raised on the same input would fail.
	"""

	missing = uuid.uuid4()
	child = uuid.uuid4()
	rows = [_Row(child, "dist", missing)]

	assert subroutine.directory.address(rows[0], rows) == "dist"


def test_a_marker_travels_in_a_header_without_its_path_or_its_connection () -> None:
	"""`SR#1438`: what the relay sends is the marker's ids and keys, and nothing of the machine.

	The path is the caller's filesystem, and the connection is the caller's private name for the
	instance - which the relay has already checked the marker speaks for - so neither travels.
	"""

	marker = subroutine.directory.Marker(
		path=pathlib.Path("/home/you/web/.subroutine"),
		connection="my-own-alias",
		workspace="acme",
		workspace_id="01a0e2ce-0000-7000-8000-000000000001",
		project="web",
		project_id="01a0e2ce-0000-7000-8000-000000000002",
	)

	said = subroutine.directory.as_header(marker)

	assert said is not None
	assert "/home/you" not in said and "my-own-alias" not in said

	back = subroutine.directory.from_header(said)

	assert back is not None
	assert (back.workspace, back.workspace_id, back.project, back.project_id) == (
		"acme", marker.workspace_id, "web", marker.project_id
	)
	assert back.connection is None, "a marker the relay sent speaks for whoever it reached"


@pytest.mark.parametrize(
	"said", [None, "", "project", "nonsense=1; =2", "path=/etc; connection=x"]
)
def test_a_header_carrying_nothing_this_reads_is_no_checkout (said: str | None) -> None:
	"""Anything that carries none of a marker's keys is read as nothing said, never refused.

	**A workspace alone is something said** since `SR#4007`: it is read back as a marker by
	``test_a_marker_naming_only_a_workspace_is_sent_and_read_back``.
	"""

	assert subroutine.directory.from_header(said) is None


@pytest.mark.parametrize(
	("workspace", "project"),
	[
		("büro", "web"),
		("acme", "web; project=elsewhere"),
		("acme", "web\r\nX-Injected: yes"),
		("acme", "100% done"),
	],
	ids=["a workspace named outside ASCII", "a separator in a value", "a line break", "a percent sign"],
)
def test_a_header_carries_any_marker_on_one_ascii_line (workspace: str, project: str) -> None:
	"""`#3746`: a header is ASCII on one line, and a marker's values need not be.

	httpx refused a letter outside ASCII before sending anything, so the relay crashed on its
	first message in a checkout marked for `büro`; a line break was refused at the socket, with
	advice about the token; and a `;` or an `=` in a value read as the next pair.
	"""

	marker = subroutine.directory.Marker(
		path=pathlib.Path(".subroutine"), workspace=workspace, project=project
	)

	said = subroutine.directory.as_header(marker)

	assert said is not None
	assert said.isascii() and said.isprintable(), said

	back = subroutine.directory.from_header(said)

	assert back is not None
	assert (back.workspace, back.project) == (workspace, project)


def test_a_header_leaves_ids_keys_and_addresses_as_they_are () -> None:
	"""`#3746`: encoding changes nothing an instance built before it reads.

	The instance reads a header's ids and its project, and each is ASCII with no `;`, `=` or `%`
	in it - a key by its pattern, and a whole address with its `/` left readable - so a header
	from this relay means the same to an instance whose reader does not decode.
	"""

	marker = subroutine.directory.Marker(
		path=pathlib.Path(".subroutine"),
		workspace_id="01a0e2ce-0000-7000-8000-000000000001",
		project="substation/dist",
		project_id="01a0e2ce-0000-7000-8000-000000000002",
	)

	assert subroutine.directory.as_header(marker) == (
		"workspace_id=01a0e2ce-0000-7000-8000-000000000001; "
		"project_id=01a0e2ce-0000-7000-8000-000000000002; project=substation/dist"
	)


def test_a_marker_naming_only_a_workspace_is_sent_and_read_back () -> None:
	"""`SR#4007`, M-11 (c) of the cold review of 2026-09-30: this said a workspace said nothing.

	``use team --here`` writes a marker naming a workspace and no project; the terminal files into
	``team`` by it, and the agent's tools were sent nothing and asked which workspace. **It is
	sent, and read back as the same marker**; a marker naming nothing at all still sends nothing.
	"""

	marker = subroutine.directory.Marker(path=pathlib.Path(".subroutine"), workspace="acme")
	sent = subroutine.directory.as_header(marker)
	back = subroutine.directory.from_header(sent)

	assert sent is not None
	assert back is not None and back.workspace == "acme" and back.project is None, back
	assert subroutine.directory.as_header(subroutine.directory.Marker(path=pathlib.Path(".subroutine"))) is None
	assert subroutine.directory.from_header("") is None
