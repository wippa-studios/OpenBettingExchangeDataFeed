"""End-to-end: fixtures -> odds -> feeds, against a real Postgres.

This is the test that proves the two feeds are actually different documents
over one database, that history is retained alongside the freshest state, and
that a settled market retires itself.

It is skipped unless TEST_DATABASE_URL points at a real database — these tests
create and drop their own schema, so they must never touch the production feed.
Run it with:

    TEST_DATABASE_URL=postgresql://betfair:pw@127.0.0.1:5432/betfeed_test \
        .venv/bin/python -m pytest tests/test_pipeline.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from betfeed.betfair import BetfairClient
from betfeed.db import connect, ensure_partitions, migrate
from betfeed.feeds import feed_document, feed_health
from betfeed.jobs import run_fixtures, run_odds
from conftest import (
    NOW,
    FakeBetting,
    FakeCatalogueMarket,
    FakeEvent,
    FakeEventResult,
    FakeMarketBook,
    FakeRunnerBook,
    FakeRunnerCatalogue,
    FakeScores,
    install_fake_client,
    make_cfg,
)

TEST_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="set TEST_DATABASE_URL to run the pipeline tests"
)


@pytest.fixture
def db():
    conn = connect(TEST_URL)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    migrate(conn)
    yield conn
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.commit()
    conn.close()


def soccer_world(start: datetime | None = None):
    """One soccer fixture, two markets, five runners.

    ``start`` is the markets' start time, and it is not only decoration: the
    fixtures job re-upserts ``market_start_time`` from the catalogue on every
    run, so a test that backdates the market in the database must build the
    fakes with a past ``start`` too or the next run puts it back in the future.
    """
    start = start or (NOW + timedelta(hours=2))
    fixture = FakeEvent("3001", "Test Utd v Arsenal", country_code="GB", venue="Old Trafford")
    markets = {
        "1.100": FakeCatalogueMarket(
            "1.100", fixture, "MATCH_ODDS", market_name="Match Odds",
            runners=[FakeRunnerCatalogue(11, "Test Utd", 1), FakeRunnerCatalogue(12, "Draw", 2),
                     FakeRunnerCatalogue(13, "Arsenal", 3)],
            start=start,
        ),
        "1.101": FakeCatalogueMarket(
            "1.101", fixture, "OVER_UNDER_25", market_name="Total Goals 0-2",
            runners=[FakeRunnerCatalogue(21, "Under", 1), FakeRunnerCatalogue(22, "Over", 2)],
            start=start,
        ),
    }
    return fixture, markets


def open_book(market_id, runners):
    return FakeMarketBook(market_id, runners, status="OPEN", inplay=False,
                          publish_time=NOW, total_matched=5000.0)


def settled_book(market_id, runners):
    return FakeMarketBook(market_id, runners, status="CLOSED_EVENT", inplay=True,
                          publish_time=NOW, total_matched=9000.0)


def run_job(db, job, betting, scores=None, **cfg_overrides):
    cfg = make_cfg(DATABASE_URL=TEST_URL, **cfg_overrides)
    client = BetfairClient(cfg, sleeper=lambda _s: None)
    install_fake_client(client, betting=betting, scores=scores or FakeScores())
    return job(db, client, cfg)


# ── feed 1 ────────────────────────────────────────────────────────────────

def test_feed1_populates_fixtures_with_no_prices(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(
        events={"1": [FakeEventResult(fixture, market_count=2)]},
        catalogue={"3001": list(markets.values())},
    )
    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    assert stats["markets"] == 2
    assert stats["selections"] == 5

    doc = feed_document(db, priced=False, limit=10)
    assert doc["priced"] is False
    assert len(doc["events"]) == 1
    event = doc["events"][0]
    assert event["event_key"] == "3001"
    assert event["name"] == "Test Utd v Arsenal"
    assert event["sport"] == "soccer"
    assert {m["market_key"] for m in event["markets"]} == {"1.100", "1.101"}
    for market in event["markets"]:
        for sel in market["selections"]:
            assert sel["price"] is None, "feed 1 must never carry a price"
            assert sel["back"] == [] and sel["lay"] == []


def test_feed1_is_idempotent(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(
        events={"1": [FakeEventResult(fixture, market_count=2)]},
        catalogue={"3001": list(markets.values())},
    )
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    first = feed_document(db, priced=False, limit=10)
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    second = feed_document(db, priced=False, limit=10)
    assert first["events"] == second["events"]

    counts = db.execute(
        "SELECT (SELECT count(*) FROM fixture) f, (SELECT count(*) FROM market) m,"
        "       (SELECT count(*) FROM selection) s"
    ).fetchone()
    assert (counts["f"], counts["m"], counts["s"]) == (1, 2, 5)


def test_feed1_sorts_runners_by_sort_priority(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    doc = feed_document(db, priced=False, market_ids=["1.100"])
    names = [s["name"] for s in doc["events"][0]["markets"][0]["selections"]]
    assert names == ["Test Utd", "Draw", "Arsenal"]  # Betfair's order, not alphabetical


def test_a_market_betfair_stops_listing_is_retired(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # Simulate a stale last_seen_at, then list only one market next time.
    db.execute("UPDATE market SET last_seen_at = now() - interval '48 hours'")
    db.commit()
    betting.catalogue = {"3001": [markets["1.100"]]}
    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    assert stats["retired"] == 1

    doc = feed_document(db, priced=False, limit=10)
    assert {m["market_key"] for m in doc["events"][0]["markets"]} == {"1.100"}


# ── feed 2 ────────────────────────────────────────────────────────────────

def test_feed2_adds_prices_and_history(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # Push the fixtures' start times inside the poll horizon.
    db.execute("UPDATE market SET market_start_time = now() + interval '2 hours'")
    db.commit()

    books = {
        "1.100": open_book("1.100", [
            FakeRunnerBook(11, back=[(2.3, 100), (2.28, 250)], lay=[(2.24, 80)]),
            FakeRunnerBook(12, back=[(3.4, 60)], lay=[(3.5, 90)]),
            FakeRunnerBook(13, back=[(3.0, 40)], lay=[(3.04, 70)]),
        ]),
        "1.101": open_book("1.101", [
            FakeRunnerBook(21, back=[(1.7, 500)], lay=[(1.76, 400)]),
            FakeRunnerBook(22, back=[(2.1, 300)], lay=[(2.14, 250)]),
        ]),
    }
    run_job(db, run_odds, FakeBetting(books=books))

    doc = feed_document(db, priced=True, limit=10)
    assert doc["priced"] is True
    match = next(m for m in doc["events"][0]["markets"] if m["market_key"] == "1.100")
    home = match["selections"][0]
    assert home["price"] == 2.3
    assert home["size"] == 100
    assert [r["price"] for r in home["back"]] == [2.3, 2.28]
    assert [r["price"] for r in home["lay"]] == [2.24]
    assert match["price_age_seconds"] is not None

    # History is retained alongside the freshest state.
    snapshots = db.execute("SELECT count(*) AS n FROM market_snapshot").fetchone()
    current = db.execute("SELECT count(*) AS n FROM market_odds_current").fetchone()
    assert snapshots["n"] == 2
    assert current["n"] == 2


def test_a_second_poll_appends_history_and_replaces_the_current_book(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("UPDATE market SET market_start_time = now() + interval '2 hours'")
    db.commit()

    first = {"1.100": open_book("1.100", [FakeRunnerBook(11, back=[(2.3, 100)])])}
    run_job(db, run_odds, FakeBetting(books=first))
    second = {"1.100": open_book("1.100", [FakeRunnerBook(11, back=[(2.6, 120)])])}
    run_job(db, run_odds, FakeBetting(books=second))

    # History keeps both observations...
    rows = db.execute(
        "SELECT ladder FROM market_snapshot WHERE market_id='1.100' ORDER BY captured_at"
    ).fetchall()
    assert [r["ladder"]["selections"][0]["price"] for r in rows] == [2.3, 2.6]
    # ...while the current row serves only the newest.
    doc = feed_document(db, priced=True, market_ids=["1.100"])
    assert doc["events"][0]["markets"][0]["selections"][0]["price"] == 2.6


def test_stale_prices_are_flagged_not_hidden(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("UPDATE market SET market_start_time = now() + interval '2 hours'")
    db.commit()
    run_job(db, run_odds, FakeBetting(books={
        "1.100": open_book("1.100", [FakeRunnerBook(11, back=[(2.3, 100)])])
    }))

    # Age the snapshot past the staleness threshold.
    db.execute("UPDATE market_odds_current SET captured_at = now() - interval '1 hour'")
    db.commit()

    doc = feed_document(db, priced=True, market_ids=["1.100"])
    assert doc["stale"] is True
    assert doc["max_price_age_seconds"] > 900
    # The price is still served — a trading customer with a stale price beats a
    # customer with no price — but it is labelled.
    assert doc["events"][0]["markets"][0]["selections"][0]["price"] == 2.3


# ── results ───────────────────────────────────────────────────────────────

def test_a_settled_book_becomes_a_result_and_retires_the_market(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # A market that has just started and settles on our next poll.
    db.execute("UPDATE market SET market_start_time = now() - interval '10 minutes'")
    db.commit()
    run_job(db, run_odds, FakeBetting(books={
        "1.100": settled_book("1.100", [
            FakeRunnerBook(11, status="WINNER"), FakeRunnerBook(12, status="LOSER"),
            FakeRunnerBook(13, status="LOSER"),
        ]),
    }))

    result = db.execute("SELECT * FROM market_result WHERE market_id='1.100'").fetchone()
    assert result is not None
    assert list(result["winner_selection_ids"]) == [11]
    assert result["status"] == "CLOSED_EVENT"
    assert result["voided"] is False

    market = db.execute("SELECT is_active, is_resolved FROM market WHERE market_id='1.100'").fetchone()
    assert market["is_resolved"] is True
    assert market["is_active"] is False  # out of poll scope for good

    scope = db.execute("SELECT market_id FROM poll_scope(%s, %s)", (30, 168)).fetchall()
    assert "1.100" not in {r["market_id"] for r in scope}

    # It is out of poll scope, but it is still readable and its result is
    # attached: v_feed1 publishes a resolved market for seven days after it
    # settles (commit 8d8544f, "resolved markets were invisible"), because the
    # result is the whole reason to keep it.
    #
    # This used to assert `match is None`. It passed only because every document
    # came back empty (#10) — a third assertion in this file that could not fail.
    doc = feed_document(db, priced=True, include_finished=True, limit=10)
    match = next(m for e in doc["events"] for m in e["markets"] if m["market_key"] == "1.100")
    assert match["result"]["status"] == "CLOSED_EVENT"
    assert match["result"]["winners"] == ["11"]


def test_results_are_readable_while_the_market_is_still_active(db):
    """A settled market's result must be available even before retirement."""
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("UPDATE market SET market_start_time = now() - interval '10 minutes'")
    db.commit()

    run_job(db, run_odds, FakeBetting(books={
        "1.100": settled_book("1.100", [FakeRunnerBook(11, status="WINNER"),
                                        FakeRunnerBook(12, status="LOSER"),
                                        FakeRunnerBook(13, status="LOSER")]),
    }))

    # Re-activate to inspect what a consumer would have seen at settlement.
    db.execute("UPDATE market SET is_active = TRUE WHERE market_id = '1.100'")
    db.execute("UPDATE selection SET is_active = TRUE WHERE market_id = '1.100'")
    db.commit()

    doc = feed_document(db, priced=True, market_ids=["1.100"], include_finished=True)
    match = doc["events"][0]["markets"][0]
    assert match["result"]["status"] == "CLOSED_EVENT"
    assert match["result"]["winners"] == ["11"]
    assert {s["result"] for s in match["selections"]} == {"WINNER", "LOSER"}


