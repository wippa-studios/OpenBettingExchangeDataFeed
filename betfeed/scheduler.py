"""The collector: two jobs, two cadences, one process.

Feed 1 (fixtures) every 12 hours, feed 2 (odds) every 5 minutes. Both are plain
functions run on their own thread with their own Postgres advisory lock, so:

  * a run that overruns its interval is skipped rather than queued — a backlog
    of stale polls is worse than a missed one;
  * a second collector process (or a human running the CLI) cannot double-poll;
  * feed 1 and feed 2 are free to overlap each other, because they touch
    different tables in practice and each holds only its own lock.

Retention is a third, much slower loop: dropping snapshot partitions is a DDL
operation and has no business running every five minutes.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

import psycopg

from .betfair import BetfairClient
from .config import Config
from .db import advisory_lock, connect, drop_old_partitions
from .jobs import run_fixtures, run_odds

log = logging.getLogger("betfeed.scheduler")

# Stable advisory locks so a manual CLI run collides with the scheduler
# instead of racing it.
LOCK_RETENTION = 0x0BEEF1003


@dataclass
class Loop:
    name: str
    interval_seconds: int
    fn: Callable[[psycopg.Connection, BetfairClient, Config], dict]

    def __post_init__(self) -> None:
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()


def _loop(
    cfg: Config,
    client: BetfairClient,
    loop: Loop,
    *,
    run_immediately: bool = True,
) -> None:
    """Run ``fn`` every interval, forever, logging and surviving its failures."""
    next_run = 0.0 if run_immediately else time.monotonic() + loop.interval_seconds
    while not loop.stop_event.is_set():
        now = time.monotonic()
        if now < next_run:
            loop.stop_event.wait(min(5.0, next_run - now))
            continue

        started = time.time()
        try:
            with connect(cfg.database_url) as conn:
                stats = loop.fn(conn, client, cfg)
            log.info(
                "[%s] done in %.1fs: %s",
                loop.name,
                time.time() - started,
                ", ".join(f"{k}={v}" for k, v in stats.items()),
            )
        except Exception as e:
            # A failed run is already recorded in feed_run by JobRun where it
            # could be; anything earlier (connection, lock) lands here.
            log.error("[%s] run failed: %s: %s", loop.name, type(e).__name__, e, exc_info=True)

        # Drift-corrected: the next run is measured from when this one ended,
        # so a slow run cannot make the cadence permanently late.
        next_run = time.monotonic() + loop.interval_seconds

    log.info("[%s] stopped", loop.name)


def _retention_loop(cfg: Config, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            with connect(cfg.database_url) as conn:
                with advisory_lock(conn, LOCK_RETENTION) as locked:
                    if locked:
                        dropped = drop_old_partitions(conn, cfg.snapshot_retention_days)
                        if dropped:
                            log.info("retention: %d partition(s) dropped", dropped)
        except Exception as e:
            log.error("retention failed: %s: %s", type(e).__name__, e)
        stop_event.wait(cfg.retention_check_every_minutes * 60)


def run_collector(cfg: Config, *, jobs: tuple[str, ...] = ("fixtures", "odds")) -> None:
    """Start the loops and block until SIGINT/SIGTERM."""
    client = BetfairClient(cfg)
    client.login()

    loops: list[Loop] = []
    if "fixtures" in jobs:
        loops.append(
            Loop("fixtures", cfg.fixtures_interval_minutes * 60, lambda c, cl, cf: run_fixtures(c, cl, cf))
        )
    if "odds" in jobs:
        loops.append(
            Loop("odds", cfg.odds_interval_minutes * 60, lambda c, cl, cf: run_odds(c, cl, cf))
        )

    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    def shutdown(signum, _frame):
        log.info("signal %s: finishing current work, then exiting", signum)
        stop_event.set()
        for loop in loops:
            loop.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for loop in loops:
        log.info(
            "[%s] every %dm", loop.name, loop.interval_seconds // 60
        )
        t = threading.Thread(target=_loop, args=(cfg, client, loop), name=f"job-{loop.name}", daemon=True)
        t.start()
        threads.append(t)

    ret = threading.Thread(
        target=_retention_loop, args=(cfg, stop_event), name="job-retention", daemon=True
    )
    ret.start()

    log.info("collector running: %s", cfg.describe())
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        for t in threads:
            t.join(timeout=30)
        log.info("collector stopped")


def run_once(cfg: Config, job: str) -> dict:
    """Run one job immediately. The operator's 'just check it works' path."""
    fn = {"fixtures": run_fixtures, "odds": run_odds}[job]
    client = BetfairClient(cfg)
    with connect(cfg.database_url) as conn:
        stats = fn(conn, client, cfg)
    return stats