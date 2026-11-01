"""Tests for ingest/sitegrid.py.

Two kinds:
  - Tiny hand-made grids (a few points with made-up positions) for the
    logic: nearest point, masked points, tilt. Every answer can be
    worked out by hand.
  - Real HRDPS and RDWPS files from data/raw/ (2026-10-28 12Z, hour 24),
    whose answers were first found by hand in
    notebooks/02-wind-rotation-check.ipynb. data/ isn't in git, so these
    skip on a machine without downloads.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from ingest.sitegrid import (
    Site,
    SiteGridError,
    _read_grid,
    haversine_km,
    load_sites,
    locate_sites,
    nearest_cell,
    row_tilt,
)

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
HRDPS = RAW / "hrdps" / "20261028T12Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT024H.grib2"
RDWPS = RAW / "rdwps" / "20261028T12Z_MSC_RDWPS_HTSGW_Sfc_RLatLon0.0225_PT024H.grib2"

NICKERSON = Site("nickerson_bank", "Nickerson Bank", 46.44, -53.39, "forecast")
WSP = Site("wsp_grand_banks", "WSP Grand Banks", 47.24, -50.95, "forecast")
# Not one of our sites: chosen because its nearest wave point is land.
ST_JOHNS = Site("st_johns_harbour", "St. John's harbour", 47.565, -52.705, "test")

need_real_files = pytest.mark.skipif(
    not (HRDPS.exists() and RDWPS.exists()), reason="real GRIB files not in data/raw/"
)


# --- haversine_km -------------------------------------------------------

def test_one_degree_of_latitude():
    # The Earth's circumference / 360 = 2 * pi * 6371 / 360 = 111.195 km.
    assert haversine_km(46, -53, 47, -53) == pytest.approx(111.195, abs=0.001)


def test_one_degree_of_longitude_is_shorter_at_nickerson():
    # Lines of longitude close in toward the pole: at 46.44N a degree of
    # longitude is about 111.195 * cos(46.44) = 76.6 km.
    expected = 111.195 * math.cos(math.radians(46.44))
    assert haversine_km(46.44, -54, 46.44, -53) == pytest.approx(expected, abs=0.1)


# --- nearest_cell, on a tiny ordinary grid ------------------------------

def _tiny_grid():
    """3 x 3 points, 0.0225 degrees apart, rows along lines of latitude.

    Flat lists in file order (row by row, from the bottom-left), like
    a real file. The middle point is number 4, at 46.0225N 53.9775W.
          6 7 8
          3 4 5
          0 1 2
    """
    step = 0.0225
    lats = np.array([46.0 + j * step for j in range(3) for i in range(3)])
    lons = np.array([-54.0 + i * step for j in range(3) for i in range(3)])
    return lats, lons


def test_nearest_is_the_middle_point():
    lats, lons = _tiny_grid()
    k, dist, moved = nearest_cell(lats, lons, 46.0225, -53.9725)  # just east of point 4
    assert k == 4
    assert dist < 1  # 0.005 degrees of longitude, about 0.4 km
    assert not moved


def test_masked_nearest_moves_to_next_closest():
    lats, lons = _tiny_grid()
    has_data = np.ones(9, dtype=bool)
    has_data[4] = False  # pretend the middle point is land
    # The site is just east of point 4, so point 5 (east) is the next
    # closest, ahead of point 3 (west).
    k, dist, moved = nearest_cell(lats, lons, 46.0225, -53.9725, has_data)
    assert k == 5
    assert moved


def test_site_off_the_grid_is_refused():
    lats, lons = _tiny_grid()
    with pytest.raises(SiteGridError, match="outside this grid"):
        nearest_cell(lats, lons, 50.0, -54.0)  # 440 km north


def test_no_data_nearby_is_refused():
    lats, lons = _tiny_grid()
    with pytest.raises(SiteGridError, match="limit"):
        nearest_cell(lats, lons, 46.0225, -53.9775, np.zeros(9, dtype=bool))


# --- row_tilt -------------------------------------------------------------

def test_ordinary_grid_has_no_tilt():
    lats, lons = _tiny_grid()
    assert row_tilt(lats, lons, 3, 4) == pytest.approx(0, abs=1e-9)


def test_row_running_south_east():
    # One row of 3 points, each a step south and an equal distance east.
    # The east step is 0.01 / cos(lat) degrees of longitude, so that in
    # km it matches the 0.01 degree south step. Exactly 45 degrees south
    # of east: -45.
    c = math.cos(math.radians(46.44))
    lats = np.array([46.45, 46.44, 46.43])
    lons = np.array([-53.40 - 0.01 / c, -53.40, -53.40 + 0.01 / c])
    assert row_tilt(lats, lons, 3, 1) == pytest.approx(-45, abs=1e-6)


def test_edge_point_is_refused():
    lats, lons = _tiny_grid()
    with pytest.raises(SiteGridError, match="edge"):
        row_tilt(lats, lons, 3, 3)  # point 3 starts a row: nothing to its left


# --- load_sites -----------------------------------------------------------

def test_load_sites_reads_our_two_forecast_sites():
    sites = {s.id: s for s in load_sites()}
    assert sites["nickerson_bank"].lat == 46.44
    assert sites["nickerson_bank"].lon == -53.39  # west is negative
    assert sites["wsp_grand_banks"].role == "forecast"


# --- real files -----------------------------------------------------------
# module scope: read each 3.3-million-point grid once, not once per test.

@pytest.fixture(scope="module")
def hrdps_cells():
    return locate_sites(HRDPS, [NICKERSON, WSP])


@pytest.fixture(scope="module")
def rdwps_cells():
    return locate_sites(RDWPS, [NICKERSON, WSP, ST_JOHNS])


@need_real_files
def test_nickerson_on_hrdps(hrdps_cells):
    c = hrdps_cells["nickerson_bank"]
    assert (c.row, c.col) == (955, 2337)
    assert c.distance_km == pytest.approx(1.14, abs=0.01)
    # The tilt that made the rotation match ECCC's WDIR in the notebook.
    assert c.tilt_deg == pytest.approx(-45.9, abs=0.1)
    assert not c.moved


@need_real_files
def test_wsp_on_hrdps(hrdps_cells):
    c = hrdps_cells["wsp_grand_banks"]
    assert (c.row, c.col) == (1033, 2362)
    assert c.distance_km == pytest.approx(0.41, abs=0.01)
    assert c.tilt_deg == pytest.approx(-47.6, abs=0.1)


@need_real_files
def test_waves_use_a_different_cell(hrdps_cells, rdwps_cells):
    # Same site, different grid, different cell: the wave grid is
    # 4 points smaller each way.
    c = rdwps_cells["nickerson_bank"]
    assert (c.row, c.col) == (953, 2335)
    assert (c.row, c.col) != (hrdps_cells["nickerson_bank"].row, hrdps_cells["nickerson_bank"].col)
    assert not c.moved  # far offshore: the nearest point is sea


@need_real_files
def test_reusing_the_hrdps_cell_for_waves_would_be_8_km_off():
    # The bug separate lookups prevent: HRDPS's (955, 2337) read from
    # the wave grid lands 8 km from Nickerson Bank, with no error.
    grid = _read_grid(RDWPS)
    k = 955 * grid.ni + 2337
    assert haversine_km(46.44, -53.39, grid.lats[k], grid.lons[k]) == pytest.approx(8.06, abs=0.05)


@need_real_files
def test_st_johns_harbour_moves_to_sea(rdwps_cells):
    c = rdwps_cells["st_johns_harbour"]
    assert c.moved
    assert (c.row, c.col) == (1004, 2313)
    assert c.distance_km == pytest.approx(4.57, abs=0.01)


@need_real_files
def test_mid_atlantic_is_refused():
    # 40N 30W is far east of HRDPS's area, which stops at about 41W.
    with pytest.raises(SiteGridError, match="mid_atlantic on HRDPS"):
        locate_sites(HRDPS, [Site("mid_atlantic", "Mid-Atlantic", 40.0, -30.0, "test")])
