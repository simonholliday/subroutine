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
import typing
import uuid

import httpx
import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
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
class Act:
	"""One thing a principal can ask an instance to do, as the matrix drives it.

	``verbs`` and ``place`` are what the permission check is asked: each verb, on the workspace and
	project ``place`` names, or on the installation where it is ``"instance"``. An act asking none is
	compared across transports and for what it says, and not against the decision. ``reads`` says it
	changes nothing, which the installation's verbs ask separately. ``rule`` names a rule of the
	act's own that can refuse what the verbs allow, so an allowed verb does not oblige an answer.

	``secret`` says the answer is a credential, a sign-in link or a calendar feed, which no session
	through the agent tools is handed (`#4520`). ``route`` is the request ``call_api`` makes, for an
	act whose request names the principal making it; every other act's is recorded from the HTTP
	client. ``theirs`` names the person whose own thing the act is on, where anybody else needs
	``workspace:admin`` to do it - which the agent tools never hold (`SR#4563`).
	"""

	key: str
	says: str
	verbs: tuple[str, ...]
	place: str | None
	call: typing.Callable[[Client, Cast], object]
	tool: tuple[str, typing.Callable[[Cast], dict[str, typing.Any]]] | None = None
	reads: bool = False
	rule: str | None = None
	secret: bool = False
	route: typing.Callable[[Cast, "Who"], dict[str, typing.Any]] | None = None
	theirs: str | None = None

	def beyond_the_agent_tools (self, who: "Who") -> bool:
		"""Say whether the agent tools' ceiling refuses this act to ``who``, whatever it holds.

		Decision `#4520`: a session through ``/mcp`` or ``subroutine mcp`` is handed no secret, holds no
		instance verb, and holds neither ``workspace:admin`` nor ``workspace:delete``.
		"""

		return (
			self.secret
			or (self.theirs is not None and who.person != self.theirs)
			or any(
				verb in P.INSTANCE_LEVEL or verb in (P.WORKSPACE_ADMIN, P.WORKSPACE_DELETE)
				for verb in self.verbs
			)
		)


def _read (
	key: str,
	says: str,
	verbs: tuple[str, ...],
	place: str | None,
	call: typing.Callable[[Client, Cast], object],
	tool: tuple[str, typing.Callable[[Cast], dict[str, typing.Any]]] | None = None,
	rule: str | None = None,
) -> Act:
	"""Return an act that only reads."""

	return Act(key, says, verbs, place, call, tool=tool, reads=True, rule=rule)


def _exporting (kind: str) -> typing.Callable[[Client, Cast], object]:
	"""Return an act taking away every row of one kind, read to the end."""

	def call (client: Client, cast: Cast) -> object:
		"""Read the whole export, since a client yields it a page at a time."""

		return list(client.export(kind, workspace=cast.slug))

	return call


#: Somebody else's account, named by every act on one.
SOMEBODY = "A person administers another account, or that account's own chain does (#4525)."

