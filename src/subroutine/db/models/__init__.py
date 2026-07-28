"""Every mapped table, imported here so the metadata is complete.

Importing this package is what populates ``Base.metadata``. Alembic's autogenerate, the
test fixtures and ``create_all`` all depend on that, so anything touching the schema
imports this rather than an individual model module.

No names are re-exported. Models are referred to by their fully-qualified names —
``subroutine.db.models.work.Task`` — which keeps the house import convention intact and
avoids a partially-initialised-package problem that aliases here would reintroduce.
"""

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
