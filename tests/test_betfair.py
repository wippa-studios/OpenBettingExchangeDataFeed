"""Betfair client behaviour: chunking, bisection, retries, coverage.

These are the parts that are hard to debug against the live API because a
failure there looks like "some markets silently missing". The fakes reproduce
the two failure modes Betfair actually returns.
"""

import pytest

from betfeed.betfair import BetfairAuthError, BetfairClient, TooMuchDataError, chunked
from betfeed.normalize import ladder_payload, normalise_book, result_from_book
from conftest import (
    FakeBetting,
    FakeCatalogueMarket,
    FakeEvent,
    FakeMarketBook,
    FakeRunnerBook,
    FakeRunnerCatalogue,
    install_fake_client,
    make_cfg,
)


def client_with(betting, **cfg_overrides):
    cfg = make_cfg(**cfg_overrides)
    client = BetfairClient(cfg, sleeper=lambda _s: None)
    return install_fake_client(client, betting=betting)


# ── chunking ──────────────────────────────────────────────────────────────

def test_chunked_is_even_and_keeps_order():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 2)) == []
    assert list(chunked([1], 100)) == [[1]]


def test_books_are_requested_in_chunks():
    betting = FakeBetting(books={f"1.{i}": FakeMarketBook(f"1.{i}", []) for i in range(250)})
    client = client_with(betting, BOOK_CHUNK_SIZE=100)
    books = list(client.list_market_book([f"1.{i}" for i in range(250)]))
    assert len(books) == 250
    assert [len(c) for c in betting.book_calls] == [100, 100, 50]


def test_too_much_data_bisects_instead_of_failing():
    """Betfair caps a price request at 200 weight points; we discover the cap
    rather than assuming it, so an unusual market shape still completes."""
    betting = FakeBetting(books={f"1.{i}": FakeMarketBook(f"1.{i}", []) for i in range(20)})
    betting.weight_limit = 8
    client = client_with(betting, BOOK_CHUNK_SIZE=20)
    books = list(client.list_market_book([f"1.{i}" for i in range(20)]))
    assert len(books) == 20
    # We started oversized, were rejected, halved until the calls went through.
    assert betting.rejected_calls[0] == [f"1.{i}" for i in range(20)]
    assert max(len(c) for c in betting.book_calls) <= 8


def test_a_single_market_over_the_limit_is_skipped_not_fatal():
    betting = FakeBetting(books={"1.1": FakeMarketBook("1.1", [])})
    betting.weight_limit = 0
    client = client_with(betting, BOOK_CHUNK_SIZE=1)
    assert list(client.list_market_book(["1.1"])) == []
    assert client.unfetchable_markets == ["1.1"]


def test_rate_limits_are_retried_then_surfaced():
    betting = FakeBetting(books={"1.1": FakeMarketBook("1.1", [])})
    betting.fail_times = 2  # transient for two calls, then fine
    client = client_with(betting)
    assert len(list(client.list_market_book(["1.1"]))) == 1
    assert len(betting.rejected_calls) == 2   # gave up twice...
    assert len(betting.book_calls) == 1       # ...then succeeded


def test_retries_stop_at_the_attempt_limit():
    betting = FakeBetting(books={"1.1": FakeMarketBook("1.1", [])})
    betting.fail_times = 99
    client = client_with(betting)
    assert list(client.list_market_book(["1.1"])) == []
    assert len(betting.rejected_calls) == 4  # attempts=4, then give up


def test_permanent_errors_are_not_retried():
    from betfairlightweight.exceptions import APIError

    betting = FakeBetting()

    def boom(**_kwargs):
        raise APIError({"error": [{"errorCode": "INVALID_INPUT", "message": "bad"}]}, "m", {}, None)

    betting.list_market_book = boom
    client = client_with(betting)
    assert list(client.list_market_book(["1.1"])) == []  # logged, not raised
    assert len(betting.book_calls) == 0


# ── catalogue coverage ────────────────────────────────────────────────────

def test_catalogue_chunks_events_at_100():
    events = {f"e{i}": [] for i in range(250)}
    betting = FakeBetting(catalogue=events)
    client = client_with(betting, CATALOGUE_CHUNK_EVENTS=100)
    list(client.list_market_catalogue(list(events)))
    assert [len(c) for c in betting.catalogue_calls] == [100, 100, 50]


