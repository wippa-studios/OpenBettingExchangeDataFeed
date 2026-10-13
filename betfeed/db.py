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
    _migrate_selection_key(conn)
    conn.execute(
        "INSERT INTO schema_migrations (version) VALUES (1) ON CONFLICT (version) DO NOTHING"
    )
    conn.commit()
    log.info("schema applied")


def _migrate_selection_key(conn: psycopg.Connection) -> None:
    """Re-key `selection` from (selection_id) to (market_id, selection_id).

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so fixing the
    primary key in schema.sql does nothing to a database that already has one.
    A Postgres primary key cannot be altered in place, but it can be dropped and
    re-added, which preserves the rows.

    The rows that DO survive are the ones whose (market_id, selection_id) pairs
    happen to be unique -- which every row is, since selection_id was previously
    the sole key. They are not all CORRECT (market_id may point at the wrong
    market), but they are a strict subset of what is re-derivable and the next
    fixtures run fills in the rest, because the ON CONFLICT target is now the
    composite key. No row is lost and none is trusted.
    """
    row = conn.execute(
        """
        SELECT pg_get_constraintdef(oid) AS def
          FROM pg_constraint
         WHERE conrelid = 'selection'::regclass AND contype = 'p'
        """
    ).fetchone()
    if row:
        current = row["def"] if isinstance(row, dict) else row[0]
        if "market_id" in current:
            return  # already composite
    # No primary key at all is a state this function can leave behind: the drop
    # succeeds and the re-add can then fail, and a re-run that assumed "no
    # constraint" meant "already migrated" would skip the repair forever. So an
    # absent key is treated as "needs the composite key", not "done".
    log.info("selection has no primary key; adding (market_id, selection_id)")
    # No NOT VALID here: Postgres accepts it only on FOREIGN KEY and CHECK, not
    # on a primary key. It is unnecessary anyway -- selection_id was the sole
    # key, so every existing (market_id, selection_id) pair is already distinct
    # and the constraint builds without a uniqueness failure.
    conn.execute("ALTER TABLE selection DROP CONSTRAINT IF EXISTS selection_pkey")
    conn.execute("ALTER TABLE selection ADD PRIMARY KEY (market_id, selection_id)")
    log.info("selection re-keyed to (market_id, selection_id); rows refill on next run")


def partition_name(day: date) -> str:
    return f"market_snapshot_{day:%Y%m%d}"


def ensure_partitions(conn: psycopg.Connection, *, ahead_days: int = 3, behind_days: int = 2) -> int:
    """Create the daily partitions a run is about to write into.

    No DEFAULT partition on purpose: a missing partition must fail the insert
    loudly, not let rows vanish into a catch-all that later blocks partition
    creation. Ranges are derived from the clock, so this is safe to call on
    every job start.

    Deliberately does NOT commit. It is called inside ``JobRun``, whose whole
    point is that the run's writes are one transaction; a commit here would
    flush a poll's partial writes at the start of the very run that is
    supposed to be all-or-nothing. The caller owns the commit — for both jobs
    that is ``JobRun.__exit__``.
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
            #
            # The bounds are inlined as literals, NOT bound parameters. A
            # partition bound is not a normal expression context: Postgres
            # refuses to infer a parameter's type in
            # `FOR VALUES FROM ($1) TO ($2)`, so the first fixtures run died with
            #   IndeterminateDatatype: could not determine data type of parameter $1
            # before collecting a single market. Casting the parameter
            # (`%s::date`) does not help either — it still cannot resolve.
            #
            # Inlining is safe here and no weaker than the line above it: `day`
            # is a `datetime.date` derived from the clock, `isoformat()` always
            # yields YYYY-MM-DD, and the partition NAME built from the same value
            # is already interpolated into this statement.
            lo = day.isoformat()
            hi = (day + timedelta(days=1)).isoformat()
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {partition_name(day)} "
                f"PARTITION OF market_snapshot FOR VALUES FROM ('{lo}') TO ('{hi}')"
            )
            created += 1
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
    run cost a handful of round trips, not one per row.

    It does NOT adapt a Python ``dict`` for a ``%s`` placeholder: every JSONB
    value must be wrapped in ``psycopg.types.json.Jsonb`` at the call site, or
    executemany raises
    ``ProgrammingError: cannot adapt type 'dict' using placeholder '%s'``.
    ``winner_selection_ids`` is the exception in the other direction — a Python
    ``list`` adapts to a Postgres array on its own. This docstring previously
    claimed the opposite, which is what made the unwrapped ``event_score``
    payload look correct.
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
        self._outer_autocommit = True

    def __enter__(self) -> "JobRun":
        self._started = time.monotonic()
        # psycopg3 only holds a transaction open when autocommit is off, and
        # every caller connects with autocommit=True (the right default for a
        # one-off read). Left alone, each statement below committed as it ran and
        # the rollback() in __exit__ had nothing to undo — so a run that died
        # halfway through left exactly the half-written state the docstring
        # promises cannot happen, while feed_run showed a tidy red row.
        self._outer_autocommit = self.conn.autocommit
        self.conn.autocommit = False
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
        finally:
            # Hand the connection back the way the caller had it. Both branches
            # above end with the connection idle, so this commits nothing.
            try:
                self.conn.autocommit = self._outer_autocommit
            except Exception:  # pragma: no cover - psycopg refuses only mid-txn
                log.exception("could not restore autocommit for job=%s", self.job)
        return False  # do not swallow exceptions