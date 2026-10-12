"""betfeed command line.

    betfeed migrate                    create/refresh the schema
    betfeed collect                    run both jobs on their cadences (daemon)
    betfeed run fixtures|odds          run one job once, now
    betfeed feed1 [--sport s] [--n N] print feed 1  (fixtures, no odds)
    betfeed feed2 [--sport s] [--n N] print feed 2  (fixtures + odds + results)
    betfeed health                     freshness and job status

Every subcommand validates configuration before doing anything, so a typo in
.env is an error message rather than an empty feed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .config import load_or_exit
from .db import connect, migrate
from .feeds import feed_document, feed_health
from .scheduler import run_collector, run_once


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="betfeed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create or refresh the database schema")

    p_collect = sub.add_parser("collect", help="run the collector (both jobs)")
    p_collect.add_argument(
        "--jobs", default="fixtures,odds", help="which jobs to run (default: both)"
    )

    p_run = sub.add_parser("run", help="run a single job once")
    p_run.add_argument("job", choices=["fixtures", "odds"])

    for name, help_text in (("feed1", "fixtures, no odds"), ("feed2", "fixtures + odds + results")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--sport", help="sport slug, e.g. soccer")
        p.add_argument("--event-type", help="Betfair event type id, e.g. 1")
        p.add_argument("--market", action="append", dest="markets", help="market id (repeatable)")
        p.add_argument("--from", dest="starts_after", help="ISO timestamp, markets starting after")
        p.add_argument("--to", dest="starts_before", help="ISO timestamp, markets starting before")
        p.add_argument("--n", type=int, default=20, help="max markets (default 20)")
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--include-finished", action="store_true")
        p.add_argument("--compact", action="store_true", help="one line per market")

    sub.add_parser("health", help="freshness and job status")

    args = parser.parse_args(argv)

    needs_db = args.command != "migrate"
    cfg = load_or_exit()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("betfeed.cli")

    if args.command == "migrate":
        with connect(cfg.database_url) as conn:
            migrate(conn)
        print("schema applied")
        return 0

    if args.command == "collect":
        jobs = tuple(j.strip() for j in args.jobs.split(",") if j.strip())
        log.info("starting collector: %s", cfg.describe())
        run_collector(cfg, jobs=jobs)
        return 0

    if args.command == "run":
        stats = run_once(cfg, args.job)
        print(json.dumps(stats, indent=2))
        return 0

    if args.command in ("feed1", "feed2"):
        with connect(cfg.database_url) as conn:
            doc = feed_document(
                conn,
                priced=(args.command == "feed2"),
                sport=args.sport,
                event_type_ids=[args.event_type] if args.event_type else None,
                market_ids=args.markets,
                starts_after=_iso(args.starts_after),
                starts_before=_iso(args.starts_before),
                limit=args.n,
                offset=args.offset,
                include_finished=args.include_finished,
            )
        if args.compact:
            _print_compact(doc)
        else:
            print(json.dumps(doc, indent=2, default=str))
        return 0

    if args.command == "health":
        with connect(cfg.database_url) as conn:
            print(json.dumps(feed_health(conn), indent=2, default=str))
        return 0

    parser.print_help()
    return 1


def _print_compact(doc: dict) -> None:
    header = f"feed {'2' if doc['priced'] else '1'}  generated={doc['generated_at']}"
    if doc.get("max_price_age_seconds") is not None:
        header += f"  oldest_price={doc['max_price_age_seconds']}s"
    if doc.get("stale"):
        header += "  STALE"
    print(header)
    for event in doc["events"]:
        score = event.get("score") or {}
        line = f"  {event['event_key']:>10}  {event['name']}"
        if score.get("home_score") is not None:
            line += f"  [{score.get('home_score')}-{score.get('away_score')}]"
        print(f"{line}  ({event['state']})")
        for market in event["markets"]:
            age = market.get("price_age_seconds")
            age_s = f" +{age}s" if age is not None else ""
            result = market.get("result")
            res = ""
            if result:
                winners = ",".join(str(w) for w in result.get("winners", []))
                res = f"  result={result.get('status')}" + (f" winners={winners}" if winners else "")
                if result.get("voided"):
                    res += " VOIDED"
            print(f"      {market['market_key']:<14} {market['name'][:34]:<34} {market.get('status')}{age_s}{res}")
            for sel in market["selections"]:
                price = sel.get("price")
                price_s = f"{price}" if price is not None else "   -  "
                back = sel.get("back") or []
                lay = sel.get("lay") or []
                depth = f"b{len(back)}/l{len(lay)}"
                mark = ""
                if sel.get("result"):
                    mark = f"  <{sel['result']}>"
                print(
                    f"          {sel['selection_key']:<9} {sel['name'][:28]:<28} "
                    f"{price_s:>7}  {sel.get('status') or '':<9} {depth}{mark}"
                )


if __name__ == "__main__":
    sys.exit(main())