"""Hand an agent's credential to the Claude Code sessions started in one directory - `#3286`.

Claude Code reads an ``env`` block from ``.claude/settings.local.json`` in the directory a session
starts in, and gives it to everything that session starts: the agent's shell and the
``subroutine`` plugin's server alike. So one entry naming a connection's variable makes both
halves of an agent act as that project's own account, while every other directory goes on as it
was (``docs/connecting.md``, *A different agent in each project*, and decision ``#337``).

**Under ``subroutine-remote`` it reaches the shell only** (`#3407`). That plugin starts no
process: the editor makes its requests itself, with the one token the plugin was given for every
project, so nothing in a project's settings is ever read on the way.

**Everything a person used to do by hand is done here, because each step was a place to go
wrong** (`#3247`, Simon's run on nuc14). The variable's name was copied from the page's example,
and a name that is not the connection's is ignored without a word. The file's shape, its
permissions and the rule that keeps it out of the repository were each left to the reader too.

**Settled before anything is minted.** :func:`prepare` refuses a settings file it could not write
back and a repository it could not keep the file out of, and :func:`ensure_ignored` adds the
missing rule, both while no credential exists. A credential minted and then stranded is a live
secret nobody can recover - and here the secret is printed only when the write has failed (Simon,
2026-09-23), so a refusal that came after minting would have nothing on screen to rescue.
"""

import contextlib
import copy
import dataclasses
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import typing

import subroutine.auth
import subroutine.errors

#: Where Claude Code keeps a checkout's own settings: the half meant for this machine alone.
SETTINGS = pathlib.Path(".claude") / "settings.local.json"

#: How long ``git`` gets to answer one question about one file.
GIT_TIMEOUT_SECONDS = 10.0

#: The variables that would point ``git`` at a repository other than the directory's own.
#:
#: **Removed rather than honoured**, because the question is about *this* directory. A
#: ``GIT_DIR`` left behind by a hook or a script would answer it about another checkout
#: entirely - the shape that once staged a test's file into this repository's own index.
_ELSEWHERE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

@dataclasses.dataclass(frozen=True)
class Handover:
	"""What giving a credential to one directory will do, settled before anything is minted."""

	#: The directory whose sessions will act as the agent.
	directory: pathlib.Path

	#: The settings file, which need not exist yet.
	settings: pathlib.Path

	#: What the file holds now, parsed: an empty dictionary when there is no file.
	held: dict[str, typing.Any]

	#: The repository's top level, or ``None`` when the directory is in no repository.
	repository: pathlib.Path | None

	#: The settings file's path from that top level, as git spells it, or ``None`` outside one.
	relative: str | None

	#: The line the repository's ``.gitignore`` still needs, or ``None`` when a rule of the
	#: repository's own already covers the file.
	missing_rule: str | None


@dataclasses.dataclass(frozen=True)
class Written:
	"""Where a credential went, and what whoever asked for it needs to hear about it."""

	path: pathlib.Path

	#: The prefix of a credential this one replaced, which goes on working until revoked.
	replaced: str | None

	#: Whether only the owner can read the file. **A report rather than a refusal**: a network
	#: drive may present every file as readable by everyone and ignore any attempt to change it,
	#: which is a fact about the drive that nothing here can put right.
	private: bool


def prepare (directory: pathlib.Path) -> Handover:
	"""Settle what handing a credential to ``directory`` will do, refusing what could not finish.

	Nothing here writes: :func:`ensure_ignored` and :func:`write` do, in that order.
	"""

	settings = directory / SETTINGS
	held = _held(settings)
	repository = _repository(directory)

	if repository is None:
		return Handover(
			directory=directory,
			settings=settings,
			held=held,
			repository=None,
			relative=None,
			missing_rule=None,
		)

	relative = _relative(settings, repository)

	# **A rule cannot take back a file the repository already has.** Git ignores only what it
	# does not track, so a settings file committed before this would carry the credential into
	# the next commit however `.gitignore` reads.
	if _tracked(repository, relative):
		raise subroutine.errors.ValidationError(
			f"{settings} is already in the repository, so ignoring it now would not keep a "
			"credential out of it.",
			hint=f"Take it out of the repository first - 'git rm --cached "
			f"{_typed_from(directory, settings)}' keeps the file - and run this again. Nothing was "
			"created.",
		)

	return Handover(
		directory=directory,
		settings=settings,
		held=held,
		repository=repository,
		relative=relative,
		missing_rule=None if _ignored_by(repository, relative) else _as_rule(relative),
	)


