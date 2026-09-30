"""Database package — SQLAlchemy async engine and ORM models.

Importing this package registers every model on the one declarative ``Base``: the app's own tables
(``backend.db.models``) and the sync tables (``backend.db.sync_models``). Python runs this file before any
``backend.db.<module>`` import, so ``Base.metadata`` holds all the tables whichever module is imported first —
``create_all`` / ``drop_all`` and Alembic's ``target_metadata`` never see half of the schema.

Order matters: ``models`` first (it defines ``Base`` and imports nothing from this package), then ``sync_models``
(which imports ``Base`` from the already-loaded ``models``). There is no import cycle.
"""
from backend.db import models  # noqa: F401  (defines Base and the 9 app tables)
from backend.db import sync_models  # noqa: F401,E402  (registers the 21 sync tables on the same Base)
