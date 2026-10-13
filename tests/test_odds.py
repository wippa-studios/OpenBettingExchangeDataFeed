"""Tick ladder: the port must agree with OpenBookmaker-Data/lib/odds.js.

Values below are the ladder's own definition, asserted independently of the
implementation so a rewrite that silently changes a published price fails here.
"""

import pytest

from conftest import FakeMarketBook, FakeRunnerBook, make_cfg

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


def test_a_value_that_is_not_a_price_is_not_a_probability():
    """`1/p` raised ZeroDivisionError at 0 and returned a NEGATIVE probability
    for a negative price — a plausible-looking number that is nonsense, and
    worse than a crash because nothing downstream flags it."""
    for bad in (0.0, -1.0, -2.5, 1.0, 0.5, None, "abc"):
        assert implied_prob(bad) is None, bad
    assert implied_prob(2.0) == 0.5  # unchanged for real prices
    assert overround([2.0, -1.0]) is None
    assert overround([]) == -1.0  # no prices: still an answer, not a crash

def test_valid_tick_is_exact_not_merely_within_half_a_step():
    """price_to_cents ROUNDS, so the old check accepted anything within half a
    step of a tick — and _rungs then stored the raw float. Measured True
    before the fix for all three of these; none is a price Betfair quotes."""
    for off in (1.015, 2.015, 1.999, 2.31, 1.005, 2.005):
        assert not valid_tick(off), f"{off} is not a tick but passed"
    # ...while every real tick still passes, including the ones whose float
    # representation is not exact (2.3 * 100 == 229.99999999999997).
    for good in (1.01, 1.5, 2.0, 2.02, 2.3, 2.5, 3.0, 3.05, 4.0, 4.1, 6.0, 6.2,
                 10.0, 10.5, 20.0, 21.0, 30.0, 32.0, 50.0, 55.0, 100.0, 110.0, 1000.0):
        assert valid_tick(good), f"{good} is a ladder tick and must stay valid"


def test_off_ladder_prices_never_reach_the_database():
    """The end-to-end half: _rungs stores the raw float, so a price that
    passes the check is what a consumer reads."""
    from betfeed.normalize import normalise_book

    book = normalise_book(FakeMarketBook(
        "1.1", [FakeRunnerBook(1, back=[(2.5, 100), (2.31, 50), (1.015, 10), (2.52, 20)])]
    ))
    prices = [r["price"] for r in book["selections"][0]["back"]]
    assert prices == [2.5, 2.52]
    assert 1.015 not in prices


def test_a_market_status_with_stray_whitespace_still_settles(caplog):
    """' CLOSED '.upper() is still ' CLOSED ', which matches nothing.

    The fail-safe direction was right — a status we cannot read is not treated
    as terminal, so nothing was wrongly settled — but the failure was silent:
    no result, no log line, and a feed that settles slowly for no visible
    reason.
    """
    from betfeed.normalize import normalise_book, result_from_book

    for raw in (" CLOSED ", "closed_event", "\tCLOSED_EVENT\n", " Closed "):
        book = normalise_book(FakeMarketBook("1.1", [FakeRunnerBook(1, status="WINNER")],
                                             status=raw))
        result = result_from_book(book)
        assert result is not None, f"{raw!r} did not settle"
        assert result["status"] == raw          # the raw value still travels
        assert result["winner_selection_ids"] == [1]

    book = normalise_book(FakeMarketBook("1.1", [FakeRunnerBook(1, status=" WINNER ")],
                                         status="OPEN"))
    assert result_from_book(book)["winner_selection_ids"] == [1]

    # An unrecognised status is still not settled, and says so.
    book = normalise_book(FakeMarketBook("1.1", [FakeRunnerBook(1)], status="WAT"))
    assert result_from_book(book) is None


def test_an_absurdly_deep_ladder_payload_is_refused_by_name(caplog):
    """_json_safe walked its input with no depth cap, so a ~1200-deep payload
    raised RecursionError — reported as "this market is bad", every poll,
    rather than as the absurd input it was."""
    from betfeed.normalize import MAX_JSON_DEPTH, _json_safe

    deep = value = {}
    for _ in range(MAX_JSON_DEPTH + 5):
        value["n"] = {}
        value = value["n"]
    with pytest.raises(ValueError, match=f"nested deeper than {MAX_JSON_DEPTH}"):
        _json_safe(deep)

    # A realistic ladder is nowhere near the cap.
    assert _json_safe({"selections": [{"back": [{"price": 2.0, "size": 1.0}]}]}) == {
        "selections": [{"back": [{"price": 2.0, "size": 1.0}]}]
    }


