"""Tests for ingest/cleanup.py, on a temporary folder and the throwaway database.

The files are tiny stand-ins with real RDWPS names (10 bytes each), so
nothing near the real data/raw/ is ever at risk. They skip if Postgres
isn't running.
"""

import dataclasses
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ingest.cleanup import clean_model, warn_if_low_disk
from ingest.db import get_or_create_run, mark_extract_failed, next_run_to_extract, set_status
from ingest.models import DEFAULT_CONFIG, expected_files, load_models

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
OLD = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)     # 9 days old: past keep_raw_days
RECENT = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)  # 2 days old: inside it


@pytest.fixture
def model(tmp_path):
    # dataclasses.replace: a copy of the frozen config with one field changed.
    return dataclasses.replace(load_models()["RDWPS"], folder=tmp_path)


def _files(model, run_time):
    """Put a run's expected files in the folder; return their paths."""
    paths = [model.folder / name for name in expected_files(model, run_time)]
    for path in paths:
        path.write_bytes(b"x" * 10)
    return paths


def _run(conn, model, run_time, extracted=True):
    run_id = get_or_create_run(conn, model.name, run_time, model.files_per_run)
    set_status(conn, run_id, "complete")
    if extracted:
        conn.execute("UPDATE runs SET extracted_at = now() WHERE id = %s", (run_id,))
    return run_id


def _deleted_at(conn, run_id):
    return conn.execute("SELECT raw_deleted_at FROM runs WHERE id = %s", (run_id,)).fetchone()[0]


def test_old_extracted_run_is_deleted(conn, model):
    paths = _files(model, OLD)
    run_id = _run(conn, model, OLD)
    assert clean_model(conn, model, NOW) == (98, 980)  # 98 files of 10 bytes
    assert not any(p.exists() for p in paths)
    assert _deleted_at(conn, run_id) is not None


def test_recent_run_is_kept(conn, model):
    paths = _files(model, RECENT)
    run_id = _run(conn, model, RECENT)
    assert clean_model(conn, model, NOW) == (0, 0)
    assert all(p.exists() for p in paths)
    assert _deleted_at(conn, run_id) is None


def test_unextracted_run_is_kept_however_old(conn, model):
    paths = _files(model, OLD)
    _run(conn, model, OLD, extracted=False)
    assert clean_model(conn, model, NOW) == (0, 0)
    assert all(p.exists() for p in paths)


def test_refused_run_is_kept_however_old(conn, model):
    # Its files are needed to debug the refusal and to retry after a fix.
    paths = _files(model, OLD)
    run_id = _run(conn, model, OLD)
    mark_extract_failed(conn, run_id, "ExtractError: test")
    assert clean_model(conn, model, NOW) == (0, 0)
    assert all(p.exists() for p in paths)


def test_only_that_runs_files_go(conn, model):
    _files(model, OLD)
    _run(conn, model, OLD)
    other_run = _files(model, RECENT)
    buoy = model.folder / "2026-10-28-2105-4400139-AUTO-swob.xml"
    stray = model.folder / "20261101T12Z_MSC_RDWPS_WVHGT_Sfc_RLatLon0.0225_PT000H.grib2"
    buoy.write_bytes(b"x")
    stray.write_bytes(b"x")  # same run, but not a file we expect

    clean_model(conn, model, NOW)
    assert all(p.exists() for p in other_run)
    assert buoy.exists() and stray.exists()


def test_incomplete_run_with_files_missing_is_cleaned(conn, model):
    paths = _files(model, OLD)
    paths[0].unlink()  # this hour never arrived
    run_id = _run(conn, model, OLD)
    assert clean_model(conn, model, NOW) == (97, 970)
    assert _deleted_at(conn, run_id) is not None


def test_dry_run_deletes_nothing(conn, model):
    paths = _files(model, OLD)
    run_id = _run(conn, model, OLD)
    assert clean_model(conn, model, NOW, dry_run=True) == (98, 980)  # reports them...
    assert all(p.exists() for p in paths)                            # ...but they stay
    assert _deleted_at(conn, run_id) is None


def test_keep_override_previews_a_shorter_window(conn, model):
    _files(model, RECENT)
    _run(conn, model, RECENT)
    assert clean_model(conn, model, NOW, dry_run=True, keep=timedelta(days=1)) == (98, 980)


def test_cleaned_run_is_not_picked_again(conn, model):
    _files(model, OLD)
    _run(conn, model, OLD)
    clean_model(conn, model, NOW)
    assert clean_model(conn, model, NOW) == (0, 0)


def test_cleaned_run_is_not_queued_for_extraction(conn, model):
    # If someone re-queues every run (e.g. to add a new site), runs whose
    # files are gone must be left out, not refused for having no files.
    _files(model, OLD)
    run_id = _run(conn, model, OLD)
    clean_model(conn, model, NOW)
    conn.execute("UPDATE runs SET extracted_at = NULL WHERE id = %s", (run_id,))
    assert next_run_to_extract(conn, [model.name]) is None


def test_failed_delete_is_retried_later(conn, model, monkeypatch):
    paths = _files(model, OLD)
    run_id = _run(conn, model, OLD)

    def refuse(self, missing_ok=False):
        raise PermissionError("not allowed")

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", refuse)
        clean_model(conn, model, NOW)  # must not raise: the ingest runs this
    assert _deleted_at(conn, run_id) is None
    assert all(p.exists() for p in paths)

    clean_model(conn, model, NOW)      # next pass, delete works again
    assert _deleted_at(conn, run_id) is not None


def test_low_disk_warns(monkeypatch, caplog):
    usage = shutil._ntuple_diskusage(total=29_000_000_000, used=27_000_000_000, free=2_000_000_000)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    with caplog.at_level(logging.WARNING, logger="ingest.cleanup"):
        assert warn_if_low_disk() is True
    assert "2.0 GB free" in caplog.text


def test_plenty_of_disk_stays_quiet(monkeypatch):
    usage = shutil._ntuple_diskusage(total=29_000_000_000, used=19_000_000_000, free=10_000_000_000)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    assert warn_if_low_disk() is False


def test_config_must_keep_files_longer_than_the_fallback_works(tmp_path):
    # Keeping files for 1 day while the fallback works for 48 hours could
    # delete files of a run that's about to be extracted again.
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text())
    raw["models"]["RDWPS"]["keep_raw_days"] = 1
    bad = tmp_path / "models.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="keep_raw_days"):
        load_models(bad)
