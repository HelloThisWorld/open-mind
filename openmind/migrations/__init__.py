"""SQLite schema migrations for the Open Mind database.

See :mod:`openmind.migrations.runner` for the guarantees, and
``docs/database-migrations.md`` for the operational guide.
"""
from .runner import (Migration, MigrationApplyError, MigrationChecksumMismatch,
                     MigrationError, MigrationResult, applied_migrations,
                     current_version, detect_legacy, discover, migrate)

__all__ = [
    "Migration",
    "MigrationApplyError",
    "MigrationChecksumMismatch",
    "MigrationError",
    "MigrationResult",
    "applied_migrations",
    "current_version",
    "detect_legacy",
    "discover",
    "migrate",
]
