"""Every act, by every kind of principal, through every way in: the permission matrix - `SR#4542`.

**Decision `#4532`**, R7 of the cold review of the permission model (`#4506`): one act table - each
act, the verbs it asks and the place it lands on - drives one test that runs every act, for every
kind of principal, through the five ways a request reaches an instance: the local client, the HTTP
client, the agent tools through ``/mcp``, ``subroutine_call_api``, and ``subroutine mcp`` driving the
application in process. Each act holds three things for every principal:

1. **The same answer on every transport**: answered everywhere, or refused everywhere with the same
   status, and in the same words where the permission check is what refused. The defects the review
   found between transports were all code that looked a row up twice.
2. **Agreement with the one decision**: where the permission check refuses one of the act's verbs on
   its place, every transport refuses; where it allows them all, every transport answers, unless the
   act names a rule of its own that can refuse it as well.
3. **Nothing it may not read**: no answer and no refusal carries text planted where the principal
   cannot read it. Every title, comment and description the cast makes is planted, each with the verb
   and place reading it takes.

**The table states the model the redesign builds** (`#4508`), not the one released, and a cell that
disagrees today is in :data:`KNOWN`, naming the item whose build makes it agree. Deleting the entry is
what closes it, and an entry whose cell has come to agree fails as stale, so the register cannot
outlive what it excuses.

**Its own step of the gate and its own CI job** (`#4532`), spread across workers: every act is driven
about seventy times, which makes this the slowest file in the suite by design.
"""

import dataclasses
import datetime
import json
import pathlib
import typing
import uuid

import httpx
import pytest
import sqlalchemy.orm

import api_support
import subroutine.acts
import subroutine.api.app
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.backup
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.local
import subroutine.domain.projects
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.mcp.protocol
import subroutine.mcp.relay
import subroutine.permissions

#: Where the matrix's remote clients believe the instance is. Never reached: every request goes
#: through the application in process.
ADDRESS = "https://subroutine.example.com"

P = subroutine.permissions
Client = subroutine.clients.base.Client


# --- The principals ----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Who:
	"""A kind of principal: whose credential, narrowed how, and arriving how."""

	key: str
	person: str
	local: bool = False
	read_only: bool = False
	scopes: tuple[str, ...] = ()
	project_scope: str | None = None
	project_write_scope: str | None = None
	pinned: bool = False


#: The principals every act is driven as: the five roles, a superuser and somebody from another
#: workspace; a credential narrowed each way a credential can be; an agent; a read-only connection;
#: and the terminal's two local principals, which present no credential at all.
PRINCIPALS: tuple[Who, ...] = (
	Who("superuser", "laurence"),
	Who("owner", "keanu"),
	Who("admin", "carrieanne"),
	Who("member", "hugo"),
	Who("contributor", "gloria"),
	Who("viewer", "joe"),
	Who("outsider", "marcus"),
	Who("scoped to reading tasks", "hugo", scopes=(P.TASK_READ,)),
	Who("scoped to reading tasks and commenting", "hugo", scopes=(P.TASK_READ, P.COMMENT_WRITE)),
	Who("scoped to filing tasks", "hugo", scopes=(P.TASK_WRITE,)),
	Who("narrowed to another project", "hugo", project_scope="ops"),
	Who("writing only in another project", "hugo", project_write_scope="ops"),
	Who("pinned to another workspace", "hugo", pinned=True),
	Who("superuser pinned to another workspace", "laurence", pinned=True),
	Who("agent", "claude"),
	Who("read-only connection", "laurence", read_only=True),
	Who("local superuser", "laurence", local=True),
	Who("local member", "hugo", local=True),
)


# --- The cast ----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Canary:
	"""Text planted where only some principals may read it, and what reading it takes."""

	text: str
	verb: str
	place: str


@dataclasses.dataclass
class Cast:
	"""One installation with everybody the matrix acts as, and everything it acts on."""

	session: sqlalchemy.orm.Session
	factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session]
	application: typing.Any
	slug: str
	people: dict[str, subroutine.db.models.identity.User]
	secrets: dict[str, str]
	places: dict[str, tuple[uuid.UUID, typing.Any]]
	refs: dict[str, int] = dataclasses.field(default_factory=dict)
	ids: dict[str, str] = dataclasses.field(default_factory=dict)
	names: dict[str, str] = dataclasses.field(default_factory=dict)
	canaries: list[Canary] = dataclasses.field(default_factory=list)

	def plant (self, text: str, verb: str, place: str) -> str:
		"""Record text that only a principal holding this verb here may read, and return it."""

		self.canaries.append(Canary(text, verb, place))

		return text


@pytest.fixture
def cast (session: sqlalchemy.orm.Session) -> Cast:
	"""MetaCortex, its people and its work, reachable by every transport.

	**Built through the product's own clients where one offers the act**, so the targets are what the
	product makes rather than rows a fixture assembled; through the domain only for the accounts,
	memberships and credentials the principals are made of.
	"""

	suffix = uuid.uuid4().hex[:6]
	setup = subroutine.domain.bootstrap.initialise(
		session,
		username=f"laurence-{suffix}",
		instance_name="MetaCortex",
		workspace_title="MetaCortex",
		workspace_slug=f"metacortex-{suffix}",
	)
	workspace = setup.workspace
	people = {"laurence": setup.user}

	# **Tank is acted on and never acts**, so an act on somebody else's account is never a principal
	# acting on its own.
	for person in ("keanu", "carrieanne", "hugo", "gloria", "joe", "marcus", "tank"):
		people[person] = subroutine.domain.users.create(session, username=f"{person}-{suffix}")

	people["claude"] = subroutine.domain.users.create(
		session,
		username=f"claude-{suffix}",
		is_service_account=True,
		responsible_user_id=people["hugo"].id,
	)

	for person, role in (
		("keanu", "owner"),
		("carrieanne", "admin"),
		("hugo", "member"),
		("gloria", "contributor"),
		("joe", "viewer"),
		("claude", "member"),
		("tank", "member"),
	):
		subroutine.domain.workspaces.add_member(session, workspace, people[person], role_key=role)

	zion = subroutine.domain.workspaces.create(
		session, slug=f"zion-{suffix}", title="Zion", owner=people["marcus"]
	)
	subroutine.domain.workspaces.add_member(session, zion, people["hugo"], role_key="member")
	projects: dict[str, typing.Any] = {}

	for key, visibility in (("web", "public"), ("ops", "public"), ("vault", "private")):
		projects[key] = subroutine.domain.projects.create(
			session,
			workspace_id=workspace.id,
			key=key,
			title=f"{key.title()} {suffix}",
			visibility=visibility,
			owner_id=setup.user.id,
		)

	session.flush()
	secrets: dict[str, str] = {}

	for who in PRINCIPALS:
		if who.local:
			continue

		narrowing: dict[str, typing.Any] = {}

		if who.scopes:
			narrowing["scopes"] = who.scopes

		if who.project_scope:
			narrowing["project_scope"] = [str(projects[who.project_scope].id)]

		if who.project_write_scope:
			narrowing["project_write_scope"] = [str(projects[who.project_write_scope].id)]

		if who.pinned:
			narrowing["workspace_id"] = zion.id

		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=people[who.person], title=who.key, **narrowing
		)
		secrets[who.key] = issued.value.get_secret_value()

	for person in ("marcus", "tank"):
		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=people[person], title=f"{person}'s own"
		)
		secrets[f"{person}'s own"] = issued.value.get_secret_value()

	session.flush()
	factory = api_support.factory_for(session)
	cast = Cast(
		session=session,
		factory=factory,
		application=api_support.build_app(factory),
		slug=workspace.slug,
		people=people,
		secrets=secrets,
		places={
			"metacortex": (workspace.id, None),
			**{key: (workspace.id, project) for key, project in projects.items()},
			"zion": (zion.id, subroutine.domain.bootstrap.inbox_for(session, zion)),
		},
	)
	cast.plant(projects["vault"].title, P.PROJECT_READ, "vault")
	_furnish(cast, suffix, zion.slug)

	return cast


