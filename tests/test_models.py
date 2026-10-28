"""Tests for ingest/models.py and config/models.yaml.

The comparisons against data/raw/ use real files received on 2026-10-28.
data/ isn't in git, so those tests skip on a machine without downloads.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ingest.filenames import parse_filename
from ingest.models import expected_files, load_models

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
MODELS = load_models()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_files_per_run():
    # 49 lead hours (000-048 inclusive), not 48: the fencepost check.
    assert MODELS["HRDPS"].files_per_run == 4 * 49 == 196
    assert MODELS["RDWPS"].files_per_run == 2 * 49 == 98


def test_expected_set_has_that_many_names():
    # A set drops duplicates, so this also proves no name is built twice.
    assert len(expected_files(MODELS["HRDPS"], utc(2026, 10, 28, 12))) == 196


@pytest.mark.parametrize(
    "model, folder, run_time",
    [
        ("HRDPS", "hrdps", utc(2026, 10, 28, 12)),
        ("RDWPS", "rdwps", utc(2026, 10, 28, 6)),
    ],
)
def test_expected_matches_real_run(model, folder, run_time):
    # The names we build must be exactly the names ECCC sent:
    # nothing missing, nothing extra, every character the same.
    prefix = run_time.strftime("%Y%m%dT%HZ")
    received = {p.name for p in (RAW / folder).glob(prefix + "_*.grib2")}
    if not received:
        pytest.skip(f"no {model} {prefix} files in {RAW / folder}")
    assert expected_files(MODELS[model], run_time) == received


@pytest.mark.parametrize("model", ["HRDPS", "RDWPS"])
def test_names_parse_back(model):
    # Every name models.py builds must be read back the same way by
    # filenames.py, so the two halves of the code can't drift apart.
    cfg = MODELS[model]
    run_time = utc(2026, 10, 28, 18)
    for name in expected_files(cfg, run_time):
        f = parse_filename(name)
        assert f.model == model
        assert f.run_time == run_time
        assert f.variable in {v.name for v in cfg.variables}
        assert f.lead_hours in cfg.lead_hours


@pytest.mark.parametrize(
    "run_time",
    [
        utc(2026, 10, 28, 3),                     # no 03Z run
        utc(2026, 10, 28, 12, 30),                # not on the hour
        datetime(2026, 10, 28, 12),               # naive: no time zone
        datetime(2026, 10, 28, 9, 30,             # 12Z, but written in NDT
                 tzinfo=timezone(timedelta(hours=-2.5))),
    ],
)
def test_bad_run_times_are_rejected(run_time):
    with pytest.raises(ValueError):
        expected_files(MODELS["HRDPS"], run_time)
