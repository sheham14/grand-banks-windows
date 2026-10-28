"""Check that a downloaded GRIB2 file is sound (build step 5, piece 4).

A file only counts as received once it passes three checks, cheapest
first:

  1. It ends in the 4 bytes "7777". Every GRIB message ends with them,
     so a half-downloaded file fails here without decoding anything.
  2. ecCodes can open it, and it holds exactly 1 message (every ECCC
     Datamart file holds one field).
  3. The header agrees with the filename: same run date and time, and
     the same lead time once units are converted. HRDPS stores lead time
     in MINUTES (PT024H holds 1440), RDWPS in HOURS (PT031H holds 31).
     A file whose inside doesn't match its name is a data bug.

What it doesn't check yet: that the variable is what the name says
(WIND really is wind). ECCC calls it WIND, ecCodes calls it 10si; that
mapping is Phase 2a's config/param_map.yaml.
"""

from datetime import datetime, timezone
from pathlib import Path

import eccodes

from ingest.filenames import ModelFile

# GRIB2 code table 4.4: the number in indicatorOfUnitOfTimeRange,
# and how many minutes one unit is. Only the units we've seen ECCC use,
# plus days; anything else is rejected rather than guessed at.
_MINUTES_PER_UNIT = {
    0: 1,         # minute (HRDPS)
    1: 60,        # hour (RDWPS)
    2: 24 * 60,   # day
}


class GribCheckError(Exception):
    """A file failed one of the checks. The message says which and why."""


def check_grib(path: Path, expected: ModelFile) -> None:
    """Raise GribCheckError unless the file at `path` is sound.

    `expected` is what parse_filename() read from the file's name; the
    header must agree with it. Returns nothing when the file is fine.
    """
    _check_end_marker(path)
    header = _read_single_header(path)

    # dataDate is an int like 20261028; dataTime is HHMM, e.g. 1200.
    run_time = datetime.strptime(
        f"{header['dataDate']}{header['dataTime']:04d}", "%Y%m%d%H%M"
    ).replace(tzinfo=timezone.utc)
    if run_time != expected.run_time:
        raise GribCheckError(
            f"{path.name}: header run time {run_time:%Y-%m-%d %H:%MZ} "
            f"doesn't match the name ({expected.run_time:%Y-%m-%d %H:%MZ})"
        )

    unit = header["indicatorOfUnitOfTimeRange"]
    if unit not in _MINUTES_PER_UNIT:
        raise GribCheckError(f"{path.name}: unknown lead time unit code {unit}")
    # Compare in whole minutes, so there's no rounding: 1440 min x 1 vs 24 h x 60.
    lead_minutes = header["forecastTime"] * _MINUTES_PER_UNIT[unit]
    if lead_minutes != expected.lead_hours * 60:
        raise GribCheckError(
            f"{path.name}: header lead time is {lead_minutes} minutes, "
            f"the name says {expected.lead_hours} hours"
        )


def _check_end_marker(path: Path) -> None:
    """Fail fast if the file doesn't end in b"7777" (e.g. still downloading)."""
    size = path.stat().st_size
    if size < 4:
        raise GribCheckError(f"{path.name}: only {size} bytes")
    with open(path, "rb") as f:
        f.seek(-4, 2)  # 2 = count from the end: go to 4 bytes before the end
        if f.read(4) != b"7777":
            raise GribCheckError(f"{path.name}: doesn't end in 7777 (incomplete?)")


def _read_single_header(path: Path) -> dict:
    """Read the keys we check from the file's only message.

    Raises GribCheckError if ecCodes can't read the file or it doesn't
    hold exactly one message.
    """
    keys = ("dataDate", "dataTime", "forecastTime", "indicatorOfUnitOfTimeRange")
    header = None
    count = 0
    try:
        with open(path, "rb") as f:
            # codes_grib_new_from_file reads the next message each call
            # and returns None when there are no more.
            while (handle := eccodes.codes_grib_new_from_file(f)) is not None:
                try:
                    count += 1
                    if count == 1:
                        header = {k: eccodes.codes_get(handle, k) for k in keys}
                finally:
                    # ecCodes is a C library: every handle must be released
                    # by hand, or its memory is never freed. In a loop that
                    # runs all day, that leak would grow forever.
                    eccodes.codes_release(handle)
    except eccodes.CodesInternalError as e:
        # The base class of ecCodes' errors, e.g. PrematureEndOfFileError
        # for a file cut short in the middle of a message.
        raise GribCheckError(f"{path.name}: ecCodes can't read it ({e})") from e

    if count != 1:
        raise GribCheckError(f"{path.name}: {count} messages, expected 1")
    return header
