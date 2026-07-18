"""Versioned migration modules, discovered by filename: ``v<NNNN>_<name>.py``.

Each module declares EXACTLY ONE of:

* ``STATEMENTS`` — a sequence of SQL statements executed in order;
* ``upgrade(conn)`` — a function, for changes that need Python.

Whichever it declares is checksummed. Once a migration has been applied to any
database, its content is frozen: express a change as a NEW migration.
"""