def test_the_fixtures_job_backfills_a_result_nobody_was_there_to_see(db):
    """If the odds job was down when the event finished, feed 1 still finds it."""
    # The event finished an hour ago and nobody has a result for it. The start
    # time is built into the FAKES, not written to the database afterwards: every
    # fixtures run re-upserts market_start_time from the catalogue, so a fake
    # still claiming NOW+2h would push the market back into the future and the
    # sweep's unresolved_scope (market_start_time < now()) would never see it.
    fixture, markets = soccer_world(start=NOW - timedelta(hours=1))
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    sweep = FakeBetting(books={
        "1.100": settled_book("1.100", [FakeRunnerBook(11, status="WINNER"),
                                        FakeRunnerBook(12, status="LOSER"),
                                        FakeRunnerBook(13, status="LOSER")]),
        "1.101": open_book("1.101", [FakeRunnerBook(21), FakeRunnerBook(22)]),
    })
    betting.books = sweep.books
    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    assert stats["results"] == 1
    assert db.execute("SELECT count(*) n FROM market_result").fetchone()["n"] == 1


def test_a_voided_market_is_marked_voided(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("UPDATE market SET market_start_time = now() - interval '10 minutes'")
    db.commit()

    run_job(db, run_odds, FakeBetting(books={
        "1.100": FakeMarketBook("1.100", [FakeRunnerBook(11, status="REMOVED"),
                                          FakeRunnerBook(12, status="REMOVED"),
                                          FakeRunnerBook(13, status="REMOVED")],
                                 status="VOIDED"),
    }))
    result = db.execute("SELECT voided, winner_selection_ids FROM market_result").fetchone()
    assert result["voided"] is True
    assert list(result["winner_selection_ids"]) == []


def test_a_result_survives_the_same_run_that_retires_the_market(db):
    """The collector was down when the event settled, and Betfair has since
    stopped listing it — the exact case _sweep_results exists for.

    Retiring first set is_active=FALSE, and unresolved_scope filters on
    is_active, so one run retired exactly the market it was there to resolve:
    inactive, unresolved, invisible, and no later job able to pick it up. The
    result was lost, not delayed.
    """
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # Time passes: last seen and kicked off 30 hours ago, inside the 48h
    # results backfill window and past the 24h retirement threshold.
    db.execute("""
        UPDATE market
           SET last_seen_at      = now() - interval '30 hours',
               market_start_time  = now() - interval '30 hours'
    """)
    db.commit()

    # ...and Betfair no longer lists it at all, which is what arms retirement.
    betting.events = {}
    betting.catalogue = {}
    betting.books = {
        "1.100": settled_book("1.100", [FakeRunnerBook(11, status="WINNER"),
                                        FakeRunnerBook(12, status="LOSER"),
                                        FakeRunnerBook(13, status="LOSER")]),
    }

    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    assert stats["results"] == 1
    row = db.execute("SELECT is_resolved, is_active FROM market WHERE market_id='1.100'").fetchone()
    assert row["is_resolved"] is True
    assert db.execute(
        "SELECT count(*) n FROM market_result WHERE market_id='1.100'"
    ).fetchone()["n"] == 1
    # And the result is readable by a consumer.
    doc = feed_document(db, priced=True, include_finished=True, market_ids=["1.100"])
    assert doc["events"][0]["markets"][0]["result"]["winners"] == ["11"]


def test_a_market_inside_the_backfill_window_is_not_retired_unresolved(db):
    """Retirement is permanent; the sweep's LIMIT means "not resolved yet" is
    not the same as "cannot be resolved". A market that started inside
    RESULTS_BACKFILL_HOURS survives a stale last_seen_at so a later run can
    still resolve it."""
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("""
        UPDATE market
           SET last_seen_at     = now() - interval '30 hours',
               market_start_time = now() - interval '2 hours'
    """)
    db.commit()

    betting.events = {}
    betting.catalogue = {}
    betting.books = {}  # Betfair returns nothing fetchable for it
    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    assert stats["results"] == 0
    assert stats["retired"] == 0
    assert db.execute("SELECT is_active FROM market WHERE market_id='1.100'").fetchone()[
        "is_active"] is True

    # Past the window it does retire — the protection is bounded, not a
    # permanent amnesty.
    db.execute("UPDATE market SET market_start_time = now() - interval '5 days'")
    db.commit()
    stats = run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    assert stats["retired"] == 2


def test_the_results_sweep_runs_before_retirement(db, monkeypatch):
    """Pins the order itself.

    The behaviour tests above cannot distinguish "swept first" from "never
    retired", because both produce a stored result. If the two calls are ever
    swapped back, this is what notices.
    """
    from betfeed.jobs import fixtures as fixtures_job

    order: list[str] = []
    for name in ("_sweep_results", "_retire_missing"):
        real = getattr(fixtures_job, name)

        def spy(*a, _real=real, _name=name, **k):
            order.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(fixtures_job, name, spy)

    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    assert order[:2] == ["_sweep_results", "_retire_missing"]


def test_a_match_forty_five_minutes_in_is_still_polled_and_still_visible(db):
    """Kickoff is not the end of the event.

    The default sports are EVENT_TYPE_IDS=1,8 — soccer and tennis — and a
    soccer match runs about two hours after kickoff. A clock-only scope dropped
    the market out of poll_scope 30 minutes after start time, then
    _retire_finished set is_active=FALSE, is_resolved=FALSE, which satisfies
    NEITHER branch of v_feed1's predicate: the live match left both feeds
    entirely — not stale, gone — for up to twelve hours, and could not settle
    in the meantime.
    """
    live = FakeEvent("3001", "Test Utd v Arsenal")
    soon = FakeEvent("3002", "Chelsea v City")
    markets = {
        "1.100": FakeCatalogueMarket(
            "1.100", live, "MATCH_ODDS",
            runners=[FakeRunnerCatalogue(11, "Test Utd", 1),
                     FakeRunnerCatalogue(12, "Draw", 2)],
            start=NOW - timedelta(minutes=45),
        ),
        "1.200": FakeCatalogueMarket(
            "1.200", soon, "MATCH_ODDS",
            runners=[FakeRunnerCatalogue(31, "Chelsea", 1),
                     FakeRunnerCatalogue(32, "City", 2)],
            start=NOW + timedelta(hours=2),
        ),
    }
    betting = FakeBetting(
        events={"1": [FakeEventResult(live), FakeEventResult(soon)]},
        catalogue={"3001": [markets["1.100"]], "3002": [markets["1.200"]]},
    )
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # In scope alongside the upcoming match, though it started 45 minutes ago.
    scope = db.execute("SELECT market_id FROM poll_scope(%s, %s)", (30, 168)).fetchall()
    assert {r["market_id"] for r in scope} == {"1.100", "1.200"}

    # One healthy odds cycle. The live match is read...
    run_job(db, run_odds, FakeBetting(books={
        "1.100": FakeMarketBook("1.100", [FakeRunnerBook(11, status="ACTIVE")],
                                status="OPEN", inplay=True, publish_time=NOW),
    }))
    assert db.execute(
        "SELECT count(*) n FROM market_odds_current WHERE market_id='1.100'"
    ).fetchone()["n"] == 1

    # ...and it must survive the cycle: still active, still in both feeds.
    assert db.execute("SELECT is_active FROM market WHERE market_id='1.100'").fetchone()[
        "is_active"] is True
    doc = feed_document(db, priced=True, limit=10)
    keys = {m["market_key"] for e in doc["events"] for m in e["markets"]}
    assert "1.100" in keys and "1.200" in keys
    live_match = next(m for e in doc["events"] for m in e["markets"] if m["market_key"] == "1.100")
    assert live_match["in_play"] is True


def test_a_finished_market_leaves_poll_scope_and_retires(db):
    """The other half: status, not the clock, is what ends the poll.

    A terminal status past the grace window takes the market out of scope and
    retires it, so an abandoned market still stops being polled.
    """
    fixture, markets = soccer_world(start=NOW - timedelta(hours=3))
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    # Betfair says it is over but we hold no result.
    db.execute("UPDATE market SET status = 'CLOSED_EVENT' WHERE market_id = '1.100'")
    db.commit()
    scope = db.execute("SELECT market_id FROM poll_scope(%s, %s)", (30, 168)).fetchall()
    assert "1.100" not in {r["market_id"] for r in scope}

    run_job(db, run_odds, FakeBetting(books={}))
    assert db.execute("SELECT is_active FROM market WHERE market_id='1.100'").fetchone()[
        "is_active"] is False


def test_one_malformed_ladder_row_does_not_take_the_whole_feed_down(db):
    """One bad row in a JSONB column used to take BOTH feeds down.

    There is no per-row isolation on the read side, so a single malformed
    market_odds_current.ladder made every SELECT against v_feed2 raise. The
    COALESCE caught a MISSING 'selections' key; it does not catch a JSON null
    (jsonb 'null' is non-NULL to COALESCE), an object or a scalar, and the
    ::BIGINT cast throws on a non-numeric selection_id.
    """
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    db.execute("UPDATE market SET market_start_time = now() + interval '2 hours'")
    db.commit()
    run_job(db, run_odds, FakeBetting(books={
        "1.100": open_book("1.100", [FakeRunnerBook(11, back=[(2.3, 100)])])}))

    for shape in (
        '{"selections": null}',
        '{"selections": {"a": 1}}',
        '"a scalar"',
        '{"selections": [{"selection_id": "abc", "price": 2.3}]}',
        '{"status": "OPEN"}',  # the shape that always worked
    ):
        db.execute(
            "UPDATE market_odds_current SET ladder = %s::jsonb WHERE market_id = '1.100'",
            (shape,),
        )
        db.commit()
        doc = feed_document(db, priced=True, market_ids=["1.100"])
        match = doc["events"][0]["markets"][0]
        # The market is still served; its unreadable prices come back empty.
        assert match["market_key"] == "1.100", shape
        assert match["selections"][0]["price"] is None, shape


# ── scores ────────────────────────────────────────────────────────────────

def _score(event_id, home="Test Utd", hs="2", away="Arsenal", as_="1"):
    """A betfairlightweight Score stand-in: `values` is the raw JSON dict."""
    return SimpleNamespace(
        event_id=event_id,
        event_status="POST",
        values={"home": {"team": home, "score": hs}, "away": {"team": away, "score": as_}},
    )


def test_a_score_is_stored_with_its_raw_values_payload(db):
    """values_raw is JSONB, so a raw dict must be wrapped.

    Unwrapped it raised
      ProgrammingError: cannot adapt type 'dict' using placeholder '%s'
    on the FIRST score of every run, which (before the guard was widened) took
    the whole fixtures run down. Nothing wrote a score through
    _capture_scores before this test.
    """
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    scores = FakeScores(available=["3001"], scores=[_score("3001")])

    stats = run_job(db, run_fixtures, betting, scores=scores, SCORES_ENABLED="true")
    assert stats["scores"] == 1

    row = db.execute(
        "SELECT home, away, home_score, away_score, values_raw FROM event_score"
    ).fetchone()
    assert (row["home"], row["away"]) == ("Test Utd", "Arsenal")
    assert (row["home_score"], row["away_score"]) == ("2", "1")
    # Stored as JSON, not as a repr, and not dropped.
    assert row["values_raw"]["home"]["team"] == "Test Utd"


def test_a_broken_scores_api_cannot_take_the_fixtures_run_down(db):
    """The docstring's promise is 'settlement still works, only the 2-1 is
    missing'. It used to hold only for an UNAUTHORISED key: the guard stopped
    after the availability probe, so a failure in list_scores or in the upsert
    propagated out of run_fixtures and killed the run."""
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})

    class ExplodingScores(FakeScores):
        def list_scores(self, update_keys=None, locale=None):
            raise RuntimeError("scores exploded")

    scores = ExplodingScores(available=["3001"], scores=[])
    stats = run_job(db, run_fixtures, betting, scores=scores, SCORES_ENABLED="true")

    assert stats["scores"] == 0
    # ...and the fixtures collected before the scores step are all still there,
    # because the whole run is one transaction that completed.
    assert stats["markets"] == 2
    assert db.execute("SELECT count(*) n FROM market").fetchone()["n"] == 2
    assert db.execute("SELECT ok FROM feed_run WHERE job='fixtures' ORDER BY run_id DESC LIMIT 1"
                      ).fetchone()["ok"] is True