def ensure_ignored (handover: Handover) -> pathlib.Path | None:
	"""Add the rule the repository still needs, and return the ``.gitignore`` it went into.

	**Added rather than asked for** (Simon, 2026-09-23): the command has been told to set this
	directory up, and the credential's safety rests on this line more than on anything else it
	writes. **Appended, never rewritten**, because ``.gitignore`` is the person's file - and
	checked afterwards, because a later ``!`` rule, in that file or in one nearer the settings,
	would still un-ignore it.
	"""

	if handover.repository is None or handover.relative is None or handover.missing_rule is None:
		return None

	gitignore = handover.repository / ".gitignore"

	try:
		ending = gitignore.read_bytes()[-1:]

	except FileNotFoundError:
		ending = b""

	except OSError as error:
		raise _unwritable(gitignore, handover.missing_rule, error) from None

	# On a line of its own, and appended, so nothing already there is ever truncated (`#2433`).
	separator = b"" if ending in (b"", b"\n") else b"\n"

	try:
		with gitignore.open("ab") as handle:
			handle.write(separator + f"{handover.missing_rule}\n".encode())

	except OSError as error:
		raise _unwritable(gitignore, handover.missing_rule, error) from None

	# **Asked about the file, not the line** (`#3585`): the two differ wherever the path holds a
	# character a line reads as a pattern.
	if not _ignored_by(handover.repository, handover.relative):
		raise subroutine.errors.ValidationError(
			f"Added {handover.missing_rule} to {gitignore}, and git still does not ignore it: "
			"another rule un-ignores it.",
			hint=f"'git check-ignore -v {_typed_from(handover.directory, handover.settings)}' "
			"names that rule. Nothing was created.",
		)

	return gitignore


def write (handover: Handover, variable: str, token: str) -> Written:
	"""Put ``token`` into the directory's settings as ``variable``, and say what happened.

	**A new file and a rename, never a truncating write.** A session starting while this runs
	reads the old file or the new one and never half of either - and on the CIFS mount this
	project is developed on, a truncating write is the one that hangs (`#2433`). The new file is
	readable only by its owner before a byte is in it, which is ``credentials.store``'s rule for
	the same reason: writing and then tightening leaves a window.
	"""

	held = copy.deepcopy(handover.held)
	environment = dict(held.get("env", {}))
	before = environment.get(variable)
	environment[variable] = token
	held["env"] = environment

	# **Through a link, never over it** (`#3583`). :func:`prepare` asked git about the file a link
	# points at, since that is what a commit would carry; replacing the link with a file of its own
	# put the token at a path git had not been asked about - and, where the link was committed,
	# into the next `git commit -a`.
	destination = handover.settings.resolve()
	destination.parent.mkdir(parents=True, exist_ok=True)

	# **Staged in a directory git ignores whole** (`#3585`), which is how pytest keeps its cache
	# out of a repository. The file used to be staged beside the settings, under a name no rule was
	# asked about, so a process killed before the rename left the credential where `git add -A`
	# takes it. ``mkdtemp`` makes the directory its owner's alone before anything is in it.
	staging = pathlib.Path(tempfile.mkdtemp(dir=destination.parent, prefix=".settings.local."))
	staged = staging / destination.name

	try:
		with open(staging / ".gitignore", "x", encoding="utf-8") as handle:
			handle.write("*\n")

		descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

		# **On the disk before its name moves**, as ``config.write_private`` has it, so a crash
		# cannot leave the settings file's name on a file that is not all there.
		with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
			json.dump(held, handle, indent=2, ensure_ascii=False)
			handle.write("\n")
			handle.flush()
			os.fsync(handle.fileno())

		os.replace(staged, destination)

	finally:
		for leftover in (staged, staging / ".gitignore"):
			with contextlib.suppress(OSError):
				leftover.unlink()

		with contextlib.suppress(OSError):
			staging.rmdir()

	mode = stat.S_IMODE(destination.stat().st_mode)
	parsed = subroutine.auth.parse_token(before) if isinstance(before, str) else None

	return Written(
		path=handover.settings,
		replaced=parsed[0] if parsed is not None and before != token else None,
		private=not mode & (stat.S_IRWXG | stat.S_IRWXO),
	)