def _furnish (cast: Cast, suffix: str, zion: str) -> None:
	"""Make the work every act is aimed at, each through the client of whoever would make it."""

	laurence = _local_client(cast, Who("superuser", "laurence"), token=cast.secrets["superuser"])
	hugo = _local_client(cast, Who("member", "hugo"), token=cast.secrets["member"])
	marcus = _local_client(cast, Who("outsider", "marcus"), token=cast.secrets["marcus's own"])
	tank = _local_client(cast, Who("tank", "tank"), token=cast.secrets["tank's own"])
	slug = cast.slug

	def task (key: str, title: str, project: str, then: str = "", **more: typing.Any) -> int:
		"""File a task as Laurence, plant its title, and remember its number.

		``then`` follows the title in the line captured, for the words a capture reads as fields.
		"""

		text = cast.plant(f"{title} {suffix}", P.TASK_READ, project)
		cast.refs[key] = laurence.capture(
			text=f"{text} {then}".strip(), project=project, workspace=slug, **more
		).task.ref

		return cast.refs[key]

	with laurence, hugo, marcus, tank:
		laurence.update_workspace(
			slug,
			description=cast.plant(f"Our company {suffix}", P.WORKSPACE_READ, "metacortex"),
		)
		task("task", "Fix the deploy script", "web")
		task("other", "Write the release notes", "web")
		task("blocked", "Ship the new home page", "web")
		task("blocker", "Approve the design", "web")
		task("repeat", "Water the plants", "web", then="by 2031-12-01", recurrence="every 7 days")
		task("vault", f"Pay the dojo invoice #hush{suffix}", "vault")
		task("ops", "Rotate the keys", "ops")
		parent = task("parent", "Rewrite the home page copy", "web")
		task("beneath the trash", "Draft the hero section", "web", parent=parent)
		laurence.discard(ref=parent, workspace=slug)

		cast.refs["document"] = laurence.create_document(
			title=cast.plant(f"How we deploy {suffix}", P.TASK_READ, "web"),
			body=cast.plant(f"Push, then wait {suffix}", P.TASK_READ, "web"),
			project="web",
			workspace=slug,
		).ref
		laurence.create_document(
			title=cast.plant(f"The dojo's terms {suffix}", P.TASK_READ, "vault"),
			body="Private.",
			project="vault",
			workspace=slug,
		)

		laurence.link(
			ref=cast.refs["blocker"],
			link_type="blocks",
			target=cast.refs["blocked"],
			workspace=slug,
		)
		cast.ids["link"] = str(
			next(
				link.id
				for link in laurence.links(ref=cast.refs["blocked"], workspace=slug)
				if link.link_type == "blocks"
			)
		)

		laurence.remark(
			ref=cast.refs["vault"],
			body=cast.plant(f"Paid in cash {suffix}", P.COMMENT_READ, "vault"),
			workspace=slug,
		)
		remarked = hugo.remark(
			ref=cast.refs["task"],
			body=cast.plant(f"The script needs a key {suffix}", P.COMMENT_READ, "web"),
			workspace=slug,
		)
		cast.ids["hugo's comment"] = str(remarked.id)

		# **An edited comment is where the change feed carries a comment's words** (S3 of `#4506`):
		# the event holds the body before and after.
		edited = api_support.call(
			cast.application,
			"PATCH",
			f"/v1/comments/{remarked.id}",
			json={"body": cast.plant(f"It needs two keys {suffix}", P.COMMENT_READ, "web")},
			headers={"authorization": f"Bearer {cast.secrets['member']}"},
		)

		assert edited.status_code == 200, edited.text

		cast.ids["hidden tag"] = next(
			str(entry.id) for entry in laurence.tags(workspace=slug) if entry.name == f"hush{suffix}"
		)
		cast.ids["tag"] = str(laurence.create_tag(name=f"later{suffix}", workspace=slug).id)
		cast.ids["status"] = str(
			laurence.create_status(
				entity_type="task", key="parked", label="Parked", category="todo", workspace=slug
			).id
		)
		cast.ids["link type"] = str(
			laurence.create_link_type(
				key="mirrors",
				title="mirrors",
				inverse_title="is mirrored by",
				category="describing",
				workspace=slug,
			).id
		)
		cast.names["view"] = laurence.save_view(
			title="Deploys", arrangement="list", q="deploy", shared=True, workspace=slug
		).key

		laurence.share_project("vault", username=cast.people["carrieanne"].username, workspace=slug)

		cast.names["deleted"] = f"nebuchadnezzar-{suffix}"
		laurence.create_workspace(slug=cast.names["deleted"], title="Nebuchadnezzar")
		laurence.delete_workspace(cast.names["deleted"])

		tank.create_calendar(title="Tank's diary", workspace=slug)
		cast.names["tank's feed"] = tank.calendars()[0].prefix
		cast.names["tank's token"] = next(
			token.prefix for token in tank.tokens() if token.title == "tank's own"
		)
		cast.refs["zion"] = marcus.capture(
			text=cast.plant(f"Collect the package {suffix}", P.TASK_READ, "zion"), workspace=zion
		).task.ref

	for name in ("tank", "claude"):
		cast.names[name] = cast.people[name].username


# --- The acts ----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Drive:
	"""How the matrix drives one act of :data:`subroutine.acts.ACTS`, joined to it by its key.

	``call`` is the act through a client, and ``tool`` the agent tool and arguments it is asked
	through, where a tool does it. ``route`` is the request ``call_api`` makes, for an act whose
	request names the principal making it; every other act's is recorded from the HTTP client.
	"""

	call: typing.Callable[[Client, Cast], object]
	tool: tuple[str, typing.Callable[[Cast], dict[str, typing.Any]]] | None = None
	route: typing.Callable[[Cast, "Who"], dict[str, typing.Any]] | None = None


@dataclasses.dataclass(frozen=True)
class Act:
	"""One act of the table as the matrix drives it, on one of the cast's places.

	**The table describes, and this drives** (Simon, 2026-10-10, `#4579`): the words, verbs, place
	and flags are :data:`subroutine.acts.ACTS`', which ``docs/permissions.md`` is written from, and
	the driving is :data:`DRIVES`'. ``verbs`` and ``place`` are what the permission check is asked,
	``place`` being the cast's place for the act's kind of place. ``theirs`` names the person in the
	cast whose own thing an act on somebody's own thing is on.
	"""

	described: subroutine.acts.Act
	place: str | None
	call: typing.Callable[[Client, Cast], object]
	tool: tuple[str, typing.Callable[[Cast], dict[str, typing.Any]]] | None
	route: typing.Callable[[Cast, "Who"], dict[str, typing.Any]] | None
	theirs: str | None

	@property
	def key (self) -> str:
		"""Return the act's key, as the table names it."""

		return self.described.key

	@property
	def says (self) -> str:
		"""Return what the act does, in the table's words."""

		return self.described.says

	@property
	def verbs (self) -> tuple[str, ...]:
		"""Return the verbs the act asks."""

		return self.described.verbs

	@property
	def reads (self) -> bool:
		"""Say whether the act changes nothing."""

		return self.described.reads

	@property
	def rule (self) -> str | None:
		"""Return the act's own rule, which can refuse what its verbs allow."""

		return self.described.rule

	@property
	def secret (self) -> bool:
		"""Say whether the act's answer is a credential, a sign-in link or a calendar feed."""

		return self.described.secret

	def beyond_the_agent_tools (self, who: "Who") -> bool:
		"""Say whether the agent tools' ceiling refuses this act to ``who``, whatever it holds.

		Decision `#4520`: everybody, for an act the table says never goes through them; and on
		somebody's own thing, everybody but them (`SR#4563`).
		"""

		return self.described.never_through_the_agent_tools() or (
			self.theirs is not None and who.person != self.theirs
		)