def test_a_score_for_an_event_we_never_collected_is_ignored(db):
    """event_score.fixture_id REFERENCES fixture(fixture_id).

    The Scores API lists POST events that listEvents need not still return.
    Writing one we never collected is an FK violation, and inside the widened
    guard that would cost us every OTHER score as well.
    """
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    scores = FakeScores(available=["3001", "9999"],
                        scores=[_score("3001"), _score("9999")])

    stats = run_job(db, run_fixtures, betting, scores=scores, SCORES_ENABLED="true")
    assert stats["scores"] == 1
    ids = [r["fixture_id"] for r in
           db.execute("SELECT fixture_id FROM event_score").fetchall()]
    assert ids == ["3001"]


# ── plumbing ──────────────────────────────────────────────────────────────

def test_health_reports_freshness_and_failures(db):
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")

    health = feed_health(db)
    assert health["jobs"]["fixtures"]["ok_runs"] == 1
    assert health["jobs"]["fixtures"]["failed_runs"] == 0
    assert health["active_fixtures"] == 1
    assert health["active_markets"] == 2
    assert health["odds_age_seconds"] is None  # no odds captured yet

    db.execute("UPDATE market SET market_start_time = now() + interval '2 hours'")
    db.commit()
    run_job(db, run_odds, FakeBetting(books={
        "1.100": open_book("1.100", [FakeRunnerBook(11, back=[(2.3, 100)])]),
        "1.101": open_book("1.101", [FakeRunnerBook(21), FakeRunnerBook(22)]),
    }))

    health2 = feed_health(db)
    assert health2["markets_with_odds"] == 2
    assert health2["odds_age_seconds"] is not None
    assert health2["jobs"]["odds"]["ok_runs"] == 1
    assert health2["newest_odds_at"] is not None


