"""Adding a second person to an instance — docs/design.md §7.1 and §7.3a, item ``#174``.

**`README.md` sent a reader to a page headed "Running it for a team", and there was no way to
create the second member of one.** ``init`` made exactly one account; the only other identity
anybody could make was a service account, which the CLI itself calls a machine identity. So a
five-person team shared one login, or every colleague was modelled as a robot.

That is worse than a missing feature, because it made several things the product already
advertises unreachable. Private projects grant sight through a membership row; roles are seeded
per workspace; every write is attributed. None of it can be exercised on an instance that holds
one person, so none of it was ever exercised by a user.

These tests run against the real CLI, because the gap was in what somebody could *do* rather
than in what the domain could express — every service these commands call already existed.
"""

import os
import pathlib
import typing

import pytest
import sqlalchemy
import typer.testing

import subroutine.cli.main
import subroutine.config
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG directory at a fresh temporary home, with nothing inherited."""

	root = tmp_path / "home"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	for name in list(os.environ):
		if name.startswith("SUBROUTINE_"):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	return root


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Return a runner for the real CLI, failing loudly on an unexpected exit code."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)
		subroutine.cli.main._said_unknown_settings = False

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


def test_a_second_person_can_be_added_and_given_a_role (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""The whole of `#174`, through the surface a reader of the README would use."""

	run("init", "--workspace", "Acme")

	run("user", "create", "thomas", "--name", "Thomas Anderson")

	assert "thomas" in run("user", "list").output

	# **No second command, since `#587`.** `user create` puts them in the only workspace with
	# the ordinary role, so what a reader of the README does is one line — and this test says
	# so by asserting the membership without ever having asked for it.
	members = run("user", "list", "--workspace", "acme").output

	assert "thomas" in members
	assert "member" in members


