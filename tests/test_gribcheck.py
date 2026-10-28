"""Tests for ingest/gribcheck.py, using real files from data/raw/.

The broken files are made from real ones: cut short, or copied under a
name that doesn't match their header. data/ isn't in git, so these skip
on a machine without downloads.
"""

import shutil
from pathlib import Path

import pytest

from ingest.filenames import parse_filename
from ingest.gribcheck import GribCheckError, check_grib

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
HRDPS = RAW / "hrdps" / "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT024H.grib2"
RDWPS = RAW / "rdwps" / "20261028T06Z_MSC_RDWPS_HTSGW_Sfc_RLatLon0.0225_PT031H.grib2"


@pytest.fixture(autouse=True)
def need_real_files():
    # autouse: runs before every test in this file without being named.
    if not (HRDPS.exists() and RDWPS.exists()):
        pytest.skip("real GRIB files not in data/raw/")


def test_hrdps_file_passes():
    # Header says 1440 minutes; the name says 24 hours. Same thing.
    check_grib(HRDPS, parse_filename(HRDPS))


def test_rdwps_file_passes():
    # Header says 31 hours; the name says 31 hours.
    check_grib(RDWPS, parse_filename(RDWPS))


def test_half_downloaded_file_fails(tmp_path):
    # tmp_path: a fresh temporary folder pytest gives each test.
    cut = tmp_path / HRDPS.name
    data = HRDPS.read_bytes()
    cut.write_bytes(data[: len(data) // 2])
    with pytest.raises(GribCheckError, match="7777"):
        check_grib(cut, parse_filename(cut))


def test_empty_file_fails(tmp_path):
    empty = tmp_path / HRDPS.name
    empty.write_bytes(b"")
    with pytest.raises(GribCheckError):
        check_grib(empty, parse_filename(empty))


def test_lead_time_mismatch_fails(tmp_path):
    # The real PT024H file, saved under a PT025H name: the header still
    # says 1440 minutes, so the check must catch the mismatch.
    wrong = tmp_path / HRDPS.name.replace("PT024H", "PT025H")
    shutil.copy(HRDPS, wrong)
    with pytest.raises(GribCheckError, match="lead time"):
        check_grib(wrong, parse_filename(wrong))


def test_run_time_mismatch_fails(tmp_path):
    # A 12Z file saved under an 06Z name.
    wrong = tmp_path / HRDPS.name.replace("T12Z", "T06Z")
    shutil.copy(HRDPS, wrong)
    with pytest.raises(GribCheckError, match="run time"):
        check_grib(wrong, parse_filename(wrong))


def test_two_messages_fail(tmp_path):
    # Two real messages joined into one file: still ends in 7777, still
    # readable, but ECCC files hold exactly one message.
    double = tmp_path / HRDPS.name
    double.write_bytes(HRDPS.read_bytes() * 2)
    with pytest.raises(GribCheckError, match="2 messages"):
        check_grib(double, parse_filename(double))
