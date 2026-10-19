"""A credential that may not read comments learns nothing from one - `SR#5121`.

M-9 of the cold review of 2026-10-17 (`#5096`). **Each read verb gates its kind of row wherever it
appears** (docs/design.md section 7.1), and the change feed has gated a comment's events by
``comment:read`` since `#4553`. Three answers still read comments for a credential without it: a
document's backlinks named the task a comment on it was on, a task's proposed links offered a link
from a comment alone, and ``touched_by`` found a task somebody had only commented on.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.domain.workspaces

User = subroutine.db.models.identity.User


class Commented:
	"""laurence's workspace: carrieanne filed a task and a document, and keanu only commented.

	keanu's comment on the task names the document, so it is the one thing tying the two together,
	and the one thing keanu did to the task.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the three accounts over the domain, and the work over HTTP as each of them."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		setup = subroutine.domain.bootstrap.initialise(
			session, username=f"laurence-{suffix}", instance_name="MetaCortex"
		)
		self.laurence = setup.user
		self.slug = setup.workspace.slug
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")

		for member in (self.carrieanne, self.keanu):
			subroutine.domain.workspaces.add_member(session, setup.workspace, member, role_key="member")

		session.flush()
		self.application = api_support.build_app(api_support.factory_for(session))
		self.document = self.call(
			self.carrieanne, "POST", "/v1/documents", json={"title": "Venues are booked a month ahead", "type": "decision"}
		)
		self.task = self.call(self.carrieanne, "POST", "/v1/tasks", json={"title": "Book the venue"})
		self.call(
			self.keanu,
			"POST",
			f"/v1/tasks/{self.task['ref']}/comments",
			json={"body": f"Done as #{self.document['ref']} says."},
		)

	def call (
		self, user: User, method: str, path: str, *, scopes: list[str] | None = None, **kwargs: typing.Any
	) -> typing.Any:
		"""Make one request with a fresh credential of ``user``'s, failing unless it succeeds."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session,
			user=user,
			title=f"{user.username}'s {uuid.uuid4().hex[:6]}",
			scopes=scopes or [],
		)
		self.session.flush()
		answer = api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {issued.value.get_secret_value()}"},
			**kwargs,
		)

		assert answer.status_code < 300, answer.text

		return answer.json()


#: What laurence reads with, with and without ``comment:read``.
READERS = [
	pytest.param(None, True, id="reading comments"),
	pytest.param(["task:read"], False, id="not reading comments"),
]


@pytest.mark.parametrize(("scopes", "told"), READERS)
def test_a_backlink_through_a_comment_needs_comment_read (
	session: sqlalchemy.orm.Session, scopes: list[str] | None, told: bool
) -> None:
	"""The document's backlinks name the task keanu's comment is on, only to a comment reader."""

	here = Commented(session)
	answer = here.call(
		here.laurence, "GET", f"/v1/documents/{here.document['ref']}/backlinks", scopes=scopes
	)
	through = [row for row in answer["items"] if row.get("via") == "comment"]

	assert bool(through) is told, answer


@pytest.mark.parametrize(("scopes", "told"), READERS)
def test_a_link_proposed_from_a_comment_needs_comment_read (
	session: sqlalchemy.orm.Session, scopes: list[str] | None, told: bool
) -> None:
	"""The task's proposed links offer the document keanu cited, only to a comment reader."""

	here = Commented(session)
	answer = here.call(
		here.laurence, "GET", f"/v1/tasks/{here.task['ref']}/proposed-links", scopes=scopes
	)

	offered = [(row["other"]["entity_type"], row["other"]["ref"]) for row in answer["items"]]

	assert (("document", here.document["ref"]) in offered) is told, answer


@pytest.mark.parametrize(("scopes", "told"), READERS)
@pytest.mark.parametrize("asked", ["touched_by.eq=KEANU", "touched_by.eq=KEANU&touched_at.gte=yesterday"])
def test_work_found_by_a_comment_needs_comment_read (
	session: sqlalchemy.orm.Session, scopes: list[str] | None, told: bool, asked: str
) -> None:
	"""``touched_by`` finds the task keanu only commented on, only for a comment reader."""

	here = Commented(session)
	answer = here.call(
		here.laurence,
		"GET",
		f"/v1/tasks?workspace_id={here.slug}&{asked.replace('KEANU', here.keanu.username)}",
		scopes=scopes,
	)

	assert (here.task["ref"] in [row["ref"] for row in answer["items"]]) is told, answer