def test_a_saturated_catalogue_chunk_splits_to_stay_complete():
    """A response exactly at max_results might be truncated, so a multi-event
    chunk is halved and refetched. Without this, feed 1 loses a whole event's
    markets whenever its neighbours saturate the response."""
    fixture_a = FakeEvent("e1", "A v B")
    fixture_b = FakeEvent("e2", "C v D")
    betting = FakeBetting(catalogue={
        "e1": [FakeCatalogueMarket(f"1.{i}", fixture_a, start=None) for i in range(10)],
        "e2": [FakeCatalogueMarket("1.999", fixture_b)],
    })
    client = client_with(betting, CATALOGUE_MAX_RESULTS=5, CATALOGUE_CHUNK_EVENTS=100)
    markets = list(client.list_market_catalogue(["e1", "e2"]))
    # e2 is recovered because the saturated chunk was split.
    assert {m.event.id for m in markets} == {"e1", "e2"}
    assert len(betting.catalogue_calls) > 1


def test_an_unsplittable_saturated_event_is_recorded_not_hidden():
    """Betfair has no market-level pagination, so a single event with more
    markets than max_results genuinely cannot be fully fetched. The client says
    so instead of letting a short feed look complete."""
    fixture = FakeEvent("e1", "A v B")
    betting = FakeBetting(catalogue={
        "e1": [FakeCatalogueMarket(f"1.{i}", fixture, start=None) for i in range(10)],
    })
    client = client_with(betting, CATALOGUE_MAX_RESULTS=5, CATALOGUE_CHUNK_EVENTS=100)
    markets = list(client.list_market_catalogue(["e1"]))
    assert len(markets) == 5
    assert client.truncated_events == ["e1"]


# ── login ─────────────────────────────────────────────────────────────────

def test_login_mode_is_validated():
    from betfeed.config import Config, ConfigError

    with pytest.raises(ConfigError, match="BETFAIR_LOGIN_MODE"):
        Config({**{"DATABASE_URL": "postgresql://x/y", "BETFAIR_APP_KEY": "k",
                   "EVENT_TYPE_IDS": "1"}, "BETFAIR_LOGIN_MODE": "telepathy"})


def test_missing_credentials_are_named():
    from betfeed.config import validate_credentials

    cfg = make_cfg(BETFAIR_LOGIN_MODE="password", BETFAIR_PASSWORD="")
    assert validate_credentials(cfg) == ["BETFAIR_PASSWORD"]
    cfg = make_cfg(BETFAIR_LOGIN_MODE="certs", BETFAIR_CERTS_PATH="")
    assert validate_credentials(cfg) == ["BETFAIR_CERTS_PATH"]


def test_login_failure_is_explained_not_raised_raw():
    from betfairlightweight.exceptions import LoginError

    cfg = make_cfg(BETFAIR_LOGIN_MODE="password")
    client = BetfairClient(cfg, sleeper=lambda _s: None)

    def boom():
        raise LoginError({"loginStatus": "FAIL", "error": "interactive login required"})

    import betfeed.betfair as mod

    original = mod.APIClient
    # Password mode calls login_interactive(), not login(). The fake has to model
    # the call the code actually makes, or this test passes against a stub that
    # no real client ever exercises.
    mod.APIClient = lambda **kwargs: type(
        "C", (), {"login": staticmethod(boom), "login_interactive": staticmethod(boom)}
    )()
    try:
        with pytest.raises(BetfairAuthError, match="2FA"):
            client.login()
    finally:
        mod.APIClient = original


def test_password_mode_uses_login_interactive_not_the_cert_endpoint():
    """A DELAY app key must authenticate against identitysso, not certlogin.

    betfairlightweight's `Login.url` is hard-wired to
    `identity_cert_uri + "certlogin"` and has no password endpoint, so calling
    `login()` in password mode POSTs to the certificate endpoint and Betfair
    answers CERT_AUTH_REQUIRED. That error reads like "this account needs 2FA
    and certificates", which is wrong for a Delay key — Betfair documents it as
    the development/testing key that authenticates with username + password.
    `LoginInteractive.url` is `identity_uri + "login"`, the real password
    endpoint. This pins which one password mode uses, because picking wrong is
    invisible until a real login.
    """
    cfg = make_cfg(BETFAIR_LOGIN_MODE="password")
    client = BetfairClient(cfg, sleeper=lambda _s: None)

    calls = []

    class FakeClient:
        session_token = None

        def login(self):
            calls.append("login")

        def login_interactive(self):
            calls.append("login_interactive")
            self.session_token = "tok"

    import betfeed.betfair as mod

    original = mod.APIClient
    mod.APIClient = lambda **kwargs: FakeClient()
    try:
        client.login()
    finally:
        mod.APIClient = original

    assert calls == ["login_interactive"], f"password mode called {calls}, not the password endpoint"


