"""FEED 2 — feed 1's markets plus live odds, every 5 minutes.

The scope comes from feed 1: this job never decides what to poll, it asks
``poll_scope()`` which returns the active markets whose start time falls inside
the horizon, plus a grace window behind the start time so a market that settles
between two polls still gets its result captured.

Per poll it writes three things:

    market_snapshot      append-only history (partitioned by captured_at)
    market_odds_current  the freshest book per market — the live half of feed 2
    market / selection   status and matched totals moved forward

A single ``captured_at`` is used for the whole batch, so the history is a set of
complete books at known instants rather than a smear across a few seconds. That
matters for anything that later reconstructs a market at a point in time.

Every snapshot also carries its result when the book happens to be settled, so
"what did the odds look like at the moment it went CLOSED" is answerable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg

from ..betfair import BetfairClient
from ..config import Config
from ..db import LOCK_ODDS, JobRun, advisory_lock, ensure_partitions, upsert_many
from ..normalize import ladder_payload, normalise_book, result_from_book
from .fixtures import write_result

log = logging.getLogger("betfeed.jobs.odds")


def run_odds(conn: psycopg.Connection, client: BetfairClient, cfg: Config) -> dict:
    stats = {"markets": 0, "snapshots": 0, "results": 0, "errors": 0}
    with advisory_lock(conn, LOCK_ODDS) as locked:
        if not locked:
            return {**stats, "skipped": True}
        with JobRun(conn, "odds") as run:
            ensure_partitions(conn, behind_days=2, ahead_days=3)
            _poll(conn, client, cfg, stats)
            run.count(ok=stats["markets"], err=stats["errors"])
    return stats


def _poll(conn, client: BetfairClient, cfg: Config, stats: dict) -> None:
    scope = conn.execute(
        "SELECT market_id, fixture_id FROM poll_scope(%s, %s)",
        (cfg.settle_grace_minutes, cfg.odds_horizon_hours),
    ).fetchall()
    market_ids = [r["market_id"] for r in scope]
    fixture_of = {r["market_id"]: r["fixture_id"] for r in scope}

    if not market_ids:
        log.info("nothing in scope (no fixture has run yet, or everything has settled)")
        return

    # One timestamp for the batch: the history is a set of books at known instants.
    captured_at = datetime.now(timezone.utc)
    log.info("polling %d market(s)", len(market_ids))

    snapshots: list = []
    current: list = []
    market_status_rows: list = []

    for mb in client.list_market_book(market_ids):
        book = normalise_book(mb)
        market_id = book["market_id"]
        if not market_id:
            continue
        payload = ladder_payload(book)
        fixture_id = fixture_of.get(market_id)

        snapshots.append(
            (
                captured_at,
                market_id,
                fixture_id,
                book["status"],
                book["inplay"],
                book["publish_time"],
                book["total_matched"],
                payload,
            )
        )
        current.append(
            (
                market_id,
                captured_at,
                book["status"],
                book["inplay"],
                book["bet_delay"],
                book["publish_time"],
                book["total_matched"],
                payload,
            )
        )
        market_status_rows.append(
            (market_id, book["status"], book["inplay"], book["total_matched"], captured_at)
        )

        result = result_from_book(book, observed_at=captured_at)
        if result:
            if write_result(conn, result):
                stats["results"] += 1

    if not snapshots:
        stats["errors"] += len(market_ids)
        log.warning("no books returned for %d market(s)", len(market_ids))
        return

    stats["snapshots"] += _write_snapshots(conn, snapshots)
    stats["markets"] += _write_current(conn, current)
    _update_market_status(conn, market_status_rows)
    _retire_finished(conn, cfg.settle_grace_minutes)

    log.info(
        "captured %d snapshot(s), %d result(s)", stats["snapshots"], stats["results"]
    )


def _write_snapshots(conn, rows: list) -> int:
    """Append-only history. Duplicate (captured_at, market_id) is a rerun of the
    same batch, so it is skipped rather than double-counted."""
    return upsert_many(
        conn,
        """
        INSERT INTO market_snapshot (captured_at, market_id, fixture_id, status, inplay,
                                     publish_time, total_matched, ladder)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (captured_at, market_id) DO NOTHING
        """,
        rows,
    )


def _write_current(conn, rows: list) -> int:
    """The freshest book per market — what feed 2 serves right now."""
    return upsert_many(
        conn,
        """
        INSERT INTO market_odds_current (market_id, captured_at, status, inplay, bet_delay,
                                         publish_time, total_matched, ladder)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_id) DO UPDATE SET
            captured_at   = EXCLUDED.captured_at,
            status        = EXCLUDED.status,
            inplay        = EXCLUDED.inplay,
            bet_delay     = EXCLUDED.bet_delay,
            publish_time  = EXCLUDED.publish_time,
            total_matched = EXCLUDED.total_matched,
            ladder        = EXCLUDED.ladder
        WHERE market_odds_current.captured_at < EXCLUDED.captured_at
        """,
        rows,
    )


def _update_market_status(conn, rows: list) -> None:
    upsert_many(
        conn,
        """
        UPDATE market
           SET status = %s, inplay = %s, total_matched = %s, last_odds_at = %s
         WHERE market_id = %s
        """,
        [(r[1], r[2], r[3], r[4], r[0]) for r in rows],
    )


def _retire_finished(conn, grace_minutes: int) -> int:
    """Markets past their grace window with no result: stop polling them.

    A market that finished without ever reporting a winner (abandoned, voided at
    the exchange, or one we simply lost the race to) would otherwise be polled
    forever, because it never leaves poll_scope() on its own.
    """
    rows = conn.execute(
        """
        UPDATE market
           SET is_active = FALSE, retired_at = now()
         WHERE is_active
           AND is_resolved = FALSE
           AND market_start_time < now() - make_interval(mins => %s)
        RETURNING market_id
        """,
        (grace_minutes,),
    ).fetchall()
    if rows:
        ids = [r["market_id"] for r in rows]
        conn.execute("UPDATE selection SET is_active = FALSE WHERE market_id = ANY(%s)", (ids,))
        log.info("retired %d market(s) past their settle grace", len(ids))
    return len(rows)


def snapshot_history(conn: psycopg.Connection, market_id: str, limit: int = 500) -> list[dict]:
    """Read-side helper: the price history of one market, newest first."""
    return conn.execute(
        """
        SELECT captured_at, status, inplay, publish_time, total_matched, ladder
          FROM market_snapshot
         WHERE market_id = %s
         ORDER BY captured_at DESC
         LIMIT %s
        """,
        (market_id, limit),
    ).fetchall()