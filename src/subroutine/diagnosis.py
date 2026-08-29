"""Whether this machine's installation is coherent — item ``#407``.

**A runbook is untested code.** ``#391`` is four pages ordered around *what you check after
each step*, which is the right shape and is still a document — and a document is not a check.
Every one of those checks is a thing a program can do, and doing them in one act removes the
part that actually goes wrong: four of the five need environment variables, and getting them
wrong produces a confident true answer about the wrong database.

**Three rules, and the first is the one that decides the design.**

*It must work when things are broken.* §12.4's property, and the same rule
:mod:`subroutine.installations` follows: a diagnostic that fails on the machine it is
diagnosing is worse than none. So nothing here raises. Every check returns a :class:`Finding`,
including a check that could not be made, and one failing never stops the rest — reading a
report that stops at the first problem is how you fix one thing and meet the next tomorrow.

*Nothing reaches the network except a connection somebody configured.* §12.4a. Asking whether
a release exists is ``subroutine db upgrade --check`` and stays there; a health command that
reached PyPI would go red when PyPI did, which is a report about somebody else's morning.

*What is not knowable is not guessed.* The plugin's version is knowable only when a plugin
started this process, and whether an MCP server came up is a question only the editor can
answer (`#236`). Both are reported when they can be and silent when they cannot, which is
§13.1's rule: publishing less beats publishing something confident and wrong.
"""

import dataclasses
import pathlib
import sys
import typing

import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.db.backup
import subroutine.db.migrate
import subroutine.db.types
import subroutine.errors
import subroutine.installations


@dataclasses.dataclass(frozen=True)
class Finding:
	"""One thing that was looked at, and what was found."""

	#: What was examined, as a short label a reader scans down the left of the output.
	area: str

	#: What was found, in the reader's terms.
	detail: str

	#: False when something is wrong *here*, on this machine, and somebody should act.
	#: **Not** false merely because a question could not be answered — see :attr:`unknown`,
	#: which is a different thing and must not turn an update script red.
	ok: bool = True

	#: True when the check could not be made at all. Reported, never counted as a failure:
	#: "no plugin started this process" is the ordinary state of a command line.
	unknown: bool = False


def examine (settings: subroutine.config.Settings | None = None) -> list[Finding]:
	"""Look at everything this machine can be asked about, and report all of it.

	Ordered as somebody reads it: what is running, where it is reading its configuration
	from, what it can reach, and what it has kept. The middle one is deliberately near the
	top — *the* lesson of 2026-08-03 is that a command run without the service's environment
	acts on a different database and does not look like it, so which roots are in force is
	the line that makes every line below it mean something.
	"""

	resolved = settings or subroutine.config.load_settings()

	return [
		*_the_program(),
		*_the_directories(),
		*_the_signing_key(resolved),
		*_the_settings(resolved),
		*_the_connections(resolved),
		*_the_backups(resolved),
	]


def _the_program () -> list[Finding]:
	"""Report which copy of this software is running, and which plugin started it."""

	plugin = subroutine.installations.plugin()
	found = [
		Finding(
			area="program",
			detail=f"{subroutine.installations.program()}, at {_where_the_program_is()}",
		)
	]

	# **Reported only when a plugin started this process**, which a command line never is.
	# Saying "no plugin" every time would put a line about a concept most readers have not
	# got at the top of the one command they run when something is already wrong.
	if plugin is not None:
		found.append(Finding(area="plugin", detail=plugin))

	return found


def _where_the_program_is () -> str:
	"""Return the path this program was launched from, or say it is not knowable.

	Worth printing beside the version because they answer different halves of the same
	confusion: two installs on one machine report two numbers, and the path is what says
	which one just answered.
	"""

	return sys.argv[0] if sys.argv and sys.argv[0] else str(pathlib.Path(sys.executable))


def _the_directories () -> list[Finding]:
	"""Report the XDG roots in force, which decide which database everything else means.

	**The single most valuable line here.** `#376` was a server running against a database
	nobody meant; `#395` was a backup of an empty one. Both were a command run without the
	service's environment, and in both the output looked exactly like success.
	"""

	return [
		Finding(area="config", detail=str(subroutine.config.config_home())),
		Finding(area="data", detail=str(subroutine.config.data_home())),
		Finding(area="state", detail=str(subroutine.config.state_home())),
	]


