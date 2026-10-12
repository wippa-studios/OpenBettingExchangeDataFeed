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


def price_to_cents(p: float) -> int:
    return _round_half_up(float(p) * 100)


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
    c = price_to_cents(p)
    if c < MIN_PRICE_CENTS or c > MAX_PRICE_CENTS:
        return False
    band = band_for(c)
    return bool(band) and (c - band[0]) % band[2] == 0


def implied_prob(p: float) -> float:
    return 1 / p


def overround(prices) -> float:
    return sum(1 / p for p in prices) - 1