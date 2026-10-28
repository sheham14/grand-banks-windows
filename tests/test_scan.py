"""Tests for ingest/scan.py: real files, a throwaway database.

Each test copies a few real files from data/raw/ into a temporary
folder (shutil.copy2 keeps their modification time, i.e. ECCC's publish
time) and points the HRDPS config at that folder.
"""

import dataclasses
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.models import load_models
from ingest.scan import scan_once

RAW_HRDPS = Path(__file__).resolve().parent.parent / "data" / "raw" / "hrdps"
NAMES = [
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2",
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT024H.grib2",
    "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT048H.grib2",
]


@pytest.fixture
def folder(tmp_path):
    if not all((RAW_HRDPS / n).exists() for n in NAMES):
        pytest.skip("real HRDPS files not in data/raw/")
    for n in NAMES:
        shutil.copy2(RAW_HRDPS / n, tmp_path / n)
    return tmp_path


@pytest.fixture
def models(folder):
    # dataclasses.replace: a copy of the frozen config with one field
    # changed. Only HRDPS, pointed at the temporary folder.
    hrdps = dataclasses.replace(load_models()["HRDPS"], folder=folder)
    return {"HRDPS": hrdps}


def test_records_new_files_once(conn, models):
    assert scan_once(conn, models) == 3
    assert scan_once(conn, models) == 0  # second pass: nothing new


def test_run_row_has_real_publish_times(conn, models):
    scan_once(conn, models)
    row = conn.execute(
        "SELECT run_time, files_expected, first_file_at, last_file_at FROM runs"
    ).fetchone()
    run_time, expected, first, last = row
    assert run_time == datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    assert expected == 196
    # ECCC's publish times for PT000H and PT048H, to the second.
    assert first.replace(microsecond=0) == datetime(2026, 10, 28, 14, 51, 30, tzinfo=timezone.utc)
    assert last.replace(microsecond=0) == datetime(2026, 10, 28, 15, 17, 14, tzinfo=timezone.utc)


def test_broken_and_stray_files_are_skipped(conn, models, folder):
    # A half-written file, and a name that isn't a model file.
    real = (folder / NAMES[0]).read_bytes()
    half = NAMES[0].replace("WIND", "WDIR")
    (folder / half).write_bytes(real[: len(real) // 2])
    (folder / "notes.grib2").write_bytes(b"")

    assert scan_once(conn, models) == 3  # only the three good files

    # Once the "download" finishes, the next pass picks it up. (Here the
    # finished file is the real WDIR PT000H copied in.)
    shutil.copy2(RAW_HRDPS / half, folder / half)
    assert scan_once(conn, models) == 1
