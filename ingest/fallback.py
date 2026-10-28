"""Fetch files the AMQP feed missed, by their known names (build step 5, piece 6).

Why notices go missing: ECCC keeps our sr3 queue for a while when we're
offline, but caps it at 10,000 notices. Our subscription matches every
HRDPS file (about 20,600 a run; the variable filter runs on our side), so
a few hours offline overflows the queue and those notices are gone.
Brief network drops can lose some too.

The rules we follow (ECCC's Datamart usage policy):
  * Never list a Datamart directory. We already know every filename we
    expect (ingest/models.py), so we request exactly those.
  * Be gentle: one request at a time with a pause, and back off when a
    file isn't there yet.

How one attempt for a late run works:
  1. Take its missing names, in lead-hour order (ECCC publishes in that
     order, so if hour 30 isn't there, hour 31 won't be either).
  2. Request each one. 200: save it. 404: not published (yet); stop.
  3. If we stopped early, wait before the next attempt for this run:
     5 minutes, then 10, 20, capped at 30 (exponential backoff).

Saved files go into the model's normal folder; the next scan pass checks
and records them exactly like sr3's files.
"""

import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from ingest.filenames import ModelFile, parse_filename
from ingest.models import ModelConfig

log = logging.getLogger("ingest.fallback")

BASE_URL = "https://dd.weather.gc.ca/"
# Tells ECCC's server who is asking, which is polite for automated requests.
USER_AGENT = "grand-banks-windows ingest fallback (missed AMQP notices)"
FIRST_DELAY = timedelta(minutes=5)
MAX_DELAY = timedelta(minutes=30)
PAUSE_SECONDS = 0.5  # between downloads


def datamart_url(model: ModelConfig, f: ModelFile, base_url: str = BASE_URL) -> str:
    """The Datamart URL of one file, from the model's path template.

    E.g. .../20261028/WXO-DD/model_hrdps/continental/2.5km/12/024/<name>
    format() fills the template: {run:%Y%m%d} formats the datetime with
    strftime codes, {lead:03d} pads 24 to "024".
    """
    path = model.datamart_path.format(run=f.run_time, lead=f.lead_hours, name=f.name)
    return base_url + path


def fetch(url: str, dest: Path, timeout: float = 60) -> bool:
    """Download url to dest. True if saved, False if 404 (not published yet).

    Other errors (network down, server error) are raised for the caller.
    """
    # Download to "<name>.part" first, then rename. The scan only looks at
    # *.grib2, and a rename is all-or-nothing, so it can never see a
    # half-downloaded file under the real name.
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(part, "wb") as out:
                shutil.copyfileobj(response, out)  # streams; never whole file in memory
            last_modified = response.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        if e.code == 404:
            return False
        raise
    except BaseException:
        # Any other failure (even Ctrl+C): don't leave a .part file behind.
        part.unlink(missing_ok=True)
        raise

    # Set the file's modification time to ECCC's publish time, from the
    # Last-Modified header, just as sr3's timeCopy does. The scan records
    # the mtime as published_at, so fallback files and sr3 files carry
    # the same kind of time. (Checked: the header equals sr3's mtime.)
    if last_modified:
        ts = parsedate_to_datetime(last_modified).timestamp()
        os.utime(part, (ts, ts))
    else:
        log.warning("%s: no Last-Modified header; published_at will be our download time", dest.name)
    os.replace(part, dest)
    return True


class Fallback:
    """Fetches missing files for late runs, with a backoff per run.

    The backoff state lives in memory only. After a restart every run is
    simply tried again straight away, which is harmless.
    """

    def __init__(self, base_url: str = BASE_URL, pause: float = PAUSE_SECONDS):
        self.base_url = base_url
        self.pause = pause
        # (model name, run time) -> when we may try again / current delay
        self._next_try: dict[tuple[str, datetime], datetime] = {}
        self._delay: dict[tuple[str, datetime], timedelta] = {}

    def attempt(
        self, model: ModelConfig, run_time: datetime, missing: frozenset[str], now: datetime
    ) -> int:
        """Try to fetch a run's missing files. Returns how many were saved."""
        key = (model.name, run_time)
        if now < self._next_try.get(key, now):
            return 0  # still backing off from the last attempt

        wanted = sorted((parse_filename(n) for n in missing),
                        key=lambda f: (f.lead_hours, f.name))
        saved = 0
        stopped = False
        for f in wanted:
            url = datamart_url(model, f, self.base_url)
            try:
                ok = fetch(url, model.folder / f.name)
            except (urllib.error.URLError, OSError) as e:
                # Network trouble or a server error: same treatment as a
                # 404, back off and try again later.
                log.warning("%s: fetch failed: %s", f.name, e)
                ok = False
            if not ok:
                stopped = True
                break
            saved += 1
            time.sleep(self.pause)

        if not stopped:
            # Everything that was missing is now on disk.
            self._next_try.pop(key, None)
            self._delay.pop(key, None)
        else:
            # Progress this time: start the backoff over. No progress:
            # double the wait, up to the cap.
            if saved:
                delay = FIRST_DELAY
            else:
                delay = min(self._delay.get(key, FIRST_DELAY / 2) * 2, MAX_DELAY)
            self._delay[key] = delay
            self._next_try[key] = now + delay

        if saved or stopped:
            log.info("%s %s fallback: saved %d of %d missing%s", model.name,
                     f"{run_time:%Y-%m-%d %HZ}", saved, len(missing),
                     f", next try in {self._delay[key]}" if stopped else "")
        return saved
