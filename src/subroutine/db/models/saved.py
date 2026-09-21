"""A saved view: a question about work, kept so nobody retypes it (docs/design.md §18).

**Not ``views.py``, and the name is deliberate.** :mod:`subroutine.views` is where every
response model lives — *view* in the sense the web has used it for thirty years — and a second
module of that name meaning *a reader's saved narrowing* would put one word's two meanings side
by side in one package. The product word stays *view*; the module word is *saved*.

**Its own table rather than a column on somebody.** A saved view belongs to a workspace and to
a person, may be shared with everybody, and is addressed by a name — none of which a settings
blob can carry, and all of which somebody has to be able to list. §18 reserved exactly this:
*"``saved_view`` storing a serialised filter — the grammar is already a persistable document"*.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.types

#: The arrangements a saved view may name, which are the browser's own three.
#:
#: **Here rather than in the browser, because the server refuses one it does not know.** The
#: page's `VIEWS` is the same list and `tests/test_web.py` is what holds them together; a view
#: naming an arrangement nothing can draw is a saved page that cannot be opened, and the
#: honest place to refuse it is where it is written rather than where it is read.
ARRANGEMENTS: tuple[str, ...] = ("agenda", "list", "board")


class SavedView(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.VersionMixin,
):
	"""A query and an arrangement, under a name — item `#1402`, Simon's decision of 2026-09-20.

	**Two halves and nothing else may join either.** The query is one ``q`` string in `#1806`'s
	grammar, which narrows and searches at once and compiles to the field registry rather than
	naming columns — so a view stays correct as fields are added, and stays readable, pasteable
	and editable by hand. The arrangement is three named fields: which arrangement, the order,
	the grouping.

	**A selection may not move into the arrangement half**, however convenient. `#649` and
	`#718` took exactly that shape out of the browser's address, where a view *name* silently
	appended a filter and made that filter unreachable on its own. What ``q`` cannot narrow, a
	saved view does not narrow — and `#3093` is the one gap that leaves, because
	``status_category`` is a registry property with no kind and so cannot be written as a term.

	**Mine by default, shared on purpose.** :attr:`owner_id` says whose it is and
	:attr:`shared` says whether the workspace can see it. The two cover both needs the item
	names — everybody retyping one narrowing, and a lead wanting to say *this is the queue our
	team works from* — without putting one person's experiment in everybody's list. On a
	one-person instance the distinction never appears, which is §1.4.

	**The name is taken whether or not it is shared**, and that is the cost of the rule that
	makes a shared address honest: one name means one view for everybody, so a link somebody
	sends draws what they were looking at. Resolving *mine first, then the workspace's* would
	need no uniqueness and is the defect `#745` refused ``me`` for — the recipient would get
	their own queue.
	"""

	__tablename__ = "saved_view"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"workspace_id", "key", name="uq_saved_view_workspace_id_key"
		),
		subroutine.db.mixins.enum_check("arrangement", ARRANGEMENTS),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()

	# **The address, derived from the title rather than typed.** `init` already does this two-step
	# for a workspace short name: what somebody writes is the title, and the key is trimmed and
	# shaped from it rather than validated, because `text.fit` refuses user input and a derived
	# value is not that — validating it made `init --workspace "<69 chars>"` refuse outright.
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)

	# What somebody typed, kept verbatim, because the key is lossy by construction and a list
	# of views showing `team-queue` where its author wrote `Team queue` is a list of slugs.
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)

	# The query half. NULL means *narrowed by nothing*, which is a real and useful view — an
	# arrangement of everything — and is a different fact from `q = ""`, which `#880` records
	# the cost of: an empty search used to be a filter matching every row containing a space.
	q: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)

	# Which of :data:`ARRANGEMENTS`. NOT NULL and with no default in the database: a view
	# always says how it is drawn, because the whole complaint behind `#1402` is that a saved
	# board came back as a list.
	arrangement: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)

	# **`order`, the same word the API field and every listing use** — one name for one fact.
	# It is a reserved word in SQL and SQLAlchemy quotes it on both backends, which is checked
	# rather than assumed: a second spelling here would need a rename recorded in
	# `test_api_writability`, and that register is keyed by what a *view* reports, so a column
	# renamed only in the database has no honest entry in it.
	order: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)

	# The axis, when there is one. NULL is *not grouped*, and only a board draws columns —
	# but the field is stored whatever the arrangement, because changing a saved board to a
	# saved list and back must not lose the axis somebody chose.
	group_by: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=True
	)

	# Whose it is. CASCADE rather than SET NULL: an unowned private view is unreachable by
	# anybody and an unowned shared one has nobody accountable for it, so neither is a state
	# worth being able to reach — which is `workspaces.create`'s own argument, one table over.
	owner_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)

	# Whether the workspace can see it. Defaulting to false in the database as well as in the
	# service is deliberate: *mine by default* is the decision, and a row written by anything
	# that forgets to say must not be the one that is published to everybody.
	shared: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, nullable=False, default=False, server_default=sqlalchemy.false()
	)
