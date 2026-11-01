"""Find each site's grid cell, and the grid's tilt there (build step 6, piece 2).

The extractor needs one forecast value per site per hour, so for each
model it must decide which grid point stands in for each site. That
answer never changes while the grid stays the same, so it's worked out
once, when the extractor starts, from any one file of that model:

    cells = locate_sites(any_hrdps_file, load_sites())
    cells["nickerson_bank"]  ->  row 955, column 2337, 1.14 km away, tilt -45.9

How (the same steps as notebooks/02-wind-rotation-check.ipynb):

  1. Every point in the file carries its own latitude and longitude. On
     HRDPS's tilted grid a row is not a line of latitude, so the row
     can't be calculated from the site's latitude. Instead we measure the
     distance from the site to every point (3.3 million of them; numpy
     does it in well under a second) and keep the closest.
  2. The tilt: which way the grid's row runs at that cell, measured from
     the points on either side. wind.rotate_to_earth() needs it to turn
     grid-relative u/v into true east and north.

Each model gets its own lookup. The RDWPS wave grid is 4 points smaller
each way than HRDPS, so the same site lands in a different cell:
Nickerson Bank is (955, 2337) on HRDPS but (953, 2335) on RDWPS.
Reusing the HRDPS cell for waves would read a point 8 km from the site,
and nothing would complain.

Simplifications, stated so nobody mistakes them for more:
  - We take the nearest point's value. Operational point forecasts often
    interpolate from the surrounding points instead; at 2.5 km spacing
    the nearest point is a reasonable stand-in.
  - The tilt is measured from neighbouring points, not computed from the
    grid's pole position with spherical trigonometry. The two agree to a
    fraction of a degree here, and the extractor's WDIR check confirms
    the result on every run.

Look at it by hand:
    python -m ingest.sitegrid data/raw/hrdps/<any HRDPS file>.grib2
"""

import argparse
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import eccodes
import numpy as np
import yaml

from ingest.filenames import parse_filename
from ingest.models import REPO_ROOT

log = logging.getLogger("ingest.sitegrid")

DEFAULT_SITES = REPO_ROOT / "config" / "sites.yaml"

# Mean Earth radius. The real Earth is slightly flattened, so distances
# can be off by up to about 0.5%: a few metres here, far below the
# 2.5 km grid spacing.
EARTH_RADIUS_KM = 6371.0

# On a 2.5 km grid a site is never more than half a cell's diagonal from
# a point: about 1.8 km. Further than 2 km means the site is outside this
# grid, or its coordinates are wrong (e.g. a longitude missing its minus).
MAX_DISTANCE_KM = 2.0

# How far we'll move a site whose nearest point has no data (land, for
# the wave model). Beyond this the point no longer stands for the site.
# 10 km is our own judgment call, not a standard.
MAX_MOVE_KM = 10.0

# The order the points are stored in, as read by hand on 2026-11-01 from
# both an HRDPS and an RDWPS file: row by row, starting at the grid's
# bottom-left corner, each row in the grid's x direction. Then the point
# in row j, column i is number j * Ni + i in the file's flat list (Ni =
# points per row). Another order would make that formula wrong, so we
# refuse it instead of guessing.
_EXPECTED_ORDER = {
    "iScansNegatively": 0,       # within a row, columns go in increasing x
    "jScansPositively": 1,       # rows go upward from the grid's bottom edge
    "jPointsAreConsecutive": 0,  # a whole row is stored before the next one
}


class SiteGridError(Exception):
    """A site can't be placed on a grid. The message says why."""


@dataclass(frozen=True)
class Site:
    """One point from config/sites.yaml."""

    id: str     # e.g. "nickerson_bank"
    name: str   # e.g. "Nickerson Bank"
    lat: float  # degrees north
    lon: float  # degrees east, so negative = west
    role: str   # "forecast" or "verification"


