"""Tests for ingest/extract.py.

Hand-worked cases for the checks, then real files from the 2026-10-28
12Z run (hour 24 only: each file takes about 2 seconds to decode). The
expected numbers come from notebooks/02-wind-rotation-check.ipynb and
ECCC's own WDIR. data/ isn't in git, so the real-file tests skip on a
machine without downloads.
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from ingest.extract import (
    ExtractError,
    _expect,
    check_direction,
    earth_wind,
    extract_run,
    locate_for_run,
)
from ingest.models import file_name, load_models

RUN = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
MODELS = load_models()
HRDPS, RDWPS = MODELS["HRDPS"], MODELS["RDWPS"]


def _have(model, variable_name):
    var = next(v for v in model.variables if v.name == variable_name)
    return (model.folder / file_name(model, var, RUN, 24)).exists()


need_real_files = pytest.mark.skipif(
    not all(_have(HRDPS, v) for v in ("UGRD", "VGRD", "WDIR")) or not _have(RDWPS, "HTSGW"),
    reason="real GRIB files not in data/raw/",
)


# --- earth_wind and check_direction: worked by hand ------------------------

def test_earth_wind_rotates_only_when_grid_relative():
    # Nickerson Bank, hour 24: the notebook's numbers.
    spd, dirn = earth_wind(7.47, 8.15, -45.9, grid_relative=True)
    assert dirn == pytest.approx(268.4, abs=0.1)
    # Same numbers, flag says earth-relative: no rotation, so 222.5.
    spd2, dirn2 = earth_wind(7.47, 8.15, -45.9, grid_relative=False)
    assert dirn2 == pytest.approx(222.5, abs=0.1)
    assert spd == pytest.approx(spd2)  # speed is the same either way


def test_small_gap_passes():
    assert check_direction(268.4, 11.06, 268.6, "test") == pytest.approx(0.2)


def test_gap_across_north_is_small():
    # 359.9 and 0.1 are 0.2 degrees apart, not 359.8.
    assert check_direction(359.9, 10, 0.1, "test") == pytest.approx(0.2)


def test_rotation_bug_is_refused():
    # The unrotated reading (222.5) against ECCC's 268.4: 45.9 apart.
    with pytest.raises(ExtractError, match="45.9 degrees apart"):
        check_direction(222.5, 11.06, 268.4, "test")


def test_calm_wind_is_not_checked():
    # 0.5 m/s: direction means little, so even 90 degrees isn't an error.
    assert check_direction(0.0, 0.5, 90.0, "test") is None


def test_unexpected_unit_is_refused():
    with pytest.raises(ExtractError, match="expected 'm s\\*\\*-1'"):
        _expect("UGRD unit", "knots", "m s**-1")


# --- real files ------------------------------------------------------------
# module scope: each grid lookup reads 3.3 million positions; do it once.

@pytest.fixture(scope="module")
def hrdps_cells():
    return locate_for_run(HRDPS, RUN)


@pytest.fixture(scope="module")
def rdwps_cells():
    return locate_for_run(RDWPS, RUN)


@need_real_files
def test_hrdps_hour_24(hrdps_cells):
    values = extract_run(HRDPS, RUN, hrdps_cells, [24])
    got = {(p.site_id, p.variable): p for p in values if p.site_id in ("nickerson_bank", "wsp_grand_banks")}
    assert len(got) == 4  # 2 sites x (speed, direction)

    d = got[("nickerson_bank", "wind_direction")]
    assert d.value == pytest.approx(268.4, abs=0.1)  # ECCC's WDIR
    assert d.unit == "degrees"
    assert d.valid_time == datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    assert d.lead_hours == 24
    assert d.member is None  # deterministic model

    s = got[("nickerson_bank", "wind_speed")]
    assert s.value == pytest.approx(11.06, abs=0.01)
    assert s.unit == "m/s"  # stored in SI, never knots

    assert got[("wsp_grand_banks", "wind_direction")].value == pytest.approx(279.6, abs=0.2)


@need_real_files
def test_flipped_tilt_is_caught(hrdps_cells):
    # The check question's bug: the tilt's sign backwards. Directions come
    # out 92 degrees wrong, and the WDIR check refuses the run.
    flipped = {k: dataclasses.replace(c, tilt_deg=-c.tilt_deg) for k, c in hrdps_cells.items()}
    with pytest.raises(ExtractError, match="WDIR"):
        extract_run(HRDPS, RUN, flipped, [24])


@need_real_files
def test_rdwps_hour_24_matches_an_independent_reader(rdwps_cells):
    # Read the same file with a different library (cfgrib, via xarray),
    # at the same row and column, and compare.
    xr = pytest.importorskip("xarray")
    values = extract_run(RDWPS, RUN, rdwps_cells, [24])
    cell = rdwps_cells["nickerson_bank"]
    var = next(v for v in RDWPS.variables if v.name == "HTSGW")
    path = RDWPS.folder / file_name(RDWPS, var, RUN, 24)
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    expected = float(ds[list(ds.data_vars)[0]][cell.row, cell.col])

    hs = next(p for p in values if p.site_id == "nickerson_bank" and p.variable == "wave_height")
    assert hs.value == pytest.approx(expected)
    assert hs.unit == "m"


@need_real_files
def test_hrdps_cells_are_refused_on_the_wave_grid(hrdps_cells):
    # The 8 km bug from piece 2, caught here: HRDPS cells come from a
    # 2540 x 1290 grid, RDWPS files are 2536 x 1286.
    with pytest.raises(ExtractError, match="grid is"):
        extract_run(RDWPS, RUN, hrdps_cells, [24])


@need_real_files
def test_missing_files_are_skipped_not_filled(hrdps_cells):
    # No files exist for a run before the project started. Nothing is
    # made up: the result is simply empty.
    before = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert extract_run(HRDPS, before, hrdps_cells, [24]) == []
