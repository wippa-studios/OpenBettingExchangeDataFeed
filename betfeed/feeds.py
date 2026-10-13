"""The read side: Postgres rows -> a contract document.

Two entry points, one implementation. ``feed_document(priced=True|False)`` is
the same code either way: feed 1 simply drops the price fields. That is
deliberate — if the two feeds were built separately they would drift, and the
subtle bugs always live in the one nobody tests.

Freshness is never faked. Feed 2 carries ``price_age_seconds`` per market and
``stale`` at document level, measured against the newest snapshot we actually
hold. A consumer can decide for itself whether the number in front of it is
too old; we do not guess a threshold on their behalf.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

from .contract import ContractError, make_feed, state_for

log = logging.getLogger("betfeed.feeds")

# A feed whose newest price is older than this is flagged `stale` at document
# level. Per-market age is always reported regardless.
DEFAULT_STALE_AFTER_SECONDS = 900  # 15 min = 3 missed 5-minute polls

# The most markets one document may carry. The CLI's --n and both MCP feed
# tools take an unbounded integer from the caller; this is the ceiling they all
# resolve to.
MAX_MARKETS_PER_DOCUMENT = 1000


def feed_document(
    conn: psycopg.Connection,
    *,
    priced: bool,
    sport: str | None = None,
    event_type_ids: list[str] | None = None,
    market_ids: list[str] | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    include_finished: bool = False,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict:
    """Build a feed document. ``priced=False`` is feed 1; ``True`` is feed 2.

    ``limit`` counts **markets**, not rows. Paging on selection rows would cut a
    market in half, and a half-market is a worse answer than a shorter page:
    the contract guarantees a market has all of its runners or none.

    ``limit`` and ``offset`` are clamped here rather than at each call site,
    so the CLI and both MCP tools inherit the bound. Unclamped, a negative
    limit raised InvalidRowCountInLimitClause from psycopg (a stack trace from
    a tool call) and 10**9 asked Postgres for every row of v_feed2 — including
    both LATERALs — and shipped the result over stdio.
    """
    limit = max(1, min(int(limit), MAX_MARKETS_PER_DOCUMENT))
    offset = max(0, int(offset))

    view = "v_feed2" if priced else "v_feed1"
    where, params = _filters(
        sport=sport,
        event_type_ids=event_type_ids,
        market_ids=market_ids,
        starts_after=starts_after,
        starts_before=starts_before,
        include_finished=include_finished,
    )

    # Step 1: choose whole markets.
    chosen = conn.execute(
        f"""
        SELECT DISTINCT ON (market_id) market_id, fixture_id
          FROM {view}
         {where}
         ORDER BY market_id
         LIMIT %s OFFSET %s
        """,
        [*params, limit, offset],
    ).fetchall()
    if not chosen:
        return make_feed([])

    ids = [r["market_id"] for r in chosen]

    # Step 2: every row of those markets. fixture_id leads the ORDER BY: the
    # grouping invariant below is "same fixture as the previous row?", so any
    # ordering that interleaves two fixtures' markets emits one event twice.
    # open_date was the leading key and is NULL for catalogue events the events
    # call did not return — exactly what a truncated or racing response yields —
    # at which point ordering fell through to market_start_time and interleaved.
    rows = conn.execute(
        f"""
        SELECT * FROM {view}
         WHERE market_id = ANY(%s)
         ORDER BY fixture_id,
                  COALESCE(open_date, market_start_time) NULLS LAST,
                  market_start_time NULLS LAST,
                  market_id, sort_priority NULLS LAST, selection_id
        """,
        (ids,),
    ).fetchall()
    if not rows:
        return make_feed([])

    events = _assemble(rows, priced=priced)
    max_age = _max_price_age(rows) if priced else None
    stale = bool(max_age is not None and max_age > stale_after_seconds)
    try:
        return make_feed(
            events,
            stale=stale,
            max_price_age_seconds=int(max_age) if max_age is not None else None,
        )
    except ContractError as e:
        # A malformed row must not take the whole feed down; say so loudly
        # rather than serving a silently truncated document.
        log.error("contract violation building feed (priced=%s): %s", priced, e)
        return make_feed([], stale=True)


def _filters(
    *,
    sport: str | None,
    event_type_ids: list[str] | None,
    market_ids: list[str] | None,
    starts_after: datetime | None,
    starts_before: datetime | None,
    include_finished: bool,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []

    if sport:
        clauses.append("sport_slug = %s")
        params.append(sport.strip().lower())
    if event_type_ids:
        clauses.append("event_type_id = ANY(%s)")
        params.append(list(event_type_ids))
    if market_ids:
        clauses.append("market_id = ANY(%s)")
        params.append(list(market_ids))
    if starts_after:
        clauses.append("market_start_time >= %s")
        params.append(starts_after)
    if starts_before:
        clauses.append("market_start_time <= %s")
        params.append(starts_before)
    if not include_finished:
        # v_feed1/v_feed2 already restrict to active rows; this additionally
        # drops events that have started, which is what a board wants.
        clauses.append("(market_start_time IS NULL OR market_start_time >= now() - interval '6 hours')")

    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _assemble(rows: list[dict], *, priced: bool) -> list[dict]:
    """Fold one row per selection back into events -> markets -> selections.

    Rows arrive ordered by market_start_time, market_id, sort_priority, so a
    single pass with "same key as previous?" checks is enough; a dict-of-lists
    would work too but reorders sports unpredictably.
    """
    events: list[dict] = []
    current_fixture: str | None = None
    current_market: str | None = None
    event: dict | None = None
    market: dict | None = None

    for row in rows:
        if row["fixture_id"] != current_fixture:
            event = {
                "event_key": row["fixture_id"],
                "name": row["event_name"],
                "sport": row["sport_slug"],
                "competition": row["competition_name"],
                "venue": row["fixture_venue"],
                "country_code": row["country_code"],
                "starts_at": row["open_date"] or row["market_start_time"],
                "state": _event_state(row),
                "score": _score(row),
                "markets": [],
            }
            events.append(event)
            current_fixture = row["fixture_id"]
            current_market = None

        assert event is not None
        if row["market_id"] != current_market:
            market = {
                "market_key": row["market_id"],
                "name": row["market_name"],
                "status": row["market_status"],
                "state": state_for(row["market_status"], row.get("inplay")),
                "in_play": bool(row.get("inplay")),
                "starts_at": row["market_start_time"],
                "total_matched": row["total_matched"],
                "price_age_seconds": _age(row.get("price_age_seconds")),
                "result": _result(row),
                "selections": [],
            }
            event["markets"].append(market)
            current_market = row["market_id"]

        assert market is not None
        market["selections"].append(_selection(row, priced=priced))

    return events


def _selection(row: dict, *, priced: bool) -> dict:
    odds = row.get("odds") or {}
    back = odds.get("back") or [] if priced else []
    lay = odds.get("lay") or [] if priced else []
    price = odds.get("price") if priced else None
    if price is None and priced:
        price = odds.get("last_price_traded")
    status = row.get("result_selection_status") or odds.get("status") or row.get("selection_status")
    return {
        "selection_key": row["selection_id"],
        "name": row["runner_name"],
        "handicap": _num(row.get("handicap")),
        # feed 1 has no price column at all; this stays None.
        "price": price,
        "size": odds.get("size") if priced else None,
        "status": status,
        "result": row.get("result_selection_status") or odds.get("status"),
        "back": back,
        "lay": lay,
    }


def _result(row: dict) -> dict | None:
    if not row.get("result_status") and row.get("result_selection_status") is None:
        return None
    return {
        "status": row.get("result_status"),
        "voided": bool(row.get("result_voided")),
        # winner_selection_ids is BIGINT[], so psycopg hands back Python ints.
        # Every other identifier in the document is a string — contract.py emits
        # `selection_key` as str() — so a consumer joining result.winners to
        # selection.selection_key got no matches. The column stays a BIGINT[];
        # only the wire type is normalised here.
        "winners": [str(w) for w in (row.get("winner_selection_ids") or [])],
        "observed_at": row.get("result_observed_at"),
        "settled_at": row.get("result_settled_at"),
    }


def _score(row: dict) -> dict | None:
    """Match score, when the Scores API captured one for this fixture.

    Only v_feed2 carries these columns: a score is a result, and results are
    feed 2's business. Feed 1 stays fixtures.
    """
    if not row.get("home_score") and not row.get("away_score"):
        return None
    return {
        "home": row.get("home"),
        "away": row.get("away"),
        "home_score": row.get("home_score"),
        "away_score": row.get("away_score"),
    }


def _event_state(row: dict) -> str:
    if row.get("is_resolved"):
        return "finished"
    return state_for(row.get("market_status"), row.get("inplay"))


def _age(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _max_price_age(rows: list[dict]) -> float | None:
    ages = [float(r["price_age_seconds"]) for r in rows if r.get("price_age_seconds") is not None]
    return max(ages) if ages else None


# ── health ─────────────────────────────────────────────────────────────────

def feed_health(conn: psycopg.Connection) -> dict:
    """Freshness and status for both feeds. This is what an alarm reads."""
    runs = conn.execute(
        """
        SELECT fr.job,
               max(fr.finished_at) FILTER (WHERE fr.ok)          AS last_ok,
               max(fr.finished_at)                               AS last_finished,
               -- `IS FALSE`, not `NOT fr.ok`: a run killed by SIGKILL, or the
               -- committed-then-abandoned feed_run insert from a job that died
               -- between __enter__ and __exit__, leaves ok = NULL and
               -- finished_at = NULL forever. count(*) FILTER (WHERE NOT ok)
               -- dropped those too, so they were invisible in every counter and
               -- in failed_recently. The freshness alarm still goes stale, so
               -- this was a reporting gap rather than a silent stall.
               bool_or(fr.ok IS FALSE) FILTER (
                   WHERE fr.finished_at > now() - interval '24 hours') AS failed_recently,
               count(*) FILTER (WHERE fr.ok)                     AS ok_runs,
               count(*) FILTER (WHERE NOT fr.ok)                 AS failed_runs,
               count(*) FILTER (WHERE fr.ok IS NULL)             AS unfinished_runs
          FROM feed_run fr
         GROUP BY fr.job
        """
    ).fetchall()

    counts = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM fixture   WHERE is_active) AS active_fixtures,
          (SELECT count(*) FROM market    WHERE is_active) AS active_markets,
          (SELECT count(*) FROM selection WHERE is_active) AS active_selections,
          (SELECT count(*) FROM market_result)             AS results,
          (SELECT count(*) FROM event_score)               AS scores,
          (SELECT count(*) FROM market_odds_current)       AS markets_with_odds,
          (SELECT max(captured_at) FROM market_odds_current) AS newest_odds_at
        """
    ).fetchone()

    out: dict[str, Any] = {
        "jobs": {r["job"]: _job_health(r) for r in runs},
        **counts,
    }
    newest = counts.get("newest_odds_at")
    out["odds_age_seconds"] = (
        int((datetime.now(timezone.utc) - newest).total_seconds()) if newest else None
    )
    out.update(_data_delay(conn))
    return out