def test_a_failed_job_is_recorded_and_rolls_back(db):
    """A job that dies mid-poll must not leave half-written state, and must be
    visible in feed_run."""
    fixture, markets = soccer_world()
    betting = FakeBetting(events={"1": [FakeEventResult(fixture, market_count=2)]},
                          catalogue={"3001": list(markets.values())})
    run_job(db, run_fixtures, betting, SCORES_ENABLED="false")
    before = db.execute("SELECT count(*) n FROM market").fetchone()["n"]
    # Without a seeded table "nothing changed" below is satisfied by an empty
    # database no matter what the failed run did.
    assert before == 2

    class Exploding(FakeBetting):
        """Collects soccer — writing two markets the seeded run never saw — and
        then dies on the next sport, with those writes still uncommitted."""

        def __init__(self, inner):
            other = FakeEvent("3002", "Chelsea v City", country_code="GB", venue="Stamford Bridge")
            super().__init__(
                events={"1": inner.events["1"] + [FakeEventResult(other)]},
                catalogue={
                    "3001": inner.catalogue["3001"],
                    "3002": [
                        FakeCatalogueMarket(
                            "1.200", other, "MATCH_ODDS",
                            runners=[FakeRunnerCatalogue(31, "Chelsea", 1),
                                     FakeRunnerCatalogue(32, "City", 2)],
                        ),
                    ],
                },
            )

        def list_events(self, filter=None, locale=None):
            # list_events, NOT list_event_types: _cache_event_types swallows any
            # failure from list_event_types on purpose ("a stale cache is better
            # than no feed"), so an exception raised there can never fail a run.
            # list_events is the first call in the job that nothing wraps.
            if "7" in ((filter or {}).get("eventTypeIds") or []):
                raise RuntimeError("boom")
            return super().list_events(filter=filter, locale=locale)

    with pytest.raises(RuntimeError):
        run_job(db, run_fixtures, Exploding(betting), EVENT_TYPE_IDS="1,7",
                SCORES_ENABLED="false")

    after = db.execute("SELECT count(*) n FROM market").fetchone()["n"]
    assert after == before, (
        f"the failed run's {after - before} write(s) survived the rollback"
    )
    failed = db.execute("SELECT * FROM feed_run WHERE NOT ok ORDER BY run_id DESC LIMIT 1").fetchone()
    assert failed is not None
    assert "boom" in failed["error"]


