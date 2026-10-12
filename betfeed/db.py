"""Postgres access: connections, advisory locks, partitions, retention, run log.

Deliberately small and synchronous. The collector is two jobs on two threads
that run for minutes at a time; an async pool would add ceremony without
removing a bottleneck, because Betfair's request weight — not the database —
is the thing that limits how fast we can go.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("betfeed.db")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Stable ids for pg_advisory_lock. Two jobs, two locks: a fixtures run and an
# odds run may overlap each other, but never overlap themselves.
LOCK_FIXTURES = 0x0BEEF1001
LOCK_ODDS = 0x0BEEF1002


def connect(database_url: str, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        autocommit=autocommit,
        connect_timeout=10,
        application_name="betfeed",
    )


@contextmanager
def connection(database_url: str, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = connect(database_url, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def advisory_lock(conn: psycopg.Connection, lock_id: int) -> Iterator[bool]:
    """Hold a session-level advisory lock for the duration of a job run.

    Yields False (and runs nothing) when another process already holds it —
    which is what keeps a slow poll from stacking up behind the next tick.
    """
    acquired = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (lock_id,)).fetchone()["ok"]
    if not acquired:
        log.warning("another run holds the lock (id=%s); skipping this tick", hex(lock_id))
        yield False
        return
    try:
        yield True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


# ── schema ─────────────────────────────────────────────────────────────────

def migrate(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO schema_migrations (version) VALUES (1) ON CONFLICT (version) DO NOTHING"
    )
    conn.commit()
    log.info("schema applied")


def partition_name(day: date) -> str:
    return f"market_snapshot_{day:%Y%m%d}"


def ensure_partitions(conn: psycopg.Connection, *, ahead_days: int = 3, behind_days: int = 2) -> int:
    """Create the daily partitions a run is about to write into.

    No DEFAULT partition on purpose: a missing partition must fail the insert
    loudly, not let rows vanish into a catch-all that later blocks partition
    creation. Ranges are derived from the clock, so this is safe to call on
    every job start.
    """
    today = datetime.now(timezone.utc).date()
    days = [today + timedelta(days=i) for i in range(-behind_days, ahead_days + 1)]
    created = 0
    with conn.cursor() as cur:
        for day in days:
            cur.execute(
                "SELECT 1 FROM pg_class WHERE relname = %s",
                (partition_name(day),),
            )
            if cur.fetchone():
                continue
            # Name is built from a date, so it cannot inject SQL.
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {partition_name(day)} "
                f"PARTITION OF market_snapshot FOR VALUES FROM (%s) TO (%s)",
                (day.isoformat(), (day + timedelta(days=1)).isoformat()),
            )
            created += 1
    conn.commit()
    if created:
        log.info("created %d snapshot partition(s)", created)
    return created


def drop_old_partitions(conn: psycopg.Connection, retention_days: int) -> int:
    """Drop snapshot partitions older than the retention window."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    rows = conn.execute(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'market_snapshot' AND c.relkind = 'r'
        """
    ).fetchall()
    dropped = 0
    with conn.cursor() as cur:
        for row in rows:
            name = row["relname"]
            suffix = name.rsplit("_", 1)[-1]
            try:
                day = datetime.strptime(suffix, "%Y%m%d").date()
            except ValueError:
                continue
            if day < cutoff:
                cur.execute(f"DROP TABLE IF EXISTS {name}")  # name is date-derived
                dropped += 1
    conn.commit()
    if dropped:
        log.info("retention: dropped %d snapshot partition(s) older than %s", dropped, cutoff)
    return dropped


# ── bulk writes ────────────────────────────────────────────────────────────

def upsert_many(
    conn: psycopg.Connection,
    sql: str,
    rows: Sequence[Sequence[Any]],
) -> int:
    """Run one parameterised upsert per row.

    psycopg3 pipelines executemany, so a few thousand fixture/market rows per
    run cost a handful of round trips, not one per row. ``dict`` values are
    dumped as ``jsonb`` by psycopg3's default adapters, which is why the ladder
    payloads below need no explicit wrapper.
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def record_changes(
    conn: psycopg.Connection,
    job: str,
    entity: str,
    changes: Iterable[tuple[str, str | None, str | None]],
) -> int:
    rows = [(job, entity, entity_id, field, old, new) for entity_id, field, old, new in changes]
    return upsert_many(
        conn,
        """
        INSERT INTO fixture_change (job, entity, entity_id, field, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        rows,
    )


# ── run log ────────────────────────────────────────────────────────────────

class JobRun:
    """Context manager that records one job run in feed_run.

    The freshness alarm is "max(now() - max(feed_run.finished_at)) per job", so
    a job that never records a row is as visible as one that records a failure.

    One transaction per job run: all of its writes commit together, or none do.
    A crash halfway through a poll therefore leaves the previous snapshot
    serving, rather than a half-written market book.
    """

    def __init__(self, conn: psycopg.Connection, job: str):
        self.conn = conn
        self.job = job
        self.run_id: int | None = None
        self.items_ok = 0
        self.items_err = 0
        self._started = 0.0

    def __enter__(self) -> "JobRun":
        self._started = time.monotonic()
        row = self.conn.execute(
            "INSERT INTO feed_run (job) VALUES (%s) RETURNING run_id", (self.job,)
        ).fetchone()
        self.run_id = row["run_id"]
        return self

    def count(self, ok: int = 0, err: int = 0) -> None:
        self.items_ok += ok
        self.items_err += err

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration = int((time.monotonic() - self._started) * 1000)
        error = f"{type(exc).__name__}: {exc}"[:2000] if exc is not None else None
        try:
            if exc is not None:
                # Roll the job's writes back, then log the failure in its own
                # transaction so the failure itself is durable.
                self.conn.rollback()
                self.conn.execute(
                    """
                    INSERT INTO feed_run (job, finished_at, ok, items_ok, items_err,
                                          duration_ms, error)
                    VALUES (%s, now(), FALSE, %s, %s, %s, %s)
                    """,
                    (self.job, self.items_ok, self.items_err, duration, error),
                )
                self.conn.commit()
            else:
                self.conn.execute(
                    """
                    UPDATE feed_run
                       SET finished_at = now(), ok = TRUE, items_ok = %s, items_err = %s,
                           duration_ms = %s
                     WHERE run_id = %s
                    """,
                    (self.items_ok, self.items_err, duration, self.run_id),
                )
                self.conn.commit()
        except Exception:  # never let bookkeeping mask the real error
            log.exception("could not record feed_run row for job=%s", self.job)
        return False  # do not swallow exceptions