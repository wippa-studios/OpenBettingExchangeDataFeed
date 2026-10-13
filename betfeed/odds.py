"""Betfair tick ladder — prices are integer cents internally.

Ported from OpenBookmaker-Data `lib/odds.js` (MIT, wippa-studios) so a price
this feed publishes is a price the customer's own bot can ladder. Every price
that enters the database goes through :func:`snap_to_tick`, and a price we could
not put on the ladder is stored as NULL rather than published as something that
never existed on Betfair.
"""

from __future__ import annotations

import math

LADDER: tuple[tuple[int, int, int], ...] = (
    (101, 200, 1),        # 1.01-2.00   step 0.01
    (202, 300, 2),        # 2.02-3.00   step 0.02
    (305, 400, 5),        # 3.05-4.00   step 0.05
    (410, 600, 10),       # 4.10-6.00   step 0.10
    (620, 1000, 20),      # 6.20-10.00  step 0.20
    (1050, 2000, 50),     # 10.5-20.00  step 0.5
    (2100, 3000, 100),    # 21-30       step 1
    (3200, 5000, 200),    # 32-50       step 2
    (5500, 10000, 500),   # 55-100      step 5
    (11000, 100000, 1000),  # 110-1000  step 10
)

MIN_PRICE_CENTS = 101
MAX_PRICE_CENTS = 100000

# Tolerance for "was this already a whole number of cents?". 2.3 * 100 is
# 229.99999999999997 in IEEE 754, so the test cannot be exact; 1e-6 cents is
# ~1e10 times the noise and ~5 million times smaller than the half-step band
# it has to reject.
_CENT_EPSILON = 1e-6


def price_to_cents(p: float) -> int:
    # A non-finite price used to raise deep inside _round_half_up's
    # math.floor(), as an OverflowError or a bare "cannot convert float NaN to
    # integer". That is raised once per ladder RUNG, from inside a loop over a
    # whole market book — so one bad price took down every other runner beside
    # it, and because the odds job had no per-market try/except, every remaining
    # market in the batch too. One bad rung and those markets stop updating,
    # silently.
    #
    # Betfair DOES emit Infinity as a SIZE (its infinite-size 1.01 lay), which
    # is handled separately and correctly. A non-finite PRICE is not an expected
    # input, so this is defence in depth — but crashing a whole batch is not a
    # proportionate response to a bad number, and the caller cannot express
    # "skip this rung" because there is no try.
    v = float(p)
    if not math.isfinite(v):
        raise ValueError(f"non-finite price: {p!r}")
    scaled = v * 100.0
    # The INPUT being finite is not enough: 1e308 is finite and 1e308 * 100 is
    # inf, so floor() still raises OverflowError two lines later. Check the value
    # that actually reaches the rounding.
    if not math.isfinite(scaled):
        raise ValueError(f"price overflows the cent scale: {p!r}")
    return _round_half_up(scaled)


def _round_half_up(x: float) -> int:
    """Round .5 up, always.

    NOT Python's round(), which is banker's rounding: round(0.5) == 0 and
    round(1.5) == 2. The ladder's index arithmetic hits exact .5 constantly
    (2.03 is exactly half a step inside the 2.02-3.00 band), so using round()
    here silently disagreed with the JavaScript this was ported from on every
    midpoint price. Betfair rounds these up.
    """
    return math.floor(x + 0.5)


def cents_to_price(c: int) -> float:
    return c / 100


def band_for(cents: int):
    for band in LADDER:
        if band[0] <= cents <= band[1]:
            return band
    return None


def snap_to_tick(p: float) -> float:
    """Clamp to [1.01, 1000] and round to the nearest valid tick.

    Prices that fall in the gap between two bands snap to the nearer end;
    ties snap down. This is what the exchange itself does, so a published price
    is always a price Betfair quoted.
    """
    c = price_to_cents(p)
    c = max(MIN_PRICE_CENTS, min(MAX_PRICE_CENTS, c))
    band = band_for(c)
    if band:
        lo, hi, step = band
        max_idx = _round_half_up((hi - lo) / step)
        idx = max(0, min(max_idx, _round_half_up((c - lo) / step)))
        return cents_to_price(lo + idx * step)

    below = above = None
    for lo, hi, _step in LADDER:
        if hi < c:
            below = hi
        if above is None and lo > c:
            above = lo
    if below is None:
        return cents_to_price(MIN_PRICE_CENTS)
    if above is None:
        return cents_to_price(MAX_PRICE_CENTS)
    return cents_to_price(below if (c - below) <= (above - c) else above)