def _exporting (kind: str) -> typing.Callable[[Client, Cast], object]:
	"""Return an act taking away every row of one kind, read to the end."""

	def call (client: Client, cast: Cast) -> object:
		"""Read the whole export, since a client yields it a page at a time."""

		return list(client.export(kind, workspace=cast.slug))

	return call


def _sent (
	method: str,
	path: typing.Callable[[Cast], str],
	*,
	body: dict[str, typing.Any] | None = None,
	in_the_workspace: bool = False,
) -> typing.Callable[[Client, Cast], object]:
	"""Return an act only HTTP reaches, sent through ``call_api`` as every client can (`SR#4879`).

	A refusal comes back as a status rather than raised, so it is read back into the error its
	problem document names, as the HTTP client reads every other refusal. ``in_the_workspace``
	names the cast's workspace, which a principal reaching two needs to say.
	"""

	def call (client: Client, cast: Cast) -> object:
		"""Send the request, and raise what a refusal names."""

		answered = client.call_api(
			method=method,
			path=path(cast),
			body=body,
			query={"workspace_id": cast.slug} if in_the_workspace else None,
		)

		if answered.status >= 400:
			raise subroutine.errors.from_problem(json.loads(answered.text), status=answered.status)

		return answered.text

	return call


def _no_copy (*arguments: typing.Any, **keywords: typing.Any) -> subroutine.db.backup.Backup:
	"""Take no copy, and say one was taken (`SR#4879`).

	The matrix asks who may take a backup. A real copy cannot be taken inside the savepoint each
	drive runs in - SQLite will not vacuum inside a transaction - and would be taken a dozen times.
	"""

	return subroutine.db.backup.Backup(
		path=pathlib.Path("matrix.db"),
		taken_at=datetime.datetime.now(datetime.UTC),
		schema_head="matrix",
		size_bytes=0,
		profile=None,
	)


#: The cast's place for each kind of place an act lands on.
CAST_PLACES: dict[str | None, str | None] = {
	None: None,
	subroutine.acts.INSTALLATION: "instance",
	subroutine.acts.WORKSPACE: "metacortex",
	subroutine.acts.PROJECT: "web",
	subroutine.acts.PRIVATE_PROJECT: "vault",
}

#: Acts driven on the cast's other project, which the credentials narrowed to it may act on.
ON_OPS = frozenset({"rename project", "move project"})

#: Whose own thing each act on somebody's own thing is on, in the cast.
THEIRS: dict[str, str] = {
	"uncomment": "hugo",
	"forget saved view": "laurence",
	"stop sharing a view": "laurence",
	"reveal a private project": "laurence",
	"share project": "laurence",
	"hide a project": "laurence",
	"move work into a private project": "laurence",
}

