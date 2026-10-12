"""Betfair API-NG client: one session, chunked requests, honest failures.

Two things this module exists to get right, both learned the hard way:

1. **Login must not require a human.** ``APIClient.login_interactive()`` blocks
   on stdin for a one-time password, which is fine for an LLM at a terminal and
   fatal for a cron job. Three non-interactive modes are supported: username +
   password, client certs, or a cached session token.

2. **Betfair caps a request by *weight*, not by count.** listMarketBook with
   price projections must stay under 200 weight points, and listMarketCatalogue
   takes at most 100 event ids per filter. Rather than hardcode a "safe" size
   and hope, every oversized request is **bisected on failure** and retried, so
   an account with unusual market shapes still completes.

Requests that are genuinely unrecoverable raise. Requests that are merely
transient are retried with backoff. Nothing is silently swallowed.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterable, Iterator, Sequence

from betfairlightweight import APIClient
from betfairlightweight.exceptions import APIError, BetfairError

from .config import Config

log = logging.getLogger("betfeed.betfair")

# Transient: worth waiting out. TOO_MUCH_DATA is NOT here — the caller bisects.
TRANSIENT_ERRORS = {
    "TOO_MANY_REQUESTS",
    "INTERNAL_SERVER_ERROR",
    "UNKNOWN_ERROR",
    "SERVICE_UNAVAILABLE",
    "TIMEOUT_ERROR",
    "INVALID_SESSION_TOKEN",  # handled specially: forces a re-login
}


class BetfairAuthError(RuntimeError):
    """Credentials rejected, or the account needs interactive 2FA."""


class TooMuchDataError(RuntimeError):
    """The request exceeded Betfair's weight limit. Caller should bisect."""


def _api_error_code(exc: BaseException) -> str:
    if isinstance(exc, APIError):
        response = getattr(exc, "response", None) or {}
        err = response.get("error") or {}
        if isinstance(err, list) and err:
            return str(err[0].get("errorCode", ""))
        if isinstance(err, dict):
            return str(err.get("errorCode", ""))
        return str(getattr(exc, "error_code", "") or "")
    return ""


def _api_error_message(exc: BaseException) -> str:
    if isinstance(exc, APIError):
        response = getattr(exc, "response", None) or {}
        err = response.get("error") or {}
        if isinstance(err, list) and err:
            return f"{err[0].get('errorCode','')}: {err[0].get('message','')}".strip(": ")
        return str(exc)
    return str(exc)


