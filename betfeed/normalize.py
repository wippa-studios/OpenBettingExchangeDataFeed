"""Turn Betfair resources into plain dicts the database can hold.

Betfair's resources expose a mix of snake_case attributes and a raw ``_data``
dict (that is where ``marketStatus`` and ``venue`` live). Everything is
flattened here so the jobs never reach into SDK internals, and so the shape of a
snapshot is defined in one readable place.

Prices are normalised on the way in: a ladder rung whose price is not a real
Betfair tick is dropped rather than stored, because a price that never existed
on the exchange is worse than a missing rung.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .contract import state_for
from .odds import valid_tick

# Market statuses that mean the market has finished and we should retire it.
TERMINAL_STATUSES = {"CLOSED", "CLOSED_EVENT", "SETTLED", "VOIDED"}
# Runner statuses that mean this runner lost/was removed rather than merely
# being open for betting.
LOSING_STATUSES = {"LOSER", "REMOVED", "REMOVED_HALF_TIME", "REMOVED_OTHER"}


def _raw(obj: Any, key: str, default=None):
    """Read a field Betfair returned but the SDK did not promote."""
    data = getattr(obj, "_data", None)
    if isinstance(data, dict) and key in data:
        return data[key]
    return default


def market_status(catalogue_market) -> str | None:
    return _raw(catalogue_market, "marketStatus")


def normalise_catalogue_market(m, event_type_id: str) -> dict:
    """One market + its runners from listMarketCatalogue."""
    event = getattr(m, "event", None)
    competition = getattr(m, "competition", None)
    description = getattr(m, "description", None)
    start = getattr(m, "market_start_time", None)

    return {
        "market_id": m.market_id,
        "fixture_id": event.id if event else None,
        "event_name": event.name if event else None,
        "event_type_id": event_type_id,
        "competition_id": getattr(competition, "id", None),
        "competition_name": getattr(competition, "name", None),
        "country_code": getattr(event, "country_code", None),
        "event_venue": getattr(event, "venue", None),
        "market_venue": _raw(m, "venue"),
        "market_type_code": getattr(description, "market_type", None),
        "market_name": m.market_name,
        "market_start_time": start,
        "market_time": getattr(description, "market_time", None),
        "status": market_status(m),
        "total_matched": getattr(m, "total_matched", None),
        "runners": [
            {
                "selection_id": r.selection_id,
                "runner_name": r.runner_name,
                "sort_priority": getattr(r, "sort_priority", None),
                "handicap": getattr(r, "handicap", None),
            }
            for r in (getattr(m, "runners", None) or [])
        ],
    }


def _rungs(rungs: Iterable | None) -> list[dict]:
    out: list[dict] = []
    for p in (rungs or []):
        price = getattr(p, "price", None)
        size = getattr(p, "size", None)
        if price is None or not valid_tick(price):
            continue  # a price Betfair would never have quoted
        out.append({"price": float(price), "size": float(size or 0.0)})
    return out


def normalise_runner_book(r) -> dict:
    """One selection's book: ladder both sides plus its status."""
    ex = getattr(r, "ex", None)
    back = _rungs(getattr(ex, "available_to_back", None))
    lay = _rungs(getattr(ex, "available_to_lay", None))
    lpt = getattr(r, "last_price_traded", None)
    best_back = max((b["price"] for b in back), default=None)
    size_at_best = next((b["size"] for b in back if b["price"] == best_back), None) if best_back else None

    return {
        "selection_id": r.selection_id,
        # Deliberately None: a market book carries no runner name. Names live in
        # the selection table (feed 1's job) and the read side joins them on.
        "name": None,
        "status": getattr(r, "status", None),
        "handicap": getattr(r, "handicap", None),
        "last_price_traded": lpt,
        "total_matched": getattr(r, "total_matched", None),
        "price": best_back,
        "size": size_at_best,
        "back": back,
        "lay": lay,
        "traded": _rungs(getattr(ex, "traded_volume", None)),
    }


def normalise_book(mb) -> dict:
    """A market book as one snapshot row plus the ladder JSONB payload."""
    publish = getattr(mb, "publish_time", None)
    return {
        "market_id": mb.market_id,
        "status": getattr(mb, "status", None),
        "inplay": bool(getattr(mb, "inplay", False)),
        "bet_delay": getattr(mb, "bet_delay", None),
        "publish_time": publish,
        "total_matched": getattr(mb, "total_matched", None),
        "number_of_winners": getattr(mb, "number_of_winners", None),
        "bsp_reconciled": getattr(mb, "bsp_reconciled", None),
        "selections": [normalise_runner_book(r) for r in (getattr(mb, "runners", None) or [])],
    }


