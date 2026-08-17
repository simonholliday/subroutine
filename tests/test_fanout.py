"""Asking every connection at once — docs/design.md §13.7.

The end-to-end behaviour is covered in ``tests/test_cli_connections.py``, against two real
instances. What is here is the part that is hard to arrange from outside: that a *bug* is not
swallowed into a "connection unavailable" line, and that the order results come back in is the
roster's rather than whichever server answered first.
"""

import types
import typing

import pytest

import subroutine.clients.base
import subroutine.connections
import subroutine.errors
import subroutine.fanout
import subroutine.views


class Fake:
	"""A connection that answers however the test told it to."""

	def __init__ (self, name: str, answer: typing.Callable[[], typing.Any]) -> None:
		"""Record what this one will do when it is asked."""

		self.connection = subroutine.connections.Connection(
			name=name, url=None if name == "local" else f"https://{name}.example.com"
		)
		self._answer = answer

	def identity (self) -> subroutine.clients.base.Identity:
		"""Answer, however that was defined."""

		return typing.cast(subroutine.clients.base.Identity, self._answer())


def answering (value: typing.Any) -> typing.Callable[[], typing.Any]:
	"""Return a callable that answers with a fixed value."""

	return lambda: value


def failing (error: BaseException) -> typing.Callable[[], typing.Any]:
	"""Return a callable that raises."""

	def raise_it () -> typing.Any:
		"""Raise the error this was built with."""

		raise error

	return raise_it


def ask (client: typing.Any) -> typing.Any:
	"""Ask one connection for its identity."""

	return client.identity()


def clients (*items: Fake) -> list[subroutine.clients.base.Client]:
	"""Present the fakes as clients, which is what ``gather`` takes."""

	return typing.cast(list[subroutine.clients.base.Client], list(items))


def test_every_connection_is_asked_and_the_answers_keep_roster_order () -> None:
	"""Otherwise grouped output reshuffles according to which server was quickest."""

	gathered = subroutine.fanout.gather(
		clients(
			Fake("local", answering("first")),
			Fake("work", answering("second")),
			Fake("side", answering("third")),
		),
		ask,
	)

	assert gathered.values() == ["first", "second", "third"]
	assert [answer.connection.name for answer in gathered.answers] == ["local", "work", "side"]
	assert not gathered.partial


def test_one_failure_is_recorded_and_the_rest_of_the_answers_stand () -> None:
	"""A person on a train should still see their own list."""

	gathered = subroutine.fanout.gather(
		clients(
			Fake("local", answering("mine")),
			Fake("work", failing(subroutine.errors.ServiceUnavailable("the VPN is off"))),
		),
		ask,
	)

	assert gathered.values() == ["mine"]
	assert gathered.partial
	assert [failure.connection.name for failure in gathered.failures] == ["work"]
	assert "the VPN is off" in gathered.failures[0].describe()


def test_the_failure_line_names_the_connection_a_person_would_recognise () -> None:
	"""``display_name`` is what somebody put in their own configuration to read."""

	failure = subroutine.fanout.Failure(
		connection=subroutine.connections.Connection(
			name="work", url="https://x.example.com", display_name="Acme"
		),
		error=subroutine.errors.ServiceUnavailable("not answering"),
	)

	assert failure.describe().startswith("Acme: ")


def test_strict_raises_the_first_failure_instead_of_collecting_it () -> None:
	"""For a script that would rather stop than act on a partial view."""

	with pytest.raises(subroutine.errors.ServiceUnavailable):
		subroutine.fanout.gather(
			clients(
				Fake("local", answering("mine")),
				Fake("work", failing(subroutine.errors.ServiceUnavailable("down"))),
			),
			ask,
			strict=True,
		)


def test_a_bug_is_never_swallowed_into_a_connection_unavailable_line () -> None:
	"""Only failures this program *describes* are caught.

	Anything else is a bug, and a bug reported as "that connection could not be reached" is a
	bug nobody ever finds — the message would name the network and the cause would be a typo
	three modules away.
	"""

	with pytest.raises(ZeroDivisionError):
		subroutine.fanout.gather(
			clients(
				Fake("local", answering("mine")),
				Fake("work", failing(ZeroDivisionError("a real bug"))),
			),
			ask,
		)


def test_a_single_connection_is_asked_without_a_thread_pool () -> None:
	"""The overwhelmingly common case, and it should not pay for concurrency.

	Asserted by the traceback of a genuine bug: run on this thread it is the bug's own
	traceback, and run through a pool it would be something reraised out of a worker.
	"""

	with pytest.raises(ZeroDivisionError) as raised:
		subroutine.fanout.gather(clients(Fake("local", failing(ZeroDivisionError("here")))), ask)

	frames: list[str] = []
	frame: types.TracebackType | None = raised.tb

	while frame is not None:
		frames.append(frame.tb_frame.f_code.co_name)
		frame = frame.tb_next

	assert "raise_it" in frames
	assert not any("worker" in name for name in frames)


def test_nothing_to_ask_is_not_an_error () -> None:
	"""Every connection turned off is a configuration refusal, not a fan-out one."""

	gathered = subroutine.fanout.gather(clients(), ask)

	assert gathered.values() == []
	assert not gathered.partial


def identity (name: str, instance_id: str) -> subroutine.clients.base.Identity:
	"""Build an identity reporting one instance id."""

	return subroutine.clients.base.Identity(
		instance=subroutine.views.Instance(id=instance_id, name=name, timezone="UTC"),
		workspaces=(),
	)


def test_two_connections_reporting_one_instance_are_refused_by_name () -> None:
	"""Otherwise every task on it is counted, printed and offered for completion twice."""

	same = "019fb1ef-098b-74bd-b09b-ba214f2ec196"
	gathered = subroutine.fanout.gather(
		clients(
			Fake("work", answering(identity("Office", same))),
			Fake("acme", answering(identity("Office", same))),
		),
		ask,
	)

	with pytest.raises(subroutine.errors.SubroutineError) as raised:
		subroutine.fanout.refuse_duplicate_instances(gathered.answers)

	assert "work" in raised.value.detail and "acme" in raised.value.detail
	assert raised.value.errors[0].field == "connections.acme"


def test_two_servers_calling_themselves_the_same_thing_are_fine () -> None:
	"""``instance_name`` is whatever whoever runs it chose; ``instance_id`` settles identity.

	One person may genuinely connect to two servers both called "Office", and refusing that
	would be refusing the ordinary case in order to catch the odd one.
	"""

	gathered = subroutine.fanout.gather(
		clients(
			Fake("work", answering(identity("Office", "019fb1ef-098b-74bd-b09b-ba214f2ec196"))),
			Fake("acme", answering(identity("Office", "019fb1f6-5714-7b8b-bf08-47740cb0b52c"))),
		),
		ask,
	)

	subroutine.fanout.refuse_duplicate_instances(gathered.answers)


def test_an_instance_that_has_not_been_set_up_is_skipped_rather_than_matched () -> None:
	"""Two connections with no instance row are not therefore the same instance."""

	empty = subroutine.clients.base.Identity(instance=None, workspaces=())
	gathered = subroutine.fanout.gather(
		clients(Fake("work", answering(empty)), Fake("acme", answering(empty))), ask
	)

	subroutine.fanout.refuse_duplicate_instances(gathered.answers)