def _data_delay(conn: psycopg.Connection) -> dict:
    """Is this a DELAY app key, and how stale is the data we are serving?

    Betfair answers a DELAY key with `is_market_data_delayed: true` on every
    book and WITHHOLDS `publish_time` entirely, so the age of the data cannot be
    computed from a timestamp -- there isn't one. Verified on this key: every
    book returned is_market_data_delayed=true and publish_time=null, while our
    own captured_at is only seconds old.

    That combination is reported rather than left implicit, because "odds_age_
    seconds: 139" reads like fresh data and is not: it measures when WE polled,
    not when Betfair priced the market. Polling faster cannot improve it; only a
    LIVE app key can, and that is a commercial change, not a config one.
    """
    row = conn.execute(
        """
        SELECT count(*) AS n,
               count(publish_time) AS with_publish_time,
               min(captured_at) AS oldest_capture
          FROM market_odds_current
        """
    ).fetchone()
    if not row or not row["n"]:
        return {"app_key_delay": None, "data_age_seconds": None}
    return {
        # True when Betfair withholds publish_time on every book we hold.
        "app_key_delay": row["with_publish_time"] == 0,
        "data_age_seconds": None,
        "note": (
            "Betfair withholds publish_time on a DELAY key, so data age is not "
            "measurable. odds_age_seconds is OUR poll age, not the data's age. "
            "Polling faster does not reduce the delay; a LIVE app key does."
        ),
    }


def _job_health(row: dict) -> dict:
    now = datetime.now(timezone.utc)
    last = row.get("last_finished")
    last_ok = row.get("last_ok")
    return {
        "last_finished_at": last,
        "last_ok_at": last_ok,
        "age_seconds": int((now - last).total_seconds()) if last else None,
        "ok_runs": row["ok_runs"],
        "failed_runs": row["failed_runs"],
        # Runs that started and never finished: a process killed mid-run, or a
        # crash between JobRun.__enter__ and __exit__. Never counted as either
        # ok or failed before.
        "unfinished_runs": row.get("unfinished_runs", 0),
        "failed_recently": bool(row["failed_recently"]),
    }


def market_history(conn: psycopg.Connection, market_id: str, limit: int = 200) -> list[dict]:
    """Price history for one market, newest first (the append-only table)."""
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