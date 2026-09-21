"""Saving a view, sharing one, and finding the ones you may see — item `#1402`.

**A saved view is a query and an arrangement**, and the two halves are kept apart on purpose.
The query is one ``q`` string in `#1806`'s grammar; the arrangement is which arrangement, the
order and the grouping. `#649` and `#718` took the shape where a view *name* silently carried a
filter out of the browser's address, and this must not rebuild it one layer down.

**Refused when it is written, not when it is read.** An order or an axis a listing cannot
answer is a stored 422 waiting for somebody who did not write it, so every field is checked
against the registry the listing itself consults — ``ordering.TASK_FIELDS`` and
``grouping.refuse_unknown_axis``. The ``q`` half is deliberately *not* checked that way:
`#1806`'s whole rule is that a term naming no field stays in the text, so there is no such
thing as a ``q`` this can refuse.

**Three permissions, all of them ones that already exist**, and no new verb — a role stores its
grants as rows, so a new verb would leave every workspace made before it without one:

* **reading** a view is ``task:read``: it is a saved question about work, and somebody who may
  not read the work has no use for the question;
* **writing your own** is ``task:write``, the verb a contributor carries;
* **sharing one** is ``project:write``, because publishing a view to everybody is shaping the
  workspace rather than doing work in it — which is exactly the line the seeded ``contributor``
  role draws when it says *"Cannot change projects"*.

**Nothing here emits an event.** The journal is the record of what happened to *work*
(`#1268`), and a saved view is a reader's own furniture; putting *si saved a view* in the feed
every agent polls would be `#1394`'s noise with none of its signal.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.saved
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.grouping
import subroutine.domain.ordering
import subroutine.domain.text
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: The longest a view's title may be. `calendars.MAX_TITLE_LENGTH`'s number, for the same
#: reason: this is a label in a list rather than a sentence, and a title nobody can read in a
#: column is not a title.
MAX_TITLE_LENGTH = 128

#: The longest a derived key may be. Longer than a project key's 32 because a project key is
#: typed constantly and a view's name is typed once and then clicked.
MAX_KEY_LENGTH = 64

#: Names a saved view may not take, because :data:`db.models.saved.ARRANGEMENTS` already means
#: something in the same position. ``?view=board`` is the arrangement; a saved view called
#: *board* would make one address mean two things, and the one that wins would depend on
#: lookup order rather than on anything anybody decided.
RESERVED: tuple[str, ...] = subroutine.db.models.saved.ARRANGEMENTS

#: What is stored when somebody saves a page they have not grouped or ordered. Named rather
#: than spelled ``None`` at three call sites, because *unset* is a real answer here — a view
#: may narrow and arrange nothing at all, which is *everything, as a list*.
UNARRANGED = None


def normalize_key (title: str) -> str:
	"""Return the address derived from a title: lower case, hyphens, nothing else.

	**Derived rather than validated**, which is `workspaces.normalize_slug`'s rule and the
	reason it is a filter where `projects.normalize_key` is not. A project key is typed by the
	person who wants it, so silently dropping a character would hand them a different project;
	a view's key is *made for* them out of a title they already wrote, so shaping it is the
	service doing its job rather than a correction they did not ask for.
	"""

	kept = [letter if letter.isalnum() else "-" for letter in title.strip().lower()]
	collapsed = "".join(kept).strip("-")

	while "--" in collapsed:
		collapsed = collapsed.replace("--", "-")

	return collapsed[:MAX_KEY_LENGTH].strip("-")


def check_title (title: str) -> str:
	"""Return a usable title, or refuse by name saying what is wrong with this one."""

	# **Through `text.require` and `text.fit` rather than a length check written here**, which
	# is what the first version of this did and is the defect `#1574` records one module over:
	# a hand-rolled length check has no character rule beside it, so a NUL in a name is a 500
	# on PostgreSQL and a stored row on SQLite. `fit` also puts the name on one line, which a
	# label in a list wants and which `#927` H-8 made the default for exactly this reason.
	cleaned = subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title", label="A view's name"),
		field="title",
		limit=MAX_TITLE_LENGTH,
		label="A view's name",
		hint="A view's name is a label in a list, so it wants to be short.",
	)
	derived = normalize_key(cleaned)

	if not derived:
		raise subroutine.errors.ValidationError(
			"That name has no letters or numbers in it, so it cannot be addressed.",
			errors=[
				subroutine.errors.FieldError(
					field="title",
					code="invalid_field_value",
					message=f"{cleaned!r} gives an empty address.",
					hint="Use at least one letter or number, e.g. 'Team queue'.",
				)
			],
		)

	if derived in RESERVED:
		known = ", ".join(RESERVED)

		raise subroutine.errors.ValidationError(
			f"{derived!r} is the name of an arrangement, so a view cannot be called it.",
			errors=[
				subroutine.errors.FieldError(
					field="title",
					code="invalid_field_value",
					message=f"{derived!r} is reserved.",
					hint=f"These are the arrangements a view is drawn as: {known}.",
				)
			],
		)

	return cleaned


def check_arrangement (arrangement: str) -> str:
	"""Return the arrangement, or refuse it by name with the ones that exist.

	**Refused here rather than by the CHECK constraint**, which would arrive as a 500 naming a
	constraint — §6.3's rule, and the reason ``PRIORITY_RANGE`` and ``MAX_TITLE_LENGTH`` exist
	beside their columns rather than only in the database.
	"""

	if arrangement in subroutine.db.models.saved.ARRANGEMENTS:
		return arrangement

	known = ", ".join(subroutine.db.models.saved.ARRANGEMENTS)

	raise subroutine.errors.ValidationError(
		f"{arrangement!r} is not an arrangement a view can be drawn as.",
		errors=[
			subroutine.errors.FieldError(
				field="arrangement",
				code="invalid_field_value",
				message=f"No arrangement called {arrangement!r}.",
				hint=f"Draw it as one of: {known}.",
			)
		],
	)


def check_order (order: str | None) -> str | None:
	"""Return the ordering a view is saved with, or refuse it by name.

	Checked against ``ordering.TASK_FIELDS``, which is the same map ``api/tasks.SORTABLE``
	is — so an order this accepts is one the listing accepts.

	**``relevance`` is deliberately not reachable here**, and that falls out of using the
	static map rather than being a second rule: relevance ranks one search's hits, and
	``ordering.refuse_ranking_without_a_search`` already says it needs a live search beside it.
	An arrangement is what a view keeps, and *how well this matched* is not one.
	"""

	if order is None or not order.strip():
		return UNARRANGED

	wanted = order.strip()
	allowed = subroutine.domain.ordering.TASK_FIELDS
	unknown = [
		name for name in (part.strip().lstrip("-") for part in wanted.split(","))
		if name and name not in allowed
	]

	if unknown:
		known = ", ".join(sorted(allowed))

		raise subroutine.errors.ValidationError(
			f"A view cannot be ordered by {unknown[0]!r}.",
			errors=[
				subroutine.errors.FieldError(
					field="order",
					code="invalid_field_value",
					message=f"No ordering called {unknown[0]!r}.",
					hint=f"Order by one of: {known}. A leading '-' reverses it.",
				)
			],
		)

	return wanted


def check_group_by (axis: str | None) -> str | None:
	"""Return the axis a view is grouped on, or refuse it by name.

	Through ``grouping.refuse_unknown_axis`` rather than a list of its own, so an axis added
	to the registry is savable the day it is added — `#1803`'s argument, and what stopped this
	being a fourth place that has to agree about what a board can do.
	"""

	if axis is None or not axis.strip():
		return UNARRANGED

	return subroutine.domain.grouping.refuse_unknown_axis(axis.strip(), kind="task")


def _taken (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	key: str,
	except_id: uuid.UUID | None = None,
) -> bool:
	"""Report whether this workspace already has a view under this address.

	**It says nothing about whose it is, and that is the rule rather than an oversight.** One
	name means one view for everybody, which is what makes a shared address honest: a link
	somebody sends draws what they were looking at. The cost is that a private view still takes
	the word, so a collision can refuse against something the caller cannot see — exactly what
	a username or a project key already leaks, and the smallest leak a unique namespace has.
	"""

	model = subroutine.db.models.saved.SavedView
	statement = sqlalchemy.select(model.id).where(
		model.workspace_id == workspace_id, model.key == key
	)

	if except_id is not None:
		statement = statement.where(model.id != except_id)

	return session.scalars(statement).first() is not None


def _refuse_a_taken_name (key: str) -> typing.NoReturn:
	"""Refuse a name this workspace has already given to a view."""

	raise subroutine.errors.Conflict(
		f"This workspace already has a view called {key!r}.",
		errors=[
			subroutine.errors.FieldError(
				field="title",
				code="duplicate_key",
				message=f"The address {key!r} is taken.",
				hint="Names are one per workspace, whether or not a view is shared. "
				"Pick another, or edit the one that has it.",
			)
		],
	)


def readable (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
) -> sqlalchemy.Select[tuple[subroutine.db.models.saved.SavedView]]:
	"""Permit reading views here, and return the statement selecting the ones this caller sees.

	**Mine, plus everybody's shared ones.** The two halves of `#1402`'s second decision, as one
	predicate, so no caller can narrow by hand and get it half right — which is
	``domain/scoping.py``'s own argument for being the one chokepoint every listing goes
	through.

	**The permission is asked here rather than in the routers**, and that is the difference
	between a rule and a sentence. Resolving a workspace says which ones a caller can *reach*,
	never what they may do there, so a read path that stopped at the resolver would publish
	``task:read`` and check nothing — the shape `#247`, `#251` and `#303` are all instances of.
	Asked in the domain, both transports inherit it; asked in each router, it is two copies of
	one rule and S3-07 is what happens next.

	**Why ``task:read``**: a saved view is a saved question about work, and its ``q`` can say
	*what is keanu holding*, so a credential narrowed away from the work has no use for it.

	**Every principal has an account behind it**, including an agent's token and a calendar
	feed's, so *mine* is always askable and there is no fourth case here.
	"""

	subroutine.domain.authorization.authorize(
		session, principal, subroutine.permissions.TASK_READ, workspace_id=workspace_id
	)

	model = subroutine.db.models.saved.SavedView

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id,
		sqlalchemy.or_(model.shared.is_(True), model.owner_id == principal.user.id),
	)


def by_key (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	key: str,
) -> subroutine.db.models.saved.SavedView:
	"""Return one view by its address, or say there is none the caller can see.

	**A view somebody else has kept private is *not found*, never *forbidden*.** Saying
	forbidden would confirm the name is in use to anybody who guessed it, which is the
	distinction ``authorization.ProjectNotVisible`` already draws one entity over.
	"""

	wanted = normalize_key(key)
	found = session.scalars(
		readable(session, principal, workspace_id=workspace_id).where(
			subroutine.db.models.saved.SavedView.key == wanted
		)
	).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no view called {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="not_found",
					message=f"No view called {wanted!r}.",
					hint="A view is yours or shared with the workspace; ask for the list to "
					"see which ones you can reach.",
				)
			],
		)

	return found


def create (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	title: str,
	arrangement: str,
	q: str | None = None,
	order: str | None = None,
	group_by: str | None = None,
	shared: bool = False,
	actor: subroutine.domain.authentication.Principal,
) -> subroutine.db.models.saved.SavedView:
	"""Save a view, and answer with the row.

	``actor`` is required rather than optional, unlike the services ``domain.bootstrap`` calls:
	a saved view has an owner by construction, and there is no moment before any principal
	exists at which one needs to be written.
	"""

	subroutine.domain.authorization.authorize(
		session, actor, subroutine.permissions.TASK_WRITE, workspace_id=workspace_id
	)

	if shared:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=workspace_id
		)

	named = check_title(title)
	key = normalize_key(named)

	if _taken(session, workspace_id=workspace_id, key=key):
		_refuse_a_taken_name(key)

	row = subroutine.db.models.saved.SavedView(
		workspace_id=workspace_id,
		key=key,
		title=named,
		q=_kept(q),
		arrangement=check_arrangement(arrangement),
		order=check_order(order),
		group_by=check_group_by(group_by),
		owner_id=actor.user.id,
		shared=shared,
	)
	session.add(row)
	session.flush()

	return row


def update (
	session: sqlalchemy.orm.Session,
	row: subroutine.db.models.saved.SavedView,
	*,
	title: str | None = None,
	arrangement: str | None = None,
	q: str | None = None,
	order: str | None = None,
	group_by: str | None = None,
	shared: bool | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal,
	given: typing.Container[str] = (),
) -> subroutine.db.models.saved.SavedView:
	"""Change a saved view, and answer with the row.

	``given`` names the fields the caller actually sent, because every one of the optional
	halves here has ``None`` as a *meaningful value* — clearing a view's grouping and not
	mentioning it are different instructions, and a signature that cannot tell them apart makes
	*unset this* unaskable. `#1396` met the same shape one surface over: ``scopes: []`` means
	*no narrowing* where the field's absence means *say nothing about it*.
	"""

	_refuse_somebody_elses(row, actor)
	subroutine.domain.authorization.authorize(
		session, actor, subroutine.permissions.TASK_WRITE, workspace_id=row.workspace_id
	)
	subroutine.domain.versions.require(row, expected_version)

	if shared is not None and shared != row.shared:
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=row.workspace_id
		)

		row.shared = shared

	if title is not None:
		named = check_title(title)
		key = normalize_key(named)

		if _taken(session, workspace_id=row.workspace_id, key=key, except_id=row.id):
			_refuse_a_taken_name(key)

		row.title = named
		row.key = key

	if arrangement is not None:
		row.arrangement = check_arrangement(arrangement)

	if "q" in given:
		row.q = _kept(q)

	if "order" in given:
		row.order = check_order(order)

	if "group_by" in given:
		row.group_by = check_group_by(group_by)

	row.version += 1
	session.flush()

	return row


def delete (
	session: sqlalchemy.orm.Session,
	row: subroutine.db.models.saved.SavedView,
	*,
	actor: subroutine.domain.authentication.Principal,
) -> None:
	"""Remove a saved view for good.

	**A hard delete, unlike a task's.** ``SoftDeleteMixin`` exists so work can be restored from
	the trash and so a ref is never reused; a view is neither — it holds no record of anything
	that happened, and leaving a deleted one in the table would go on holding its name against
	the next person who wants it, which is the one thing this table's unique constraint is for.
	"""

	_refuse_somebody_elses(row, actor)
	subroutine.domain.authorization.authorize(
		session, actor, subroutine.permissions.TASK_WRITE, workspace_id=row.workspace_id
	)
	session.delete(row)
	session.flush()


def _kept (q: str | None) -> str | None:
	"""Return the query half as it is stored: what was written, or nothing at all.

	**An empty string becomes NULL**, which is `#880`'s rule stored rather than re-derived:
	*no search* and *a search for nothing* are different questions, and ``q=" "`` was once a
	real filter matching every row containing a space.
	"""

	if q is None:
		return None

	# **`text.readable` because this is prose with no width**, so *does it fit* cannot be
	# asked of it and *is it text* still has to be. A `q` is one line somebody typed into a
	# search box; a control character in it is a 500 on PostgreSQL, stored on SQLite, and a
	# `db copy` that names no table — which is the divergence `#1584` exists to close.
	subroutine.domain.text.readable(q, field="q", label="A view's query")

	return q.strip() or None


def _refuse_somebody_elses (
	row: subroutine.db.models.saved.SavedView,
	actor: subroutine.domain.authentication.Principal,
) -> None:
	"""Refuse to change a view somebody else wrote, however shared it is.

	**Sharing publishes a view; it does not hand it over.** The rule comments already follow
	(§5.10): *"an administrator rewriting somebody's words under their name is not a permission
	anybody should hold"*. A shared view is one person's statement of how the team's queue is
	read, and a second person silently changing what everybody's saved link draws is that same
	defect with a wider audience.
	"""

	if row.owner_id == actor.user.id:
		return

	raise subroutine.errors.Forbidden(
		f"The view {row.key!r} belongs to somebody else.",
		errors=[
			subroutine.errors.FieldError(
				field="key",
				code="forbidden",
				message="Only the person who saved a view may change it.",
				hint="Save your own copy of it under another name.",
			)
		],
	)