def next_tick(p: float) -> float:
    c = price_to_cents(snap_to_tick(p))
    band = band_for(c)
    if not band:
        return cents_to_price(MAX_PRICE_CENTS)
    lo, hi, step = band
    idx = _round_half_up((c - lo) / step)
    if idx < _round_half_up((hi - lo) / step):
        return cents_to_price(c + step)
    i = LADDER.index(band)
    if i == len(LADDER) - 1:
        return cents_to_price(MAX_PRICE_CENTS)
    return cents_to_price(LADDER[i + 1][0])


def prev_tick(p: float) -> float:
    c = price_to_cents(snap_to_tick(p))
    band = band_for(c)
    if not band:
        return cents_to_price(MIN_PRICE_CENTS)
    lo, _hi, step = band
    idx = _round_half_up((c - lo) / step)
    if idx > 0:
        return cents_to_price(c - step)
    i = LADDER.index(band)
    if i == 0:
        return cents_to_price(MIN_PRICE_CENTS)
    return cents_to_price(LADDER[i - 1][1])


def valid_tick(p: float) -> bool:
    """Is this EXACTLY a price on the Betfair ladder?

    Exactness matters. This used to be `price_to_cents(p)` and a grid check,
    but price_to_cents ROUNDS, so anything within half a step of a tick passed
    and was then stored raw by normalize._rungs — measured True for 1.015,
    2.015 and 1.999, none of which is a price Betfair quotes, and 1.015 went
    into the database as 1.015 rather than 1.02.

    Round first (so float noise like 2.3 * 100 == 229.99999999999997 still
    reads as 230), then require the input to have BEEN that whole number of
    cents. A price genuinely between ticks — 2.31 — stays rejected rather than
    being snapped into something plausible, which is what the contract wants
    too: a price we could not put on the ladder is dropped, not invented.
    """
    try:
        value = float(p)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        # Not a "not a tick" answer: a fault worth naming, as it always was.
        return price_to_cents(value)  # raises ValueError with the reason
    scaled = value * 100.0
    if not math.isfinite(scaled):
        return price_to_cents(value)  # raises ValueError: finite input, infinite scale
    c = _round_half_up(scaled)
    if abs(scaled - c) > _CENT_EPSILON:
        return False  # within half a cent of a whole cent: not a real tick
    if c < MIN_PRICE_CENTS or c > MAX_PRICE_CENTS:
        return False
    band = band_for(c)
    return bool(band) and (c - band[0]) % band[2] == 0


def implied_prob(p: float) -> float | None:
    """Raw decimal-odds price -> implied probability, or None if it is not one.

    A price is only a probability if it is greater than 1. `1/p` on its own
    raised ZeroDivisionError at zero and cheerfully returned a NEGATIVE
    probability for a negative price — a plausible-looking number that is
    nonsense, and worse than a crash because nothing downstream flags it.

    Prices below 1 cannot reach the database (valid_tick rejects them at the
    boundary), but these helpers are public: the MCP server and any consumer
    importing betfeed.odds call them directly, on book-derived input.
    """
    try:
        value = float(p)
    except (TypeError, ValueError):
        return None
    if value <= 1:
        return None
    return 1 / value


def overround(prices) -> float | None:
    """Sum of implied probabilities minus 1, or None if any price is not one.

    Betting markets are overround (sum of implied probabilities > 1). A price
    at or below 1 is not a decimal-odds price, so this refuses rather than
    returning a number built from nonsense.
    """
    total = 0.0
    for p in prices:
        q = implied_prob(p)
        if q is None:
            return None
        total += q
    return total - 1