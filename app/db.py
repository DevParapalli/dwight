import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.settings import settings

_DDL_PATH = Path(__file__).parent / "sql" / "001_init.sql"


def new_id() -> str:
    return str(uuid.uuid7())


def get_connection() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL still allows only one writer at a time, and without this a second
    # writer fails instantly rather than waiting its turn. The progress emitter
    # writes run_events from its own connection while a pipeline stage holds a
    # long write transaction, so the collision is routine, not exceptional.
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def connect():
    """One unit of work: commits on success, rolls back and re-raises on error,
    always closes. sqlite3.Connection's own context manager only commits/rolls
    back -- it never closes -- so plain `with get_connection() as conn` leaks a
    handle every time it's used."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after a database may already exist. CREATE TABLE IF NOT EXISTS
# will not add a column to a table that is already there, so without this an
# existing database keeps working right up until something reads the new column.
# This is not a migration system and is not trying to be one; it only adds
# columns, never removes or rewrites them.
_ADDED_COLUMNS = (
    ("escalations", "context", "TEXT"),
)


def _add_missing_columns(conn) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    ddl = _DDL_PATH.read_text()
    with connect() as conn:
        conn.executescript(ddl)
        _add_missing_columns(conn)


def _ddl_table_names() -> list[str]:
    """Table names taken from the DDL rather than a hand-kept list, so this
    cannot silently miss a table added later."""
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _DDL_PATH.read_text())


# Derived data that survives a reset. llm_cache is not run data: it is keyed by
# (model, prompt_hash), so it can never go stale -- a changed prompt is simply a
# different key -- and rebuilding it means paying for every model call again.
# Wiping it on a "clear the test data" action made each re-run slower for no
# benefit, which is the opposite of what the action is for.
_PRESERVED_ON_TRUNCATE = {"llm_cache"}


def truncate_all(include_cache: bool = False) -> dict[str, int]:
    """Empties every table this application owns. Returns rows deleted per table.

    Testing-only escape hatch behind settings.enable_nuke -- see the /nuke route.
    DELETE rather than DROP so the schema survives and the app keeps working
    without a restart, and sqlite_sequence is cleared too so AUTOINCREMENT
    counters (run_events.seq) restart from 1 like a fresh database.

    The table name is interpolated because SQL has no parameter slot for an
    identifier. It is safe here and only here: the names come from the DDL file
    in this repo, never from a request, and the word-character pattern that extracts them
    cannot match a quote, space or semicolon.

    Foreign keys are disabled for this connection so the tables can be emptied
    in any order. That setting is per-connection and this one is closed on the
    way out, so nothing leaks to the rest of the application.
    """
    preserved = set() if include_cache else _PRESERVED_ON_TRUNCATE
    deleted: dict[str, int] = {}
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in _ddl_table_names():
            if table in preserved:
                continue
            deleted[table] = conn.execute(f"DELETE FROM {table}").rowcount
        conn.execute("DELETE FROM sqlite_sequence")
    return {t: n for t, n in deleted.items() if n}