#: How each act of :data:`subroutine.acts.ACTS` is driven, by its key.
DRIVES: dict[str, Drive] = {
	"identity": Drive(
		lambda c, s: c.identity(),
	),
	"reference": Drive(
		lambda c, s: c.reference("agent"),
	),
	"meta": Drive(
		lambda c, s: c.meta(workspace=s.slug),
	),
	"me": Drive(
		lambda c, s: c.me(),
		tool=("subroutine_whoami", lambda s: {}),
	),
	"user": Drive(
		lambda c, s: c.user(username=s.names["tank"]),
	),
	"users": Drive(
		lambda c, s: c.users(),
	),
	"tokens": Drive(
		lambda c, s: c.tokens(),
	),
	"calendars": Drive(
		lambda c, s: c.calendars(),
	),
	"instance workspaces": Drive(
		lambda c, s: c.instance_workspaces(),
	),
	"unreachable projects": Drive(
		lambda c, s: c.unreachable_projects(),
	),
	"unadministered workspaces": Drive(
		lambda c, s: c.unadministered_workspaces(),
	),
	"unowned workspaces": Drive(
		lambda c, s: c.unowned_workspaces(),
	),
	"workspace settings": Drive(
		lambda c, s: c.workspace_settings(workspace=s.slug),
	),
	"members": Drive(
		lambda c, s: c.members(workspace=s.slug),
	),
	"statuses": Drive(
		lambda c, s: c.statuses(workspace=s.slug),
	),
	"link types": Drive(
		lambda c, s: c.link_types(workspace=s.slug),
	),
	"tags": Drive(
		lambda c, s: c.tags(workspace=s.slug),
	),
	"saved views": Drive(
		lambda c, s: c.saved_views(workspace=s.slug),
	),
	"saved view": Drive(
		lambda c, s: c.saved_view(key=s.names["view"], workspace=s.slug),
	),
	"projects": Drive(
		lambda c, s: c.projects(workspace=s.slug),
	),
	"project settings": Drive(
		lambda c, s: c.project_settings("web", workspace=s.slug),
	),
	"project members": Drive(
		lambda c, s: c.project_members("vault", workspace=s.slug),
	),
	"agenda": Drive(
		lambda c, s: c.agenda(workspace=s.slug),
	),
	"count tasks": Drive(
		lambda c, s: c.count_tasks(workspace=s.slug),
	),
	"tasks": Drive(
		lambda c, s: c.tasks(workspace=s.slug),
		tool=("subroutine_list", lambda s: {}),
	),
	"search": Drive(
		lambda c, s: c.tasks(workspace=s.slug, q="key"),
		tool=("subroutine_search", lambda s: {"q": "key"}),
	),
	"documents": Drive(
		lambda c, s: c.documents(workspace=s.slug),
	),
	"count documents": Drive(
		lambda c, s: c.count_documents(workspace=s.slug),
	),
	"changes": Drive(
		lambda c, s: c.changes(workspace=s.slug),
		tool=("subroutine_changes", lambda s: {}),
	),
	"journal": Drive(
		lambda c, s: c.journal(workspace=s.slug),
		tool=("subroutine_journal", lambda s: {}),
	),
	**{f"export {kind}": Drive(_exporting(kind)) for kind in ("workspace", "tasks", "comments", "events")},
	"task": Drive(
		lambda c, s: c.task(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["task"]}),
	),
	"private task": Drive(
		lambda c, s: c.task(ref=s.refs["vault"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["vault"]}),
	),
	"document": Drive(
		lambda c, s: c.document(ref=s.refs["document"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["document"]}),
	),
	"comments": Drive(
		lambda c, s: c.comments(ref=s.refs["task"], workspace=s.slug),
	),
	"history": Drive(
		lambda c, s: c.history(ref=s.refs["task"], workspace=s.slug),
	),
	"item journal": Drive(
		lambda c, s: c.item_journal(ref=s.refs["task"], workspace=s.slug),
	),
	"links": Drive(
		lambda c, s: c.links(ref=s.refs["blocked"], workspace=s.slug),
	),
	"backlinks": Drive(
		lambda c, s: c.backlinks(ref=s.refs["task"], workspace=s.slug),
	),
	"beneath": Drive(
		lambda c, s: c.beneath(ref=s.refs["task"], workspace=s.slug),
	),
	"governing": Drive(
		lambda c, s: c.governing(ref=s.refs["task"], workspace=s.slug),
	),
	"verifications": Drive(
		lambda c, s: c.verifications(ref=s.refs["task"], workspace=s.slug),
	),
	"proposed links": Drive(
		lambda c, s: c.proposed_links(ref=s.refs["task"], workspace=s.slug),
	),
	"occurrences": Drive(
		lambda c, s: c.occurrences(ref=s.refs["repeat"], workspace=s.slug),
	),
	"capture": Drive(
		lambda c, s: c.capture(text="Order a new phone", project="web", workspace=s.slug),
		tool=("subroutine_add", lambda s: {"text": "Order a new phone +web"}),
	),
	"update": Drive(
		lambda c, s: c.update(ref=s.refs["task"], title="Fix it", workspace=s.slug),
		tool=("subroutine_update", lambda s: {"ref": s.refs["task"], "title": "Fix it"}),
	),
	"schedule": Drive(
		lambda c, s: c.schedule(ref=s.refs["task"], starts=datetime.date(2031, 5, 6), workspace=s.slug),
	),
	"complete": Drive(
		lambda c, s: c.complete(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["task"]}),
	),
	"complete beneath the trash": Drive(
		lambda c, s: c.complete(ref=s.refs["beneath the trash"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["beneath the trash"]}),
	),
	"skip": Drive(
		lambda c, s: c.skip(ref=s.refs["repeat"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["repeat"], "skip": True}),
	),
	"move": Drive(
		lambda c, s: c.move(ref=s.refs["task"], parent=s.refs["other"], workspace=s.slug),
	),
	"claim": Drive(
		lambda c, s: c.claim(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_claim", lambda s: {"ref": s.refs["task"]}),
	),
	"release": Drive(
		lambda c, s: c.release(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_claim", lambda s: {"ref": s.refs["task"], "release": True}),
	),
	"discard": Drive(
		lambda c, s: c.discard(ref=s.refs["task"], workspace=s.slug),
	),
	"undiscard": Drive(
		lambda c, s: c.undiscard(ref=s.refs["parent"], workspace=s.slug),
	),
	"verify": Drive(
		lambda c, s: c.verify(ref=s.refs["task"], passed=True, summary="Ran it.", workspace=s.slug),
	),
	"link": Drive(
		lambda c, s: c.link(
			ref=s.refs["task"], link_type="blocks", target=s.refs["other"], workspace=s.slug
		),
		tool=(
			"subroutine_link",
			lambda s: {"ref": s.refs["task"], "other": s.refs["other"], "type": "blocks"},
		),
	),
	"link to private work": Drive(
		lambda c, s: c.link(
			ref=s.refs["task"], link_type="blocks", target=s.refs["vault"], workspace=s.slug
		),
		tool=(
			"subroutine_link",
			lambda s: {"ref": s.refs["task"], "other": s.refs["vault"], "type": "blocks"},
		),
	),
	"unlink": Drive(
		lambda c, s: c.unlink(ref=s.refs["blocked"], link_id=s.ids["link"], workspace=s.slug),
	),
	"remark": Drive(
		lambda c, s: c.remark(ref=s.refs["task"], body="Seen.", workspace=s.slug),
		tool=("subroutine_comment", lambda s: {"ref": s.refs["task"], "body": "Seen."}),
	),
	"uncomment": Drive(
		lambda c, s: c.uncomment(
			ref=s.refs["task"], comment_id=s.ids["hugo's comment"], workspace=s.slug
		),
	),
	"create document": Drive(
		lambda c, s: c.create_document(title="On call", body="Rota.", project="web", workspace=s.slug),
		tool=(
			"subroutine_document",
			lambda s: {"title": "On call", "body": "Rota.", "project": "web"},
		),
	),
	"update document": Drive(
		lambda c, s: c.update_document(ref=s.refs["document"], title="Deploying", workspace=s.slug),
		tool=("subroutine_document", lambda s: {"ref": s.refs["document"], "title": "Deploying"}),
	),
	"create status": Drive(
		lambda c, s: c.create_status(
			entity_type="task", key="waiting", label="Waiting", category="todo", workspace=s.slug
		),
	),
	"update status": Drive(
		lambda c, s: c.update_status(which=s.ids["status"], label="On hold"),
	),
	"delete status": Drive(
		lambda c, s: c.delete_status(which=s.ids["status"]),
	),
	"create link type": Drive(
		lambda c, s: c.create_link_type(
			key="echoes",
			title="echoes",
			inverse_title="is echoed by",
			category="describing",
			workspace=s.slug,
		),
	),
	"update link type": Drive(
		lambda c, s: c.update_link_type(which=s.ids["link type"], title="reflects"),
	),
	"delete link type": Drive(
		lambda c, s: c.delete_link_type(which=s.ids["link type"]),
	),
	"create tag": Drive(
		lambda c, s: c.create_tag(name="urgent", workspace=s.slug),
	),
	"update tag": Drive(
		lambda c, s: c.update_tag(which=s.ids["tag"], name="someday"),
	),
	"delete tag": Drive(
		lambda c, s: c.delete_tag(which=s.ids["tag"]),
	),
	"rename a hidden tag": Drive(
		lambda c, s: c.update_tag(which=s.ids["hidden tag"], name="renamed"),
	),
	"save view": Drive(
		lambda c, s: c.save_view(title="Notes", arrangement="list", q="notes", workspace=s.slug),
	),
	"update saved view": Drive(
		lambda c, s: c.update_saved_view(key=s.names["view"], title="Deploying", workspace=s.slug),
	),
	"forget saved view": Drive(
		lambda c, s: c.forget_saved_view(key=s.names["view"], workspace=s.slug),
	),
	"stop sharing a view": Drive(
		lambda c, s: c.update_saved_view(key=s.names["view"], shared=False, workspace=s.slug),
	),
	"create project": Drive(
		lambda c, s: c.create_project(key="docs", title="Docs", workspace=s.slug),
		tool=("subroutine_project", lambda s: {"key": "docs", "title": "Docs"}),
	),
	"update project": Drive(
		lambda c, s: c.update_project("web", title="Website", workspace=s.slug),
	),
	"rename project": Drive(
		lambda c, s: c.rename_project("ops", key="infra", workspace=s.slug),
	),
	"move project": Drive(
		lambda c, s: c.move_project("ops", parent="web", workspace=s.slug),
	),
	"reveal a private project": Drive(
		lambda c, s: c.update_project("vault", visibility="public", workspace=s.slug),
	),
	"share project": Drive(
		lambda c, s: c.share_project("vault", username=s.names["tank"], workspace=s.slug),
	),
	"unshare project": Drive(
		lambda c, s: c.unshare_project(
			"vault", username=s.people["carrieanne"].username, workspace=s.slug
		),
	),
	"hide a project": Drive(
		lambda c, s: c.update_project("web", visibility="private", workspace=s.slug),
	),
	"move work into a private project": Drive(
		lambda c, s: c.update(ref=s.refs["task"], project="vault", workspace=s.slug),
		tool=("subroutine_update", lambda s: {"ref": s.refs["task"], "project": "vault"}),
	),
	"create workspace": Drive(
		lambda c, s: c.create_workspace(slug=f"nebula-{s.slug}", title="Nebula"),
	),
	"update workspace": Drive(
		lambda c, s: c.update_workspace(s.slug, title="MetaCortex Inc"),
	),
	"rename workspace": Drive(
		lambda c, s: c.rename_workspace(s.slug, slug=f"mc-{s.slug}"),
	),
	"delete workspace": Drive(
		lambda c, s: c.delete_workspace(s.slug),
	),
	"restore workspace": Drive(
		lambda c, s: c.restore_workspace(s.names["deleted"]),
	),
	"add member": Drive(
		lambda c, s: c.add_member(
			username=s.people["marcus"].username, role="viewer", workspace=s.slug
		),
	),
	"set member role": Drive(
		lambda c, s: c.set_member_role(username=s.names["tank"], role="viewer", workspace=s.slug),
	),
	"remove member": Drive(
		lambda c, s: c.remove_member(username=s.names["tank"], workspace=s.slug),
	),
	"set own timezone": Drive(
		lambda c, s: c.set_timezone(username=c.me().user.username, timezone="Europe/London"),
		route=lambda s, who: {
			"method": "PATCH",
			"path": f"/v1/users/{s.people[who.person].username}",
			"body": {"timezone": "Europe/London"},
		},
	),
	"issue own token": Drive(
		lambda c, s: c.issue_token(title="Spare"),
	),
	"create calendar": Drive(
		lambda c, s: c.create_calendar(title="Diary", workspace=s.slug),
	),
	"create user": Drive(
		lambda c, s: c.create_user(username=f"dozer-{s.slug}"),
	),
	"issue token for somebody": Drive(
		lambda c, s: c.issue_token(title="Tank's spare", username=s.names["tank"]),
	),
	"sign-in link for somebody": Drive(
		lambda c, s: c.create_login_link(username=s.names["tank"]),
	),
	"sign somebody out": Drive(
		lambda c, s: c.sign_out_everywhere(username=s.names["tank"]),
	),
	"revoke somebody's token": Drive(
		lambda c, s: c.revoke_token(id_or_prefix=s.names["tank's token"]),
	),
	"reset somebody's feed": Drive(
		lambda c, s: c.reset_calendar(id_or_prefix=s.names["tank's feed"]),
	),
	"revoke somebody's feed": Drive(
		lambda c, s: c.revoke_calendar(id_or_prefix=s.names["tank's feed"]),
	),
	"deactivate somebody": Drive(
		lambda c, s: c.set_active(username=s.names["tank"], active=False),
	),
	"transfer an agent": Drive(
		lambda c, s: c.transfer_agent(username=s.names["claude"], to=s.names["tank"]),
	),
	"update instance": Drive(
		lambda c, s: c.update_instance(name="MetaCortex Inc"),
	),
	# **What only HTTP reaches** (`SR#4879`), sent through ``call_api``, which every client has.
	"backups": Drive(_sent("GET", lambda s: "/v1/admin/backups")),
	"take backup": Drive(_sent("POST", lambda s: "/v1/admin/backups")),
	"workspaces": Drive(_sent("GET", lambda s: "/v1/workspaces")),
	"workspace": Drive(_sent("GET", lambda s: f"/v1/workspaces/{s.slug}")),
	"project": Drive(_sent("GET", lambda s: "/v1/projects/web", in_the_workspace=True)),
	"project history": Drive(
		_sent("GET", lambda s: "/v1/projects/web/events", in_the_workspace=True)
	),
	"delete project": Drive(_sent("DELETE", lambda s: "/v1/projects/web", in_the_workspace=True)),
	"restore project": Drive(
		_sent("POST", lambda s: "/v1/projects/web/restore", in_the_workspace=True)
	),
	"edit comment": Drive(
		_sent(
			"PATCH",
			lambda s: "/v1/comments/" + s.ids["hugo's comment"],
			body={"body": "Rewritten, as its author"},
		)
	),
	"colour workspace": Drive(
		lambda c, s: c.update_workspace(s.slug, settings={"appearance.colour": "green"}),
	),
}


def _driven (described: subroutine.acts.Act) -> Act:
	"""Return an act of the table as the matrix drives it."""

	drive = DRIVES[described.key]

	return Act(
		described=described,
		place="ops" if described.key in ON_OPS else CAST_PLACES[described.place],
		call=drive.call,
		tool=drive.tool,
		route=drive.route,
		theirs=THEIRS.get(described.key),
	)


#: Every act of the table that is driven here. One that is not is a failure of its own, in
#: ``test_every_act_is_driven_and_nothing_else``, rather than an error collecting this file.
ACTS: tuple[Act, ...] = tuple(_driven(act) for act in subroutine.acts.ACTS if act.key in DRIVES)

#: Client methods that are not acts, each with why.
NOT_AN_ACT: dict[str, str] = {
	"read_repeat": "Reads a repeat's words on this machine, and asks the instance nothing.",
	"close": "Closes the client.",
}

#: Cells known to disagree today, by act and principal, each naming the item whose build makes it
#: agree. A principal of ``"*"`` stands for every principal driving that act. Deleting an entry is
#: what closes it, and an entry whose cell agrees fails as stale.
#:
#: **Empty since `SR#4567`**, whose build closed the last three: the terminal's exemptions for its
#: local principal (S8 and D6 of `#4506`), which signed another account out and minted its
#: credential and sign-in link where the same account over HTTP was refused.
KNOWN: dict[tuple[str, str], str] = {}


# --- The decision, written down ----------------------------------------------------------------

#: Every role in MetaCortex, through a credential nothing narrows, and the terminal's two principals.
_ROLES = frozenset(
	{
		"superuser",
		"owner",
		"admin",
		"member",
		"contributor",
		"viewer",
		"agent",
		"local superuser",
		"local member",
	}
)

#: Who administers the installation: Laurence, through a credential nothing narrows.
_INSTALLATION = frozenset({"superuser", "local superuser"})

#: Who administers MetaCortex: its owners, its administrator, and Laurence.
_ADMINISTRATORS = _INSTALLATION | {"owner", "admin"}

#: Who reaches the private project: Laurence, who made it, and Carrie-Anne, who it is shared with.
_VAULT = _INSTALLATION | {"admin"}

#: Hugo's credentials, each narrowed one way.
_READING = frozenset({"scoped to reading tasks", "scoped to reading tasks and commenting"})
_OPS = frozenset({"narrowed to another project", "writing only in another project"})

#: The decision, written down: who may ask each verb on each place the acts and the canaries use.
#:
#: **The matrix's oracle, and not the code's own decision**, which a fault would make agree with
#: itself on every transport: measured, removing the domain's read-only refusal passed a matrix that
#: asked the decision what to expect. ``test_the_decision_is_as_written`` holds the code to this, and
#: every act holds every transport to it. **A read-only connection is a rule rather than a column**
#: (decision `#4510`): its person's own reads, and nothing else. An item that changes the model
#: changes a line here, as `#4557` did for the superuser who read Zion without belonging to it.
DECIDED: dict[tuple[str, str], frozenset[str]] = {
	(P.INSTANCE_ADMIN, "instance"): _INSTALLATION,
	(P.INSTANCE_USER_CREATE, "instance"): _INSTALLATION,
	(P.INSTANCE_WORKSPACE_CREATE, "instance"): _INSTALLATION,
	(P.WORKSPACE_READ, "metacortex"): _ROLES | _OPS,
	(P.WORKSPACE_WRITE, "metacortex"): _ADMINISTRATORS,
	(P.WORKSPACE_ADMIN, "metacortex"): _ADMINISTRATORS,
	(P.WORKSPACE_DELETE, "metacortex"): _INSTALLATION | {"owner"},
	(P.USER_ADMIN, "metacortex"): _ADMINISTRATORS,
	(P.STATUS_WRITE, "metacortex"): _ADMINISTRATORS,
	(P.LINK_TYPE_WRITE, "metacortex"): _ADMINISTRATORS,
	(P.TAG_WRITE, "metacortex"): _ROLES - {"contributor", "viewer"},
	(P.PROJECT_READ, "metacortex"): _ROLES | _OPS,
	(P.PROJECT_WRITE, "metacortex"): _ROLES - {"contributor", "viewer"},
	(P.TASK_READ, "metacortex"): _ROLES | _READING | _OPS,
	(P.PROJECT_READ, "web"): _ROLES | {"writing only in another project"},
	(P.PROJECT_WRITE, "web"): _ROLES - {"contributor", "viewer"},
	(P.TASK_READ, "web"): _ROLES | _READING | {"writing only in another project"},
	(P.TASK_WRITE, "web"): _ROLES - {"viewer"} | {"scoped to filing tasks"},
	(P.TASK_DELETE, "web"): _ADMINISTRATORS,
	(P.PROJECT_DELETE, "web"): _ADMINISTRATORS,
	(P.COMMENT_READ, "web"): _ROLES | {"writing only in another project"},
	(P.COMMENT_WRITE, "web"): _ROLES - {"viewer"} | {"scoped to reading tasks and commenting"},
	(P.PROJECT_WRITE, "ops"): _ROLES - {"contributor", "viewer"} | _OPS,
	(P.TASK_READ, "ops"): _ROLES | _READING | _OPS,
	(P.TASK_READ, "vault"): _VAULT,
	(P.TASK_WRITE, "vault"): _VAULT,
	(P.PROJECT_READ, "vault"): _VAULT,
	(P.PROJECT_WRITE, "vault"): _VAULT,
	(P.COMMENT_READ, "vault"): _VAULT,
	(P.TASK_READ, "zion"): frozenset(
		{
			"member",
			"outsider",
			"writing only in another project",
			"pinned to another workspace",
			"local member",
		}
	)
	| _READING,
}


def _plain (who: Who) -> Who:
	"""Return the principal holding this one's person's credential with nothing narrowing it."""

	return next(
		one
		for one in PRINCIPALS
		if one.person == who.person
		and not (one.local or one.read_only or one.pinned or one.scopes)
		and one.project_scope is None
		and one.project_write_scope is None
	)


def _allowed (who: Who, verb: str, place: str, *, reading: bool) -> bool:
	"""Say whether the decision, as written down, lets this principal ask this verb here.

	**A read is a read verb, or an installation verb asked by an act that only reads** (decision
	`#4510`): the installation's verbs gate its listings as well as its changes.
	"""

	if who.read_only:
		reads = verb in P.READS or (reading and verb in P.INSTANCE_LEVEL)

		return reads and _allowed(_plain(who), verb, place, reading=reading)

	return who.key in DECIDED[(verb, place)]


# --- The transports ----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Outcome:
	"""What one transport answered: refused or not, with what status and words, and all it said."""

	refused: bool
	status: int | None
	detail: str | None
	said: str

	@property
	def kind (self) -> tuple[bool, int | None, str | None]:
		"""Return what two transports must agree on: answered, or refused with the same status.

		**A refusal of the permission check is told apart by its words as well**, since one status,
		403, carries every reason the check gives (`#4506` F P5: "403 with its failure enum"). A 404 or
		a 422 is compared by status alone: the transports word a missing item each their own way, and
		that is not a disagreement about whether it is there.
		"""

		return (self.refused, self.status, self.detail if self.status == 403 else None)

	def __str__ (self) -> str:
		"""Say it in one line, for a report."""

		if not self.refused:
			return "answered"

		return f"refused {self.status or '(no status)'}: {self.detail}"


#: The failures the agent tools rendered, newest last, so a tool's refusal can be given a status: a
#: tool answers in words alone.
RENDERED: list[BaseException] = []


def _status_of (failure: BaseException | None) -> int | None:
	"""Return the status a tool's failure stands for.

	**A tool refuses a missing item as a** ``LookupError`` **and an unusable argument as a**
	``ValueError``, in its own words, since an agent reads no status: those are a 404 and a 422 in
	every other transport's terms.
	"""

	if isinstance(failure, subroutine.errors.SubroutineError):
		return failure.status

	if isinstance(failure, LookupError):
		return 404

	if isinstance(failure, ValueError):
		return 422

	return None


def _plainly (text: str) -> str:
	"""Return a refusal in the words every surface uses, so the tools' renaming is no difference."""

	return subroutine.mcp.protocol._in_this_surfaces_words(subroutine.domain.text.plain(text)).strip()


def _local_client (cast: Cast, who: Who, *, token: str | None) -> subroutine.clients.local.Client:
	"""Return the terminal's own client, for a credential or for the local person it names."""

	return subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local", read_only=who.read_only),
		subroutine.config.Settings(
			dev_mode=True,
			local_user=cast.people[who.person].username if who.local else None,
		),
		session_factory=cast.factory,
		token=token,
	)


