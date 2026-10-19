"""A rule that binds the whole workspace refuses every write from beyond a credential's places alike.

`SR#5125`, the *Inconsistent concealment* bullet of M-23 of the cold review of 2026-10-17. A rule
binding the whole workspace is read by every credential in it (decision `#4134`), so a write to one
outside a narrowed credential's projects is refused 403, as beyond them, rather than 404, which
would deny a document its reader can see. Retitling it and commenting on it were; **linking to it,
linking from it and filing a section under it answered 404 about a project the request never
named**, since the check took the link's ends and the section's project for something other than
the document.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.domain.tasks


class Bound:
	"""MetaCortex: a rule in ``ops`` binding the whole workspace, a task in ``web``, and laurence's
	credential narrowed to ``web``."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the workspace and its two projects through the domain, and the credential."""

		suffix = uuid.uuid4().hex[:6]
		setup = subroutine.domain.bootstrap.initialise(
			session, username=f"laurence-{suffix}", instance_name="MetaCortex"
		)
		web, ops = (
			subroutine.domain.projects.create(
				session, workspace_id=setup.workspace.id, key=key, title=key.title()
			)
			for key in ("web", "ops")
		)
		self.rule = subroutine.domain.documents.create(
			session,
			project=ops,
			title="Name the project on every capture",
			body="Decided.",
			type_key="decision",
			binds="workspace",
		).ref
		self.task = subroutine.domain.tasks.create(session, project=web, title="Fix the deploy script").ref
		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=setup.user, title="Narrowed to web", project_scope=[str(web.id)]
		)
		session.flush()
		self.secret = issued.value.get_secret_value()
		self.application = api_support.build_app(api_support.factory_for(session))

	def call (self, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
		"""Make one request with the narrowed credential."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {self.secret}"},
			**kwargs,
		)


#: Each write to the rule from beyond the credential's places, as the request it is.
WRITES: dict[str, typing.Callable[[Bound], tuple[str, str, dict[str, typing.Any]]]] = {
	"retitling it": lambda here: ("PATCH", f"/v1/documents/{here.rule}", {"title": "Name it"}),
	"commenting on it": lambda here: ("POST", f"/v1/documents/{here.rule}/comments", {"body": "Agreed."}),
	"linking a task to it": lambda here: (
		"POST",
		f"/v1/tasks/{here.task}/links",
		{"target": here.rule, "target_type": "document", "link_type": "relates_to"},
	),
	"linking it to a task": lambda here: (
		"POST",
		f"/v1/documents/{here.rule}/links",
		{"target": here.task, "target_type": "task", "link_type": "relates_to"},
	),
	"filing a section under it": lambda here: (
		"POST",
		"/v1/documents",
		{"title": "Captures from the terminal", "parent": here.rule},
	),
}


@pytest.mark.parametrize("write", list(WRITES))
def test_every_write_to_a_binding_rule_beyond_the_credential_is_refused_as_beyond_it (
	session: sqlalchemy.orm.Session, write: str
) -> None:
	"""The narrowed credential reads the rule, and each write is the one 403 saying why."""

	here = Bound(session)

	assert here.call("GET", f"/v1/documents/{here.rule}").status_code == 200

	method, path, body = WRITES[write](here)
	answer = here.call(method, path, json=body)

	assert answer.status_code == 403, answer.text
	assert "acts beyond the projects the token you used may change" in answer.json()["detail"], answer.text