def _held (settings: pathlib.Path) -> dict[str, typing.Any]:
	"""Return what the settings file holds, refusing one that could not be written back whole.

	**Refused rather than replaced.** The file is Claude Code's and may carry permissions and
	hooks somebody chose, and rewriting one that did not parse would lose all of them to make
	room for one line.
	"""

	try:
		text = settings.read_text(encoding="utf-8")

	except FileNotFoundError:
		return {}

	# **Named, not raised past** (`#3585`): a file in another encoding escaped every refusal here
	# and ended the command with a traceback.
	except UnicodeDecodeError as error:
		raise subroutine.errors.ValidationError(
			f"{settings} is not UTF-8 text, from byte {error.start}.",
			hint="Save it as UTF-8, or move it aside, and run this again. Nothing was created.",
		) from None

	except OSError as error:
		raise subroutine.errors.ValidationError(
			f"{settings} could not be read: {error.strerror or error}.",
			hint="Check the file's ownership and permissions. Nothing was created.",
		) from None

	if not text.strip():
		return {}

	try:
		held = json.loads(text)

	except json.JSONDecodeError as error:
		raise subroutine.errors.ValidationError(
			f"{settings} is not valid JSON: {error.msg}, at line {error.lineno}, column "
			f"{error.colno}.",
			hint="Correct it, or move it aside, and run this again. Nothing was created.",
		) from None

	if not isinstance(held, dict):
		raise subroutine.errors.ValidationError(
			f"{settings} holds a JSON {type(held).__name__}, where Claude Code reads an object.",
			hint="Correct it, or move it aside, and run this again. Nothing was created.",
		)

	if not isinstance(held.get("env", {}), dict):
		raise subroutine.errors.ValidationError(
			f"The 'env' in {settings} is not an object, so a variable cannot be added to it.",
			hint="Correct it, or move it aside, and run this again. Nothing was created.",
		)

	# **And it has to go back out as it came in** (`#3585`). JSON can spell a string no UTF-8 file
	# can hold - ``"\ud800"``, half of one character - and finding that out while writing is finding
	# it out after the credential exists.
	try:
		json.dumps(held, ensure_ascii=False).encode("utf-8")

	except UnicodeEncodeError as error:
		raise subroutine.errors.ValidationError(
			f"{settings} holds text that could not be written back as UTF-8: {error.reason}.",
			hint="Correct it, or move it aside, and run this again. Nothing was created.",
		) from None

	return held


def _repository (directory: pathlib.Path) -> pathlib.Path | None:
	"""Return the top level of the repository ``directory`` is in, or ``None`` if it is in none.

	**No ``git`` is an answer only when there is no repository either.** Without the program
	nothing can ask what the repository ignores, and a credential written into a checkout that
	does not ignore it is one ``git add -A`` from being published.
	"""

	if shutil.which("git") is None:
		if any((place / ".git").exists() for place in (directory, *directory.parents)):
			raise subroutine.errors.ValidationError(
				f"{directory} is in a git repository, and git is not installed here to say "
				"whether the repository ignores the settings file.",
				hint="Install git, or put the credential in by hand as docs/connecting.md shows. "
				"Nothing was created.",
			)

		return None

	answered = _git(directory, "rev-parse", "--show-toplevel")

	if answered.returncode == 0:
		return pathlib.Path(answered.stdout.strip())

	# **Only "not a repository" means none.** Git refuses a repository owned by somebody else
	# with the same exit status, and that is a repository all the same - one whose rules this
	# could not read.
	if "not a git repository" in answered.stderr:
		return None

	raise _unanswered(directory, answered)


def _relative (settings: pathlib.Path, repository: pathlib.Path) -> str:
	"""Return the settings file's path from the repository's top level, as git spells it."""

	target = settings.resolve()

	try:
		return target.relative_to(repository.resolve()).as_posix()

	# **Only a link can lead outside**, since git found the directory inside (`#3585`). Refused
	# as *not inside*, it named a file that plainly was, and not the place the credential would go.
	except ValueError:
		raise subroutine.errors.ValidationError(
			f"{settings} leads to {target}, outside {repository}.",
			hint="A rule here cannot keep a file there out of any repository it is in. Point the "
			"link inside the checkout, or take it away, and run this again. Nothing was created.",
		) from None


#: What a line of ``.gitignore`` reads as a pattern rather than as itself: a backslash, and the
#: three characters that stand for others.
_PATTERN = re.compile(r"([\\*?\[])")


def _as_rule (relative: str) -> str:
	"""Return the ``.gitignore`` line that names ``relative`` and nothing else - `#3585`.

	**Escaped, because a line is a pattern.** Written as it stood, a directory called ``#notes``
	made the line a comment, ``[draft]`` a character class and ``!x`` a negation, and each was
	then refused as un-ignored by some *other* rule - with the line left in the file.
	"""

	# **A line cannot hold a line break**, and two lines would be two rules, one of them a
	# directory's name alone: anything else in the repository called that would be ignored too.
	if "\n" in relative or "\r" in relative:
		raise subroutine.errors.ValidationError(
			f"The path {relative!r} has a line break in it, which a line of .gitignore cannot hold.",
			hint="Run this from a directory whose path has none. Nothing was created.",
		)

	rule = _PATTERN.sub(r"\\\1", relative)

	# **Git drops the spaces a line ends in**, unless each is escaped - which a file a link leads
	# to may have in its name.
	kept = rule.rstrip(" ")
	rule = kept + "\\ " * (len(rule) - len(kept))

	# **Only at the start does ``#`` begin a comment, and ``!`` a negation.**
	return f"\\{rule}" if rule.startswith(("#", "!")) else rule