class _Recording(httpx.BaseTransport):
	"""Pass each request to the application in process, and keep it."""

	def __init__ (self, application: typing.Any) -> None:
		"""Wrap the transport the rest of the suite drives applications with."""

		self.inner = api_support.SyncTransport(application)
		self.requests: list[httpx.Request] = []

	def handle_request (self, request: httpx.Request) -> httpx.Response:
		"""Keep the request and answer it."""

		self.requests.append(request)

		return self.inner.handle_request(request)


def _http_client (
	cast: Cast, who: Who, transport: httpx.BaseTransport
) -> subroutine.clients.http.Client:
	"""Return the HTTP client a remote connection builds, presenting this principal's credential."""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url=ADDRESS, read_only=who.read_only),
		token=cast.secrets[who.key],
		transport=transport,
		base_url=api_support.BASE_URL,
	)


def _by_client (act: Act, cast: Cast, client: Client) -> Outcome:
	"""Drive the act through a client and say what came back."""

	with client:
		try:
			answered = act.call(client, cast)

		except subroutine.errors.SubroutineError as refused:
			return Outcome(
				True, refused.status, _plainly(refused.detail), f"{refused.detail}\n{refused.hint}"
			)

	if act.reads and answered is None:
		# **A client asked for one item answers nothing where there is none**, and over HTTP that is
		# the 404 it was given, which every other transport reports as one.
		return Outcome(True, 404, None, "None")

	return Outcome(False, None, None, repr(answered))


