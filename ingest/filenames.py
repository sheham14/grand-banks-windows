"""Read ECCC Datamart model filenames (build step 5, piece 1).

Everything the ingest job needs to know about a file, before opening it,
is in its name. This module turns a name into those parts, and nothing
else: no file access, no database, so it's easy to test.

A real name, split into its parts:

    20261028T12Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT007H.grib2
    └ run time ┘     └model┘└var┘└level┘└── grid ───┘ └lead┘

RDWPS names have exactly the same shape, just different values:

    20261028T06Z_MSC_RDWPS_HTSGW_Sfc_RLatLon0.0225_PT031H.grib2

Model + run time say which run a file belongs to.
Variable + lead time say which file it is within that run.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The pattern a filename must match, one part per line.
#
# re.VERBOSE lets us spread the pattern over several lines and put a
# comment after each part. Spaces and anything after "#" are ignored.
#
# (?P<name>...) is a "named group": whatever it matches can be read back
# later as match["name"], so we never count positions by hand.
_FILENAME = re.compile(
    r"""
    (?P<run_date>\d{8})          # run date, YYYYMMDD, e.g. 20261028
    T(?P<run_hour>\d{2})Z        # 'T' + run hour + 'Z' (UTC), e.g. T12Z
    _MSC_                        # producer: Meteorological Service of Canada
    (?P<model>[A-Z]+)            # model, e.g. HRDPS or RDWPS
    _(?P<variable>[A-Z0-9]+)     # variable, e.g. UGRD, WIND, HTSGW
    _(?P<level>[A-Za-z0-9.-]+)   # level: AGL-10m = 10 m above ground, Sfc = surface
    _(?P<grid>[A-Za-z]+[0-9.]+)  # grid: RLatLon0.0225 = rotated lat-lon, 0.0225 deg spacing
    _PT(?P<lead>\d{3})H          # lead time as an ISO 8601 duration, e.g. PT007H = 7 hours
    \.grib2
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ModelFile:
    """The parts of one model filename.

    frozen=True makes it read-only: once parsed, nothing can quietly
    change a file's run time later in the pipeline.
    """

    name: str
    model: str
    run_time: datetime  # timezone-aware, UTC
    variable: str
    level: str
    lead_hours: int

    @property
    def valid_time(self) -> datetime:
        """The time this file's forecast is for: run time + lead time."""
        return self.run_time + timedelta(hours=self.lead_hours)


def parse_filename(name: str | Path) -> ModelFile:
    """Split an ECCC model filename into its parts.

    Accepts a bare filename or a full path; only the last part is used.
    Raises ValueError for anything that isn't a model filename, such as
    a half-downloaded temp file or a buoy XML file. Raising (instead of
    returning None) means a bad name can't slip through unnoticed; the
    scan loop decides whether to log it and move on.
    """
    # Path(...).name keeps only the filename:
    # "data/raw/hrdps/2026...grib2" -> "2026...grib2"
    name = Path(name).name

    # fullmatch: the WHOLE name must fit the pattern, start to end.
    # (Anchoring is right here because we match the bare name ourselves.
    # It was wrong in sr3, which adds extra text after the URL.)
    match = _FILENAME.fullmatch(name)
    if match is None:
        raise ValueError(f"not an ECCC model filename: {name!r}")

    # Turn "20261028" + "12" into a datetime. strptime also rejects
    # impossible dates like month 13, raising ValueError for us.
    #
    # strptime returns a "naive" datetime (no time zone attached).
    # The Z in the name means UTC, so we attach UTC explicitly; our
    # convention is that every stored time is timezone-aware UTC.
    run_time = datetime.strptime(
        match["run_date"] + match["run_hour"], "%Y%m%d%H"
    ).replace(tzinfo=timezone.utc)

    return ModelFile(
        name=name,
        model=match["model"],
        run_time=run_time,
        variable=match["variable"],
        level=match["level"],
        # "007" -> 7. The filename always gives hours, even for HRDPS,
        # whose GRIB header stores the same lead time in minutes (420).
        lead_hours=int(match["lead"]),
    )
