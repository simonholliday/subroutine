"""The declarative base and the constraint naming convention every table inherits.

Names matter more here than they usually do. SQLite cannot drop an unnamed constraint,
so Alembic rebuilds tables in "batch" mode to change them — and batch mode needs every
constraint to have a predictable name. Setting the convention once, before the first
migration exists, avoids a class of migration that simply cannot be written later.
"""

import sqlalchemy
import sqlalchemy.orm

#: Applied to every constraint and index. ``column_0_N_name`` covers composite keys, so
#: multi-column constraints get stable names rather than being silently truncated to the
#: first column.
NAMING_CONVENTION = {
	"ix": "ix_%(table_name)s_%(column_0_N_name)s",
	"uq": "uq_%(table_name)s_%(column_0_N_name)s",
	"ck": "ck_%(table_name)s_%(constraint_name)s",
	"fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
	"pk": "pk_%(table_name)s",
}


class Base(sqlalchemy.orm.DeclarativeBase):
	"""Base class for every mapped table in Subroutine."""

	metadata = sqlalchemy.MetaData(naming_convention=NAMING_CONVENTION)