def test_adding_a_colleague_does_not_cost_you_your_own_list (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**The footgun this had to avoid**, and it is the same one service accounts had.

	Local mode picks an account by there being exactly one (§12.1a). The moment a second
	exists it refuses — correctly — with "there is more than one account, so there is no way
	to tell whose to-do list to show". So on an instance somebody actually uses, adding a
	colleague broke `subroutine add` for them, at the moment they were being helpful.

	Service accounts stopped counting towards that total on 2026-07-30 for the same reason:
	setting somebody up must not take something away.
	"""

	run("init", "--workspace", "Acme")
	run("add", "Buy milk")

	created = run("user", "create", "thomas")

	assert "go on acting as" in created.output

	# The operator's own commands still work, and still show their own item.
	assert "Buy milk" in run("list").output
	run("add", "Another thing")


def test_a_role_is_named_rather_than_assumed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""What somebody may do is the decision being taken, so the command will not take it."""

	run("init", "--workspace", "Acme")
	run("user", "create", "thomas")

	refused = run("user", "add", "thomas", expect=1)

	assert "--role" in refused.output


def test_somebody_added_by_mistake_can_be_removed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#140`'s argument, one level up: a membership that can only be granted is permanent.

	The mistake here is not a tidy backlog — it is somebody seeing a private project they
	should not.
	"""

	run("init", "--workspace", "Acme")
	run("user", "create", "thomas")

	run("user", "remove", "thomas")

	assert "thomas" not in run("user", "list", "--workspace", "acme").output

	# And the account itself survives, because what somebody wrote stays theirs.
	assert "thomas" in run("user", "list").output


def test_the_last_administrator_cannot_be_removed (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""A workspace nobody can administer has thrown away the remedy for every later mistake.

	Including this one: there is no way to grant a role in a workspace with no administrator,
	so it cannot be repaired from inside.
	"""

	# `--username`, because without it `init` names the account after whoever is running the
	# suite (`getpass.getuser()`). This test used to ask for `si` back, which is the developer's
	# login — so everywhere else it refused with "there is no account called 'si'", passing
	# expect=1 for a reason that has nothing to do with administrators (`#227`).
	run("init", "--workspace", "Acme", "--username", "owner")

	refused = run("user", "remove", "owner", expect=1)

	assert "administer" in refused.output


def test_a_new_account_belongs_to_nothing_until_somebody_says_so (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Two decisions, deliberately separate — and often two different people.

	Creating an account is an instance-tier act; deciding where somebody may work is a
	workspace one. Collapsing them would mean anybody who can add a colleague can also put
	them wherever they like.
	"""

	created = subroutine.domain.users.create(session, username="thomas")

	assert subroutine.domain.workspaces.readable(
		session, subroutine.domain.authentication.Principal(user=created)
	) == []


def test_joining_somebody_to_a_workspace_is_a_permission_check (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#188`, found while building this and fixed in the same sitting.

	``add_member`` took an actor, recorded an event attributed to it, and asked it nothing —
	while `CLAUDE.md`'s list of the services that check permissions named it explicitly. A
	rule written down, believed, and implemented by nothing.

	``tests/test_actor_discipline.py`` could not see it: that checks every *call site* passes
	``actor=``, which is what makes the ``None`` default safe, and not that the service given
	one then does anything with it. A guard checks the shape it was written from.
	"""

	owner = subroutine.domain.users.create(session, username="owner")
	workspace = subroutine.domain.workspaces.create(
		session, slug="acme", title="Acme", owner=owner, timezone="UTC"
	)

	outsider = subroutine.domain.users.create(session, username="mallory")
	joining = subroutine.domain.users.create(session, username="thomas")

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.domain.workspaces.add_member(
			session,
			workspace,
			joining,
			role_key="member",
			actor=subroutine.domain.authentication.Principal(user=outsider),
		)


def test_an_instance_administrator_is_not_called_by_a_workspace_role_name (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""``#204``, from the release-candidate review. Two commands, one column, two vocabularies.

	``user list`` rendered ``is_superuser`` as the bare word ``admin``, which is also the key
	of a workspace role — and ``user list --workspace acme`` prints its answer in the same
	column position. So the same person read as ``admin`` in one and ``owner`` in the other,
	where the first named a role she does not hold and the second could legitimately have
	printed it.

	Both listings are asserted together, because the collision only exists between them.
	"""

	run("init", "--workspace", "Acme")
	run("user", "create", "thomas", "--name", "Thomas Anderson")

	instance = run("user", "list").output
	workspace = run("user", "list", "--workspace", "acme").output

	assert "instance admin" in instance, instance
	# The bare word belongs to the workspace listing alone, and only where a role holds it.
	assert "owner" in workspace
	assert "member" in workspace


def test_a_person_can_say_which_timezone_they_are_in_and_read_it_back (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#994`. §6.5's user level was settable at creation and by no surface afterwards.

	**Both halves in one test, deliberately.** A setting that can be written and not read back
	is half a control: the value decides which day the agenda is about (`#989`) and shows
	nowhere else, so somebody who mistypes it has no way to find out.
	"""

	run("init", "--workspace", "Acme")

	# **`init` records the machine's zone on the account**, which `#994` did not expect — it
	# measured the served instance, where the founder's is null, and read that as what a fresh
	# install produces. So the account this reports on is already correct, and what the item
	# describes is somebody made by `user create`, or somebody who has since moved.
	said = run("user", "timezone").output

	assert "Europe/London" in said
	assert "Tip:" not in said, (
		"`#1002`: a reader who has already said where they are is not invited to say it again"
	)

	said = run("user", "timezone", "Australia/Sydney").output

	assert "Australia/Sydney" in said
	assert "agenda" in said, "the one thing it changes is said, because it is not obvious"

	assert "Australia/Sydney" in run("user", "timezone").output

	cleared = run("user", "timezone", "--clear").output

	assert "Australia/Sydney" not in cleared

	# **Cleared is a state that reports itself**, and it is the only way to reach that branch
	# here: local mode acts as the founder, whose zone `init` has already filled in.
	unset = run("user", "timezone").output

	assert "not said" in unset, (
		"a person who has cleared it is told the workspace's zone is showing through"
	)
	assert "Tip:" in unset, "and told how to say it, which is the case the tip was written for"


def test_nobody_is_offered_a_way_to_set_somebody_else_s_timezone (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""Simon's decision of 2026-08-18, checked where a person would look for the loophole.

	The command takes a zone and no username, so there is nothing to name — which is the
	decision expressed as an absence rather than as a refusal. Asserted on the *help*, because
	an argument that does not exist cannot be tested by passing it: a second positional would
	be read as part of the zone and refused as an unknown one, which is the right answer for
	the wrong reason.
	"""

	run("init", "--workspace", "Acme")

	offered = run("user", "timezone", "--help").output

	assert "username" not in offered.lower(), (
		"a username argument would be a way to set somebody else's, which no permission grants"
	)
def test_a_new_account_joins_the_only_workspace_without_being_asked (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decision 2: do not ask a question with exactly one answer.

	The rule this stands for is that it **self-adjusts**: silent here, insistent in
	:func:`test_a_new_account_needs_a_named_workspace_when_there_are_several`, with no setting
	and nothing to revisit. A fresh instance has one workspace because nobody chose it; an
	instance with several has them because somebody did.
	"""

	run("init", "--workspace", "Acme")

	created = run("user", "create", "thomas").output

	assert "thomas is now member in acme" in created


def test_a_new_account_takes_the_ordinary_role_unless_one_is_named (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decision 1, both halves — the default and that it is still overridable."""

	run("init", "--workspace", "Acme")

	assert "is now member in acme" in run("user", "create", "thomas").output
	assert "is now viewer in acme" in run("user", "create", "kim", "--role", "viewer").output


def test_a_new_account_needs_a_named_workspace_when_there_are_several (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decision 2's other half, and the refusal names what the caller can see."""

	run("init", "--workspace", "Acme")
	run("workspace", "create", "clients", "Clients")

	refused = run("user", "create", "thomas", expect=1).output

	assert "say which one they work in" in refused
	assert "acme" in refused and "clients" in refused


def test_an_ambiguous_workspace_is_refused_before_the_account_is_made (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**Found by driving `#587`, and it is the worst failure this command can have.**

	The refusal used to arrive *after* ``create_user``, leaving a person with an account and no
	membership — and the same command re-run then refused again, because the username was
	taken. There was no way forward from either.

	It is ``token create``'s own rule one command along: *checked before anything is issued, so
	a credential is never minted and then stranded.* Asserted on the roster rather than on the
	refusal, because the message was always right; what was wrong was what had already happened
	when it printed.
	"""

	run("init", "--workspace", "Acme")
	run("workspace", "create", "clients", "Clients")
	run("user", "create", "thomas", expect=1)

	assert "thomas" not in run("user", "list").output


def test_a_second_thing_in_one_command_still_knows_who_is_asking (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**The other ordering bug driving `#587` found, and it could not exist before.**

	Local mode identifies the operator by there being exactly one account (§12.1a). Creating a
	colleague makes that ambiguous *within the running command*, so everything after it — the
	membership, a credential, a link — resolves an operator and finds two.

	``_keep_the_operators_own_list`` already cured it in the configuration file and not in the
	settings this process is holding, which was invisible while ``user create`` did one thing
	and nothing afterwards. The membership below is what proves the second call went through;
	the assertion after it is that the operator's own list still works.
	"""

	run("init", "--workspace", "Acme")

	assert "is now member in acme" in run("user", "create", "thomas").output
	assert "acme" in run("whoami").output


def test_a_superuser_joins_no_workspace_and_says_so_rather_than_guessing (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decision 1's special case, and the refusal beside it.

	An instance owner needs no workspace role, and granting one quietly would be a permission
	taken by default. A role or a workspace named beside ``--superuser`` is therefore not a
	narrower superuser — it is two different accounts asked for in one command, which
	``agent create`` refuses in the same words about a profile.
	"""

	run("init", "--workspace", "Acme")

	made = run("user", "create", "sam", "--superuser").output

	assert "Created sam" in made
	assert "is now" not in made

	refused = run("user", "create", "kim", "--superuser", "--role", "admin", expect=1).output

	assert "joins no workspace and takes no role" in refused


def test_naming_no_way_in_succeeds_and_names_both (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decisions 3 and 4, which are one sentence apart and are one idea.

	The account and the role are real work that succeeded, so this is ``init``'s shape rather
	than ``db restore``'s refusal — and **both** commands are named, never one, because the
	paths are additive: somebody may need a link for the browser and a credential for whoever
	is configuring their machine.
	"""

	run("init", "--workspace", "Acme")

	said = run("user", "create", "thomas").output

	assert "login link --username thomas" in said
	assert "token create --username thomas" in said


def test_a_way_in_is_produced_in_the_same_command (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#587` decision 4: both may be asked for, and both arrive.

	Driven by Tim, who needs a sign-in link *and* a credential — the link for the web
	interface, the credential for the colleague setting his machine up. A command framed as
	either/or refuses the one person who needs both.
	"""

	run("init", "--workspace", "Acme")

	said = run("user", "create", "tim", "--browser", "--terminal").output

	assert "A sign-in link for tim" in said
	assert "/signin?link=" in said
	assert said.count("That is the only time it is shown") == 2


def test_a_machine_identity_is_refused_a_browser_before_anything_is_written (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""**The third ordering fault driving found**, and the rule behind it was never wrong.

	``domain/sessions`` has always refused a browser session for a service account, because a
	credential carries a scope and a reach and a session carries neither. It fired *after* the
	account was created and joined, so a correct rule left a half-made identity behind.

	Refused from the flags now, which is a different question — *these two arguments mean two
	things* — asked before anything is written.
	"""

	run("init", "--workspace", "Acme")

	refused = run("user", "create", "bot", "--agent", "--browser", expect=1).output

	assert "--agent and --browser mean two different things" in refused
	assert "bot" not in run("user", "list").output


def test_adding_somebody_to_a_workspace_they_are_in_says_who_they_already_are (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#1439`: a unique index is not a sentence, and it used to be the whole answer.

	The insert let the database decide, which reached the reader as *"the database could not be
	read"* with a hint to check ``database_url`` — a message about our internals, a false claim
	about what failed, and a remedy pointing at a setting that was correct.

	**Rare until `#587`**, because the documented flow ran ``user create`` and then ``user add``
	exactly once. Now the second step is a duplicate, so every habit and every older transcript
	lands here.
	"""

	run("init", "--workspace", "Acme")
	run("user", "create", "thomas")

	refused = run("user", "add", "thomas", "--role", "member", expect=1).output

	assert "thomas is already member in acme" in refused
	assert "UNIQUE" not in refused and "database_url" not in refused


def test_user_add_still_puts_somebody_in_a_second_workspace (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""What ``user add`` is *for* once `#587` folded the first membership into ``user create``.

	Here rather than assumed, because the two changes above delete every existing exercise of
	this command and a verb nothing drives is a verb nobody notices breaking.
	"""

	run("init", "--workspace", "Acme")
	run("workspace", "create", "clients", "Clients")
	run("user", "create", "thomas", "--workspace", "acme")

	joined = run("user", "add", "thomas", "--role", "viewer", "--workspace", "clients").output

	assert "thomas is now viewer in clients" in joined