def _the_signing_key (settings: subroutine.config.Settings) -> list[Finding]:
	"""Report whether this installation has the key that signs a listing's page cursor.

	**The cheapest possible incoherence to detect, and nothing was detecting it** (`#1254`).
	``init`` is the only thing that writes ``secret_key``, so an instance whose database
	arrived any other way — copied, restored, promoted from a personal install — has none, and
	the first listing longer than a page raises where nobody can connect it to the cause. This
	command's whole claim is whether this machine's installation is coherent, and a key that is
	simply absent is inside that claim.

	The key itself is never printed. Whether there is one is the question; what it is belongs
	in the file it was written to.
	"""

	if settings.secret_key:
		return [Finding(area="signing key", detail="set")]

	if settings.dev_mode:
		return [
			Finding(
				area="signing key",
				detail="none, and dev_mode is on, so one is made up per process",
			)
		]

	return [
		Finding(
			area="signing key",
			detail=(
				"none — listings longer than a page will fail, because the cursor that "
				"carries them is signed with it"
			),
			ok=False,
		)
	]


def _the_settings (settings: subroutine.config.Settings) -> list[Finding]:
	"""Report a setting whose value is legal and whose meaning is exposure — `SR#1558`.

	**This command validated nothing.** Its stated job is to *"say whether this machine's
	installation is coherent"*, and it reported facts: paths, versions, whether a key exists,
	how many backups there are. Six freshly-initialised instances, one configuration difference
	each — an open CORS list on a public instance, rate limiting off on a public instance, a
	page size of zero, a negative timeout, a depth setting that did nothing — and the closing
	line on all six, including the healthy one, was *"Nothing here needs attention."*

	**Four of those six can no longer be set at all** (`SR#1559`), so what is left here is the
	pair that are genuinely a judgement rather than a bad number: legal values whose meaning is
	who can reach this instance. That is the right division — a number nobody could mean is
	refused when the file loads, and a decision an operator might have made on purpose is
	reported rather than overruled.

	**The lesson is one this codebase already recorded one command along.** ``db/backup.py``'s
	``check_engine`` (`#172`): *"docs/hosting.md already stated the rule, which is the shape
	worth noticing: the document knew and the program did not."* Here the *code* knows —
	``config.py`` measures and documents the ``["*"]`` danger in full, and ``docs/hosting.md``
	gives the operator a curl one-liner to check it — and the program said nothing.

	**Only when ``public_url`` is set**, because that is what says this instance is reachable
	by somebody other than the person at the keyboard. It is the same signal `#286` and `#832`
	use to decide whether rate limiting applies at all, so a laptop meets none of this.
	"""

	if not settings.public_url:
		return [Finding(area="exposure", detail="not published, so nothing is reachable from outside")]

	findings = []

	if "*" in settings.cors_origins:
		findings.append(
			Finding(
				area="cors_origins",
				detail=(
					"'*' on a published instance — any page on any site can read and write "
					"as anybody who is signed in and visits it. Name the origins that need "
					"it, or leave the list empty"
				),
				ok=False,
			)
		)
	else:
		findings.append(
			Finding(
				area="cors_origins",
				detail=(
					"empty, so only this instance's own pages may call it"
					if not settings.cors_origins
					else f"{len(settings.cors_origins)} named"
				),
			)
		)

	# **Reported either way, like the list above it.** An operator reads this to learn what is
	# in force, and a check that speaks only when it is unhappy leaves them unable to tell
	# *limiting is on* from *nothing looked*. What is stated is what is **in force** rather than
	# what the file says: unset means on here, because `#286` decides it from `public_url`
	# first and this whole function has already established that it is set.
	if settings.rate_limit is False:
		findings.append(
			Finding(
				area="rate_limit",
				detail=(
					"off on a published instance, so nothing slows down somebody guessing "
					"credentials"
				),
				ok=False,
			)
		)
	else:
		findings.append(
			Finding(
				area="rate_limit",
				detail="on" if settings.rate_limit else "on, because this instance is published",
			)
		)

	return findings


def _the_connections (settings: subroutine.config.Settings) -> list[Finding]:
	"""Ask every configured connection who it is and what it is running.

	``/readyz`` and ``whoami`` at once, per connection — `#391`'s step-1 check and its "one
	check that covers everything", which were two commands and an environment.
	"""

	try:
		roster = subroutine.connections.roster(settings)

	except subroutine.errors.SubroutineError as broken:
		return [Finding(area="connections", detail=str(broken), ok=False)]

	if not roster.connections:
		return [Finding(area="connections", detail="none are configured", unknown=True)]

	found: list[Finding] = []

	for connection in roster.connections:
		found.append(_one_connection(connection, roster, settings))

		# **Its own line rather than folded into the one above**, because the two are different
		# facts and either can be true without the other. A connection that cannot be reached
		# is reported as unreachable — and folding this in there would mean the exposure was
		# only ever mentioned about servers that happened to answer, which is precisely
		# backwards for somebody who has just typed the address wrong.
		if subroutine.connections.in_the_clear(connection):
			found.append(
				Finding(
					area=connection.name,
					detail="reached over plain http, so its token crosses the network "
					"readable by anything in between",
					ok=False,
				)
			)

	return found