def _from_a_tool (result: dict[str, typing.Any], *, tool: str) -> Outcome:
	"""Read a tool result: an error's first line is its refusal, and its status what was rendered.

	**A status line is read only from ``subroutine_call_api``** (`SR#4616`), whose answer opens with
	one. The change feed's tools open theirs with an event's number, so one between 400 and 999 was
	read as a refusal and its text parsed as a problem document - on PostgreSQL, whose counter is
	shared by the whole run, in some orderings and not others.
	"""

	text = "\n".join(block.get("text", "") for block in result.get("content", ()))

	if result.get("isError"):
		return Outcome(
			True,
			_status_of(RENDERED[-1] if RENDERED else None),
			_plainly(text.split("\n", 1)[0]),
			text,
		)

	if tool == "subroutine_call_api" and text[:3].isdigit() and text[3:4] in ("", " "):
		# **`call_api` answers with the status and the body**, and a refusal is a successful call.
		status = int(text[:3])

		if status >= 400:
			problem = json.loads(text[4:])

			return Outcome(True, status, _plainly(problem["detail"]), text)

	return Outcome(False, None, None, text)


def _rpc (name: str, arguments: dict[str, typing.Any]) -> str:
	"""Return one tool call as the JSON-RPC message an agent's client sends."""

	return json.dumps(
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "tools/call",
			"params": {"name": name, "arguments": arguments},
		}
	)