# ── one bad market must not cost the rest of the batch ────────────────────────
#
# The odds job iterates a poll scope of ~240 markets in ONE loop, and
# normalise_book -> valid_tick ran arithmetic on whatever arrived on the wire. A
# single non-finite price raised from deep inside math.floor(), and because there
# was no per-market guard that ended the WHOLE loop: every market after the bad
# one silently stopped updating, with no error recorded anywhere. poll_scope then
# picked the same markets next cycle, so it repeated forever — a board quietly
# going stale with a green job log.
#
# Two halves to the fix, both asserted here: the arithmetic refuses a non-finite
# price with a clear reason instead of an opaque OverflowError, and the loop
# isolates one market so the batch survives.


def test_a_non_finite_price_is_rejected_with_a_clear_reason():
    """Not `OverflowError: cannot convert float infinity to integer`.

    Two distinct rejections, because there are two distinct faults: a
    non-finite input, and a finite input whose value OVERFLOWS once scaled to
    cents (1e308 is finite; 1e308 * 100 is not). Both must name themselves, so an
    operator reading a log knows which one they hit.
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError) as e:
            valid_tick(bad)
        assert "non-finite" in str(e.value), f"unclear reason for {bad!r}: {e.value}"

    # 1e308 is finite. Checking only the INPUT leaves floor() to raise
    # OverflowError on the scaled value — which is exactly what this test found.
    with pytest.raises(ValueError) as e:
        valid_tick(1e308)
    assert "overflow" in str(e.value), f"1e308 must be rejected as an overflow: {e.value}"


def test_the_finite_boundary_is_unchanged():
    """The new check must not make the ladder permissive."""
    for good in (1.01, 1.5, 2.0, 100.0, 1000.0):
        assert valid_tick(good) is True, f"{good} is a ladder tick and must stay valid"
    assert valid_tick(0.99) is False, "below the floor"
    assert valid_tick(1000.01) is False, "above the ceiling"


def test_infinite_SIZE_is_still_handled():
    """Betfair's infinite-size 1.01 lay is a REAL response and must still work.

    The finiteness check is on PRICE only. A non-finite size is normalised to
    null, which is what stops it becoming invalid JSON — load-bearing behaviour
    that a blanket check would have broken.
    """
    from betfeed.normalize import _json_safe

    assert _json_safe({"size": float("inf")}) == {"size": None}
    assert _json_safe({"price": float("nan")}) == {"price": None}


def test_one_bad_market_does_not_abort_the_batch():
    """Three markets in scope, the middle one unreadable. The other two must
    still be written, and the failure must be counted and named."""
    from betfeed.jobs import odds as odds_job

    attempted = []

    class FakeConn:
        """Just enough of a connection for the scope query."""

        def execute(self, q, params=None):
            return self

        def fetchall(self):
            return [
                {"market_id": "1.1", "fixture_id": "10"},
                {"market_id": "1.2", "fixture_id": "10"},
                {"market_id": "1.3", "fixture_id": "10"},
            ]

    class FakeMarketBook:
        def __init__(self, market_id):
            self.market_id = market_id

    class FakeRunner:
        def __init__(self, selection_id):
            self.selection_id = selection_id

    class FakeClient:
        # Yields ALL THREE, always. A generator that RAISES is permanently dead,
        # so a raise here could only ever prove "the batch stops", not "the bad
        # market is skipped". The realistic failure is one unreadable BOOK among
        # healthy ones — the client streams fine and the normaliser rejects one —
        # and that is what the per-market guard is for.
        def list_market_book(self, ids):
            for mid in ids:
                attempted.append(mid)
                yield FakeMarketBook(mid)

    written = {"snapshots": 0, "current": 0}
    real_normalise = odds_job.normalise_book

    def flaky_normalise(mb):
        if mb.market_id == "1.2":
            raise ValueError("non-finite price: nan")
        return real_normalise(mb)

    monkey = {
        "ensure_partitions": lambda *a, **k: None,
        "advisory_lock": lambda *a, **k: _Lock(),
        "JobRun": _JobRun,
        "normalise_book": flaky_normalise,
        "_write_snapshots": lambda c, r: written.__setitem__("snapshots", len(r)) or len(r),
        "_write_current": lambda c, r: written.__setitem__("current", len(r)) or len(r),
        "_update_market_status": lambda *a, **k: None,
        "_retire_finished": lambda *a, **k: None,
        "write_result": lambda *a, **k: False,
    }
    import betfeed.jobs.odds as mod
    saved = {k: getattr(mod, k) for k in monkey}
    for k, v in monkey.items():
        setattr(mod, k, v)
    try:
        stats = odds_job.run_odds(FakeConn(), FakeClient(), make_cfg())
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)

    assert attempted == ["1.1", "1.2", "1.3"], f"every market attempted (got {attempted})"
    assert stats["errors"] == 1, f"exactly one failure counted (got {stats['errors']})"
    assert written["snapshots"] == 2, f"the two good markets were still stored (got {written['snapshots']})"


class _Lock:
    def __enter__(self):
        return True

    def __exit__(self, *a):
        return False


class _JobRun:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def count(self, **k):
        pass
