"""Tests for ingest/filenames.py.

Every filename here is a real one from data/raw/, received 2026-10-28.
"""

from datetime import datetime, timezone

import pytest

from ingest.filenames import parse_filename

HRDPS_NAME = "20261028T12Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT007H.grib2"
RDWPS_NAME = "20261028T06Z_MSC_RDWPS_HTSGW_Sfc_RLatLon0.0225_PT031H.grib2"


def test_hrdps_name():
    f = parse_filename(HRDPS_NAME)
    assert f.model == "HRDPS"
    assert f.run_time == datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    assert f.variable == "UGRD"
    assert f.level == "AGL-10m"
    assert f.lead_hours == 7


def test_rdwps_name():
    f = parse_filename(RDWPS_NAME)
    assert f.model == "RDWPS"
    assert f.run_time == datetime(2026, 10, 28, 6, tzinfo=timezone.utc)
    assert f.variable == "HTSGW"
    assert f.level == "Sfc"
    assert f.lead_hours == 31


def test_run_time_is_utc():
    # A naive datetime here would break the "all times UTC" convention.
    assert parse_filename(HRDPS_NAME).run_time.tzinfo == timezone.utc


def test_valid_time_crosses_midnight():
    # 06Z + 31 h = 13Z the next day.
    f = parse_filename(RDWPS_NAME)
    assert f.valid_time == datetime(2026, 10, 29, 13, tzinfo=timezone.utc)


def test_hour_zero_is_the_run_time():
    f = parse_filename("20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2")
    assert f.valid_time == f.run_time


def test_full_path_is_accepted():
    f = parse_filename("data/raw/hrdps/" + HRDPS_NAME)
    assert f.name == HRDPS_NAME


@pytest.mark.parametrize(
    "name",
    [
        HRDPS_NAME + ".tmp",                      # a download still in progress
        HRDPS_NAME.removesuffix(".grib2"),        # extension missing
        "2026-10-28-0805-4400139-AUTO-swob.xml",  # a buoy file, not a model file
        "",
    ],
)
def test_other_names_are_rejected(name):
    with pytest.raises(ValueError):
        parse_filename(name)
