"""What a complete model run should contain (build step 5, piece 2).

The ingest job needs to know when a run is complete. Rather than storing
a number like 196, we build the exact set of filenames a run should have,
from the facts in config/models.yaml:

    every variable  x  every lead hour  =  one expected file each

    HRDPS: 4 variables x 49 hours (000-048) = 196 files
    RDWPS: 2 variables x 49 hours (000-048) =  98 files

Having the names, not just the count, lets the job say WHICH files are
missing (expected minus received), and gives the fallback the exact
names to request from the Datamart.

The names are built in the same format that ingest/filenames.py parses;
the tests check that the two agree.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# The repo root and config/models.yaml, found relative to this file so
# they work no matter which folder the code is started from.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "models.yaml"


@dataclass(frozen=True)
class Variable:
    """One variable we collect, e.g. UGRD at AGL-10m."""

    name: str   # as in the filename, e.g. "UGRD"
    level: str  # as in the filename, e.g. "AGL-10m"


@dataclass(frozen=True)
class ModelConfig:
    """Everything config/models.yaml says about one model.

    frozen=True: read-only after loading, like ModelFile in filenames.py.
    Tuples instead of lists for the same reason (lists can be changed).
    """

    name: str                       # e.g. "HRDPS"
    run_hours: tuple[int, ...]      # e.g. (0, 6, 12, 18)
    lead_hours: tuple[int, ...]     # every lead hour, e.g. (0, 1, ..., 48)
    grid: str                       # e.g. "RLatLon0.0225"
    variables: tuple[Variable, ...]
    folder: Path                    # where sr3 saves this model's files
    datamart_path: str              # URL path template, used by the fallback
    late_after: timedelta           # run time + this, not complete -> "late"
    give_up_after: timedelta        # run time + this, not complete -> "incomplete"
    backfill: timedelta             # how far back the fallback fills gaps

    @property
    def files_per_run(self) -> int:
        """How many files one complete run has (196 for HRDPS)."""
        return len(self.variables) * len(self.lead_hours)


def load_models(path: Path = DEFAULT_CONFIG) -> dict[str, ModelConfig]:
    """Read config/models.yaml into {"HRDPS": ModelConfig, ...}."""
    # safe_load only builds plain data (dicts, lists, strings, numbers).
    # Plain yaml.load can run code hidden in a YAML file; never use it.
    with open(path) as f:
        raw = yaml.safe_load(f)

    models = {}
    for name, m in raw["models"].items():
        lead = m["lead_hours"]
        if lead["step"] <= 0 or lead["last"] < lead["first"]:
            raise ValueError(f"{name}: bad lead_hours {lead}")

        models[name] = ModelConfig(
            name=name,
            run_hours=tuple(m["run_hours"]),
            # range() stops BEFORE its end value, so "last + 1" is what
            # makes hour 48 included. Without it we'd get 48 hours, not 49:
            # the fencepost error, and every run would look 4 files short.
            lead_hours=tuple(range(lead["first"], lead["last"] + 1, lead["step"])),
            grid=m["grid"],
            variables=tuple(Variable(v["name"], v["level"]) for v in m["variables"]),
            # The YAML gives a path relative to the repo root; make it
            # absolute so it works from any working directory.
            folder=REPO_ROOT / m["folder"],
            datamart_path=m["datamart_path"],
            late_after=timedelta(hours=m["late_after_hours"]),
            give_up_after=timedelta(hours=m["give_up_after_hours"]),
            backfill=timedelta(hours=m["backfill_hours"]),
        )
    return models


def expected_files(model: ModelConfig, run_time: datetime) -> frozenset[str]:
    """The exact filenames a complete run of `model` at `run_time` has.

    run_time must be timezone-aware UTC, exactly on one of the model's
    run hours. Anything else means a bug upstream, so we raise
    ValueError instead of guessing.
    """
    check_run_time(model, run_time)

    # One name per variable per lead hour.
    # A frozenset (read-only set) because order doesn't matter and we
    # only ever ask "is this name in it?" or "what's missing?".
    return frozenset(
        file_name(model, v, run_time, lead)
        for v in model.variables
        for lead in model.lead_hours
    )


def check_run_time(model: ModelConfig, run_time: datetime) -> None:
    """Raise ValueError unless run_time is a real run time of `model`:
    timezone-aware UTC, on the hour, and one of its run hours."""
    # utcoffset() is None for a naive time and non-zero for other zones.
    if run_time.utcoffset() != timedelta(0):
        raise ValueError(f"run_time must be timezone-aware UTC, got {run_time!r}")
    if (run_time.minute, run_time.second, run_time.microsecond) != (0, 0, 0):
        raise ValueError(f"run_time must be on the hour, got {run_time:%H:%M:%S}")
    if run_time.hour not in model.run_hours:
        raise ValueError(
            f"{model.name} has no {run_time:%H}Z run (runs at {model.run_hours})"
        )


def file_name(model: ModelConfig, variable: Variable, run_time: datetime, lead: int) -> str:
    """The Datamart filename of one variable at one lead hour of one run.

    The only place filenames are built, so the ingest (expected_files)
    and the extractor can never disagree about a name.
    """
    # "%Y%m%dT%HZ" writes e.g. 20261028T12Z: the T and Z are copied as-is.
    # {lead:03d} pads to 3 digits, so 7 becomes "007", matching ECCC's PT007H.
    return (
        f"{run_time:%Y%m%dT%HZ}_MSC_{model.name}_{variable.name}_{variable.level}"
        f"_{model.grid}_PT{lead:03d}H.grib2"
    )
