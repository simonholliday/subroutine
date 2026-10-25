"""Three administrative commands change what they say they change - `SR#5149`.

**A bullet of L-11 of the cold review of 2026-10-17**: ``project prioritise``, ``user reactivate``
and ``login revoke`` were invoked by no test, only named in prose and walked by ``--help``. Each is
driven here at a terminal and its effect read back from the database, not its words alone. They run
on the terminal's own instance, where every terminal test here runs.
"""

import typing

import sqlalchemy
import sqlalchemy.orm
import typer.testing

import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.session
import subroutine.domain.sessions
import subroutine.domain.users
import test_multi_user

#: The terminal's harness, bound here so pytest finds it.
home = test_multi_user.home
run = test_multi_user.run

Run = typing.Callable[..., typer.testing.Result]


def _opened () -> sqlalchemy.Engine:
	"""Return an engine on the database the terminal's instance keeps."""

	return subroutine.db.session.create_engine(subroutine.config.load_settings().database_url)


def _prioritised (engine: sqlalchemy.Engine) -> str | None:
	"""Return the key of the project the workspace prioritises, or ``None``."""

	workspace = subroutine.db.models.identity.Workspace
	project = subroutine.db.models.project.Project

	with sqlalchemy.orm.Session(engine) as session:
		return session.scalars(
			sqlalchemy.select(project.key)
			.join(workspace, workspace.prioritised_project_id == project.id)
			.where(workspace.slug == "metacortex")
		).one_or_none()


def test_prioritise_points_the_workspace_at_the_project (run: Run) -> None:
	"""``project prioritise web`` makes it the workspace's priority, and ``--none`` clears it."""

	run("init", "--workspace", "Metacortex")
	run("project", "create", "web", "Website rebuild")
	engine = _opened()

	try:
		assert _prioritised(engine) is None

		said = run("project", "prioritise", "web").output

		assert _prioritised(engine) == "web", said

		run("project", "prioritise", "--none")

		assert _prioritised(engine) is None

	finally:
		engine.dispose()


def test_reactivate_brings_back_an_account_that_had_left (run: Run) -> None:
	"""``user reactivate keanu`` after ``user deactivate keanu`` makes the account active again."""

	run("init", "--workspace", "Metacortex")
	run("user", "create", "keanu", "--name", "Keanu Reeves")
	engine = _opened()

	def active () -> bool:
		"""Say whether keanu's account is active."""

		with sqlalchemy.orm.Session(engine) as session:
			return subroutine.domain.users.by_username(session, "keanu").is_active

	try:
		run("user", "deactivate", "keanu", "--yes")

		assert not active()

		said = run("user", "reactivate", "keanu").output

		assert active(), said

	finally:
		engine.dispose()


def test_login_revoke_ends_every_session_and_spends_every_unused_link (run: Run) -> None:
	"""``login revoke keanu`` revokes the browser sessions keanu holds and spends their unused links.

	**The session and the links are made in the database**, because no terminal command opens a
	browser session.
	"""

	run("init", "--workspace", "Metacortex")
	run("user", "create", "keanu", "--name", "Keanu Reeves")
	engine = _opened()
	sessions = subroutine.db.models.identity.WebSession
	links = subroutine.db.models.identity.LoginLink

	def live () -> tuple[int, int]:
		"""Count keanu's sessions not yet revoked and links not yet spent."""

		with sqlalchemy.orm.Session(engine) as session:
			keanu = subroutine.domain.users.by_username(session, "keanu")
			signed_in = session.scalars(
				sqlalchemy.select(sessions.id).where(
					sessions.user_id == keanu.id, sessions.revoked_at.is_(None)
				)
			).all()
			unspent = session.scalars(
				sqlalchemy.select(links.id).where(
					links.user_id == keanu.id, links.redeemed_at.is_(None)
				)
			).all()

			return len(signed_in), len(unspent)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			keanu = subroutine.domain.users.by_username(session, "keanu")
			_link, opening = subroutine.domain.sessions.mint_link(session, user=keanu)
			subroutine.domain.sessions.redeem(session, opening)
			subroutine.domain.sessions.mint_link(session, user=keanu)
			session.commit()

		assert live() == (1, 1)

		said = run("login", "revoke", "keanu").output

		assert live() == (0, 0), said
		assert "out of 1 browser" in said, said

	finally:
		engine.dispose()