@dataclass(frozen=True)
class SiteCell:
    """Where one site sits on one model's grid."""

    site_id: str
    row: int            # j, counted from the grid's bottom edge
    col: int            # i, counted from the grid's left edge
    index: int          # row * Ni + col: where its value sits in the file's flat list
    lat: float          # where that grid point really is
    lon: float
    distance_km: float  # from the site to that point
    tilt_deg: float     # which way the grid's row runs there, for wind.rotate_to_earth()
    moved: bool         # True if the nearest point had no data, so a further one was used


@dataclass(frozen=True)
class _Grid:
    """What locate_sites needs from a file: positions and which points have data."""

    ni: int                       # points per row
    lats: np.ndarray              # flat list, one latitude per point
    lons: np.ndarray              # flat list, one longitude per point
    has_data: np.ndarray | None   # True where the point has a value; None = all do


def load_sites(path: Path = DEFAULT_SITES) -> list[Site]:
    """Read config/sites.yaml into a list of Site.

    Every site is returned, whatever its role: verification sites
    (Phase 2a) need forecasts extracted too, to score them against.
    """
    # safe_load: see the note in models.load_models().
    with open(path) as f:
        raw = yaml.safe_load(f)

    sites = []
    for s in raw["sites"]:
        lat, lon = float(s["lat"]), float(s["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"{s['id']}: impossible position {lat}, {lon}")
        sites.append(Site(id=s["id"], name=s["name"], lat=lat, lon=lon, role=s["role"]))
    return sites


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance over the Earth's surface in km (the haversine formula).

    Follows the curve of the Earth, so a degree of longitude counts for
    less the further north you go: 77 km at Nickerson Bank, against
    111 km for a degree of latitude. Takes single numbers or whole numpy
    arrays, so one call measures a site against every grid point.
    """
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    # No need to wrap the longitude difference into -180..180 first:
    # sin^2 of half the angle repeats every 360 degrees anyway.
    dlmb = np.radians(np.asarray(lon2) - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def nearest_cell(
    lats: np.ndarray,
    lons: np.ndarray,
    site_lat: float,
    site_lon: float,
    has_data: np.ndarray | None = None,
) -> tuple[int, float, bool]:
    """Find the grid point closest to a site.

    lats, lons: the file's flat lists of point positions.
    has_data: for grids with a mask, True where a point has a value.
      RDWPS masks land and everything outside the wave model's area
      (65% of its points). None means every point has data (HRDPS).

    Returns (index, distance_km, moved). Raises SiteGridError if the
    site is off the grid, or no point with data is within MAX_MOVE_KM.
    """
    dist = haversine_km(site_lat, site_lon, lats, lons)  # one distance per point
    k = int(dist.argmin())  # argmin: the position of the smallest
    if dist[k] > MAX_DISTANCE_KM:
        raise SiteGridError(
            f"nearest point is {dist[k]:.0f} km away, so "
            f"({site_lat}, {site_lon}) is outside this grid"
        )
    if has_data is None or has_data[k]:
        return k, float(dist[k]), False

    # The nearest point has no data (for waves: it's land). Take the
    # nearest point that does, if it's close enough to still stand for
    # the site. Real example: St. John's harbour's nearest RDWPS point is
    # land; the nearest sea point is 4.57 km away.
    dist_with_data = np.where(has_data, dist, np.inf)  # masked points can't win
    k = int(dist_with_data.argmin())
    if dist_with_data[k] > MAX_MOVE_KM:
        raise SiteGridError(
            f"nearest point with data is {dist_with_data[k]:.1f} km away "
            f"(limit {MAX_MOVE_KM:.0f} km)"
        )
    return k, float(dist_with_data[k]), True


def row_tilt(lats: np.ndarray, lons: np.ndarray, ni: int, index: int) -> float:
    """Which way the grid's row runs at a point, in degrees from true east.

    Anticlockwise is positive, the convention wind.rotate_to_earth()
    uses: a row running 45.9 degrees SOUTH of east (Nickerson Bank)
    is -45.9.

    Measured between the points on either side in the same row: numbers
    index - 1 and index + 1 in the flat list. Using both sides, not just
    the next point as the notebook did, centres the measurement on the
    cell itself.
    """
    col = index % ni
    if col == 0 or col == ni - 1:
        raise SiteGridError(f"point {index} is on the grid's edge, with no neighbour on one side")
    before, after = index - 1, index + 1

    d_north = lats[after] - lats[before]
    # Wrap into -180..180 in case the two points straddle the 180 degree
    # line. They never do here, but it costs one line.
    d_lon = (lons[after] - lons[before] + 180.0) % 360.0 - 180.0
    # A degree of longitude is shorter than a degree of latitude away
    # from the equator, by cos(latitude). Scaling by it makes the two
    # comparable, like measuring both in km. Over 5 km this flat
    # approximation is far more accurate than the 0.1 degree we need.
    d_east = d_lon * math.cos(math.radians(lats[index]))
    # atan2 gives the angle from east, anticlockwise: what we want.
    return math.degrees(math.atan2(d_north, d_east))


def locate_sites(grib_path: Path, sites: list[Site]) -> dict[str, SiteCell]:
    """Place every site on the grid used by the file at grib_path.

    Any file of a model will do: all of a model's files share one grid.
    Returns {site id: SiteCell}. Raises SiteGridError if the file's
    points are stored in an unexpected order or a site can't be placed.
    """
    grid = _read_grid(grib_path)
    model = parse_filename(grib_path).model

    cells = {}
    for site in sites:
        try:
            k, dist, moved = nearest_cell(grid.lats, grid.lons, site.lat, site.lon, grid.has_data)
            tilt = row_tilt(grid.lats, grid.lons, grid.ni, k)
        except SiteGridError as e:
            # Add which site and model, so the log says where it failed.
            raise SiteGridError(f"{site.id} on {model}: {e}") from e

        # Undo index = row * Ni + col: divmod gives (row, col) in one step.
        row, col = divmod(k, grid.ni)
        cells[site.id] = SiteCell(
            site_id=site.id, row=row, col=col, index=k,
            lat=float(grid.lats[k]), lon=float(grid.lons[k]),
            distance_km=dist, tilt_deg=tilt, moved=moved,
        )

        message = "%s %s: cell (%d, %d), %.2f km away, tilt %.1f deg"
        args = (site.id, model, row, col, dist, tilt)
        if moved:
            # Worth a warning: this value comes from further away than usual.
            log.warning(message + " (moved: the nearest point has no data)", *args)
        else:
            log.info(message, *args)
    return cells


def _read_grid(path: Path) -> _Grid:
    """Read the point positions, and the data mask if any, from one file."""
    # Every file passed gribcheck on arrival, so it holds exactly one
    # message; we read the first.
    with open(path, "rb") as f:
        handle = eccodes.codes_grib_new_from_file(f)
        if handle is None:
            raise SiteGridError(f"{path.name}: no GRIB message in the file")
        try:
            order = {key: eccodes.codes_get(handle, key) for key in _EXPECTED_ORDER}
            if order != _EXPECTED_ORDER:
                raise SiteGridError(f"{path.name}: points stored in an unexpected order {order}")

            ni = eccodes.codes_get(handle, "Ni")
            # ecCodes works out every point's position from the grid
            # definition in the file (section 3: the rotated pole, spacing
            # and size), in the same order as the values.
            lats = eccodes.codes_get_array(handle, "latitudes")
            lons = eccodes.codes_get_array(handle, "longitudes")

            has_data = None
            if eccodes.codes_get(handle, "bitmapPresent"):
                # The bitmap (section 6) marks which points have a value.
                # ecCodes fills the others with missingValue (9999 in
                # these files), so comparing against it recovers the mask.
                values = eccodes.codes_get_values(handle)
                has_data = values != eccodes.codes_get(handle, "missingValue")
        finally:
            # Release the C library's memory (see gribcheck._read_single_header).
            eccodes.codes_release(handle)
    return _Grid(ni=ni, lats=lats, lons=lons, has_data=has_data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show where each site sits on a model's grid.")
    parser.add_argument("grib_file", type=Path, help="any one file of the model")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    locate_sites(args.grib_file, load_sites())


# Runs main() only when started as a program, not when imported.
if __name__ == "__main__":
    main()
