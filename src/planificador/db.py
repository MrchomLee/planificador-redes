"""Engine, sessions, SQLite pragmas and schema initialisation/migrations (SPEC §4).

Transaction guidance for all modules (research 04 §2.7):

- Keep transactions short. **Never** keep a transaction open across network I/O (OAuth exchange, uploads,
  provider calls): read → commit/close → network → short guarded write (``UPDATE ... WHERE status=...`` and
  check ``rowcount``).
- With the pysqlite driver's default behaviour a transaction (and the SQLite write lock) only starts at the
  first INSERT/UPDATE/DELETE; plain SELECTs run in autocommit, so readers never block the scheduler.
- ``Database.write_lock`` is a process-wide re-entrant lock available for code that wants to serialise
  multi-statement writes (e.g. the engine's claim + event insert).
- ``session.begin_nested()`` (SAVEPOINT) is safe: because pysqlite only emits BEGIN before DML, a SAVEPOINT
  issued first would itself become the outermost transaction and ``RELEASE`` would commit it on the spot
  (surviving a later rollback). :func:`_begin_before_savepoint` opens a ``BEGIN IMMEDIATE`` transaction first.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from planificador.models import Base, SchemaVersion, utcnow

log = logging.getLogger(__name__)

#: Current schema version. Bump it and add ``MIGRATIONS[new_version] = fn`` for every schema change.
SCHEMA_VERSION = 1

#: ``version -> fn(conn)`` upgrading the schema from ``version - 1`` to ``version``. Functions run inside the
#: init transaction (all or nothing, see :func:`init_db`), in ascending order, after ``create_all`` (which
#: only creates *missing tables*; use the migration to add columns/indexes to existing tables, e.g.
#: ``conn.execute(text("ALTER TABLE ..."))``).
MIGRATIONS: dict[int, Callable[[Connection], None]] = {}

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA journal_size_limit=67108864",
)


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _begin_before_savepoint(conn: Connection, _name: str | None) -> None:
    """Make SAVEPOINTs nest inside a real transaction (see module docstring).

    ``IMMEDIATE`` takes the write lock up front (savepoints are used around writes), so the transaction never
    has to upgrade a read snapshot to a write lock, which SQLite refuses without waiting (SQLITE_BUSY)."""
    dbapi_connection = conn.connection.dbapi_connection
    if dbapi_connection is not None and not dbapi_connection.in_transaction:
        dbapi_connection.execute("BEGIN IMMEDIATE")


def make_engine(db_path: Path | str, *, echo: bool = False) -> Engine:
    """SQLite engine with our pragmas applied on every new DBAPI connection.

    ``":memory:"`` gives a single shared in-memory connection (``StaticPool``), handy for quick unit tests.
    """
    kwargs: dict[str, Any] = {
        "echo": echo,
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    if str(db_path) == ":memory:":
        url = URL.create("sqlite", database=":memory:")
        kwargs["poolclass"] = StaticPool
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = URL.create("sqlite", database=str(path))
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _apply_pragmas)
    event.listen(engine, "savepoint", _begin_before_savepoint)
    return engine


def run_migrations(
    conn: Connection,
    current: int,
    target: int = SCHEMA_VERSION,
    migrations: dict[int, Callable[[Connection], None]] | None = None,
) -> int:
    """Apply ``migrations[current+1 .. target]`` in order and stamp the version. Returns the new version."""
    migrations = MIGRATIONS if migrations is None else migrations
    version = current
    for v in range(current + 1, target + 1):
        fn = migrations.get(v)
        if fn is None:
            raise RuntimeError(f"Missing migration to schema version {v}")
        log.info("Migrating database schema to version %s", v)
        fn(conn)
        version = v
        conn.execute(
            update(SchemaVersion).where(SchemaVersion.id == 1).values(version=v, updated_at=utcnow())
        )
    return version


def init_db(engine: Engine, *, target: int | None = None) -> int:
    """Create missing tables and run pending migrations. Idempotent. Returns the schema version.

    Everything runs in one explicit SQLite transaction: pysqlite would otherwise autocommit each DDL
    statement, so a migration failing halfway would leave its ``ALTER TABLE`` applied with the old version
    stamp (and the retry would then fail on "duplicate column" forever). Migrations therefore cannot toggle
    ``PRAGMA foreign_keys`` (ignored inside a transaction); use ``PRAGMA defer_foreign_keys=ON`` instead.
    """
    target = SCHEMA_VERSION if target is None else target
    with engine.begin() as conn:
        dbapi_connection = conn.connection.dbapi_connection
        if dbapi_connection is not None and not dbapi_connection.in_transaction:
            dbapi_connection.execute("BEGIN IMMEDIATE")
        Base.metadata.create_all(conn)
        current = conn.execute(select(SchemaVersion.version).where(SchemaVersion.id == 1)).scalar()
        if current is None:
            # Fresh database: create_all just built the current schema.
            conn.execute(SchemaVersion.__table__.insert().values(id=1, version=target, updated_at=utcnow()))
            return target
        if current > target:
            log.warning("Database schema version %s is newer than this app (%s)", current, target)
            return int(current)
        if current < target:
            return run_migrations(conn, int(current), target)
        return int(current)


class Database:
    """Owns the engine and the session factory for one SQLite file."""

    def __init__(self, db_path: Path | str, *, echo: bool = False) -> None:
        self.path = db_path if str(db_path) == ":memory:" else Path(db_path)
        self.engine = make_engine(db_path, echo=echo)
        self.SessionLocal: sessionmaker[Session] = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=True
        )
        self.write_lock = threading.RLock()

    def init(self) -> int:
        return init_db(self.engine)

    def session(self) -> Session:
        """New session (caller closes it; prefer ``session_scope`` or the FastAPI dependency)."""
        return self.SessionLocal()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """Commit on success, roll back on error, always close."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def checkpoint(self) -> None:
        """``PRAGMA wal_checkpoint(TRUNCATE)`` — call on clean shutdown / daily."""
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # pragma: no cover - best effort
            log.exception("WAL checkpoint failed")

    def backup(self, dest: Path) -> Path:
        """Consistent online copy via ``VACUUM INTO`` (never copy the live file while running)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with self.engine.connect() as conn:
            conn.execute(text("VACUUM INTO :dest"), {"dest": str(dest)})
        return dest

    def dispose(self) -> None:
        self.engine.dispose()

    def __repr__(self) -> str:
        return f"<Database {self.path}>"


# ------------------------------------------------------------------------------- current database
# ``create_app`` installs its Database here so code running outside a request (templating globals,
# background helpers) can open a session with ``db.SessionLocal()``. Prefer ``app.state.db`` when available.

_current: Database | None = None


def set_current_database(database: Database | None) -> None:
    global _current
    _current = database


def get_current_database() -> Database:
    if _current is None:
        raise RuntimeError("No database configured yet (create_app() has not run)")
    return _current


def SessionLocal() -> Session:  # noqa: N802 - mirrors the conventional sessionmaker name
    """New session on the current app database (caller must close it)."""
    return get_current_database().session()
