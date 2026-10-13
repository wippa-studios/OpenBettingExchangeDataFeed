"""Betfair Exchange MCP server — tools over Betfair API-NG, plus our two feeds.

This file is a thin adapter in both directions:

  * Betfair -> the client  : list markets, list books, funds. Same
    :class:`betfeed.betfair.BetfairClient` the collector uses, so chunking,
    retries and login behaviour cannot differ between an LLM asking a question
    and the 5-minute odds job asking the same question.
  * Postgres -> the client : ``get_feed1`` / ``get_feed2`` / ``feed_health`` read
    what the collector wrote. An MCP client therefore never has to poll Betfair
    itself to see the current feed, and cannot burn market-data weight doing it.

Feed 1 is fixtures only (no odds). Feed 2 is feed 1 plus prices and results.
Both are the same contract document; the difference is whether prices are
present, and the document says which it is via ``priced``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from betfeed.betfair import BetfairClient
from betfeed.config import load_config
from betfeed.db import connect

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stderr,  # stdout is the MCP transport; logs must not touch it
)
log = logging.getLogger("betfair_mcp")

# Config is loaded leniently on purpose. This module is imported to *list*
# tools, and an MCP client that cannot connect shows only "server failed to
# start". So a missing or invalid config leaves the tools visible and each one
# reports what is wrong when called, instead of the process dying at import.
cfg = None
_config_error: str | None = None
try:
    cfg = load_config()
except Exception as _e:  # ConfigError, or anything a future field might raise
    _config_error = str(_e)

_trading: BetfairClient | None = None


def _cfg():
    if cfg is None:
        raise RuntimeError(
            f"Configuration is not usable: {_config_error}. "
            "Create a .env (see .env.example) — DATABASE_URL is required for the "
            "feed tools; the Betfair tools additionally need an app key."
        )
    return cfg


def trading() -> BetfairClient:
    """Lazily build the Betfair client.

    Deferred so importing this module (or listing its tools) never blocks on a
    login. Every tool that needs Betfair calls this first.
    """
    global _trading
    if _trading is None:
        from betfeed.config import validate_credentials

        missing = validate_credentials(_cfg())
        if missing:
            raise RuntimeError(
                f"missing credentials: {', '.join(missing)}. "
                "Set them in .env (see .env.example)."
            )
        _trading = BetfairClient(_cfg())
        _trading.login()
    return _trading


def _db():
    """A short-lived connection. Reads here are small and infrequent."""
    return connect(_cfg().database_url)


# No `settings={"host": ...}` here. The server runs transport="stdio" and binds
# no socket, so the setting was inert — but it was set to 0.0.0.0, the
# "listen on every interface" value, and this server has NO authentication.
# That is safe over stdio and unsafe the instant anyone switches the transport
# to SSE/HTTP, which is a one-line change someone will eventually make in a
# config rather than remembering this.
mcp = FastMCP(name="betfair_exchange_server")


# ── Betfair, live ──────────────────────────────────────────────────────────

@mcp.tool()
def get_account_funds() -> dict[str, Any]:
    """
    Retrieves the current available funds in the Betfair account.

    Returns:
        Dict[str, Any]: available_to_bet_balance (float), exposure (float),
            retained_commission (float), or {"error": "..."}.
    """
    try:
        return trading().account_funds()
    except Exception as e:
        return {"error": f"Failed to retrieve account funds: {e}"}


@mcp.tool()
def list_event_types(locale: Optional[str] = None) -> dict[str, Any]:
    """
    Retrieves the list of Event Types (sports) available on the Exchange.

    Args:
        locale (Optional[str]): response language, e.g. "en".

    Returns:
        Dict[str, Any]: {"event_types": [{"id", "name", "market_count"}]}
    """
    try:
        return {"event_types": trading().list_event_types(locale=locale)}
    except Exception as e:
        return {"error": f"Failed to list event types: {e}"}


@mcp.tool()
def list_events(event_type_ids: list[str], locale: Optional[str] = None) -> dict[str, Any]:
    """
    Retrieves the events (fixtures) for the given sports.

    Args:
        event_type_ids (list[str]): Betfair event type ids, e.g. ["1"] Soccer,
            ["8"] Tennis, ["7"] Horse Racing.
        locale (Optional[str]): response language.

    Returns:
        Dict[str, Any]: {"events": [{"id", "name", "country_code", "venue",
            "open_date", "market_count"}]}
    """
    try:
        client = trading()
        events: list[dict] = []
        for event_type_id in event_type_ids:
            for e in client.list_events(event_type_id, locale=locale):
                events.append({**e, "open_date": e["open_date"].isoformat() if e["open_date"] else None})
        return {"events": events}
    except Exception as e:
        return {"error": f"Failed to list events: {e}"}


@mcp.tool()
def list_market_catalogue(
    event_ids: list[str],
    market_type_codes: Optional[list[str]] = None,
    max_results: int = 1000,
    locale: Optional[str] = None,
) -> dict[str, Any]:
    """
    Retrieves information about published markets for the given events.
    No prices: this is fixture data.

    Args:
        event_ids (list[str]): Betfair event ids.
        market_type_codes (Optional[list[str]]): e.g. ["MATCH_ODDS"]. Filtered
            server-side, so asking for one type does not pay for every type.
        max_results (int): stop after this many markets (default 1000). Clamped
            down to CATALOGUE_MAX_RESULTS: a DELAY app key answers above 200
            with ANGX-0001, and raising that is the operator's decision, not a
            per-call one.
        locale (Optional[str]): response language.

    Returns:
        Dict[str, Any]: {"markets": [{"market_id", "market_name", "market_type",
            "market_start_time", "total_matched", "status", "event_id",
            "event_name", "runners": [{"selection_id", "runner_name",
            "handicap", "sort_priority"}]}]}
    """
    try:
        from betfeed.normalize import normalise_catalogue_market

        client = trading()
        markets = []
        for m in client.list_market_catalogue(
            event_ids, market_type_codes=market_type_codes, max_results=max_results, locale=locale
        ):
            norm = normalise_catalogue_market(m, "")
            markets.append(
                {
                    "market_id": norm["market_id"],
                    "market_name": norm["market_name"],
                    "market_type": norm["market_type_code"],
                    "market_start_time": (
                        norm["market_start_time"].isoformat() if norm["market_start_time"] else None
                    ),
                    "total_matched": norm["total_matched"],
                    "status": norm["status"],
                    "event_id": norm["fixture_id"],
                    "event_name": norm["event_name"],
                    "runners": norm["runners"],
                }
            )
            if len(markets) >= max_results:
                break
        return {"markets": markets}
    except Exception as e:
        return {"error": f"Failed to list market catalogue: {e}"}


@mcp.tool()
def list_market_book(
    market_ids: list[str],
    include_traded_volume: bool = False,
    ladder_levels: int = 3,
    locale: Optional[str] = None,
) -> dict[str, Any]:
    """
    Retrieves dynamic market data: prices, market and selection status.

    Args:
        market_ids (list[str]): Betfair market ids.
        include_traded_volume (bool): include the traded volume ladder.
        ladder_levels (int): ladder depth per side, 1-10 (default 3). Clamped
            to that range.
        locale (Optional[str]): response language.

    Returns:
        Dict[str, Any]: {"market_books": [{"market_id", "status", "inplay",
            "publish_time", "total_matched", "runners": [{"selection_id",
            "status", "last_price_traded", "total_matched",
            "available_to_back": [{"price","size"}],
            "available_to_lay": [{"price","size"}]}]}]}

    ``include_best_offers`` was removed from this signature. It was accepted,
    documented and ignored — the config always asked Betfair for best offers —
    and there is no useful answer without them: no offers means no ladder, which
    is the one thing this tool exists to return.
    """
    try:
        from betfeed.normalize import normalise_book

        client = trading()
        books = []
        for mb in client.list_market_book(
            market_ids, traded=include_traded_volume, ladder_levels=ladder_levels, locale=locale
        ):
            book = normalise_book(mb)
            books.append(
                {
                    "market_id": book["market_id"],
                    "status": book["status"],
                    "inplay": book["inplay"],
                    "publish_time": book["publish_time"].isoformat() if book["publish_time"] else None,
                    "total_matched": book["total_matched"],
                    "runners": [
                        {
                            "selection_id": s["selection_id"],
                            "status": s["status"],
                            "last_price_traded": s["last_price_traded"],
                            "total_matched": s["total_matched"],
                            "available_to_back": s["back"],
                            "available_to_lay": s["lay"],
                            "traded_volume": s["traded"],
                        }
                        for s in book["selections"]
                    ],
                }
            )
        return {"market_books": books}
    except Exception as e:
        return {"error": f"Failed to list market book: {e}"}


# ── our feeds, read from Postgres ──────────────────────────────────────────

def _feed_or_error(**kwargs) -> dict[str, Any]:
    try:
        with _db() as conn:
            from betfeed.feeds import feed_document

            return feed_document(conn, **kwargs)
    except Exception as e:
        log.exception("feed read failed")
        return {"error": f"Feed unavailable: {type(e).__name__}: {e}"}


@mcp.tool()
def get_feed1_fixtures(
    sport: Optional[str] = None,
    event_type_id: Optional[str] = None,
    market_ids: Optional[list[str]] = None,
    starts_after: Optional[str] = None,
    starts_before: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    include_finished: bool = False,
) -> dict[str, Any]:
    """
    FEED 1 — fixtures only, no odds. Refreshed every 12 hours.

    Read from the database the collector maintains, so this costs no Betfair
    requests. Every selection has price=null and the document has priced=false.

    Args:
        sport (Optional[str]): sport slug, e.g. "soccer", "horse-racing".
        event_type_id (Optional[str]): Betfair event type id, e.g. "1".
        market_ids (Optional[list[str]]): restrict to these market ids.
        starts_after (Optional[str]): ISO timestamp; markets starting at/after it.
        starts_before (Optional[str]): ISO timestamp; markets starting at/before it.
        limit (int): maximum markets (default 20). Counts markets, not runners.
        offset (int): market offset for paging.
        include_finished (bool): include fixtures that have already started.

    Returns:
        Dict[str, Any]: the contract document: {version, priced=false,
            generated_at, events: [{event_key, name, sport, competition, venue,
            starts_at, state, markets: [{market_key, name, status, state,
            starts_at, selections: [{selection_key, name, handicap,
            price=null, status}]}]}]}
    """
    from datetime import datetime

    def _ts(v: Optional[str]) -> Optional[datetime]:
        return datetime.fromisoformat(v) if v else None

    return _feed_or_error(
        priced=False,
        sport=sport,
        event_type_ids=[event_type_id] if event_type_id else None,
        market_ids=market_ids,
        starts_after=_ts(starts_after),
        starts_before=_ts(starts_before),
        limit=limit,
        offset=offset,
        include_finished=include_finished,
    )


@mcp.tool()
def get_feed2_odds(
    sport: Optional[str] = None,
    event_type_id: Optional[str] = None,
    market_ids: Optional[list[str]] = None,
    starts_after: Optional[str] = None,
    starts_before: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    include_finished: bool = False,
) -> dict[str, Any]:
    """
    FEED 2 — feed 1 plus live prices, ladders and results. Refreshed every 5 minutes.

    Same shape as feed 1 with three additions: each selection carries price,
    size, back[] and lay[] ladders; each market carries price_age_seconds and
    its result (winners, voided); each event carries the match score when the
    Scores API captured one.

    Check `stale` and `max_price_age_seconds` before trusting a price — they
    are measured against the newest snapshot actually held, so a stalled
    collector shows up as an old number rather than a silent lie.

    Args:
        sport (Optional[str]): sport slug, e.g. "soccer", "horse-racing".
        event_type_id (Optional[str]): Betfair event type id, e.g. "1".
        market_ids (Optional[list[str]]): restrict to these market ids.
        starts_after (Optional[str]): ISO timestamp; markets starting at/after it.
        starts_before (Optional[str]): ISO timestamp; markets starting at/before it.
        limit (int): maximum markets (default 20). Counts markets, not runners.
        offset (int): market offset for paging.
        include_finished (bool): include fixtures that have already started.

    Returns:
        Dict[str, Any]: the contract document, priced=true when any price was
        available, plus per-market price_age_seconds and result.
    """
    from datetime import datetime

    def _ts(v: Optional[str]) -> Optional[datetime]:
        return datetime.fromisoformat(v) if v else None

    return _feed_or_error(
        priced=True,
        sport=sport,
        event_type_ids=[event_type_id] if event_type_id else None,
        market_ids=market_ids,
        starts_after=_ts(starts_after),
        starts_before=_ts(starts_before),
        limit=limit,
        offset=offset,
        include_finished=include_finished,
    )


@mcp.tool()
def get_market_history(market_id: str, limit: int = 100) -> dict[str, Any]:
    """
    The stored price history for one market, newest snapshot first.

    This is the append-only table behind feed 2, so it answers "what did this
    market's book look like at 19:42" rather than only "what does it look like
    now".

    Args:
        market_id (str): a Betfair market id.
        limit (int): number of snapshots to return (default 100, max 1000).

    Returns:
        Dict[str, Any]: {"market_id": str, "snapshots": [{"captured_at",
            "status", "inplay", "publish_time", "total_matched", "ladder"}]}
    """
    try:
        with _db() as conn:
            from betfeed.feeds import market_history

            rows = market_history(conn, market_id, limit=min(max(limit, 1), 1000))
        return {"market_id": market_id, "snapshots": rows}
    except Exception as e:
        return {"error": f"History unavailable for {market_id}: {type(e).__name__}: {e}"}


@mcp.tool()
def feed_health() -> dict[str, Any]:
    """
    Freshness and status for both feeds, and for the collector behind them.

    Returns:
        Dict[str, Any]: {"jobs": {"fixtures": {...}, "odds": {...}},
            "active_fixtures", "active_markets", "active_selections",
            "results", "scores", "markets_with_odds", "newest_odds_at",
            "odds_age_seconds"}

        An alarm should watch `odds_age_seconds` and each job's `age_seconds`:
        those are the numbers that say whether the feed is fresh.
    """
    try:
        with _db() as conn:
            from betfeed.feeds import feed_health as _health

            return _health(conn)
    except Exception as e:
        return {"error": f"Health unavailable: {type(e).__name__}: {e}"}


@mcp.tool()
def collector_config() -> dict[str, Any]:
    """
    What the collector is configured to do: sports, market types, cadences.

    Credentials are never returned.

    Returns:
        Dict[str, Any]: event_type_ids, event_type_names, market_type_codes,
            feed1_every_minutes, feed2_every_minutes, odds_horizon_hours,
            settle_grace_minutes, ladder_levels, book_chunk_size,
            snapshot_retention_days, login_mode.
    """
    if cfg is None:
        return {"error": f"Configuration is not usable: {_config_error}"}
    return {
        "event_type_ids": cfg.event_type_ids,
        "event_type_names": cfg.event_type_names,
        "market_type_codes": cfg.market_type_codes or ["(all)"],
        "feed1_every_minutes": cfg.fixtures_interval_minutes,
        "feed2_every_minutes": cfg.odds_interval_minutes,
        "odds_horizon_hours": cfg.odds_horizon_hours,
        "settle_grace_minutes": cfg.settle_grace_minutes,
        "ladder_levels": cfg.ladder_levels,
        "book_chunk_size": cfg.book_chunk_size,
        "include_traded_volume": cfg.include_traded_volume,
        "snapshot_retention_days": cfg.snapshot_retention_days,
        "results_backfill_hours": cfg.results_backfill_hours,
        "scores_enabled": cfg.scores_enabled,
        "login_mode": cfg.login_mode,
    }


# --- Main execution block ---
def main() -> int:
    """Run the MCP server on stdio.

    A console-script entry point, so it returns an exit code rather than
    calling sys.exit inline. That keeps `uv run server.py`, `betfeed-mcp` and a
    `from betfeed.mcp_server import main` all behaving identically.
    """
    print("Starting Betfair Exchange MCP server...", file=sys.stderr)
    log.info("collector: %s", cfg.describe() if cfg else f"unconfigured ({_config_error})")
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        print(f"Error running MCP server: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())