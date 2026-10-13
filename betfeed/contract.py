"""The public feed contract — what a consumer receives.

Mirrors OpenBookmaker-Data's `lib/contract.js` so the wire shape is already
known to the rest of the stack, with one deliberate change:

    price is OPTIONAL.

That is the whole difference between feed 1 and feed 2. A fixtures-only
document has ``price: null`` on every selection; a feed-2 document carries the
newest ladder. The document says which it is via ``priced``, so a consumer can
branch without a second endpoint and without guessing whether a null price
means "no odds yet" or "odds feed is broken".

Betfair's own status vocabulary is preserved as ``status`` alongside the
contract's three-state ``state``. Collapsing SUSPENDED into "upcoming" — which
is what a naive normaliser does — loses the fact that a market has stopped
trading, and that is exactly the information a consumer needs to decide
whether a price is tradeable.
"""

from __future__ import annotations

import re
from typing import Any

from .odds import snap_to_tick, valid_tick

VERSION = 1
STATES = ("upcoming", "live", "finished")

# Betfair status -> contract state. Anything unrecognised is 'upcoming': an
# event we cannot classify is still pre-game, and the raw status is kept.
_STATUS_TO_STATE = {
    "open": "upcoming",
    "suspended": "upcoming",
    "active": "upcoming",
    "prematch": "upcoming",
    "inactive": "upcoming",
    "closed": "finished",
    "closed_event": "finished",
    "settled": "finished",
    "voided": "finished",
    "inplay": "live",
    "open_inplay": "live",
    "live": "live",
}


class ContractError(ValueError):
    """A document that we refuse to publish, with the path that broke it."""


