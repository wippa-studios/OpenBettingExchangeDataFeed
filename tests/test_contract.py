"""The feed contract, including the one thing it exists to prove:

a fixtures-only document (no prices anywhere) is a valid feed.
"""

from datetime import datetime, timezone

import pytest

from betfeed.contract import (
    ContractError,
    make_feed,
    slug_sport,
    state_for,
    validate_feed,
)


def _event(**over):
    market = over.pop("market", None) or {
        "market_key": "1.234",
        "name": "Match Odds",
        "status": "OPEN",
        "starts_at": "2026-10-13T14:00:00+00:00",
        "selections": [
            {"selection_key": 1, "name": "Home", "price": None},
            {"selection_key": 2, "name": "Draw", "price": None},
            {"selection_key": 3, "name": "Away", "price": None},
        ],
    }
    ev = {
        "event_key": "999",
        "name": "Test Utd v Arsenal",
        "sport": "Soccer",
        "competition": "Premier League",
        "starts_at": "2026-10-13T14:00:00+00:00",
        "markets": [market],
    }
    ev.update(over)
    return ev


def test_feed1_validates_without_any_prices():
    """The whole point: null prices are feed 1, not a broken document."""
    doc = make_feed([_event()])
    assert doc["version"] == 1
    assert doc["priced"] is False
    sels = doc["events"][0]["markets"][0]["selections"]
    assert len(sels) == 3
    assert all(s["price"] is None for s in sels)


def test_feed2_reports_itself_as_priced():
    market = {
        "market_key": "1.234",
        "name": "Match Odds",
        "status": "OPEN",
        "starts_at": "2026-10-13T14:00:00+00:00",
        "selections": [{"selection_key": 1, "name": "Home", "price": 2.5}],
    }
    doc = make_feed([_event(market=market)])
    assert doc["priced"] is True
    assert doc["events"][0]["markets"][0]["selections"][0]["price"] == 2.5


def test_a_price_off_the_ladder_is_refused():
    """A price Betfair would never quote must not reach a consumer."""
    market = {
        "market_key": "1.234",
        "name": "Match Odds",
        "selections": [{"selection_key": 1, "name": "Home", "price": 2.31}],
    }
    with pytest.raises(ContractError, match="tick ladder"):
        make_feed([_event(market=market)])


def test_ladder_carries_through_validation():
    market = {
        "market_key": "1.234",
        "name": "Match Odds",
        "selections": [{
            "selection_key": 1,
            "name": "Home",
            "price": 2.5,
            "size": 120.0,
            "back": [{"price": 2.5, "size": 120.0}, {"price": 2.52, "size": 80.0}],
            "lay": [{"price": 2.48, "size": 90.0}],
        }],
    }
    doc = make_feed([_event(market=market)])
    sel = doc["events"][0]["markets"][0]["selections"][0]
    assert [r["price"] for r in sel["back"]] == [2.5, 2.52]
    assert sel["lay"][0]["price"] == 2.48
    assert sel["size"] == 120.0


def test_betfair_status_is_not_collapsed():
    """SUSPENDED must not silently read as a normal open market."""
    market = {
        "market_key": "1.234",
        "name": "Match Odds",
        "status": "SUSPENDED",
        "selections": [{"selection_key": 1, "name": "Home", "price": None}],
    }
    doc = make_feed([_event(market=market)])
    m = doc["events"][0]["markets"][0]
    assert m["status"] == "SUSPENDED"
    assert m["state"] == "upcoming"
    assert state_for("INPLAY") == "live"
    assert state_for("OPEN", inplay=True) == "live"
    assert state_for("CLOSED_EVENT") == "finished"
    assert state_for("SETTLED") == "finished"
    assert state_for("something-new") == "upcoming"  # unknown -> pre-game


def test_structural_errors_name_the_path():
    with pytest.raises(ContractError, match=r"events\[0\].event_key is required"):
        validate_feed({"version": 1, "events": [{"name": "x", "starts_at": "2026-01-01"}]})
    with pytest.raises(ContractError, match=r"markets\[0\].selections\[1\].name is required"):
        make_feed([_event(market={
            "market_key": "1.1",
            "name": "m",
            "selections": [
                {"selection_key": 1, "name": "Home", "price": None},
                {"selection_key": 2, "price": None},
            ],
        })])


def test_a_start_time_that_is_a_datetime_is_accepted():
    """A TIMESTAMPTZ column arrives as a datetime, not an ISO string.

    The build path in feeds.py puts the raw column value in starts_at and
    relies on the contract to normalise it. When the contract insisted on a
    `str`, EVERY document built from a real database raised on its first event
    and was served as an empty, stale:true feed — the check tested the type
    rather than presence, so present and missing failed identically.
    """
    doc = make_feed([_event(starts_at=datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc))])
    assert doc["events"][0]["starts_at"] == "2026-10-13T14:00:00+00:00"


def test_a_missing_start_time_is_still_refused():
    """The other half of the same gate: absent is absent, not a datetime."""
    for missing in (None, "", "   "):
        with pytest.raises(ContractError, match=r"events\[0\].starts_at is required"):
            make_feed([_event(starts_at=missing)])
    with pytest.raises(ContractError, match=r"events\[0\].starts_at is required"):
        make_feed([{k: v for k, v in _event().items() if k != "starts_at"}])


def test_one_event_key_appears_once_per_document():
    """The read side groups rows into events by "same fixture as the previous
    row?", so an ordering that interleaves two fixtures emits each of them
    twice. The consumer cannot do anything sensible with that, so it is
    refused rather than shipped."""
    with pytest.raises(ContractError, match=r"event_key '999' is already in this document"):
        make_feed([_event(), _event()])


def test_a_market_with_no_selections_is_refused():
    """Half a market is worse than no market."""
    with pytest.raises(ContractError, match="at least one selection"):
        make_feed([_event(market={"market_key": "1.1", "name": "m", "selections": []})])


def test_version_is_enforced():
    with pytest.raises(ContractError, match="unsupported feed version"):
        validate_feed({"version": 99, "events": []})


def test_sport_slugs_are_normalised():
    assert slug_sport("Horse Racing") == "horse-racing"
    assert slug_sport("eSports ") == "esports"
    assert slug_sport(None) == "other"
    assert slug_sport("") == "other"


def test_results_and_scores_survive_validation():
    market = {
        "market_key": "1.234",
        "name": "Match Odds",
        "status": "CLOSED_EVENT",
        "state": "finished",
        "result": {"status": "CLOSED_EVENT", "voided": False, "winners": [1]},
        "selections": [{
            "selection_key": 1, "name": "Home", "price": None,
            "status": "WINNER", "result": "WINNER",
        }],
    }
    doc = make_feed([_event(market=market, score={"home_score": "2", "away_score": "1"})])
    m = doc["events"][0]["markets"][0]
    assert m["result"]["winners"] == [1]
    assert m["selections"][0]["result"] == "WINNER"
    assert doc["events"][0]["score"]["home_score"] == "2"


def test_generated_at_defaults_to_now():
    doc = make_feed([])
    assert doc["generated_at"]
    assert datetime.fromisoformat(doc["generated_at"]).tzinfo is not None


def test_empty_feed_is_valid():
    doc = make_feed([])
    assert doc["events"] == []
    assert doc["priced"] is False