def _one_connection (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
) -> Finding:
	"""Ask one connection what it is, catching everything it can do instead of answering.

	**There is deliberately no "disabled" branch.** ``connections.roster`` returns only the
	live ones, so a connection switched off in ``config.toml`` never reaches here — a check
	for it would be a branch that can never run, which is the shape of defect this codebase
	spends most of its time finding. A disabled connection is simply not listed, which is what
	``subroutine connections`` does too.
	"""

	try:
		with subroutine.clients.opening.for_connection(connection, roster, settings) as client:
			me = client.me()

	# **Every failure, not only ours.** A connection is a socket, a credential and somebody
	# else's uptime; `fanout._attempt` catches only `SubroutineError` because every path is
	# supposed to translate, and this is the one command that must survive the path that did
	# not. A traceback here would be the diagnostic failing on the machine it is diagnosing.
	except Exception as failure:
		return Finding(area=connection.name, detail=_readable(failure), ok=False)

	kind = "agent" if me.user.is_service_account else "person"
	version = me.instance_version or "too old to say"
	schema = "" if me.schema_revision is None else f", schema {me.schema_revision}"

	return Finding(
		area=connection.name,
		detail=f"{version}{schema}, as {me.user.username} ({kind})",
	)


def _the_backups (settings: subroutine.config.Settings) -> list[Finding]:
	"""Report where backups go, and when the newest one was taken.

	**Read through ``db.backup.catalogue``, not by listing the directory**, so that this and
	``subroutine db backups`` cannot come to disagree about what counts as one — a file
	dropped in by hand is not a backup, and the naming convention that decides is already
	written down in one place.

	It says *when*, not whether the contents are any good. A backup of an empty database is a
	valid database and passes every check §12.6 makes (`#395`); the row counts that would
	catch it are recorded at the moment one is taken and are not in the file, so opening every
	one to count would turn a health check into a full read of a backup directory. What this
	answers is the question an operator actually gets wrong, which is "when did I last take
	one" — and the answer to that is usually older than they think.
	"""

	try:
		where = subroutine.db.backup.directory(settings)

	except subroutine.errors.SubroutineError as broken:
		return [Finding(area="backups", detail=str(broken), ok=False)]

	if not where.is_dir():
		return [
			Finding(
				area="backups",
				detail=f"{where} is not there, so none have been taken",
				unknown=True,
			)
		]

	try:
		found = subroutine.db.backup.catalogue(settings)

	except OSError as failure:
		return [Finding(area="backups", detail=f"{where}: {failure}", ok=False)]

	if not found:
		return [Finding(area="backups", detail=f"{where} holds none yet", unknown=True)]

	newest = found[0]
	age = (subroutine.db.types.utcnow() - newest.taken_at).days

	# **Stated, never judged.** How old is too old is the operator's question — a laptop and
	# a served instance want different answers, and a threshold invented here would either
	# nag somebody who is fine or reassure somebody who is not. Days, because "2026-08-03"
	# asks a reader to do arithmetic at the moment they are least inclined to.
	when = "today" if age == 0 else ("1 day ago" if age == 1 else f"{age} days ago")

	# The schema the copy carries against the one this build wants. §12.6 restores an older
	# backup and then offers an upgrade, so a difference is not a fault — it is the difference
	# between restoring and restoring *and then* migrating, which is worth knowing before the
	# morning somebody needs it.
	expects = subroutine.db.migrate.head_revision()
	behind = (
		"" if expects is None or newest.schema_head == expects else ", at an older schema"
	)

	return [
		Finding(
			area="backups",
			detail=(
				f"{len(found)} in {where}, newest {newest.name} "
				f"({newest.size_bytes:,} bytes, {when}{behind})"
			),
		)
	]


def _readable (failure: Exception) -> str:
	"""Return what went wrong, in as few words as carry the answer.

	A `SubroutineError` has already been written for a person to read. Anything else has not,
	so it is named by its type as well as its message — "ConnectError: [Errno -2] Name or
	service not known" tells somebody the address is wrong, where the message alone reads
	like a fault in this program.
	"""

	if isinstance(failure, subroutine.errors.SubroutineError):
		return str(failure)

	return f"{type(failure).__name__}: {failure}"


def verdict (findings: typing.Sequence[Finding]) -> str:
	"""Return the closing line: what was looked at, and whether anything wants attention."""

	wrong = [one for one in findings if not one.ok]

	# Counted rather than judged. The unknowns are printed above this line, so a reader who
	# cares that the plugin was not knowable can see it, and one who does not is not stopped
	# by a question nobody asked.
	if not wrong:
		return "Nothing here needs attention."

	return f"{len(wrong)} of {len(findings)} need attention."