def slug_sport(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "other"


def state_for(status: Any, inplay: bool | None = None) -> str:
    key = str(status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if inplay:
        return "live"
    return _STATUS_TO_STATE.get(key, "upcoming")


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _present(value: Any) -> bool:
    """Is there a value here at all — whatever its type?

    ``starts_at`` reaches the validator as whatever psycopg returned for a
    TIMESTAMPTZ column, which is a ``datetime``, and is normalised by
    :func:`_iso` on the way out. Testing it with ``_non_empty_str`` asked for a
    type the build path never produces, so a *present* start time and a *missing*
    one raised the identical error and there was no input that passed.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return _iso(value) not in (None, "")


def _identifier(value: Any) -> str | None:
    """Coerce an id to a string, or None if it is not an id at all.

    Betfair selection ids are integers; market and event ids are strings.
    Rejecting anything non-string would reject every runner on the exchange.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if _non_empty_str(value):
        return value.strip()
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _validate_price(price: Any, path: str) -> float | None:
    """Price is optional; when present it must be a real ladder price.

    A price Betfair would never have quoted is a bug we would rather catch
    before a customer does, so it is rejected rather than snapped into
    something plausible-looking.
    """
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        raise ContractError(f"{path}.price must be a number or null") from None
    if value <= 1 or value > 1000:
        raise ContractError(f"{path}.price must be in (1, 1000] or null, got {value}")
    if not valid_tick(value):
        raise ContractError(f"{path}.price {value} is not on the Betfair tick ladder")
    return value


def _validate_ladder(raw: Any, path: str) -> list[dict[str, float | None]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ContractError(f"{path} must be a list")
    out = []
    for i, rung in enumerate(raw):
        if not isinstance(rung, dict):
            raise ContractError(f"{path}[{i}] must be an object")
        price = _validate_price(rung.get("price"), f"{path}[{i}]")
        if price is None:
            raise ContractError(f"{path}[{i}].price is required")
        # Size is "number or null", never null-as-zero. Betfair's infinite-size
        # offer — the 1.01 lay that takes any stake — is stored as null because
        # it has no JSON representation, and `float(x or 0.0)` published that
        # as {"price": 1.01, "size": 0.0}: the most liquid offer on the book
        # reported as nothing available. The selection-level size in the same
        # document was already None for the same value, so one input was
        # represented two different ways. The README already warns consumers
        # that treating null size as zero under-reports the 1.01 lay; betfeed's
        # own output was doing it.
        raw_size = rung.get("size")
        if raw_size is None:
            size = None
        else:
            try:
                size = float(raw_size)
            except (TypeError, ValueError):
                raise ContractError(f"{path}[{i}].size must be a number or null") from None
        out.append({"price": price, "size": size})
    return out


def validate_feed(doc: dict) -> dict:
    """Validate + normalise. Raises ContractError naming the offending path.

    A feed we cannot trust is worse than a feed we refuse.
    """
    if not isinstance(doc, dict):
        raise ContractError("feed must be an object")
    if doc.get("version") != VERSION:
        raise ContractError(f"unsupported feed version {doc.get('version')!r} (expected {VERSION})")
    raw_events = doc.get("events")
    if not isinstance(raw_events, list):
        raise ContractError("feed.events must be a list")

    events: list[dict] = []
    priced_any = False
    seen_events: set[str] = set()

    for i, e in enumerate(raw_events):
        at = f"events[{i}]"
        if not isinstance(e, dict):
            raise ContractError(f"{at} must be an object")
        if _identifier(e.get("event_key")) is None:
            raise ContractError(f"{at}.event_key is required")
        key = str(_identifier(e["event_key"]))
        if key in seen_events:
            # Two documents' worth of markets under one event_key is what a
            # consumer cannot do anything sensible with, and it is produced by
            # a read-side ordering bug rather than by bad data — so it is
            # refused here rather than shipped.
            raise ContractError(f"{at}.event_key {key!r} is already in this document")
        seen_events.add(key)
        if not _non_empty_str(e.get("name")):
            raise ContractError(f"{at}.name is required")
        if not _present(e.get("starts_at")):
            raise ContractError(f"{at}.starts_at is required")

        markets: list[dict] = []
        for j, m in enumerate(e.get("markets") or []):
            mt = f"{at}.markets[{j}]"
            if not isinstance(m, dict):
                raise ContractError(f"{mt} must be an object")
            if _identifier(m.get("market_key")) is None:
                raise ContractError(f"{mt}.market_key is required")
            if not _non_empty_str(m.get("name")):
                raise ContractError(f"{mt}.name is required")

            selections: list[dict] = []
            for k, s in enumerate(m.get("selections") or []):
                st = f"{mt}.selections[{k}]"
                if not isinstance(s, dict):
                    raise ContractError(f"{st} must be an object")
                if _identifier(s.get("selection_key")) is None:
                    raise ContractError(f"{st}.selection_key is required")
                if not _non_empty_str(s.get("name")):
                    raise ContractError(f"{st}.name is required")
                price = _validate_price(s.get("price"), st)
                if price is not None:
                    priced_any = True
                selections.append(
                    {
                        "selection_key": str(s["selection_key"]),
                        "name": s["name"],
                        "handicap": s.get("handicap"),
                        "price": price,
                        "size": s.get("size"),
                        "status": s.get("status"),
                        "result": s.get("result"),
                        "back": _validate_ladder(s.get("back"), f"{st}.back"),
                        "lay": _validate_ladder(s.get("lay"), f"{st}.lay"),
                    }
                )
            if not selections:
                raise ContractError(f"{mt} must have at least one selection")

            markets.append(
                {
                    "market_key": str(m["market_key"]),
                    "name": m["name"],
                    "state": m.get("state") or "upcoming",
                    "status": m.get("status"),
                    "in_play": bool(m.get("in_play")),
                    "starts_at": _iso(m.get("starts_at")),
                    "total_matched": m.get("total_matched"),
                    "result": m.get("result"),
                    "price_age_seconds": m.get("price_age_seconds"),
                    "selections": selections,
                }
            )

        events.append(
            {
                "event_key": str(e["event_key"]),
                "name": e["name"],
                "sport": slug_sport(e.get("sport")),
                "competition": e.get("competition"),
                "venue": e.get("venue"),
                "country_code": e.get("country_code"),
                "starts_at": _iso(e["starts_at"]),
                "state": e.get("state") or "upcoming",
                "score": e.get("score"),
                "markets": markets,
            }
        )

    out = {
        "version": VERSION,
        # Derived from the data, never asserted: feed 1 is `false`, feed 2 is
        # `true` whenever any price was actually available. A consumer never
        # has to infer which feed it got, and a feed-2 document that found no
        # prices reports `false` instead of claiming to be priced.
        "priced": priced_any,
        "generated_at": _iso(doc.get("generated_at")) or "",
        "stale": bool(doc.get("stale", False)),
        "max_price_age_seconds": doc.get("max_price_age_seconds"),
        "events": events,
    }
    if not out["generated_at"]:
        from datetime import datetime, timezone

        out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def make_feed(events: list[dict], generated_at: str | None = None, *, stale: bool = False,
              max_price_age_seconds: int | None = None) -> dict:
    """Validate and normalise a feed document.

    ``priced`` is derived by :func:`validate_feed`, not passed in: the document
    says whether it actually carries prices.
    """
    from datetime import datetime, timezone

    return validate_feed(
        {
            "version": VERSION,
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "stale": stale,
            "max_price_age_seconds": max_price_age_seconds,
            "events": events,
        }
    )


def snap(price: Any) -> float | None:
    """Snap an upstream price onto the ladder, or None if it is not a price."""
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if value <= 1 or value > 1000:
        return None
    return snap_to_tick(value)