"""Tick ladder: the port must agree with OpenBookmaker-Data/lib/odds.js.

Values below are the ladder's own definition, asserted independently of the
implementation so a rewrite that silently changes a published price fails here.
"""

from betfeed.odds import (
    LADDER,
    MAX_PRICE_CENTS,
    MIN_PRICE_CENTS,
    band_for,
    implied_prob,
    next_tick,
    overround,
    prev_tick,
    price_to_cents,
    snap_to_tick,
    valid_tick,
)


def test_bands_cover_the_documented_ladder():
    assert LADDER[0] == (101, 200, 1)          # 1.01-2.00 step 0.01
    assert LADDER[1] == (202, 300, 2)          # 2.02-3.00 step 0.02
    assert LADDER[2] == (305, 400, 5)          # 3.05-4.00 step 0.05
    assert LADDER[3] == (410, 600, 10)         # 4.10-6.00 step 0.10
    assert LADDER[4] == (620, 1000, 20)        # 6.20-10.00 step 0.20
    assert LADDER[-1] == (11000, 100000, 1000)  # 110-1000 step 10


def test_exact_ticks_are_left_alone():
    for price in (1.01, 1.5, 2.0, 2.02, 2.5, 3.0, 3.05, 4.0, 4.1, 6.0, 6.2, 10.0,
                  10.5, 20.0, 21.0, 30.0, 32.0, 50.0, 55.0, 100.0, 110.0, 1000.0):
        assert valid_tick(price), f"{price} should be a tick"
        assert snap_to_tick(price) == price


def test_gap_prices_snap_to_the_nearer_band():
    # A price in the dead zone between two bands snaps to the nearer end, and a
    # tie snaps down. These are the gaps in the ladder itself: 2.01, 4.01-4.09,
    # 6.01-6.19, 10.01-10.49, 20.01-20.99 and so on.
    assert snap_to_tick(2.01) == 2.0    # 1c below vs 1c above -> down
    assert snap_to_tick(4.05) == 4.0    # 5c below (4.00) vs 5c above (4.10) -> down
    assert snap_to_tick(4.06) == 4.1    # 6c below vs 4c above -> up
    assert snap_to_tick(6.1) == 6.0     # tie -> down
    assert snap_to_tick(10.2) == 10.0   # tie -> down
    assert snap_to_tick(21.5) == 22.0   # inside the 21-30 band, halfway -> up
    assert snap_to_tick(34.0) == 34.0   # already on a tick, not in a gap


def test_midpoints_round_up_like_the_source():
    """Regression: Python's round() is banker's rounding and disagrees here.

    2.03 is exactly half a step inside the 2.02-3.00 band. JavaScript's
    Math.round sends it to 2.04; Python's round() sends it to 2.02. Betfair
    rounds up, so a port that used round() quietly published a different price
    on every midpoint.
    """
    assert snap_to_tick(2.03) == 2.04
    assert snap_to_tick(3.03) == 3.05   # half a 0.05 step -> up
    assert snap_to_tick(4.15) == 4.2    # half a 0.10 step -> up
    assert snap_to_tick(21.5) == 22.0
    assert price_to_cents(2.03) == 203


def test_gap_midpoints_still_snap_down():
    """A price in the dead zone between bands is not a band midpoint.

    4.05 falls in the 4.00-4.10 gap, so the gap rule applies (tie snaps down)
    even though it is also exactly half a 0.10 step. The in-band rule and the
    gap rule must not be confused for one another.
    """
    assert snap_to_tick(4.05) == 4.0
    assert snap_to_tick(4.06) == 4.1


def test_clamping():
    assert snap_to_tick(0.5) == 1.01
    assert snap_to_tick(-10) == 1.01
    assert snap_to_tick(5000) == 1000.0
    assert not valid_tick(0.99)
    assert not valid_tick(1000.01)


def test_snap_produces_a_valid_tick_for_any_price():
    for cents in range(MIN_PRICE_CENTS - 50, MAX_PRICE_CENTS + 50, 7):
        snapped = snap_to_tick(cents / 100)
        assert valid_tick(snapped), f"{cents/100} snapped to {snapped}, which is not a tick"


def test_next_and_prev_are_inverses():
    # One step up then one step down returns you home...
    for price in (1.01, 1.5, 2.02, 2.5, 3.05, 4.1, 6.2, 10.5, 21.0, 55.0, 110.0, 990.0):
        assert prev_tick(next_tick(price)) == price
    # ...and one step down then one step up, except at the floor where prev
    # clamps to 1.01 and there is no "down" to undo.
    for price in (1.02, 1.5, 2.02, 3.05, 4.1, 10.5, 21.0, 55.0, 110.0, 990.0):
        assert next_tick(prev_tick(price)) == price


def test_next_and_prev_clamp_at_the_ends():
    assert next_tick(1000.0) == 1000.0
    assert prev_tick(1.01) == 1.01
    # Crossing a band boundary steps to the next band's floor.
    assert next_tick(2.0) == 2.02
    assert prev_tick(2.02) == 2.0


def test_cents_conversion_is_exact():
    assert price_to_cents(2.31) == 231
    assert price_to_cents(1.01) == 101


def test_band_lookup():
    assert band_for(150) == (101, 200, 1)
    assert band_for(201) is None      # in the gap
    assert band_for(100000) == (11000, 100000, 1000)


def test_overround_and_probability():
    assert implied_prob(2.0) == 0.5
    # Two 2.0s is a 100% book: no margin.
    assert overround([2.0, 2.0]) == 0.0
    # A 2.0/2.0 book quoted at 1.95/1.95 has margin.
    assert overround([1.95, 1.95]) > 0