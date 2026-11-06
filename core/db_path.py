"""
Canonical DB path resolver for AI DEV DIRECTOR.

Every component (StateMachine, ChatManager, ProviderManager, dashboard) must obtain the
SQLite path through get_db_path(). The path is absolute and anchored at the project source
root, so running the dashboard or CLI from a different cwd can never create a split-brain DB.

Test isolation: when DIRECTOR_TESTING=1 and TEST_DB_PATH is set, that path is returned instead.
"""
import os
import sqlite3
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = SOURCE_ROOT / "data" / "director.db"


class ClosingConnection(sqlite3.Connection):
    """`with conn:` commits or rolls back as usual, then also closes. Plain sqlite3 leaves the handle open until
    garbage collection, and on Windows an open handle keeps the DB file from being deleted or replaced."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect_db(path, **kwargs) -> sqlite3.Connection:
    return sqlite3.connect(str(path), factory=ClosingConnection, **kwargs)


def get_db_path() -> Path:
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_DB_PATH"):
        return Path(os.environ["TEST_DB_PATH"]).resolve()
    return DEFAULT_DB_PATH


def get_runs_dir() -> Path:
    """Where run evidence is kept. Under DIRECTOR_TESTING=1 the suite's TEST_RUNS_DIR keeps test runs out of the
    source tree (the same isolation rule as get_db_path)."""
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_RUNS_DIR"):
        return Path(os.environ["TEST_RUNS_DIR"]).resolve()
    return SOURCE_ROOT / "runs"


def get_lock_dir() -> Path:
    """Directory for cross-process lock files (next to the DB; isolated under TEST_DB_PATH in tests)."""
    return get_db_path().parent / "locks"
