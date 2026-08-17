"""Asking every connection at once, and surviving one of them being unreachable.

SPEC.md §13.7. **Reads fan out; writes never do.** A read is issued to every connection
concurrently and merged here. A write names exactly one connection, explicitly or by default,
because there is no transaction that could span two instances and no sensible way to report a
half-failure.

**Fan-out has to survive an instance being unreachable.** The work VPN is off, the laptop is
on a train, the server is restarting — none of that should stop a person seeing their own
list. So a connection that fails is named in the output and skipped, and the command still
exits 0 with the results it has: an agenda that refuses to print because one of three servers
is down is worse than an agenda with a line saying which one. ``--strict`` reverses that, for
scripts that would rather stop than act on a partial view.

**Cursors do not compose, and this module does not pretend otherwise.** Keyset pagination is
per-instance; a fanned-out read applies its limit per connection and the merged result is a
merge of pages, not a single ordered page. Sorting is re-applied after the merge, on fields
the client can compute for itself. Deep pagination across connections is not supported and is
not going to be.
"""

import concurrent.futures
import dataclasses
import typing

import subroutine.clients.base
import subroutine.config
import subroutine.connections
import subroutine.errors

Result = typing.TypeVar("Result")

#: How many connections to ask at once. Small on purpose: this is bounded by how many
#: instances a person has configured, which §13.7 expects to be one or two and never dozens.
MAX_WORKERS = 8


@dataclasses.dataclass(frozen=True)
class Answer(typing.Generic[Result]):
	"""What one connection said."""

	connection: subroutine.connections.Connection
	value: Result


@dataclasses.dataclass(frozen=True)
class Failure:
	"""One connection that could not be asked, and why."""

	connection: subroutine.connections.Connection
	error: subroutine.errors.SubroutineError

	def describe (self) -> str:
		"""Return the line to print beside the results that did arrive."""

		return f"{self.connection.label}: {self.error.detail}"


@dataclasses.dataclass(frozen=True)
class Gathered(typing.Generic[Result]):
	"""Every answer, and every connection that could not give one."""

	answers: tuple[Answer[Result], ...]
	failures: tuple[Failure, ...]

	def values (self) -> list[Result]:
		"""Return just the answers, for a caller that does not need to say where from."""

		return [answer.value for answer in self.answers]

	@property
	def partial (self) -> bool:
		"""Report whether anything was missed."""

		return bool(self.failures)


def gather (
	clients: typing.Sequence[subroutine.clients.base.Client],
	ask: typing.Callable[[subroutine.clients.base.Client], Result],
	*,
	strict: bool = False,
) -> Gathered[Result]:
	"""Ask every connection the same question, concurrently, and collect what came back.

	``strict`` makes any failure fatal. Without it a failure is recorded and the rest of the
	answers stand, which is the default because the alternative is a person on a train being
	told nothing at all about their own to-do list.

	**A single connection is asked on this thread**, not through the pool. That is the
	overwhelmingly common case, and running it here means the traceback of a genuine bug is
	the traceback of a genuine bug rather than something reraised out of a worker.
	"""

	if not clients:
		return Gathered(answers=(), failures=())

	if len(clients) == 1:
		return _collect([(clients[0], _attempt(clients[0], ask))], strict=strict)

	with concurrent.futures.ThreadPoolExecutor(
		max_workers=min(MAX_WORKERS, len(clients)),
		thread_name_prefix="subroutine-fanout",
	) as pool:
		# Submitted in roster order and read back in roster order, so that grouped output
		# does not reshuffle itself according to which server happened to answer first.
		running = [(client, pool.submit(_attempt, client, ask)) for client in clients]

		return _collect(
			[(client, future.result()) for client, future in running], strict=strict
		)


def _attempt (
	client: subroutine.clients.base.Client,
	ask: typing.Callable[[subroutine.clients.base.Client], Result],
) -> Result | subroutine.errors.SubroutineError:
	"""Ask one connection, returning either its answer or the failure it reported.

	Only failures this program describes are caught. Anything else is a bug, and a bug that
	was swallowed into a "connection unavailable" line would be a bug nobody ever finds.
	"""

	try:
		return ask(client)

	except subroutine.errors.SubroutineError as error:
		return error


def _collect (
	outcomes: typing.Sequence[
		tuple[subroutine.clients.base.Client, Result | subroutine.errors.SubroutineError]
	],
	*,
	strict: bool,
) -> Gathered[Result]:
	"""Sort the outcomes into answers and failures, or raise the first one under ``strict``."""

	answers: list[Answer[Result]] = []
	failures: list[Failure] = []

	for client, outcome in outcomes:
		if isinstance(outcome, subroutine.errors.SubroutineError):
			if strict:
				raise outcome

			failures.append(Failure(connection=client.connection, error=outcome))

		else:
			answers.append(Answer(connection=client.connection, value=outcome))

	return Gathered(answers=tuple(answers), failures=tuple(failures))


def duplicate_instances (
	identities: typing.Sequence[Answer[subroutine.clients.base.Identity]],
) -> subroutine.errors.ValidationError | None:
	"""Return the refusal two connections naming one server deserve, or ``None``.

	**Returned rather than raised, because whether it matters is decided later** (`#942`).
	Two names for one instance is harmless for a command that reports each connection on its
	own and fatal for one that combines them, and which of those is happening is known where
	the answers are flattened rather than where they are fetched.
	:func:`refuse_duplicate_instances` is this plus the raise, for callers that already know.
	"""

	seen: dict[str, str] = {}

	for answer in identities:
		if answer.value.instance is None:
			continue

		identifier = str(answer.value.instance.id)
		first = seen.get(identifier)

		if first is None:
			seen[identifier] = answer.connection.name

			continue

		return subroutine.errors.ValidationError(
			f"Connections {first!r} and {answer.connection.name!r} are the same instance, so "
			"everything on it would be counted twice.",
			hint=f"Remove one of them from {subroutine.config.config_file_path()}, or turn "
			"it off with 'enabled = false'.",
			errors=[
				subroutine.errors.FieldError(
					field=f"connections.{answer.connection.name}",
					code="duplicate_key",
					message=f"Reports the same instance id as {first!r} "
					f"({answer.value.instance.name}).",
				)
			],
		)

	return None


def refuse_duplicate_instances (
	identities: typing.Sequence[Answer[subroutine.clients.base.Identity]],
) -> None:
	"""Refuse to go on when two connections turn out to be the same server.

	Two colleagues may call one server ``work`` and ``acme``, and one person may connect to
	two servers both calling themselves "Office" — neither is a problem, because
	``instance_id`` settles what is what (§13.7). What *is* a problem is the same instance
	configured twice under two names, because then every task in a merged agenda is counted,
	printed and offered for completion twice.

	Named rather than deduplicated. Silently dropping one would leave a person with a
	connection that does nothing and no way to find out why.
	"""

	found = duplicate_instances(identities)

	if found is not None:
		raise found