def test_password_mode_fails_loudly_when_no_session_token_comes_back():
    """A login that returns no token must raise, not mark itself logged in.

    `session_token` is what every subsequent Betfair call authenticates with.
    Silently proceeding without one would surface much later as a confusing 401
    on the first catalogue request instead of a clear login failure.
    """
    cfg = make_cfg(BETFAIR_LOGIN_MODE="password")
    client = BetfairClient(cfg, sleeper=lambda _s: None)

    class FakeClient:
        session_token = None

        def login(self):
            raise AssertionError("password mode must not touch the cert endpoint")

        def login_interactive(self):
            pass  # succeeds but yields no token

    import betfeed.betfair as mod

    original = mod.APIClient
    mod.APIClient = lambda **kwargs: FakeClient()
    try:
        with pytest.raises(BetfairAuthError):
            client.login()
    finally:
        mod.APIClient = original


# ── normalisation and results ─────────────────────────────────────────────

def test_off_ladder_prices_are_dropped_not_stored():
    book = normalise_book(FakeMarketBook(
        "1.1", [FakeRunnerBook(1, back=[(2.5, 100), (2.31, 50), (2.52, 20)])]
    ))
    assert [r["price"] for r in book["selections"][0]["back"]] == [2.5, 2.52]
    assert book["selections"][0]["price"] == 2.52  # best back = highest


def test_best_price_is_the_highest_back():
    book = normalise_book(FakeMarketBook(
        "1.1", [FakeRunnerBook(1, back=[(2.3, 10), (2.5, 60), (2.4, 30)])]
    ))
    sel = book["selections"][0]
    assert sel["price"] == 2.5
    assert sel["size"] == 60  # the size at that price, not the first


def test_ladder_payload_has_no_runner_names():
    """A market book carries no names; inventing one would be a lie."""
    payload = ladder_payload(normalise_book(FakeMarketBook("1.1", [FakeRunnerBook(7)])))
    sel = payload["selections"][0]
    assert "name" not in sel
    assert sel["selection_id"] == 7


def test_an_open_book_is_not_a_result():
    book = normalise_book(FakeMarketBook("1.1", [FakeRunnerBook(1, status="ACTIVE")], status="OPEN"))
    assert result_from_book(book) is None


def test_a_settled_book_produces_a_result():
    book = normalise_book(FakeMarketBook(
        "1.1",
        [FakeRunnerBook(1, status="WINNER"), FakeRunnerBook(2, status="LOSER")],
        status="CLOSED_EVENT",
    ))
    result = result_from_book(book)
    assert result["winner_selection_ids"] == [1]
    assert result["status"] == "CLOSED_EVENT"
    assert result["voided"] is False
    assert {r["selection_id"] for r in result["runners"]} == {1, 2}


def test_a_place_market_keeps_every_winner():
    book = normalise_book(FakeMarketBook(
        "1.1",
        [FakeRunnerBook(1, status="WINNER"), FakeRunnerBook(2, status="WINNER"),
         FakeRunnerBook(3, status="LOSER")],
        status="CLOSED_EVENT",
    ))
    assert sorted(result_from_book(book)["winner_selection_ids"]) == [1, 2]


def test_a_voided_market_is_marked_voided():
    book = normalise_book(FakeMarketBook(
        "1.1",
        [FakeRunnerBook(1, status="REMOVED"), FakeRunnerBook(2, status="REMOVED")],
        status="VOIDED",
    ))
    result = result_from_book(book)
    assert result["voided"] is True
    assert result["winner_selection_ids"] == []


def test_catalogue_normalisation_reads_status_and_venue_from_raw_data():
    fixture = FakeEvent("e1", "A v B", venue="Old Trafford")
    market = FakeCatalogueMarket(
        "1.1", fixture, market_type="MATCH_ODDS",
        runners=[FakeRunnerCatalogue(1, "A", sort_priority=1)],
    )
    from betfeed.normalize import normalise_catalogue_market

    n = normalise_catalogue_market(market, "1")
    assert n["status"] == "OPEN"          # from _data["marketStatus"]
    assert n["market_venue"] == "Wembley"  # from _data["venue"]
    assert n["market_type_code"] == "MATCH_ODDS"
    assert n["runners"][0]["sort_priority"] == 1


def test_score_parsing_is_defensive():
    from betfeed.normalize import score_from_betfair

    assert score_from_betfair(None) is None
    assert score_from_betfair("nonsense") is None
    assert score_from_betfair({}) is None
    parsed = score_from_betfair({"home": {"team": "Arsenal", "score": "2"},
                                 "away": {"team": "Chelsea", "score": "1"}})
    assert parsed == {"home": "Arsenal", "away": "Chelsea",
                      "home_score": "2", "away_score": "1"}
    # A sport whose payload is not home/away is skipped, not guessed at.
    assert score_from_betfair({"homeRuns": 3}) is None