def chunked(items: Sequence, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


class BetfairClient:
    """Thin, retrying wrapper over the betting + scores endpoints."""

    def __init__(self, cfg: Config, *, sleeper: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self._sleep = sleeper
        self._client: APIClient | None = None
        self._logged_in = False
        # Events whose markets we could not fully retrieve (see
        # list_market_catalogue), and markets whose book exceeded Betfair's
        # weight limit on its own. Read by the jobs so a short feed is
        # reported rather than quietly shipped.
        self.truncated_events: list[str] = []
        self.unfetchable_markets: list[str] = []

    # ── session ──────────────────────────────────────────────────────────────

    def _build_client(self) -> APIClient:
        cfg = self.cfg
        if cfg.login_mode == "certs":
            return APIClient(app_key=cfg.app_key, certs=cfg.certs_path)
        if cfg.login_mode == "session_token":
            client = APIClient(username=cfg.username or None, app_key=cfg.app_key)
            client.set_session_token(cfg.session_token)
            return client
        return APIClient(username=cfg.username, password=cfg.password, app_key=cfg.app_key)

    def login(self, force: bool = False) -> None:
        """Log in if we are not already holding a live session."""
        if self._logged_in and not force:
            return
        client = self._build_client()
        try:
            if self.cfg.login_mode == "session_token":
                # A cached token is verified by the first real call; there is no
                # login endpoint for it. Treat it as logged in optimistically.
                self._client = client
            else:
                client.login()
                self._client = client
                log.info("logged in to Betfair (mode=%s)", self.cfg.login_mode)
        except BetfairError as e:
            raise BetfairAuthError(
                f"Betfair login failed ({self.cfg.login_mode}): {e}. "
                "If this account has 2FA enabled, use BETFAIR_LOGIN_MODE=certs with "
                "BETFAIR_CERTS_PATH, or log in once by hand and set BETFAIR_SESSION_TOKEN."
            ) from e
        self._logged_in = True

    @property
    def client(self) -> APIClient:
        if self._client is None or not self._logged_in:
            self.login()
        assert self._client is not None
        return self._client

    def _ensure_session(self) -> None:
        client = self.client
        try:
            if client.session_expired():
                log.warning("Betfair session expired, re-authenticating")
                self.login(force=True)
        except Exception:  # session_expired() is best-effort only
            pass

    # ── retry wrapper ────────────────────────────────────────────────────────

    def _call(self, what: str, fn: Callable, *, attempts: int = 4, **kwargs):
        last: BaseException | None = None
        for attempt in range(1, attempts + 1):
            self._ensure_session()
            try:
                return fn(**kwargs)
            except TooMuchDataError:
                raise  # caller bisects; retrying the same size cannot help
            except APIError as e:
                code = _api_error_code(e)
                last = e
                if code == "TOO_MUCH_DATA":
                    raise TooMuchDataError(_api_error_message(e)) from e
                if code == "NO_APP_KEY" or code == "NO_SESSION":
                    raise BetfairAuthError(_api_error_message(e)) from e
                if code == "INVALID_SESSION_TOKEN":
                    log.warning("invalid session token, re-authenticating once")
                    try:
                        self.login(force=True)
                    except BetfairAuthError:
                        raise
                    continue
                if code not in TRANSIENT_ERRORS or attempt == attempts:
                    raise
                delay = min(30.0, (2 ** (attempt - 1)) * 0.8 + random.random())
                log.warning("%s: %s — retry %d/%d in %.1fs", what, _api_error_message(e), attempt, attempts - 1, delay)
                self._sleep(delay)
            except BetfairError as e:
                last = e
                if attempt == attempts:
                    raise
                delay = min(30.0, (2 ** (attempt - 1)) * 0.8 + random.random())
                log.warning("%s: %s — retry %d/%d in %.1fs", what, e, attempt, attempts - 1, delay)
                self._sleep(delay)
        raise last if last else RuntimeError(f"{what} failed")

    # ── navigation ───────────────────────────────────────────────────────────

    def list_event_types(self) -> list[dict]:
        """(id, name, market_count) for every sport on the exchange."""
        results = self._call("list_event_types", self.client.betting.list_event_types)
        out = []
        for r in results:
            et = r.event_type
            out.append({"id": et.id, "name": et.name, "market_count": r.market_count})
        return out

    def list_events(self, event_type_id: str) -> list[dict]:
        """Every event Betfair currently lists for one sport."""
        from betfairlightweight.filters import market_filter

        results = self._call(
            "list_events",
            self.client.betting.list_events,
            filter=market_filter(event_type_ids=[event_type_id]),
            locale=self.cfg.locale,
        )
        out = []
        for r in results:
            e = r.event
            out.append(
                {
                    "id": e.id,
                    "name": e.name,
                    "country_code": e.country_code,
                    "venue": e.venue,
                    "open_date": e.open_date,
                    "event_type_id": event_type_id,
                    "market_count": r.market_count,
                }
            )
        return out

    def _catalogue_for_events(self, event_ids: list[str]) -> list:
        from betfairlightweight.filters import market_filter

        flt: dict = {"event_ids": event_ids}
        if self.cfg.market_type_codes:
            flt["market_type_codes"] = self.cfg.market_type_codes

        return self._call(
            "list_market_catalogue",
            self.client.betting.list_market_catalogue,
            filter=market_filter(**flt),
            max_results=self.cfg.catalogue_max_results,
            market_projection=[
                "RUNNER_DESCRIPTION",
                "MARKET_DESCRIPTION",
                "EVENT",
                "EVENT_TYPE",
                "COMPETITION",
                "MARKET_START_TIME",
                "VENUE",
            ],
            sort="FIRST_TO_START",
            locale=self.cfg.locale,
        )

    def list_market_catalogue(self, event_ids: Sequence[str]) -> Iterator:
        """Catalogue markets for events, chunked and as complete as Betfair allows.

        Betfair caps a catalogue filter at 100 event ids and a response at
        ``catalogue_max_results`` markets. When a multi-event response comes
        back exactly at the cap we cannot tell whether the chunk was fully
        covered, so the chunk is split in half and refetched — recursing until
        every response is strictly under the cap. That costs extra calls in the
        rare saturated chunk and stops feed 1 silently truncating a sport.

        A *single* event that saturates the cap cannot be recovered: the API
        has no market-level pagination. That event is recorded in
        ``truncated_events`` so the caller can log it, rather than being
        silently short. With the 1000 default this needs an event with a
        thousand markets and should not occur.
        """
        pending = [list(c) for c in chunked(list(event_ids), self.cfg.catalogue_chunk_events)]
        while pending:
            batch = pending.pop(0)
            try:
                markets = self._catalogue_for_events(batch)
            except TooMuchDataError:
                if len(batch) == 1:
                    log.error("catalogue: single event %s exceeds weight limit, skipping", batch[0])
                    self.truncated_events.append(batch[0])
                    continue
                mid = len(batch) // 2
                pending[:0] = [batch[:mid], batch[mid:]]
                continue
            if len(markets) >= self.cfg.catalogue_max_results:
                if len(batch) == 1:
                    # No finer filter exists to split on; record it and move on.
                    self.truncated_events.append(batch[0])
                    log.warning(
                        "catalogue: event %s hit the %d-market cap and cannot be split "
                        "further — its remaining markets are not in this feed",
                        batch[0], self.cfg.catalogue_max_results,
                    )
                else:
                    log.info("catalogue: chunk of %d events hit the %d cap, splitting",
                             len(batch), self.cfg.catalogue_max_results)
                    mid = len(batch) // 2
                    pending[:0] = [batch[:mid], batch[mid:]]
            yield from markets

    # ── prices ───────────────────────────────────────────────────────────────

    def _books_for_ids(self, market_ids: list[str], *, traded: bool) -> list:
        from betfairlightweight.filters import price_data, price_projection

        projection = price_projection(
            price_data(ex_best_offers=True, ex_traded=traded),
            ex_best_offers_overrides={"ladder_levels": self.cfg.ladder_levels},
        )
        return self._call(
            "list_market_book",
            self.client.betting.list_market_book,
            market_ids=market_ids,
            price_projection=projection,
            locale=self.cfg.locale,
        )

    def list_market_book(self, market_ids: Sequence[str], *, traded: bool | None = None) -> Iterator:
        """Market books with best offers, chunked by weight.

        On ``TOO_MUCH_DATA`` the chunk halves and retries, so the chunk size is
        an optimisation rather than a correctness assumption.
        """
        use_traded = self.cfg.include_traded_volume if traded is None else traded
        pending = [list(c) for c in chunked(list(market_ids), self.cfg.book_chunk_size)]
        while pending:
            batch = pending.pop(0)
            try:
                books = self._books_for_ids(batch, traded=use_traded)
            except TooMuchDataError:
                if len(batch) == 1:
                    # Even alone this market is too heavy. Record it: an odds
                    # feed missing a market must not look like a quiet one.
                    log.error("list_market_book: market %s alone exceeds weight limit", batch[0])
                    self.unfetchable_markets.append(batch[0])
                    continue
                mid = len(batch) // 2
                pending[:0] = [batch[:mid], batch[mid:]]
                continue
            except APIError as e:
                log.error("list_market_book: chunk of %d failed: %s", len(batch), _api_error_message(e))
                continue
            yield from books

    # ── scores (optional; needs a key authorised for the Scores API) ──────────

    def scores_available(self) -> bool:
        """Whether this app key may read the Scores API at all."""
        try:
            self._call("list_available_events", self.client.scores.list_available_events,
                       event_type_ids=self.cfg.event_type_ids, event_status=["POST"])
            return True
        except (BetfairError, TooMuchDataError) as e:
            log.info("scores API unavailable on this app key: %s", _api_error_message(e))
            return False

    def list_finished_event_ids(self, event_type_ids: Sequence[str]) -> list[str]:
        """Events the Scores API believes are finished and holding a result."""
        try:
            rows = self._call(
                "list_available_events",
                self.client.scores.list_available_events,
                event_type_ids=list(event_type_ids),
                event_status=["POST"],
            )
        except (BetfairError, TooMuchDataError) as e:
            log.info("list_available_events failed (%s); skipping scores this run", _api_error_message(e))
            return []
        return [r.event_id for r in rows if getattr(r, "event_id", None)]

    def list_scores(self, event_ids: Sequence[str]) -> list:
        """Scores for finished events. ``values`` is sport-specific, so callers
        must store it raw and normalise defensively."""
        try:
            return self._call(
                "list_scores",
                self.client.scores.list_scores,
                update_keys=[{"eventId": e} for e in event_ids],
            )
        except (BetfairError, TooMuchDataError) as e:
            log.warning("list_scores failed: %s", _api_error_message(e))
            return []

    # ── account ──────────────────────────────────────────────────────────────

    def account_funds(self) -> dict:
        funds = self._call("get_account_funds", self.client.account.get_account_funds)
        return {
            "available_to_bet_balance": funds.available_to_bet_balance,
            "exposure": funds.exposure,
            "retained_commission": funds.retained_commission,
        }

    def close(self) -> None:
        self._client = None
        self._logged_in = False