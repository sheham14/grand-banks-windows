"""Tests for ingest/fallback.py.

URLs are checked against the real paths ECCC's notices gave sr3 on
2026-10-28. Downloads are tested against a small web server on this
machine serving a real file, laid out like the Datamart, so the tests
never send requests to ECCC.
"""

import dataclasses
import shutil
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ingest.fallback import Fallback, datamart_url, fetch
from ingest.filenames import parse_filename
from ingest.models import load_models

MODELS = load_models()
RAW_HRDPS = Path(__file__).resolve().parent.parent / "data" / "raw" / "hrdps"
H000 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT000H.grib2"
H001 = "20261028T12Z_MSC_HRDPS_WIND_AGL-10m_RLatLon0.0225_PT001H.grib2"
NOW = datetime(2026, 10, 28, 16, 30, tzinfo=timezone.utc)


# --- URLs ------------------------------------------------------------

def test_hrdps_url_matches_real_notice():
    name = "20261028T06Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT000H.grib2"
    assert datamart_url(MODELS["HRDPS"], parse_filename(name)) == (
        "https://dd.weather.gc.ca/20261028/WXO-DD/model_hrdps/continental/2.5km/06/000/" + name)


def test_rdwps_url_matches_real_notice():
    # No lead-hour folder for RDWPS.
    name = "20261028T06Z_MSC_RDWPS_PWPER_Sfc_RLatLon0.0225_PT002H.grib2"
    assert datamart_url(MODELS["RDWPS"], parse_filename(name)) == (
        "https://dd.weather.gc.ca/20261028/WXO-DD/model_rdwps/national/2.5km/06/" + name)


# --- a local stand-in for the Datamart -------------------------------

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass  # don't print every request during tests


@pytest.fixture
def datamart(tmp_path):
    """Serve tmp_path/server over HTTP, holding one real file at its
    Datamart path. Returns the server's base URL."""
    src = RAW_HRDPS / H000
    if not src.exists():
        pytest.skip("real HRDPS file not in data/raw/")
    root = tmp_path / "server"
    url_path = datamart_url(MODELS["HRDPS"], parse_filename(H000), base_url="")
    (root / url_path).parent.mkdir(parents=True)
    shutil.copy2(src, root / url_path)  # copy2 keeps ECCC's mtime

    # Port 0: the system picks a free port. The server runs in a
    # background thread while the test talks to it.
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture
def hrdps(tmp_path):
    """HRDPS config whose download folder is a temporary one."""
    folder = tmp_path / "hrdps"
    folder.mkdir()
    return dataclasses.replace(MODELS["HRDPS"], folder=folder)


# --- fetch -----------------------------------------------------------

def test_fetch_saves_file_with_publish_time(datamart, hrdps):
    url = datamart_url(hrdps, parse_filename(H000), base_url=datamart)
    dest = hrdps.folder / H000
    assert fetch(url, dest) is True
    assert dest.read_bytes() == (RAW_HRDPS / H000).read_bytes()
    # mtime comes from Last-Modified, i.e. ECCC's publish time (HTTP dates
    # are whole seconds, so compare to the second).
    assert int(dest.stat().st_mtime) == int((RAW_HRDPS / H000).stat().st_mtime)
    assert list(hrdps.folder.glob("*.part")) == []


def test_fetch_404_saves_nothing(datamart, hrdps):
    url = datamart_url(hrdps, parse_filename(H001), base_url=datamart)
    assert fetch(url, hrdps.folder / H001) is False
    assert list(hrdps.folder.iterdir()) == []


# --- Fallback.attempt and its backoff --------------------------------

def test_attempt_saves_what_exists_then_backs_off(datamart, hrdps):
    fb = Fallback(base_url=datamart, pause=0)
    missing = frozenset({H000, H001})  # the server only has H000

    assert fb.attempt(hrdps, parse_filename(H000).run_time, missing, NOW) == 1
    assert (hrdps.folder / H000).exists()

    run_time = parse_filename(H000).run_time
    # Progress was made, so the wait is the first delay: 5 minutes.
    assert fb.attempt(hrdps, run_time, frozenset({H001}), NOW + timedelta(minutes=4)) == 0
    # At 5 minutes it tries again (and H001 is still missing)...
    assert fb.attempt(hrdps, run_time, frozenset({H001}), NOW + timedelta(minutes=5)) == 0
    # ...and with no progress the wait doubles to 10 minutes.
    assert fb._delay[("HRDPS", run_time)] == timedelta(minutes=10)


def test_backoff_is_capped(datamart, hrdps):
    fb = Fallback(base_url=datamart, pause=0)
    run_time = parse_filename(H001).run_time
    t = NOW
    for _ in range(8):  # 5, 10, 20, 30, 30, ...
        fb.attempt(hrdps, run_time, frozenset({H001}), t)
        t += fb._delay[("HRDPS", run_time)]
    assert fb._delay[("HRDPS", run_time)] == timedelta(minutes=30)