def _typed_from (directory: pathlib.Path, settings: pathlib.Path) -> str:
	"""Return the settings file's path as somebody in ``directory`` would type it - `#3585`.

	**From where they are, and escaped rather than quoted.** The hints gave the path from the
	repository's top level, which is another file to somebody in a directory beneath it, and
	printed it bare, so a shell read ``#notes/...`` as a comment. A hint already quotes the
	command it suggests, and a quoted path inside that would close the quotation.
	"""

	typed = os.path.relpath(settings.resolve(), directory.resolve())

	return re.sub(r"([^\w@%+=:,./-])", r"\\\1", typed)


def _tracked (repository: pathlib.Path, relative: str) -> bool:
	"""Report whether the repository already tracks the file at ``relative``."""

	answered = _git(repository, "ls-files", "--error-unmatch", "--", relative)

	if answered.returncode not in (0, 1):
		raise _unanswered(repository, answered)

	return answered.returncode == 0


def _ignored_by (repository: pathlib.Path, relative: str) -> bool:
	"""Report whether a rule inside the repository keeps ``relative`` out of it.

	**The exit status cannot say, measured.** A later ``!`` rule that un-ignores the file still
	exits 0 under ``-v``, printing the rule that decided, so the line itself is read. **And a rule
	outside the repository does not count**: ``.git/info/exclude`` protects one clone and a
	person's own ignore file one machine, and neither reaches the checkout a second machine opens
	- which is how Superconductor's check passed for the wrong reason (`#3246`).
	"""

	# **Asked with ``--stdin -z`` and read field by field** (`#3583`). Otherwise git quotes a path
	# holding anything but plain ASCII - ``"/home/you/caf\303\251/..."`` - and a quoted path is not
	# absolute, so a person's own ignore file under an accented home directory counted as the
	# repository's own. ``-z`` alone is refused: git takes it only with ``--stdin``.
	answered = _git(repository, "check-ignore", "-v", "-z", "--stdin", given=f"{relative}\0")

	if answered.returncode not in (0, 1):
		raise _unanswered(repository, answered)

	# The source, its line, the rule and the path, each ended by a NUL, or nothing at all.
	fields = answered.stdout.split("\0")

	if len(fields) < 4 or not fields[0]:
		return False

	source, rule = fields[0], fields[2]

	return not (
		rule.startswith("!")
		or pathlib.PurePath(source).is_absolute()
		or source.startswith(("~", ".git/"))
	)


def _git (
	directory: pathlib.Path, *arguments: str, given: str | None = None
) -> subprocess.CompletedProcess[str]:
	"""Ask ``git`` one question about ``directory``, in a language its answers can be read in.

	``given`` is written to its standard input, for a question asked with ``--stdin``.
	"""

	environment = {name: value for name, value in os.environ.items() if name not in _ELSEWHERE}
	environment["LC_ALL"] = "C"

	try:
		return subprocess.run(
			["git", *arguments],
			cwd=directory,
			env=environment,
			input=given,
			capture_output=True,
			text=True,
			timeout=GIT_TIMEOUT_SECONDS,
			check=False,
		)

	except (OSError, subprocess.TimeoutExpired) as error:
		raise subroutine.errors.ValidationError(
			f"git could not be asked about {directory}: {error}.",
			hint="Nothing was created.",
		) from None


def _unanswered (
	directory: pathlib.Path, answered: subprocess.CompletedProcess[str]
) -> subroutine.errors.ValidationError:
	"""Return the refusal for a question git declined to answer, in git's own words."""

	said = answered.stderr.strip().splitlines()

	return subroutine.errors.ValidationError(
		f"git could not say whether {directory} keeps the settings file out of its repository: "
		f"{said[0] if said else f'exit status {answered.returncode}'}.",
		hint="Nothing was created.",
	)


def _unwritable (
	gitignore: pathlib.Path, rule: str, error: OSError
) -> subroutine.errors.ValidationError:
	"""Return the refusal for a ``.gitignore`` that could not take its line."""

	return subroutine.errors.ValidationError(
		f"{gitignore} could not be written: {error.strerror or error}.",
		hint=f"Add '{rule}' to it by hand, and run this again. Nothing was created.",
	)
