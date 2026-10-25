"""Postgres access.

A small thread-safe connection pool: FastAPI runs plain `def` endpoints in a
worker threadpool, so each request borrows one connection and returns it.
Every call is timed into `db_query_duration_seconds{operation=...}` so a slow
database is visible separately from a slow payment provider.
"""

import contextlib
import threading
import time

import psycopg2
import psycopg2.extras
# IntegrityError is re-exported for orders.py: a duplicate idempotency key.
from psycopg2 import IntegrityError, pool as pg_pool  # noqa: F401

from .config import DATABASE_URL, DB_POOL_WAIT_SECONDS
from .telemetry import DB_POOL_IN_USE, DB_POOL_MAX, DB_QUERY_LATENCY

_POOL: pg_pool.ThreadedConnectionPool | None = None
# One slot per connection. psycopg2's pool never waits: getconn() raises
# PoolError the moment all connections are out. Acquiring a slot first turns
# that into a short, bounded queue.
_SLOTS: threading.BoundedSemaphore | None = None


class PoolTimeout(Exception):
    """No connection became free within DB_POOL_WAIT_SECONDS."""


def init_pool(minconn: int = 1, maxconn: int = 10) -> None:
    global _POOL, _SLOTS
    if _POOL is None:
        _POOL = pg_pool.ThreadedConnectionPool(minconn, maxconn, dsn=DATABASE_URL)
        _SLOTS = threading.BoundedSemaphore(maxconn)
        DB_POOL_MAX.set(maxconn)


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.closeall()
        _POOL = None


@contextlib.contextmanager
def cursor(operation: str, commit: bool = False):
    """Borrow a connection, time the work, always return the connection."""
    if _POOL is None:
        raise RuntimeError("connection pool not initialised")
    if not _SLOTS.acquire(timeout=DB_POOL_WAIT_SECONDS):
        raise PoolTimeout(f"no free database connection within {DB_POOL_WAIT_SECONDS}s")
    try:
        conn = _POOL.getconn()
    except Exception:
        _SLOTS.release()
        raise
    DB_POOL_IN_USE.inc()
    started = time.perf_counter()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        DB_QUERY_LATENCY.labels(operation=operation).observe(time.perf_counter() - started)
        _POOL.putconn(conn)
        DB_POOL_IN_USE.dec()
        _SLOTS.release()


def ping() -> bool:
    with cursor("ping") as cur:
        cur.execute("SELECT 1 AS ok")
        return cur.fetchone()["ok"] == 1


# Assignment 2 columns. init.sql creates them on a fresh volume; this upgrades a
# database created by the Assignment 1 schema in place. Every statement is
# idempotent, so running it on every start (from either process) is safe.
_MIGRATIONS = (
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS request_hash TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS orders_idempotency_key_idx "
    "ON orders (idempotency_key)",
)


def ensure_schema() -> None:
    with cursor("migrate", commit=True) as cur:
        for statement in _MIGRATIONS:
            cur.execute(statement)
