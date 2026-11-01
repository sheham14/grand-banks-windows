"""Tests for ingest/wind.py.

Every expected value here can be worked out by hand: arrows pointing
straight east, north, south or west, a 3-4-5 triangle, a 90-degree turn.
The one real case is the Nickerson Bank cell from
notebooks/02-wind-rotation-check.ipynb (HRDPS 2026-10-28 12Z, hour 24),
where ECCC's own WDIR field gives the answer: 268.4 degrees.
"""

import pytest

from ingest.wind import (
    angle_between,
    direction_from,
    knots_to_ms,
    ms_to_knots,
    rotate_to_earth,
    speed,
)

# Directions to 0.01 degree, speeds to 0.01 m/s.
approx = lambda x: pytest.approx(x, abs=0.01)  # noqa: E731


# --- direction_from: the four compass points ---------------------------
# u is the eastward part, v the northward part. The wind comes FROM the
# opposite side to where the air is moving.

def test_air_moving_east_is_a_westerly():
    assert direction_from(10, 0) == approx(270)


def test_air_moving_north_is_a_southerly():
    assert direction_from(0, 10) == approx(180)


def test_air_moving_west_is_an_easterly():
    # The learning log's example: 90 degrees is an easterly, so u is negative.
    assert direction_from(-10, 0) == approx(90)


def test_air_moving_south_is_a_northerly_written_as_0():
    # 0, not 360: we keep every direction in [0, 360).
    assert direction_from(0, -10) == approx(0)


def test_diagonal():
    # Air moving north-east (equal east and north parts) comes from the
    # south-west: 225.
    assert direction_from(5, 5) == approx(225)


def test_direction_is_always_below_360():
    for u, v in [(0, -10), (1e-12, -10), (-1e-12, -10), (0.001, -5)]:
        assert 0 <= direction_from(u, v) < 360


# --- speed -------------------------------------------------------------

def test_speed_3_4_5_triangle():
    assert speed(3, 4) == approx(5)
    assert speed(-3, -4) == approx(5)  # direction doesn't matter


# --- rotate_to_earth ---------------------------------------------------

def test_no_tilt_changes_nothing():
    assert rotate_to_earth(7.47, 8.15, 0) == (approx(7.47), approx(8.15))


def test_rows_running_north():
    # tilt +90: the grid's rows run due north. "10 m/s along the row"
    # is then 10 m/s northward, a southerly.
    u_east, v_north = rotate_to_earth(10, 0, 90)
    assert (u_east, v_north) == (approx(0), approx(10))
    assert direction_from(u_east, v_north) == approx(180)


@pytest.mark.parametrize("tilt", [-90, -45.9, 0, 12.5, 90, 180])
def test_rotation_never_changes_speed(tilt):
    # The whole reason the bug hides: turning the axes keeps the length.
    u_east, v_north = rotate_to_earth(7.47, 8.15, tilt)
    assert speed(u_east, v_north) == approx(speed(7.47, 8.15))


def test_nickerson_bank_matches_ecccs_wdir():
    """The Phase 1 "Done when" test: a real grid-relative pair, rotated.

    At cell (955, 2337) the file gives u = 7.47, v = 8.15 along the grid,
    and the rows run 45.9 degrees south of east. ECCC's WDIR says 268.4,
    ECCC's WIND says 11.07 (11.06 here; ECCC rounds before storing).
    """
    u_east, v_north = rotate_to_earth(7.47, 8.15, -45.9)
    assert direction_from(u_east, v_north) == pytest.approx(268.4, abs=0.1)
    assert speed(u_east, v_north) == pytest.approx(11.06, abs=0.01)


def test_nickerson_bank_without_rotation_is_the_bug():
    # Reading u as east gives 222.5: a south-westerly instead of a
    # westerly, wrong by the full tilt. This is the mistake the rotation
    # exists to prevent.
    wrong = direction_from(7.47, 8.15)
    assert wrong == pytest.approx(222.5, abs=0.1)
    assert angle_between(wrong, 268.4) == pytest.approx(45.9, abs=0.1)


# --- angle_between -----------------------------------------------------

@pytest.mark.parametrize(
    "a, b, gap",
    [
        (359, 1, 2),     # across north: 2, not 358
        (1, 359, 2),     # same, the other order
        (10, 350, 20),
        (0, 180, 180),   # opposite directions: the biggest possible gap
        (90, 90, 0),
        (0, 360, 0),     # 360 and 0 are the same direction
    ],
)
def test_angle_between(a, b, gap):
    assert angle_between(a, b) == approx(gap)


# --- knots -------------------------------------------------------------

def test_10_ms_in_knots():
    # 10 m/s = 36,000 m per hour = 36000 / 1852 nautical miles per hour.
    assert ms_to_knots(10) == pytest.approx(19.438, abs=0.001)


def test_25_knots_in_ms():
    # A typical example operational limit: 25 knots is about 12.86 m/s.
    assert knots_to_ms(25) == pytest.approx(12.861, abs=0.001)


def test_knots_round_trip():
    assert knots_to_ms(ms_to_knots(11.06)) == pytest.approx(11.06)
