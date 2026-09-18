"""An instance that has agreed to it asks what has been released, once a day — `#2222`.

Simon's decision of 2026-09-07 is the whole shape: opt-in, absent meaning no, and asked only in
the background of a request from somebody signed in, so an idle instance makes no request at
all. On 2026-09-16 he settled who may start it: anyone signed in, person or agent, and never a
calendar app polling a feed.

**The guard that matters most copies ``test_upgrade_without_check_reaches_no_network``**: the
fetch is replaced with something that fails the test if it is called at all, and an instance
that has not agreed is driven through ordinary requests.
"""

import datetime
import pathlib
import threading
import typing

import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.auth
import subroutine.config
import subroutine.domain.authentication
import subroutine.domain.calendars
import subroutine.errors
import subroutine.releases
import subroutine.views
import test_api_tasks

#: A record to answer with, standing in for the published one.
FOUND = subroutine.releases.Published(
	releases=(subroutine.releases.Release(version="9.9.9", schema="c" * 12, date="2026-09-16"),),
)

#: The moment every clock here starts from.
START = datetime.datetime(2026, 9, 16, 9, 0, tzinfo=datetime.UTC)


class Asking:
	"""A stand-in for the network that counts how often it is asked, and can be made to fail."""

	def __init__ (self, *, failing: bool = False) -> None:
		"""Start having been asked nothing."""

		self.times = 0
		self.failing = failing

	def __call__ (self) -> subroutine.releases.Published:
		"""Answer, or fail the way the real fetch does."""

		self.times += 1

		if self.failing:
			raise subroutine.errors.ServiceUnavailable("Could not read the list of releases.")

		return FOUND


class Clock:
	"""A clock a test moves by hand."""

	def __init__ (self) -> None:
		"""Start at :data:`START`."""

		self.now = START

	def __call__ (self) -> datetime.datetime:
		"""Say what time it is."""

		return self.now


def _watching (
	world: test_api_tasks.World, asking: Asking, clock: Clock
) -> subroutine.releases.Watch:
	"""Give one application a watch, as an operator's ``[releases] check = true`` would."""

	watch = subroutine.releases.Watch(fetch=asking, clock=clock)
	world.application.state.releases = watch

	return watch


def _settled (watch: subroutine.releases.Watch) -> None:
	"""Wait for the check a request started, which a request itself never does."""

	if watch.asking is not None:
		watch.asking.join(timeout=10)


