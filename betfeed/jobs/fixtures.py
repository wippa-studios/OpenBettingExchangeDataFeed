"""FEED 1 — fixtures, every 12 hours. No odds.

What it does, in order:

1. Refresh the event-type cache (which sports exist).
2. For each configured sport: list events, then page the market catalogue for
   those events. ``market_projection`` deliberately requests no price data —
   this job never asks Betfair for a price, so it cannot accidentally burn
   market-data weight or store an odds value that feed 2 owns.
3. Upsert fixture / market / selection. Rows are only rewritten when a value
   actually changed, so ``last_seen_at`` stays a real "Betfair still lists
   this" signal rather than churn.
4. Retire anything Betfair stopped listing, so feed 2 stops polling it.
5. Sweep results for events that finished since the last run — a 5-minute job
   that was down at the moment of settlement would otherwise leave the result
   missing forever.
6. Optionally capture match scores, if this app key is authorised for the
   Scores API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..betfair import BetfairClient
from ..config import Config
from ..contract import slug_sport
from ..db import LOCK_FIXTURES, JobRun, advisory_lock, ensure_partitions, upsert_many
from ..normalize import (
    normalise_book,
    normalise_catalogue_market,
    result_from_book,
    score_from_betfair,
)

log = logging.getLogger("betfeed.jobs.fixtures")

# A market we have not seen in this many hours is treated as gone, so a single
# truncated catalogue response cannot retire half a sport.
STALE_AFTER_HOURS = 24


def run_fixtures(conn: psycopg.Connection, client: BetfairClient, cfg: Config) -> dict:
    stats = {"fixtures": 0, "markets": 0, "selections": 0, "retired": 0, "results": 0, "scores": 0}
    with advisory_lock(conn, LOCK_FIXTURES) as locked:
        if not locked:
            return {**stats, "skipped": True}
        with JobRun(conn, "fixtures") as run:
            ensure_partitions(conn, behind_days=2, ahead_days=3)
            _cache_event_types(conn, client, cfg)
            for event_type_id in cfg.event_type_ids:
                _collect_sport(conn, client, cfg, event_type_id, stats)
            _retire_missing(conn, stats)
            _sweep_results(conn, client, cfg, stats)
            if cfg.scores_enabled:
                _capture_scores(conn, client, cfg, stats)
            # Anything we could not fetch is reported, so a short feed is
            # visible rather than merely smaller.
            stats["truncated_events"] = len(client.truncated_events)
            stats["unfetchable_markets"] = len(client.unfetchable_markets)
            run.count(ok=stats["markets"], err=stats["unfetchable_markets"])
    return stats


def _cache_event_types(conn, client: BetfairClient, cfg: Config) -> None:
    try:
        rows = client.list_event_types()
    except Exception as e:  # a stale cache is better than no feed
        log.warning("list_event_types failed (%s); keeping the cached list", e)
        return
    upsert_many(
        conn,
        """
        INSERT INTO betfair_event_type (id, name, market_count) VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name, market_count = EXCLUDED.market_count, seen_at = now()
        """,
        [(r["id"], r["name"], r["market_count"]) for r in rows],
    )
    log.info("event types: %d seen", len(rows))


def _collect_sport(
    conn, client: BetfairClient, cfg: Config, event_type_id: str, stats: dict
) -> None:
    label = cfg.event_type_names.get(event_type_id, event_type_id)
    events = client.list_events(event_type_id)
    if not events:
        log.warning("%s: Betfair listed no events", label)
        return

    event_meta = {e["id"]: e for e in events}
    fixture_rows: list = []
    market_rows: list = []
    selection_rows: list = []
    seen_markets: list[str] = []
    seen_fixtures: list[str] = []

    for m in client.list_market_catalogue(list(event_meta)):
        market = normalise_catalogue_market(m, event_type_id)
        if not market["market_id"] or not market["fixture_id"]:
            continue
        if not market["market_type_code"]:
            # Without a type code we cannot answer "is this a market we poll".
            continue

        meta = event_meta.get(market["fixture_id"], {})
        seen_markets.append(market["market_id"])
        seen_fixtures.append(market["fixture_id"])

        fixture_rows.append(
            (
                market["fixture_id"],
                event_type_id,
                label,
                slug_sport(label),
                market["competition_id"],
                market["competition_name"],
                market["event_name"] or market["fixture_id"],
                market["country_code"],
                meta.get("venue") or market["event_venue"],
                meta.get("open_date"),
                meta.get("market_count"),
            )
        )
        market_rows.append(
            (
                market["market_id"],
                market["fixture_id"],
                market["market_type_code"],
                market["market_name"] or market["market_type_code"],
                market["market_start_time"],
                market["market_time"],
                market["market_venue"],
                market["status"],
                market["total_matched"],
            )
        )
        for r in market["runners"]:
            if r["selection_id"] is None:
                continue
            selection_rows.append(
                (r["selection_id"], market["market_id"], r["runner_name"], r["sort_priority"], r["handicap"])
            )

    # Touch the rows this run actually saw. Rows the run did NOT see keep their
    # old last_seen_at, which is exactly what retirement keys off.
    _touch(conn, "market", seen_markets)
    _touch(conn, "fixture", seen_fixtures)

    # Order matters: selection FKs market, market FKs fixture.
    stats["fixtures"] += _upsert_fixtures(conn, fixture_rows)
    stats["markets"] += _upsert_markets(conn, market_rows)
    stats["selections"] += _upsert_selections(conn, selection_rows)
    log.info(
        "%s: %d events -> %d markets, %d selections",
        label,
        len(event_meta),
        len(market_rows),
        len(selection_rows),
    )


def _touch(conn, table: str, ids: list[str]) -> None:
    if not ids:
        return
    # ids are Betfair identifiers we just read back out of the catalogue; the
    # table name is a literal from this module, never user input.
    conn.execute(
        f"UPDATE {table} SET last_seen_at = now() WHERE {_key_column(table)} = ANY(%s)",
        (list(set(ids)),),
    )


def _key_column(table: str) -> str:
    return "fixture_id" if table == "fixture" else "market_id"


def _upsert_fixtures(conn, rows: list) -> int:
    if not rows:
        return 0
    return upsert_many(
        conn,
        """
        INSERT INTO fixture (fixture_id, event_type_id, event_type_name, sport_slug,
                             competition_id, competition_name, event_name, country_code,
                             venue, open_date, market_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE SET
            event_type_id    = EXCLUDED.event_type_id,
            event_type_name  = EXCLUDED.event_type_name,
            sport_slug       = EXCLUDED.sport_slug,
            competition_id   = EXCLUDED.competition_id,
            competition_name = EXCLUDED.competition_name,
            event_name       = EXCLUDED.event_name,
            country_code     = EXCLUDED.country_code,
            venue            = EXCLUDED.venue,
            open_date        = EXCLUDED.open_date,
            market_count     = EXCLUDED.market_count,
            is_active        = TRUE,
            last_seen_at     = now()
        WHERE fixture.event_name IS DISTINCT FROM EXCLUDED.event_name
           OR fixture.open_date   IS DISTINCT FROM EXCLUDED.open_date
           OR fixture.venue       IS DISTINCT FROM EXCLUDED.venue
           OR fixture.market_count IS DISTINCT FROM EXCLUDED.market_count
           OR NOT fixture.is_active
        """,
        rows,
    )


def _upsert_markets(conn, rows: list) -> int:
    if not rows:
        return 0
    return upsert_many(
        conn,
        """
        INSERT INTO market (market_id, fixture_id, market_type_code, market_name,
                            market_start_time, market_time, venue, status, total_matched)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (market_id) DO UPDATE SET
            fixture_id        = EXCLUDED.fixture_id,
            market_type_code  = EXCLUDED.market_type_code,
            market_name       = EXCLUDED.market_name,
            market_start_time = EXCLUDED.market_start_time,
            market_time       = EXCLUDED.market_time,
            venue             = EXCLUDED.venue,
            status            = EXCLUDED.status,
            total_matched     = EXCLUDED.total_matched,
            is_active         = TRUE,
            last_seen_at      = now()
        WHERE market.market_name       IS DISTINCT FROM EXCLUDED.market_name
           OR market.market_start_time IS DISTINCT FROM EXCLUDED.market_start_time
           OR market.status            IS DISTINCT FROM EXCLUDED.status
           OR NOT market.is_active
        """,
        rows,
    )


def _upsert_selections(conn, rows: list) -> int:
    if not rows:
        return 0
    return upsert_many(
        conn,
        """
        INSERT INTO selection (selection_id, market_id, runner_name, sort_priority, handicap)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (market_id, selection_id) DO UPDATE SET
            market_id     = EXCLUDED.market_id,
            runner_name   = EXCLUDED.runner_name,
            sort_priority = EXCLUDED.sort_priority,
            handicap      = EXCLUDED.handicap,
            is_active     = TRUE,
            last_seen_at  = now()
        WHERE selection.runner_name IS DISTINCT FROM EXCLUDED.runner_name
           OR selection.market_id IS DISTINCT FROM EXCLUDED.market_id
           OR NOT selection.is_active
        """,
        rows,
    )


def _retire_missing(conn, stats: dict) -> None:
    """Retire markets Betfair stopped listing.

    Only after STALE_AFTER_HOURS: a truncated response must not retire a sport.
    Retirement is what stops feed 2 spending odds calls on dead markets.
    """
    rows = conn.execute(
        """
        UPDATE market
           SET is_active = FALSE, retired_at = now()
         WHERE is_active
           AND last_seen_at < now() - make_interval(hours => %s)
        RETURNING market_id
        """,
        (STALE_AFTER_HOURS,),
    ).fetchall()
    if not rows:
        return
    ids = [r["market_id"] for r in rows]
    conn.execute("UPDATE selection SET is_active = FALSE WHERE market_id = ANY(%s)", (ids,))
    conn.execute(
        """
        INSERT INTO fixture_change (job, entity, entity_id, field, old_value, new_value)
        SELECT 'fixtures', 'market', unnest(%s::text[]), 'is_active', 'true', 'false'
        """,
        (ids,),
    )
    stats["retired"] += len(ids)
    log.info("retired %d market(s) no longer listed", len(ids))


def _sweep_results(conn, client: BetfairClient, cfg: Config, stats: dict) -> None:
    """Capture results for markets that finished while nothing was watching.

    Betfair will not return a closed market in a request that also contains open
    ones, so this asks for exactly the unresolved set. The cost is bounded by
    RESULTS_BACKFILL_HOURS and this job runs twice a day.
    """
    scope = conn.execute(
        "SELECT market_id FROM unresolved_scope(%s) LIMIT %s",
        (cfg.results_backfill_hours, cfg.results_backfill_max_markets),
    ).fetchall()
    market_ids = [r["market_id"] for r in scope]
    if not market_ids:
        return

    log.info("results sweep: %d unresolved market(s)", len(market_ids))
    captured = 0
    for mb in client.list_market_book(market_ids, traded=False):
        book = normalise_book(mb)
        result = result_from_book(book, observed_at=datetime.now(timezone.utc))
        if result:
            write_result(conn, result)
            captured += 1
    stats["results"] += captured
    if captured:
        log.info("results sweep captured %d result(s)", captured)


def write_result(conn, result: dict) -> bool:
    """Store a settlement, then retire the market it belongs to.

    Shared with the odds job: settlement is settlement, whoever observed it.
    """
    market_id = result["market_id"]
    row = conn.execute(
        "SELECT fixture_id, market_start_time FROM market WHERE market_id = %s", (market_id,)
    ).fetchone()
    if row is None:
        log.warning("result for unknown market %s ignored", market_id)
        return False

    upsert_many(
        conn,
        """
        INSERT INTO market_result (market_id, fixture_id, status, voided,
                                   winner_selection_ids, market_start_time,
                                   observed_at, source, runners)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s)
        ON CONFLICT (market_id) DO UPDATE SET
            status               = EXCLUDED.status,
            voided               = EXCLUDED.voided,
            winner_selection_ids = EXCLUDED.winner_selection_ids,
            observed_at          = now(),
            source               = EXCLUDED.source,
            runners              = EXCLUDED.runners
        WHERE market_result.status               IS DISTINCT FROM EXCLUDED.status
           OR market_result.winner_selection_ids IS DISTINCT FROM EXCLUDED.winner_selection_ids
           OR market_result.voided               IS DISTINCT FROM EXCLUDED.voided
           OR market_result.runners              IS DISTINCT FROM EXCLUDED.runners
        """,
        [
            (
                market_id,
                row["fixture_id"],
                result["status"],
                result["voided"],
                result["winner_selection_ids"],
                row["market_start_time"],
                result.get("source", "book"),
                # market_result.runners is JSONB and psycopg3 does NOT adapt a
                # Python dict on its own -- executemany raised
                #   ProgrammingError: cannot adapt type 'dict' using placeholder '%s'
                # on the first real result. Jsonb() is the documented wrapper and
                # also encodes with json.dumps rather than repr, so a runner name
                # containing a quote cannot corrupt the payload.
                #
                # winner_selection_ids needs no wrapper: psycopg3 adapts a Python
                # list to a Postgres array, and the column is BIGINT[].
                Jsonb(result["runners"]),
            )
        ],
    )
    conn.execute(
        """
        UPDATE market
           SET is_resolved = TRUE, is_active = FALSE,
               retired_at = now(), last_seen_at = now(), status = %s
         WHERE market_id = %s
        """,
        (result.get("status"), market_id),
    )
    conn.execute(
        "UPDATE selection SET is_active = FALSE WHERE market_id = %s AND is_active",
        (market_id,),
    )
    return True


def _capture_scores(conn, client: BetfairClient, cfg: Config, stats: dict) -> None:
    """Scores API: match score where the app key is authorised for it.

    Requires a separately authorised key. Without one this is a no-op that logs
    why — settlement still works, only the 2-1 is missing.
    """
    try:
        if not client.scores_available():
            return
        finished = client.list_finished_event_ids(cfg.event_type_ids)
    except Exception as e:
        log.info("scores capture skipped: %s", e)
        return
    if not finished:
        return

    rows = []
    for start in range(0, len(finished), 50):
        for score in client.list_scores(finished[start : start + 50]):
            values = getattr(score, "values", None)
            parsed = score_from_betfair(values)
            if parsed is None:
                continue
            rows.append(
                (
                    score.event_id,
                    getattr(score, "event_status", None),
                    parsed["home"],
                    parsed["away"],
                    parsed["home_score"],
                    parsed["away_score"],
                    values,
                )
            )
    if not rows:
        return
    upsert_many(
        conn,
        """
        INSERT INTO event_score (fixture_id, event_status, home, away,
                                 home_score, away_score, values_raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE SET
            event_status = EXCLUDED.event_status,
            home         = EXCLUDED.home,
            away         = EXCLUDED.away,
            home_score   = EXCLUDED.home_score,
            away_score   = EXCLUDED.away_score,
            values_raw   = EXCLUDED.values_raw,
            captured_at  = now()
        """,
        rows,
    )
    stats["scores"] += len(rows)
    log.info("scores: captured %d event score(s)", len(rows))