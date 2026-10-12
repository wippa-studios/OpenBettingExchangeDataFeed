"""Shared test fixtures.

``make_cfg`` builds a Config without touching .env or the network, and the
Fake* classes stand in for betfairlightweight resources. They mimic the two
things the real SDK does that our code depends on: snake_case attributes for
promoted fields, and a ``_data`` dict for the rest (``marketStatus``,
``venue``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from betfeed.config import DEFAULTS, Config

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def make_cfg(**overrides) -> Config:
    raw = {
        **DEFAULTS,
        "DATABASE_URL": "postgresql://betfair@127.0.0.1:5432/betfeed_test",
        "BETFAIR_APP_KEY": "test-key",
        "BETFAIR_USERNAME": "test-user",
        "BETFAIR_PASSWORD": "test-pass",
        "EVENT_TYPE_IDS": "1",
        "FIXTURES_INTERVAL_MINUTES": "720",
        "ODDS_INTERVAL_MINUTES": "5",
    }
    raw.update({k: str(v) for k, v in overrides.items()})
    return Config(raw)


# ── fake Betfair resources ────────────────────────────────────────────────

class FakePriceSize:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class FakeEx:
    def __init__(self, back=(), lay=(), traded=()):
        self.available_to_back = [FakePriceSize(*p) for p in back]
        self.available_to_lay = [FakePriceSize(*p) for p in lay]
        self.traded_volume = [FakePriceSize(*p) for p in traded]


class FakeRunnerBook:
    def __init__(self, selection_id, status="ACTIVE", back=(), lay=(),
                 last_price_traded=None, total_matched=0.0, handicap=None):
        self.selection_id = selection_id
        self.status = status
        self.handicap = handicap
        self.last_price_traded = last_price_traded
        self.total_matched = total_matched
        self.ex = FakeEx(back, lay)


class FakeMarketBook:
    def __init__(self, market_id, runners, status="OPEN", inplay=False,
                 publish_time=None, total_matched=0.0, bet_delay=0):
        self.market_id = market_id
        self.runners = runners
        self.status = status
        self.inplay = inplay
        self.publish_time = publish_time
        self.total_matched = total_matched
        self.bet_delay = bet_delay
        self.number_of_winners = None
        self.bsp_reconciled = None
        self._data = {"marketId": market_id, "status": status}


class FakeRunnerCatalogue:
    def __init__(self, selection_id, runner_name, sort_priority=None, handicap=None):
        self.selection_id = selection_id
        self.runner_name = runner_name
        self.sort_priority = sort_priority
        self.handicap = handicap


class FakeEvent:
    def __init__(self, id, name, country_code="GB", venue="", open_date=None):
        self.id = id
        self.name = name
        self.country_code = country_code
        self.venue = venue
        self.open_date = open_date or NOW


class FakeCompetition:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class FakeDescription:
    def __init__(self, market_type, market_time=None):
        self.market_type = market_type
        self.market_time = market_time or NOW
        self.turn_in_play_enabled = True


class FakeCatalogueMarket:
    """A MarketCatalogue stand-in, including the _data fields we read."""

    def __init__(self, market_id, fixture, market_type="MATCH_ODDS", runners=(),
                 market_name="Match Odds", market_status="OPEN", start=None):
        self.market_id = market_id
        self.market_name = market_name
        self.total_matched = 1000.0
        self.market_start_time = start or NOW + timedelta(hours=2)
        self.event = fixture
        self.event_type = SimpleNamespace(id="1", name="Soccer")
        self.competition = SimpleNamespace(id="10", name="Premier League")
        self.description = FakeDescription(market_type)
        self.runners = list(runners)
        self._data = {
            "marketId": market_id,
            "marketStatus": market_status,
            "venue": "Wembley",
            "marketName": market_name,
        }


class FakeEventResult:
    def __init__(self, event, market_count=1):
        self.event = event
        self.market_count = market_count


# ── fake client ───────────────────────────────────────────────────────────

def too_much_data():
    from betfairlightweight.exceptions import APIError

    return APIError(
        {"error": [{"errorCode": "TOO_MUCH_DATA", "message": "too much data"}]},
        "listMarketBook", {}, None,
    )


def too_many_requests():
    from betfairlightweight.exceptions import APIError

    return APIError(
        {"error": [{"errorCode": "TOO_MANY_REQUESTS", "message": "slow down"}]},
        "listMarketBook", {}, None,
    )


class FakeBetting:
    """Records calls and serves canned responses.

    ``weight_limit`` mimics Betfair's real behaviour: a request with more than
    N market ids fails with TOO_MUCH_DATA instead of returning.
    """

    def __init__(self, books=None, catalogue=None, events=None, weight_limit=None):
        self.books = books or {}
        self.catalogue = catalogue or {}
        self.events = events or {}
        self.weight_limit = weight_limit
        self.book_calls: list[list[str]] = []
        self.rejected_calls: list[list[str]] = []
        self.catalogue_calls: list[list[str]] = []
        self.fail_times = 0

    def list_events(self, filter=None, locale=None):
        ids = (filter or {}).get("eventTypeIds") or []
        out = []
        for eid in ids:
            out.extend(self.events.get(eid, []))
        return out

    def list_market_catalogue(self, filter=None, market_projection=None, max_results=1,
                              sort=None, locale=None):
        ids = (filter or {}).get("eventIds") or []
        self.catalogue_calls.append(list(ids))
        out = []
        for eid in ids:
            out.extend(self.catalogue.get(eid, []))
        return out[:max_results]

    def list_market_book(self, market_ids=None, price_projection=None, locale=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            self.rejected_calls.append(list(market_ids))
            raise too_many_requests()
        if self.weight_limit is not None and len(market_ids) > self.weight_limit:
            self.rejected_calls.append(list(market_ids))
            raise too_much_data()
        self.book_calls.append(list(market_ids))
        return [self.books[m] for m in market_ids if m in self.books]


class FakeScores:
    def __init__(self, available=None, scores=None):
        self.available = available or []
        self.scores = scores or []

    def list_available_events(self, event_type_ids=None, event_status=None, event_ids=None):
        return [SimpleNamespace(event_id=e) for e in self.available]

    def list_scores(self, update_keys=None, locale=None):
        return self.scores


def install_fake_client(client, *, betting=None, scores=None):
    """Point a BetfairClient at fakes without any login."""
    client._client = SimpleNamespace(
        betting=betting or FakeBetting(),
        scores=scores or FakeScores(),
        session_expired=lambda: False,
    )
    client._logged_in = True
    return client


@pytest.fixture
def no_sleep():
    """A client whose retry backoff does not actually sleep."""
    return lambda _seconds: None