def test_partitioning_does_not_commit_the_callers_writes(db):
    """ensure_partitions runs INSIDE the job's transaction, and must not commit.

    It commits nothing itself; JobRun.__exit__ owns the commit. If it did
    commit, it would flush a poll's partial writes at the start of the very run
    that is supposed to be all-or-nothing (#14). No other test could see this,
    because partitioning happens before the job's first write, so the commit
    would have had nothing pending to flush.

    A second connection is the only honest witness: it sees committed rows only.
    """
    other = connect(TEST_URL)
    try:
        db.autocommit = False
        db.execute(
            "INSERT INTO betfair_event_type (id, name, market_count) VALUES ('999', 'probe', 0)"
        )
        ensure_partitions(db, behind_days=2, ahead_days=3)
        assert other.execute(
            "SELECT 1 FROM betfair_event_type WHERE id = '999'"
        ).fetchone() is None, "ensure_partitions committed the caller's pending write"
        db.rollback()
    finally:
        db.autocommit = True
        other.close()


def test_a_second_connection_cannot_take_the_same_lock(db):
    """This is what stops two collectors, or a collector and a hand-run CLI
    invocation, polling the same markets at the same time."""
    from betfeed.db import advisory_lock

    other = connect(TEST_URL)
    try:
        with advisory_lock(db, 0x0BEEF1001) as first:
            assert first is True
            with advisory_lock(other, 0x0BEEF1001) as second:
                assert second is False
        # Released, so the other connection can now take it.
        with advisory_lock(other, 0x0BEEF1001) as third:
            assert third is True
    finally:
        other.close()


def test_a_skipped_run_is_visible(db):
    """When the lock is held the job does nothing — and says so."""
    from betfeed.db import advisory_lock

    other = connect(TEST_URL)
    try:
        with advisory_lock(other, 0x0BEEF1002):
            stats = run_job(db, run_odds, FakeBetting(books={}), SCORES_ENABLED="false")
        assert stats["skipped"] is True
    finally:
        other.close()