def _json_safe(value):
    """Replace non-finite floats with None, recursively.

    Betfair reports an INFINITE size for the best-lay-at-minimum-odds offer
    (the 1.01 lay that takes any stake). `json.dumps` writes that as the bare
    token `Infinity`, which Python accepts but which is NOT valid RFC 8259 JSON,
    so Postgres rejects the whole row:

        InvalidTextRepresentation: invalid input syntax for type json
        DETAIL:  Token "Infinity" is invalid.
        CONTEXT:  JSON data, line 1: ... "price": 1.26, "size": Infinity...

    That aborts the entire odds poll on any market carrying a 1.01 lay, which is
    almost every actively-quoted market. NaN has the same problem. Neither has a
    JSON representation, so they are stored as null -- meaning "no finite size
    offered", which is the honest reading of an infinite-size quote.
    """
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def ladder_payload(book: dict) -> dict:
    """The JSONB stored on market_snapshot / market_odds_current.

    Runner *names* are absent on purpose: Betfair's market book carries no
    runner names, and the stream-based recorder in the wider stack needs a
    separate backfill for the same reason. Names live in the selection table,
    which is what feed 1 populated, and the read side joins them back on.
    """
    return _json_safe({
        "status": book["status"],
        "inplay": book["inplay"],
        "bet_delay": book["bet_delay"],
        "publish_time": book["publish_time"].isoformat() if book["publish_time"] else None,
        "total_matched": float(book["total_matched"]) if book["total_matched"] is not None else None,
        "selections": [
            {
                "selection_id": s["selection_id"],
                "status": s["status"],
                "handicap": float(s["handicap"]) if s["handicap"] is not None else None,
                "last_price_traded": (
                    float(s["last_price_traded"]) if s["last_price_traded"] is not None else None
                ),
                "price": s["price"],
                "size": s["size"],
                "back": s["back"],
                "lay": s["lay"],
            }
            for s in book["selections"]
        ],
    })


def result_from_book(book: dict, *, observed_at: datetime | None = None) -> dict | None:
    """Settlement carried by a market book, or None if it has not finished.

    This is the free result: any book whose status is terminal, or which has
    assigned a WINNER, gives us the outcome with no extra API call.
    """
    status = (book["status"] or "").upper()
    runners = book["selections"]
    winners = [s["selection_id"] for s in runners if (s["status"] or "").upper() == "WINNER"]
    losers = [s["selection_id"] for s in runners if (s["status"] or "").upper() in LOSING_STATUSES]

    settled = status in TERMINAL_STATUSES or bool(winners or losers)
    if not settled:
        return None

    voided = bool(losers) and not winners and status not in ("VOIDED",)
    if status == "VOIDED":
        voided = True

    return {
        "market_id": book["market_id"],
        "status": book["status"],
        "voided": voided,
        "winner_selection_ids": winners,
        "observed_at": observed_at,
        "source": "book",
        "runners": [
            {
                "selection_id": s["selection_id"],
                "name": s.get("name"),
                "status": s["status"],
                "price": s.get("price"),
                "last_price_traded": s.get("last_price_traded"),
            }
            for s in runners
        ],
    }


def score_from_betfair(values: Any) -> dict | None:
    """Pull home/away score out of the Scores API's sport-specific payload.

    ``values`` has a different shape per sport and Betfair does not document
    all of them, so this reads defensively and returns None rather than
    guessing. The raw payload is stored alongside regardless.
    """
    if not isinstance(values, dict):
        return None

    def side(name: str) -> tuple[str | None, str | None]:
        block = values.get(name)
        if isinstance(block, dict):
            team = block.get("team") or block.get("name")
            score = block.get("score")
            return (str(team) if team is not None else None, str(score) if score is not None else None)
        if isinstance(block, str):
            return (None, block)
        return (None, None)

    home_team, home_score = side("home")
    away_team, away_score = side("away")
    if home_score is None and away_score is None:
        return None
    return {
        "home": home_team,
        "away": away_team,
        "home_score": home_score,
        "away_score": away_score,
    }


def market_state(status: Any, inplay: bool | None = None) -> str:
    return state_for(status, inplay)