def _at_mcp (cast: Cast, who: Who, name: str, arguments: dict[str, typing.Any]) -> Outcome:
	"""Call one agent tool through the instance's own ``/mcp``."""

	params = {"workspace": cast.slug}

	if who.read_only:
		params["read_only"] = "true"

	answered = api_support.call(
		cast.application,
		"POST",
		"/mcp",
		params=params,
		content=_rpc(name, arguments),
		headers={
			"authorization": f"Bearer {cast.secrets[who.key]}",
			"content-type": "application/json",
		},
	)

	assert answered.status_code == 200, answered.text

	return _from_a_tool(answered.json()["result"], tool=name)


def _driving_the_cast (cast: Cast, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Have ``subroutine mcp`` in process drive an application on the cast's database.

	**An application of its own, never the cast's**: the relay stands its principal in for reading a
	credential by overriding the application's dependency, for good, so on the shared application
	every HTTP and agent-tool call after the first relay call was answered as the relay's principal
	rather than its own credential - measured, a mutant HTTP client that dropped the read-only flag
	was refused anyway. **And only the relay's own**: ``call_api`` builds an application of its own
	for the request it makes, with a session factory, and replacing that too sent it back into the
	outer one, which recursed (the trap `#4562` met).
	"""

	real = subroutine.api.app.create_app
	monkeypatch.setattr(
		subroutine.api.app,
		"create_app",
		lambda **kwargs: (
			real(**kwargs) if "session_factory" in kwargs else api_support.build_app(cast.factory)
		),
	)


def _relay (
	cast: Cast, who: Who, monkeypatch: pytest.MonkeyPatch
) -> typing.Callable[[str, dict[str, typing.Any]], Outcome]:
	"""Start ``subroutine mcp`` on a local connection, acting as this principal, in process.

	The application it drives is the cast's, by :func:`_driving_the_cast`, and the credential is the
	one a terminal would resolve for that connection.
	"""

	if who.local:
		monkeypatch.delenv("SUBROUTINE_TOKEN_LOCAL", raising=False)

	else:
		monkeypatch.setenv("SUBROUTINE_TOKEN_LOCAL", cast.secrets[who.key])

	connection = subroutine.connections.Connection(name="local", read_only=who.read_only)
	answer = subroutine.mcp.relay.answering(
		connection,
		subroutine.connections.Roster(connections=(connection,), default=connection.name),
		subroutine.config.Settings(
			dev_mode=True,
			local_user=cast.people[who.person].username if who.local else None,
		),
		workspace=cast.slug,
	)

	def call (name: str, arguments: dict[str, typing.Any]) -> Outcome:
		"""Send one tool call through the relay."""

		answered = answer(_rpc(name, arguments))

		assert answered is not None and "result" in answered, answered

		return _from_a_tool(answered["result"], tool=name)

	return call


def _isolated (cast: Cast, drive: typing.Callable[[], Outcome]) -> Outcome:
	"""Drive one transport inside a savepoint that is rolled back, so each meets the same installation.

	**On the connection, not the session.** Every client and request opens a session of its own on
	this connection, each taking a savepoint inside whatever is open there; the test's own session
	sits inside a savepoint already, and one begun through it undid none of their writes - measured,
	a deleted comment stayed deleted, so every transport after the first met a changed installation.
	"""

	RENDERED.clear()
	point = cast.session.connection().begin_nested()

	try:
		return drive()

	finally:
		point.rollback()
		cast.session.expire_all()


def _route (act: Act, cast: Cast) -> dict[str, typing.Any]:
	"""Return the request the HTTP client makes for this act, for ``call_api`` to make in turn.

	**Recorded from the client rather than written beside the act**, so ``call_api`` sends exactly
	what the HTTP client does, and the local client, which goes nowhere near a route, is what checks
	that the route is the act. Recorded as the superuser, whose request reaches the act's own route
	rather than stopping at a lookup: the last request that is not a read is the act, or the last
	read where the act only reads.
	"""

	recording = _Recording(cast.application)
	_isolated(
		cast,
		lambda: _by_client(act, cast, _http_client(cast, Who("superuser", "laurence"), recording)),
	)

	assert recording.requests, f"{act.key}: the HTTP client sent nothing"

	writes = [request for request in recording.requests if request.method != "GET"]
	request = (writes or recording.requests)[-1]
	query = {name: value for name, value in request.url.params.items() if name != "read_only"}

	return {
		"method": request.method,
		"path": request.url.path,
		**({"query": query} if query else {}),
		**({"body": json.loads(request.content)} if request.content else {}),
	}


def _outcomes (
	act: Act,
	cast: Cast,
	who: Who,
	route: dict[str, typing.Any],
	relay: typing.Callable[[str, dict[str, typing.Any]], Outcome],
) -> dict[str, Outcome]:
	"""Drive one act as one principal through every transport that principal can arrive by.

	**The terminal's two local principals present no credential**, so they arrive only by the local
	client and the relay in process; everybody else arrives by all five. The relay calls the act's
	own tool where it has one, and ``call_api`` otherwise.
	"""

	token = None if who.local else cast.secrets[who.key]

	if act.route is not None:
		route = act.route(cast, who)

	tool = act.tool or ("subroutine_call_api", lambda cast: route)
	found = {
		"local client": _isolated(
			cast, lambda: _by_client(act, cast, _local_client(cast, who, token=token))
		),
		"relay": _isolated(cast, lambda: relay(tool[0], tool[1](cast))),
	}

	if who.local:
		return found

	found["HTTP client"] = _isolated(
		cast,
		lambda: _by_client(
			act, cast, _http_client(cast, who, api_support.SyncTransport(cast.application))
		),
	)
	found["call_api"] = _isolated(cast, lambda: _at_mcp(cast, who, "subroutine_call_api", route))

	if act.tool is not None:
		name, arguments = act.tool
		found["agent tool"] = _isolated(cast, lambda: _at_mcp(cast, who, name, arguments(cast)))

	return found


# --- The decision ------------------------------------------------------------------------------


def _principal (cast: Cast, who: Who) -> subroutine.domain.authentication.Principal:
	"""Return the principal the permission check is asked about, as an instance would build it."""

	if who.local:
		found = subroutine.domain.local.principal(
			cast.session, local_user=cast.people[who.person].username
		)

	else:
		found = subroutine.domain.authentication.authenticate(
			cast.session, cast.secrets[who.key], record_use=False
		)

	return dataclasses.replace(found, read_only=who.read_only)


def _decided (
	cast: Cast,
	principal: subroutine.domain.authentication.Principal,
	verb: str,
	place: str,
	*,
	reading: bool,
) -> bool:
	"""Ask the one permission check whether this principal may do this here."""

	if place == "instance":
		return subroutine.domain.authorization.may_instance(principal, verb, reading=reading)

	workspace_id, project = cast.places[place]

	return (subroutine.domain.authorization.refusal(
		cast.session, principal, verb, workspace_id=workspace_id, project=project
	) is None)


#: The two doors a principal arrives by, and the transports through each. **The agent tools are a door
#: of their own** (decision `#4520`): a session through them carries a ceiling the terminal's does
#: not, so the two answer alike except where that ceiling refuses.
DOORS: dict[str, tuple[str, ...]] = {
	"the terminal": ("local client", "HTTP client"),
	"the agent tools": ("relay", "call_api", "agent tool"),
}


def _one_line (outcomes: dict[str, Outcome]) -> str:
	"""Say what each transport answered, for a report."""

	return "; ".join(f"{transport} {outcome}" for transport, outcome in outcomes.items())


def _disagreements (act: Act, cast: Cast, who: Who, outcomes: dict[str, Outcome]) -> list[str]:
	"""Say every way this cell breaks the matrix's three rules, or nothing."""

	found: list[str] = []
	doors = {
		door: {name: outcomes[name] for name in names if name in outcomes}
		for door, names in DOORS.items()
	}

	for door, met in doors.items():
		if len({outcome.kind for outcome in met.values()}) > 1:
			found.append(f"the transports through {door} disagree: {_one_line(met)}")

	terminal, tools = outcomes["local client"], outcomes["relay"]
	ceiling = act.beyond_the_agent_tools(who)

	if ceiling:
		for transport, outcome in doors["the agent tools"].items():
			if not outcome.refused:
				found.append(f"{transport} answered what the agent tools' ceiling refuses (#4520)")

	elif terminal.kind != tools.kind:
		found.append(f"the terminal and the agent tools disagree: {_one_line(outcomes)}")

	if act.verbs and act.place is not None:
		allowed = all(_allowed(who, verb, act.place, reading=act.reads) for verb in act.verbs)

		for transport, outcome in outcomes.items():
			if not allowed and not outcome.refused:
				found.append(f"{transport} answered what the decision refuses ({act.verbs})")

			through_the_tools = transport in DOORS["the agent tools"]

			if allowed and outcome.refused and act.rule is None and not (ceiling and through_the_tools):
				found.append(f"{transport} refused what the decision allows ({act.verbs}): {outcome}")

	for canary in cast.canaries:
		if _allowed(who, canary.verb, canary.place, reading=True):
			continue

		for transport, outcome in outcomes.items():
			if canary.text in outcome.said:
				found.append(f"{transport} said what it may not read: {canary.text!r}")

	return found


@pytest.mark.parametrize("act", ACTS, ids=lambda act: act.key)
def test_every_principal_meets_one_answer_through_every_transport (
	cast: Cast, act: Act, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""One act, as every principal, through every transport: one answer, the decision's, no leak."""

	rendering = subroutine.mcp.protocol._explained

	def rendered (failure: BaseException, tool: typing.Any = None) -> str:
		"""Keep the failure a tool rendered, for its status, and render it as before."""

		RENDERED.append(failure)

		return rendering(failure, tool)

	monkeypatch.setattr(subroutine.mcp.protocol, "_explained", rendered)
	monkeypatch.setattr(subroutine.db.backup, "take", _no_copy)
	_driving_the_cast(cast, monkeypatch)
	route = {} if act.route is not None else _route(act, cast)
	cells: dict[str, list[str]] = {}

	for who in PRINCIPALS:
		relay = _relay(cast, who, monkeypatch)
		outcomes = _outcomes(act, cast, who, route, relay)

		# **A floor under the loop**, since "no cell disagreed" and "no cell ran" read alike: the two
		# local principals arrive by two transports, and everybody else by four, or five with a tool.
		assert len(outcomes) == (2 if who.local else 4 + (act.tool is not None)), outcomes

		found = _disagreements(act, cast, who, outcomes)

		if found:
			cells[who.key] = found

	known = {who: reason for (key, who), reason in KNOWN.items() if key == act.key}
	unexcused = {
		who: found for who, found in cells.items() if who not in known and "*" not in known
	}
	stale = sorted(who for who in known if (who == "*" and not cells) or (who != "*" and who not in cells))
	report = "\n".join(f"- {who}:\n    " + "\n    ".join(found) for who, found in unexcused.items())

	assert not unexcused, f"{act.key} ({act.says}):\n{report}"
	assert not stale, f"{act.key}: these now agree, so delete them from KNOWN: {stale}"


def test_the_decision_is_as_written (cast: Cast) -> None:
	"""The one permission check answers every principal on every verb and place as :data:`DECIDED`
	says, reading and writing, so a fault in the decision fails here rather than agreeing with
	itself on every transport."""

	asked = {(verb, act.place) for act in ACTS if act.place for verb in act.verbs}
	planted = {(canary.verb, canary.place) for canary in cast.canaries}

	assert asked | planted == set(DECIDED), "an act or a canary asks what the table does not say"

	for verb, place in DECIDED:
		for reading in (False, True):
			decided = {
				who.key
				for who in PRINCIPALS
				if _decided(cast, _principal(cast, who), verb, place, reading=reading)
			}
			written = {
				who.key for who in PRINCIPALS if _allowed(who, verb, place, reading=reading)
			}

			assert decided == written, (
				f"{verb} on {place}, {'reading' if reading else 'writing'}: "
				f"{sorted(decided ^ written)} differ from the table"
			)


def test_every_act_is_driven_and_nothing_else () -> None:
	"""**Every act the table describes is driven here, and nothing else is** (`SR#4579`).

	The table is what ``docs/permissions.md`` lists, so an act it names and nothing drives would be a
	published claim nothing tests, and a drive naming no act would test what nobody reads.
	"""

	described = {act.key for act in subroutine.acts.ACTS}
	own = {act.key for act in subroutine.acts.ACTS if act.own}
	projects = {act.key for act in subroutine.acts.ACTS if act.place == subroutine.acts.PROJECT}

	assert described - set(DRIVES) == set(), "an act the table describes and nothing drives"
	assert set(DRIVES) - described == set(), "a drive naming no act of the table"
	assert set(THEIRS) == own, "THEIRS and the table's acts on somebody's own thing differ"
	assert ON_OPS - projects == set(), "ON_OPS names an act that does not land on a project"


def test_every_client_method_is_an_act_or_says_why_not () -> None:
	"""**A method added to the clients is driven here or written down as not an act**, so the matrix
	cannot quietly stop covering the surface it guards."""

	methods = {
		name
		for name, value in vars(subroutine.clients.base.Client).items()
		if callable(value) and not name.startswith("_")
	}
	driven = {act.call.__code__.co_names for act in ACTS}
	called = set().union(*driven) & methods

	assert methods - called - set(NOT_AN_ACT) == set(), "a client method no act drives"
	assert called & set(NOT_AN_ACT) == set(), "a method in NOT_AN_ACT that an act drives"
	assert set(NOT_AN_ACT) <= methods, "NOT_AN_ACT names a method the client does not have"


def test_only_call_api_s_answer_is_read_for_a_status_line () -> None:
	"""`SR#4616`: a change feed read opening with event 412 was taken for a refusal.

	Its text was then parsed as a problem document and raised, in some orderings on PostgreSQL and
	not others. A refusal from ``subroutine_call_api`` is still read as one.
	"""

	feed = {"content": [{"type": "text", "text": "412 created #5 Fix the build"}]}

	assert not _from_a_tool(feed, tool="subroutine_changes").refused

	refused = {"content": [{"type": "text", "text": '404 {"detail": "There is no task \'9\' here."}'}]}
	read = _from_a_tool(refused, tool="subroutine_call_api")

	assert read.refused and read.status == 404, read