def test_an_instance_that_has_not_agreed_never_asks (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""§12.4a, asserted rather than promised: absent means no, on every request.

	The fetch itself is replaced, so a watch built anywhere - not only the one this test can see -
	would fail it.
	"""

	def forbidden (*_args: typing.Any, **_kwargs: typing.Any) -> subroutine.releases.Published:
		"""Fail loudly rather than answering."""

		raise AssertionError("an instance that had not agreed asked what has been released")

	monkeypatch.setattr(subroutine.releases, "record", forbidden)

	world = test_api_tasks._world(session)

	assert world.application.state.releases is None

	for _ in range(3):
		assert world.call("GET", "/v1/tasks").status_code == 200


def test_agreeing_is_what_builds_something_that_can_ask () -> None:
	"""The other half of the rule above: the setting is read, and only its ``true`` builds a watch."""

	def built (**releases: typing.Any) -> object:
		"""Return what an application built with these settings holds."""

		settings = subroutine.config.Settings(dev_mode=True, releases=releases)

		return subroutine.api.app.create_app(settings=settings).state.releases

	assert isinstance(built(check=True), subroutine.releases.Watch)
	assert built(check=False) is None
	assert built() is None


def test_an_agreed_instance_asks_once_a_day_while_somebody_is_using_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The first request of a day asks; the rest of the day does not; the next day asks again."""

	world = test_api_tasks._world(session)
	asking, clock = Asking(), Clock()
	watch = _watching(world, asking, clock)

	assert asking.times == 0, "nothing asks before anybody has used the instance"

	for _ in range(3):
		assert world.call("GET", "/v1/tasks").status_code == 200
		_settled(watch)

	assert asking.times == 1
	assert watch.latest == subroutine.releases.Asked(at=START, record=FOUND)

	clock.now = START + subroutine.releases.CHECK_EVERY - datetime.timedelta(minutes=1)
	world.call("GET", "/v1/tasks")
	_settled(watch)

	assert asking.times == 1, "asked again before a day had passed"

	clock.now = START + subroutine.releases.CHECK_EVERY
	world.call("GET", "/v1/tasks")
	_settled(watch)

	assert asking.times == 2


def test_a_check_that_fails_leaves_the_request_alone_and_waits_a_day (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A failed check is not a failed request, and it is not retried on the next one.

	The clock is the last attempt, not the last answer, so a host that cannot be reached is asked
	tomorrow rather than on every request. And the failure is kept as a failure: read as *nothing
	newer*, it would tell every surface that published this that the instance is up to date.
	"""

	world = test_api_tasks._world(session)
	asking, clock = Asking(failing=True), Clock()
	watch = _watching(world, asking, clock)

	assert world.call("GET", "/v1/tasks").status_code == 200
	_settled(watch)

	assert watch.latest is not None
	assert watch.latest.record is None
	assert watch.latest.failure == "Could not read the list of releases."

	assert world.call("GET", "/v1/tasks").status_code == 200
	_settled(watch)

	assert asking.times == 1


def test_a_failed_check_keeps_what_the_check_before_it_found () -> None:
	"""`SR#2891`: a failure replaced the last record heard, and a day's news went with it.

	**Driven through a real watch rather than by setting its fields**, which is how every
	notice case is built - and why removing the line that keeps the record left all of them
	green. A check that answers, then one a day later that fails: the failure is published, and
	so is what the first one found.
	"""

	asking, clock = Asking(), Clock()
	watch = subroutine.releases.Watch(fetch=asking, clock=clock)

	watch.ask_if_due()
	_settled(watch)

	asking.failing = True
	clock.now = START + subroutine.releases.CHECK_EVERY + datetime.timedelta(minutes=1)

	watch.ask_if_due()
	_settled(watch)

	news = subroutine.views.release_news(watch)

	assert asking.times == 2, "the second check was never made, so nothing here was asked"
	assert news.failure == "Could not read the list of releases.", news
	assert [release.version for release in news.releases] == ["9.9.9"], news


def test_nobody_signed_in_starts_a_check (session: sqlalchemy.orm.Session) -> None:
	"""A health check, a refused credential and a calendar app polling a feed never ask.

	Only somebody recognised starts a check (Simon, 2026-09-16), and a feed poll runs with nobody
	present - so each of these is an instance nobody is using, as far as asking goes.
	"""

	world = test_api_tasks._world(session)
	asking, clock = Asking(), Clock()
	watch = _watching(world, asking, clock)

	_feed, minted = subroutine.domain.calendars.create(
		session,
		subroutine.domain.authentication.Principal(user=world.user),
		workspace_id=world.workspace.id,
		title="Mine",
		now=START,
	)
	session.flush()
	prefix, secret = typing.cast(
		tuple[str, str],
		subroutine.auth.parse_token(
			minted.value.get_secret_value(), kind=subroutine.auth.CALENDAR_KIND
		),
	)

	polled = api_support.call(world.application, "GET", f"/v1/calendars/{prefix}/{secret}.ics")

	assert polled.status_code == 200, polled.text
	assert api_support.call(world.application, "GET", "/healthz").status_code == 200
	assert api_support.call(
		world.application, "GET", "/v1/tasks", headers={"Authorization": "Bearer sr_nonsense"}
	).status_code == 401

	assert watch.asking is None and asking.times == 0

	# **And somebody signed in does**, so the silence above is about who asked rather than a
	# watch that could not ask at all.
	world.call("GET", "/v1/tasks")
	_settled(watch)

	assert asking.times == 1


def test_a_config_file_turns_it_on_and_a_misspelling_is_reported (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``[releases]`` is read from the file, and a typo inside it is named rather than ignored.

	A misspelled ``check`` fails safe - nothing asks - but somebody who believes they turned it on
	is who the unknown-settings warning exists for, and this is the one setting written as a table.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
	written = subroutine.config.config_file_path()
	written.parent.mkdir(parents=True, exist_ok=True)

	assert subroutine.config.load_settings().releases.check is False

	written.write_text("[releases]\ncheck = true\n", encoding="utf-8")

	assert subroutine.config.load_settings().releases.check is True
	assert subroutine.config.unknown_settings() == []

	written.write_text("[releases]\nchek = true\n", encoding="utf-8")

	assert subroutine.config.load_settings().releases.check is False
	assert dict(subroutine.config.unknown_settings()) == {"releases.chek": "releases.check"}


def test_me_says_nobody_agreed_to_ask_rather_than_nothing_newer (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2223`. Not checking is an answer of its own, and it is not *up to date*."""

	world = test_api_tasks._world(session)
	news = world.call("GET", "/v1/me").json()["releases"]

	assert news == {
		"checking": False, "asked_at": None, "failure": None, "releases": [], "plugins": {}
	}


def test_me_publishes_what_the_check_found_once_it_has_answered (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2223`. Before the check answers it says so; after, it carries the whole record.

	**The fetch is held open until the test lets it go**, because asking who you are is itself a
	signed-in request and starts the day's check - so without holding it this would be a race
	between the check finishing and the document being built.
	"""

	world = test_api_tasks._world(session)
	answer = threading.Event()
	record = subroutine.releases.Published(
		releases=FOUND.releases, plugins={"subroutine": "9.9.10"}
	)

	def held () -> subroutine.releases.Published:
		"""Answer only once the test has read the document built before the answer."""

		answer.wait(timeout=10)

		return record

	watch = subroutine.releases.Watch(fetch=held, clock=Clock())
	world.application.state.releases = watch

	waiting = world.call("GET", "/v1/me").json()["releases"]

	assert waiting == {
		"checking": True, "asked_at": None, "failure": None, "releases": [], "plugins": {}
	}

	answer.set()
	_settled(watch)

	answered = world.call("GET", "/v1/me").json()["releases"]

	assert answered["checking"] is True
	assert answered["asked_at"] is not None and answered["failure"] is None
	assert answered["releases"] == [
		{"version": "9.9.9", "schema_revision": "c" * 12, "date": "2026-09-16"}
	]
	assert answered["plugins"] == {"subroutine": "9.9.10"}


def test_me_says_a_check_failed_rather_than_that_nothing_is_newer (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2223`. The third null: asked, and could not find out, said as that and nothing else."""

	world = test_api_tasks._world(session)
	watch = _watching(world, Asking(failing=True), Clock())

	world.call("GET", "/v1/tasks")
	_settled(watch)

	news = world.call("GET", "/v1/me").json()["releases"]

	assert news["checking"] is True and news["asked_at"] is not None
	assert news["failure"] == "Could not read the list of releases."
	assert news["releases"] == [] and news["plugins"] == {}
