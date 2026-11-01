"""Wind math for the extractor (build step 6, piece 1).

Pure arithmetic, no files: everything here takes numbers and returns
numbers, so every function can be checked against values worked out by
hand (tests/test_wind.py).

The problem this solves. A GRIB file stores wind as two numbers, u and
v. On an ordinary map u is the eastward part of the wind and v the
northward part. But ECCC's HRDPS and RDWPS files say (with the flag
uvRelativeToGrid = 1) that u and v follow the model's OWN grid lines,
which are tilted. At Nickerson Bank the grid's rows run 45.9 degrees
south of east. Treat u as east there and every direction comes out
45.9 degrees wrong, while the speed stays perfect, so a speed check
never notices. notebooks/02-wind-rotation-check.ipynb shows it with
real numbers.

So the order is always:

    u_east, v_north = rotate_to_earth(u, v, tilt)   # only if grid-relative
    spd = speed(u_east, v_north)                     # m/s
    dirn = direction_from(u_east, v_north)           # degrees, where it comes FROM

Units: speeds stay in m/s (SI) everywhere inside the pipeline.
ms_to_knots() is only for display and for operational limits, which
offshore work states in knots.
"""

import math

# A knot is one nautical mile per hour, and a nautical mile is exactly
# 1852 metres (an international definition, not a measurement). So
# 1 m/s = 3600 m per hour = 3600 / 1852 knots, about 1.944.
METRES_PER_NAUTICAL_MILE = 1852.0
SECONDS_PER_HOUR = 3600.0


def rotate_to_earth(u_grid: float, v_grid: float, tilt_deg: float) -> tuple[float, float]:
    """Turn grid-relative u/v into true-east / true-north parts.

    tilt_deg is the direction the grid's rows run (the grid's x-axis),
    measured from true east, anticlockwise positive. Rows that run
    45.9 degrees SOUTH of east, as at Nickerson Bank, are -45.9.
    piece 2 measures it for each site from the file's lat/lon tables.

    The arrow itself doesn't move; we only describe it on different
    axes. That's why the speed before and after is the same.

    One angle is enough because the grid's rows and columns still cross
    at right angles: they are the latitude and longitude lines of a
    tilted globe. So the grid's y-axis is always the x-axis plus 90 degrees,
    and the change of axes is a plain rotation.
    """
    a = math.radians(tilt_deg)
    # The standard 2-D rotation. Check with tilt = 90 (rows run north):
    # a wind of u_grid = 10 "along the row" becomes u_east = 0,
    # v_north = 10, i.e. air moving north. tests/test_wind.py checks this.
    u_east = u_grid * math.cos(a) - v_grid * math.sin(a)
    v_north = u_grid * math.sin(a) + v_grid * math.cos(a)
    return u_east, v_north


def speed(u: float, v: float) -> float:
    """Wind speed: the length of the arrow, sqrt(u^2 + v^2).

    Same units as u and v (m/s from ECCC). Works on grid-relative or
    earth-relative parts alike, since turning an arrow never changes its
    length.
    """
    return math.hypot(u, v)


def direction_from(u_east: float, v_north: float) -> float:
    """Compass direction the wind blows FROM, in degrees [0, 360).

    Needs EARTH-relative parts (after rotate_to_earth if the file's
    flag says grid-relative). 0 = from the north, 90 = from the east,
    180 = from the south, 270 = from the west.

    atan2(v, u) gives the angle the air is moving TOWARD, measured the
    way maths does: from east, anticlockwise. Weather differs twice:
    compass bearings go clockwise from north, and wind direction names
    where the air comes FROM, the opposite end of the arrow.
    270 - angle does both conversions in one step:
        moving east  (angle 0)   -> 270, a westerly
        moving north (angle 90)  -> 180, a southerly
        moving west  (angle 180) -> 90,  an easterly
        moving south (angle -90) -> 360, written as 0, a northerly

    Simplification: weather reports often write a north wind as 360 and
    keep 0 for calm. We always use 0 to 359.99 and leave calm to the
    speed. With no wind at all (0, 0) the answer is 270 and means
    nothing, so callers check the speed before trusting a direction.
    """
    toward = math.degrees(math.atan2(v_north, u_east))  # -180 to 180, anticlockwise from east
    # 270 - toward is between 90 and 450; % 360 folds 360-450 back to 0-90.
    return (270.0 - toward) % 360.0


def angle_between(a_deg: float, b_deg: float) -> float:
    """The smaller gap between two directions, 0 to 180 degrees.

    Plain subtraction is wrong for directions: 359 and 1 are 2 degrees
    apart, not 358. The extractor uses this to compare our direction
    with ECCC's WDIR.
    """
    gap = abs(a_deg - b_deg) % 360.0
    # Going the other way round the circle may be shorter.
    return min(gap, 360.0 - gap)


def ms_to_knots(ms: float) -> float:
    """m/s -> knots. Only for display and operational limits."""
    return ms * SECONDS_PER_HOUR / METRES_PER_NAUTICAL_MILE


def knots_to_ms(knots: float) -> float:
    """knots -> m/s, e.g. to compare a limit like "25 knots" with stored data."""
    return knots * METRES_PER_NAUTICAL_MILE / SECONDS_PER_HOUR
