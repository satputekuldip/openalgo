"""Shared SQLAlchemy engine factory.

Centralizes engine creation so every database engine in the project follows the
same connection-pooling policy. This exists to enforce one rule across the whole
codebase, including all broker ``master_contract_db.py`` modules:

    All SQLite engines MUST use NullPool.

With NullPool each operation gets a fresh connection that is closed immediately
after use, so no file descriptors to the SQLite database file accumulate. A real
pool (the SQLAlchemy default QueuePool, or an explicit ``pool_size``/``max_overflow``)
holds connections open per worker thread and leaks descriptors over the lifetime
of the long-running Gunicorn/eventlet process.

StaticPool must NOT be used for SQLite: a single shared connection causes
"bad parameter or other API misuse" and "cannot commit - SQL statements in
progress" errors when concurrent requests corrupt the shared cursor state.

For non-SQLite backends (e.g. PostgreSQL) a normal connection pool is used.

SQLite pragmas (WAL journal mode, synchronous=NORMAL) are applied to every
connection process-wide by the listener in ``database/__init__.py`` — they do
not need to be set here.

See CLAUDE.md "SQLite Connection Pooling (NullPool)" and the reference
implementation in ``database/auth_db.py``.
"""

import os
import re
import threading

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def create_db_engine(database_url=None):
    """Create a SQLAlchemy engine with the project-wide pooling policy.

    Args:
        database_url: SQLAlchemy database URL. Falls back to the ``DATABASE_URL``
            environment variable when not provided.

    Returns:
        A configured SQLAlchemy ``Engine``. SQLite URLs use ``NullPool`` with
        ``check_same_thread=False``; other backends use a QueuePool.
    """
    database_url = database_url or os.getenv("DATABASE_URL")

    if database_url and "sqlite" in database_url:
        # SQLite: NullPool so each checkout creates a fresh connection that is
        # closed immediately. Session cleanup is handled by app.py
        # teardown_appcontext. StaticPool must NOT be used (see module docstring).
        return create_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )

    if database_url and database_url.startswith(("postgresql", "postgres:")):
        return _postgres_engine(database_url)

    # Other non-SQLite backends: use a real connection pool.
    return create_engine(database_url, pool_size=50, max_overflow=100, pool_timeout=10)


# PostgreSQL: one database, one schema per former SQLite file. A URL selects its
# schema with a ``schema`` query parameter, which is not a libpq option, so it is
# removed before connecting and applied as the connection's search_path:
#
#     postgresql+psycopg://openalgo:openalgo@127.0.0.1:5433/openalgo?schema=logs
#
# About thirty modules build an engine from the same URL. Each used to get its
# own pool of up to 150 connections, which would exhaust max_connections long
# before any of them was busy, so engines are shared per URL.
_PG_POOL_SIZE = 10
_PG_MAX_OVERFLOW = 30
_PG_ENGINES = {}
_PG_ENGINES_LOCK = threading.Lock()
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _postgres_engine(database_url):
    url = make_url(database_url)
    if url.drivername in ("postgresql", "postgres"):
        # psycopg 3 is the driver this project ships; psycopg2 is not installed.
        url = url.set(drivername="postgresql+psycopg")

    schema = url.query.get("schema")
    if schema is not None and not _SCHEMA_NAME.match(schema):
        raise ValueError(f"Invalid PostgreSQL schema name in database URL: {schema!r}")
    url = url.difference_update_query(["schema"])

    key = (url.render_as_string(hide_password=False), schema)
    with _PG_ENGINES_LOCK:
        engine = _PG_ENGINES.get(key)
        if engine is not None:
            return engine

        engine = create_engine(
            url,
            pool_size=_PG_POOL_SIZE,
            max_overflow=_PG_MAX_OVERFLOW,
            pool_timeout=10,
            pool_pre_ping=True,
        )
        if schema:
            _bind_schema(engine, schema)
        _PG_ENGINES[key] = engine
        return engine


def _bind_schema(engine, schema):
    """Create ``schema`` if missing and make it every connection's search_path."""

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, connection_record):
        # The name is validated against _SCHEMA_NAME, so quoting it is enough.
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
        dbapi_connection.commit()