ACTS: tuple[Act, ...] = (
	# --- Reading the installation and oneself --------------------------------------------------
	_read("identity", "Say which instance this is", (), None, lambda c, s: c.identity()),
	_read("reference", "Read the agent's guide", (), None, lambda c, s: c.reference("agent")),
	_read("meta", "Read a workspace's vocabulary", (), "metacortex", lambda c, s: c.meta(workspace=s.slug)),
	_read("me", "Say who is asking", (), None, lambda c, s: c.me(), tool=("subroutine_whoami", lambda s: {})),
	_read("user", "Read an account by name", (), None, lambda c, s: c.user(username=s.names["tank"])),
	_read("users", "List the accounts", (), None, lambda c, s: c.users()),
	_read("tokens", "List one's credentials", (), None, lambda c, s: c.tokens()),
	_read("calendars", "List one's calendar feeds", (), None, lambda c, s: c.calendars()),
	_read(
		"instance workspaces",
		"List every workspace on the installation",
		(P.INSTANCE_ADMIN,),
		"instance",
		lambda c, s: c.instance_workspaces(),
	),
	_read(
		"unreachable projects",
		"List the projects nobody can reach",
		(P.INSTANCE_ADMIN,),
		"instance",
		lambda c, s: c.unreachable_projects(),
	),
	_read(
		"unadministered workspaces",
		"List the workspaces nobody can administer",
		(P.INSTANCE_ADMIN,),
		"instance",
		lambda c, s: c.unadministered_workspaces(),
	),
	# --- Reading a workspace ---------------------------------------------------------------------
	_read(
		"workspace settings",
		"Read a workspace's settings",
		(P.WORKSPACE_READ,),
		"metacortex",
		lambda c, s: c.workspace_settings(workspace=s.slug),
	),
	_read(
		"members",
		"List a workspace's members",
		(P.WORKSPACE_READ,),
		"metacortex",
		lambda c, s: c.members(workspace=s.slug),
	),
	_read("statuses", "List the statuses", (), "metacortex", lambda c, s: c.statuses(workspace=s.slug)),
	_read("link types", "List the link types", (), "metacortex", lambda c, s: c.link_types(workspace=s.slug)),
	_read("tags", "List the tags", (), "metacortex", lambda c, s: c.tags(workspace=s.slug)),
	_read("saved views", "List the saved views", (), "metacortex", lambda c, s: c.saved_views(workspace=s.slug)),
	_read(
		"saved view",
		"Read a shared saved view",
		(),
		"metacortex",
		lambda c, s: c.saved_view(key=s.names["view"], workspace=s.slug),
	),
	_read(
		"projects",
		"List the projects",
		(P.PROJECT_READ,),
		"metacortex",
		lambda c, s: c.projects(workspace=s.slug),
	),
	_read(
		"project settings",
		"Read a project's settings",
		(P.PROJECT_READ,),
		"web",
		lambda c, s: c.project_settings("web", workspace=s.slug),
	),
	_read(
		"project members",
		"List who a private project is shared with",
		(P.PROJECT_READ,),
		"vault",
		lambda c, s: c.project_members("vault", workspace=s.slug),
	),
	_read(
		"agenda",
		"Read the agenda",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.agenda(workspace=s.slug),
	),
	_read(
		"count tasks",
		"Count the work",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.count_tasks(workspace=s.slug),
	),
	_read(
		"tasks",
		"List the work",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.tasks(workspace=s.slug),
		tool=("subroutine_list", lambda s: {}),
	),
	_read(
		"search",
		"Search the work",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.tasks(workspace=s.slug, q="key"),
		tool=("subroutine_search", lambda s: {"q": "key"}),
	),
	_read(
		"documents",
		"List the documents",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.documents(workspace=s.slug),
	),
	_read(
		"count documents",
		"Count the documents",
		(P.TASK_READ,),
		"metacortex",
		lambda c, s: c.count_documents(workspace=s.slug),
	),
	_read(
		"changes",
		"Read what changed",
		(),
		"metacortex",
		lambda c, s: c.changes(workspace=s.slug),
		tool=("subroutine_changes", lambda s: {}),
	),
	_read(
		"journal",
		"Read what happened",
		(),
		"metacortex",
		lambda c, s: c.journal(workspace=s.slug),
		tool=("subroutine_journal", lambda s: {}),
	),
	*(
		_read(
			f"export {kind}",
			f"Take away every {kind.rstrip('s')} row",
			(),
			"metacortex",
			_exporting(kind),
		)
		for kind in ("workspace", "tasks", "comments", "events")
	),
	# --- Reading one item ------------------------------------------------------------------------
	_read(
		"task",
		"Read a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.task(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["task"]}),
	),
	_read(
		"private task",
		"Read a task in a private project",
		(P.TASK_READ,),
		"vault",
		lambda c, s: c.task(ref=s.refs["vault"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["vault"]}),
	),
	_read(
		"document",
		"Read a document",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.document(ref=s.refs["document"], workspace=s.slug),
		tool=("subroutine_show", lambda s: {"ref": s.refs["document"]}),
	),
	_read(
		"comments",
		"Read a task's comments",
		(P.COMMENT_READ,),
		"web",
		lambda c, s: c.comments(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"history",
		"Read a task's history",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.history(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"item journal",
		"Read what happened to a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.item_journal(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"links",
		"Read a task's links",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.links(ref=s.refs["blocked"], workspace=s.slug),
	),
	_read(
		"backlinks",
		"Read what mentions a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.backlinks(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"beneath",
		"Read what is beneath a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.beneath(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"governing",
		"Read what governs a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.governing(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"verifications",
		"Read what was checked against a task",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.verifications(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"proposed links",
		"Read the links a task's words suggest",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.proposed_links(ref=s.refs["task"], workspace=s.slug),
	),
	_read(
		"occurrences",
		"Read when a repeating task comes round",
		(P.TASK_READ,),
		"web",
		lambda c, s: c.occurrences(ref=s.refs["repeat"], workspace=s.slug),
	),
	# --- Writing work --------------------------------------------------------------------------
	Act(
		"capture",
		"File a task in a project",
		(P.TASK_WRITE,),
		"web",
		lambda c, s: c.capture(text="Order a new phone", project="web", workspace=s.slug),
		tool=("subroutine_add", lambda s: {"text": "Order a new phone +web"}),
	),
	Act(
		"update",
		"Retitle a task",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.update(ref=s.refs["task"], title="Fix it", workspace=s.slug),
		tool=("subroutine_update", lambda s: {"ref": s.refs["task"], "title": "Fix it"}),
	),
	Act(
		"schedule",
		"Plan a task for a day",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.schedule(ref=s.refs["task"], starts=datetime.date(2031, 5, 6), workspace=s.slug),
	),
	Act(
		"complete",
		"Finish a task",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.complete(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["task"]}),
	),
	Act(
		"complete beneath the trash",
		"Finish a task whose parent is in the trash",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.complete(ref=s.refs["beneath the trash"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["beneath the trash"]}),
		rule="Nothing in the trash, or beneath it, changes but by restoring or deleting it.",
	),
	Act(
		"skip",
		"Let one occurrence of a repeat go by",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.skip(ref=s.refs["repeat"], workspace=s.slug),
		tool=("subroutine_done", lambda s: {"ref": s.refs["repeat"], "skip": True}),
	),
	Act(
		"move",
		"Put a task under another",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.move(ref=s.refs["task"], parent=s.refs["other"], workspace=s.slug),
	),
	Act(
		"claim",
		"Take a task",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.claim(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_claim", lambda s: {"ref": s.refs["task"]}),
	),
	Act(
		"release",
		"Give a task back",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.release(ref=s.refs["task"], workspace=s.slug),
		tool=("subroutine_claim", lambda s: {"ref": s.refs["task"], "release": True}),
	),
	Act(
		"discard",
		"Put a task in the trash",
		(P.TASK_READ, P.TASK_DELETE),
		"web",
		lambda c, s: c.discard(ref=s.refs["task"], workspace=s.slug),
	),
	Act(
		"undiscard",
		"Take a task out of the trash",
		(P.TASK_READ, P.TASK_DELETE),
		"web",
		lambda c, s: c.undiscard(ref=s.refs["parent"], workspace=s.slug),
	),
	Act(
		"verify",
		"Record a check against a task",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.verify(ref=s.refs["task"], passed=True, summary="Ran it.", workspace=s.slug),
	),
	Act(
		"link",
		"Link two tasks",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.link(
			ref=s.refs["task"], link_type="blocks", target=s.refs["other"], workspace=s.slug
		),
		tool=(
			"subroutine_link",
			lambda s: {"ref": s.refs["task"], "other": s.refs["other"], "type": "blocks"},
		),
	),
	Act(
		"link to private work",
		"Link a task to one in a private project",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.link(
			ref=s.refs["task"], link_type="blocks", target=s.refs["vault"], workspace=s.slug
		),
		tool=(
			"subroutine_link",
			lambda s: {"ref": s.refs["task"], "other": s.refs["vault"], "type": "blocks"},
		),
		rule="Both ends must be work the principal can see.",
	),
	Act(
		"unlink",
		"Withdraw a link",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.unlink(ref=s.refs["blocked"], link_id=s.ids["link"], workspace=s.slug),
	),
	Act(
		"remark",
		"Comment on a task",
		(P.TASK_READ, P.COMMENT_WRITE),
		"web",
		lambda c, s: c.remark(ref=s.refs["task"], body="Seen.", workspace=s.slug),
		tool=("subroutine_comment", lambda s: {"ref": s.refs["task"], "body": "Seen."}),
	),
	Act(
		"uncomment",
		"Delete a comment its author wrote",
		(P.TASK_READ, P.COMMENT_READ, P.COMMENT_WRITE),
		"web",
		lambda c, s: c.uncomment(
			ref=s.refs["task"], comment_id=s.ids["hugo's comment"], workspace=s.slug
		),
		rule="A comment is deleted by its author or by an administrator.",
		theirs="hugo",
	),
	Act(
		"create document",
		"Write a document",
		(P.TASK_WRITE,),
		"web",
		lambda c, s: c.create_document(title="On call", body="Rota.", project="web", workspace=s.slug),
		tool=(
			"subroutine_document",
			lambda s: {"title": "On call", "body": "Rota.", "project": "web"},
		),
	),
	Act(
		"update document",
		"Revise a document",
		(P.TASK_READ, P.TASK_WRITE),
		"web",
		lambda c, s: c.update_document(ref=s.refs["document"], title="Deploying", workspace=s.slug),
		tool=("subroutine_document", lambda s: {"ref": s.refs["document"], "title": "Deploying"}),
	),
	# --- A workspace's vocabulary and views ------------------------------------------------------
	Act(
		"create status",
		"Add a status",
		(P.STATUS_WRITE,),
		"metacortex",
		lambda c, s: c.create_status(
			entity_type="task", key="waiting", label="Waiting", category="todo", workspace=s.slug
		),
	),
	Act(
		"update status",
		"Rename a status",
		(P.STATUS_WRITE,),
		"metacortex",
		lambda c, s: c.update_status(which=s.ids["status"], label="On hold"),
	),
	Act(
		"delete status",
		"Remove a status",
		(P.STATUS_WRITE,),
		"metacortex",
		lambda c, s: c.delete_status(which=s.ids["status"]),
	),
	Act(
		"create link type",
		"Add a link type",
		(P.LINK_TYPE_WRITE,),
		"metacortex",
		lambda c, s: c.create_link_type(
			key="echoes",
			title="echoes",
			inverse_title="is echoed by",
			category="describing",
			workspace=s.slug,
		),
	),
	Act(
		"update link type",
		"Reword a link type",
		(P.LINK_TYPE_WRITE,),
		"metacortex",
		lambda c, s: c.update_link_type(which=s.ids["link type"], title="reflects"),
	),
	Act(
		"delete link type",
		"Remove a link type",
		(P.LINK_TYPE_WRITE,),
		"metacortex",
		lambda c, s: c.delete_link_type(which=s.ids["link type"]),
	),
	Act(
		"create tag",
		"Add a tag",
		(P.TAG_WRITE,),
		"metacortex",
		lambda c, s: c.create_tag(name="urgent", workspace=s.slug),
	),
	Act(
		"update tag",
		"Rename a tag",
		(P.TAG_WRITE,),
		"metacortex",
		lambda c, s: c.update_tag(which=s.ids["tag"], name="someday"),
	),
	Act(
		"delete tag",
		"Remove a tag",
		(P.TAG_WRITE,),
		"metacortex",
		lambda c, s: c.delete_tag(which=s.ids["tag"]),
	),
	Act(
		"rename a hidden tag",
		"Rename a tag used only on private work",
		(P.TAG_WRITE,),
		"metacortex",
		lambda c, s: c.update_tag(which=s.ids["hidden tag"], name="renamed"),
		rule="A tag the principal cannot see is not there to rename.",
	),
	Act(
		"save view",
		"Save a view",
		(),
		"metacortex",
		lambda c, s: c.save_view(title="Notes", arrangement="list", q="notes", workspace=s.slug),
	),
	Act(
		"update saved view",
		"Change somebody else's shared view",
		(),
		"metacortex",
		lambda c, s: c.update_saved_view(key=s.names["view"], title="Deploying", workspace=s.slug),
		rule="A saved view is changed by whoever saved it.",
	),
	Act(
		"forget saved view",
		"Forget somebody else's shared view",
		(),
		"metacortex",
		lambda c, s: c.forget_saved_view(key=s.names["view"], workspace=s.slug),
		rule="A saved view is forgotten by whoever saved it.",
		theirs="laurence",
	),
	# --- Projects --------------------------------------------------------------------------------
	Act(
		"create project",
		"Make a project",
		(P.PROJECT_WRITE,),
		"metacortex",
		lambda c, s: c.create_project(key="docs", title="Docs", workspace=s.slug),
		tool=("subroutine_project", lambda s: {"key": "docs", "title": "Docs"}),
		rule="A credential narrowed to projects makes none outside them (#4527).",
	),
	Act(
		"update project",
		"Retitle a project",
		(P.PROJECT_WRITE,),
		"web",
		lambda c, s: c.update_project("web", title="Website", workspace=s.slug),
	),
	Act(
		"rename project",
		"Change a project's key",
		(P.PROJECT_WRITE,),
		"ops",
		lambda c, s: c.rename_project("ops", key="infra", workspace=s.slug),
	),
	Act(
		"move project",
		"Put a project inside another",
		(P.PROJECT_WRITE,),
		"ops",
		lambda c, s: c.move_project("ops", parent="web", workspace=s.slug),
		rule="The new parent is written into, so it is within reach too.",
	),
	Act(
		"reveal a private project",
		"Make a private project public",
		(P.PROJECT_WRITE,),
		"vault",
		lambda c, s: c.update_project("vault", visibility="public", workspace=s.slug),
		rule="Whoever stewards a private project decides who sees it (#4509).",
	),
	Act(
		"share project",
		"Share a private project with one more person",
		(P.PROJECT_WRITE,),
		"vault",
		lambda c, s: c.share_project("vault", username=s.names["tank"], workspace=s.slug),
		rule="Whoever stewards a private project decides who sees it (#4509).",
	),
	Act(
		"unshare project",
		"Take a private project away from somebody",
		(P.PROJECT_WRITE,),
		"vault",
		lambda c, s: c.unshare_project(
			"vault", username=s.people["carrieanne"].username, workspace=s.slug
		),
		rule="Whoever stewards a private project decides who sees it (#4509).",
	),
	# --- Workspaces ------------------------------------------------------------------------------
	Act(
		"create workspace",
		"Make a workspace",
		(P.INSTANCE_WORKSPACE_CREATE,),
		"instance",
		lambda c, s: c.create_workspace(slug=f"nebula-{s.slug}", title="Nebula"),
	),
	Act(
		"update workspace",
		"Describe a workspace",
		(P.WORKSPACE_WRITE,),
		"metacortex",
		lambda c, s: c.update_workspace(s.slug, title="MetaCortex Inc"),
	),
	Act(
		"rename workspace",
		"Change a workspace's short name",
		(P.WORKSPACE_WRITE,),
		"metacortex",
		lambda c, s: c.rename_workspace(s.slug, slug=f"mc-{s.slug}"),
	),
	Act(
		"delete workspace",
		"Delete a workspace",
		(P.WORKSPACE_DELETE,),
		"metacortex",
		lambda c, s: c.delete_workspace(s.slug),
	),
	Act(
		"restore workspace",
		"Bring a deleted workspace back",
		(P.WORKSPACE_DELETE,),
		None,
		lambda c, s: c.restore_workspace(s.names["deleted"]),
		rule="Only somebody who could delete it brings a workspace back.",
	),
	Act(
		"add member",
		"Add somebody to a workspace",
		(P.USER_ADMIN,),
		"metacortex",
		lambda c, s: c.add_member(
			username=s.people["marcus"].username, role="viewer", workspace=s.slug
		),
	),
	Act(
		"set member role",
		"Change somebody's role",
		(P.USER_ADMIN,),
		"metacortex",
		lambda c, s: c.set_member_role(username=s.names["tank"], role="viewer", workspace=s.slug),
	),
	Act(
		"remove member",
		"Take somebody out of a workspace",
		(P.USER_ADMIN,),
		"metacortex",
		lambda c, s: c.remove_member(username=s.names["tank"], workspace=s.slug),
	),
	# --- Accounts and credentials ----------------------------------------------------------------
	Act(
		"set own timezone",
		"Say where one keeps one's diary",
		(),
		None,
		lambda c, s: c.set_timezone(username=c.me().user.username, timezone="Europe/London"),
		route=lambda s, who: {
			"method": "PATCH",
			"path": f"/v1/users/{s.people[who.person].username}",
			"body": {"timezone": "Europe/London"},
		},
	),
	Act(
		"issue own token",
		"Mint a credential for oneself",
		(),
		None,
		lambda c, s: c.issue_token(title="Spare"),
		rule="A credential never mints a wider one (#4527).",
		secret=True,
	),
	Act(
		"create calendar",
		"Mint a calendar feed for oneself",
		(),
		"metacortex",
		lambda c, s: c.create_calendar(title="Diary", workspace=s.slug),
		rule="A credential never mints a wider one (#4527).",
		secret=True,
	),
	Act(
		"create user",
		"Make an account for a person",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.create_user(username=f"dozer-{s.slug}"),
		rule="Only a person makes a person (#4515).",
	),
	Act(
		"issue token for somebody",
		"Mint a credential for somebody else",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.issue_token(title="Tank's spare", username=s.names["tank"]),
		rule=SOMEBODY,
		secret=True,
	),
	Act(
		"sign-in link for somebody",
		"Mint a sign-in link for somebody else",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.create_login_link(username=s.names["tank"]),
		rule=SOMEBODY,
		secret=True,
	),
	Act(
		"sign somebody out",
		"Sign another person out everywhere",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.sign_out_everywhere(username=s.names["tank"]),
		rule=SOMEBODY,
	),
	Act(
		"revoke somebody's token",
		"Revoke another person's credential",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.revoke_token(id_or_prefix=s.names["tank's token"]),
		rule=SOMEBODY,
	),
	Act(
		"reset somebody's feed",
		"Reset another person's calendar feed",
		(),
		None,
		lambda c, s: c.reset_calendar(id_or_prefix=s.names["tank's feed"]),
		rule="Only its owner resets a feed (#4525).",
		secret=True,
	),
	Act(
		"revoke somebody's feed",
		"Revoke another person's calendar feed",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.revoke_calendar(id_or_prefix=s.names["tank's feed"]),
		rule=SOMEBODY,
	),
	Act(
		"deactivate somebody",
		"Mark somebody as having left",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.set_active(username=s.names["tank"], active=False),
		rule=SOMEBODY,
	),
	Act(
		"transfer an agent",
		"Hand somebody's agent to another person",
		(P.INSTANCE_USER_CREATE,),
		"instance",
		lambda c, s: c.transfer_agent(username=s.names["claude"], to=s.names["tank"]),
		rule=SOMEBODY,
	),
	Act(
		"update instance",
		"Rename the installation",
		(P.INSTANCE_ADMIN,),
		"instance",
		lambda c, s: c.update_instance(name="MetaCortex Inc"),
	),
)

#: Client methods that are not acts, each with why.
NOT_AN_ACT: dict[str, str] = {
	"call_api": "The way in that `subroutine_call_api` is: every act is driven through it.",
	"read_repeat": "Reads a repeat's words on this machine, and asks the instance nothing.",
	"close": "Closes the client.",
}

#: Cells known to disagree today, by act and principal, each naming the item whose build makes it
#: agree. A principal of ``"*"`` stands for every principal driving that act. Deleting an entry is
#: what closes it, and an entry whose cell agrees fails as stale.
KNOWN: dict[tuple[str, str], str] = {
	# `#4567`: the terminal's exemptions for its local principal (S8 and D6 of `#4506`).
	("sign somebody out", "local member"): "#4567: S8, a local principal acts on another account.",
	("issue token for somebody", "local member"): (
		"#4567: S8, a local principal mints another account's credential."
	),
	("sign-in link for somebody", "local member"): (
		"#4567: S8, a local principal mints another account's sign-in link."
	),
}


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
	(P.COMMENT_READ, "web"): _ROLES | {"writing only in another project"},
	(P.COMMENT_WRITE, "web"): _ROLES - {"viewer"} | {"scoped to reading tasks and commenting"},
	(P.PROJECT_WRITE, "ops"): _ROLES - {"contributor", "viewer"} | _OPS,
	(P.TASK_READ, "ops"): _ROLES | _READING | _OPS,
	(P.TASK_READ, "vault"): _VAULT,
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
