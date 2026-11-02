"""Pull forecast values at our sites out of one model run (build step 6, piece 3).

For every lead hour of a run, this reads the model's files, picks out
the value at each site's grid cell (found by sitegrid.py), and returns
one record per site, hour and variable:

    HRDPS  UGRD + VGRD  -> wind_speed (m/s), wind_direction (degrees, "from")
           WDIR         -> not stored: ECCC's own direction, used as the answer key
    RDWPS  HTSGW        -> wave_height (m)
           PWPER        -> wave_period_peak (s)

The records match the columns of step 7's point_forecasts table. Saving
them, and running this automatically for each new run, is step 7.

Wind, step by step (the same as notebooks/02-wind-rotation-check.ipynb):
  1. Read u and v at the cell.
  2. If the files say u/v follow the grid (uvRelativeToGrid = 1), turn
     them to true east/north by the cell's tilt (wind.rotate_to_earth).
  3. Work out speed and direction.
  4. Compare the direction with ECCC's WDIR at the same cell. A gap of
     more than 5 degrees refuses the whole run.

Why so strict about WDIR: a rotation bug leaves every speed correct and
every direction wrong (46 degrees here; 92 with the tilt's sign flipped),
and nothing else would notice. On the 2026-10-28 12Z run, our directions
matched WDIR within 0.18 degrees at both sites for all 49 hours, so 5
degrees is far above honest rounding and far below any real bug.

A trap this avoids: ECCC sets uvRelativeToGrid = 1 in EVERY HRDPS file,
WDIR and WIND included, but WDIR is already measured from true north (its
unit is "Degree true"). The flag only means something for a u/v pair, so
it's read from UGRD and VGRD only. Rotating everything that carries the
flag would rotate WDIR too and break the check meant to catch rotation bugs.

What it does with problems:
  - wrong unit, wrong grid size, direction off  -> ExtractError: refuse the run
  - a missing file, or no value at a cell       -> skip that value, log it.
    Never filled in: a gap stays a gap.

Speed: the files use JPEG 2000 compression, which can only be decoded
whole, so each file costs about 2 seconds even though we want a handful
of points. ECCC's WIND file isn't read at all, to save a quarter of that.

Look at a run by hand:
    python -m ingest.extract HRDPS 2026-10-28T12
    python -m ingest.extract RDWPS 2026-10-28T12 --hours 0 24 48
"""

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes

from ingest.models import ModelConfig, Variable, check_run_time, file_name, load_models
from ingest.sitegrid import SiteCell, load_sites, locate_sites
from ingest.wind import angle_between, direction_from, ms_to_knots, rotate_to_earth, speed

log = logging.getLogger("ingest.extract")

# What we read and what we call it. Phase 2a moves this into
# config/param_map.yaml, next to GFS's names for the same things.
#   ECCC name: (our name, unit the file must declare, unit we store)
# Units in files are written the way ecCodes writes them ("m s**-1" = m/s).
SCALARS = {
    "HTSGW": ("wave_height", "m", "m"),       # significant wave height
    "PWPER": ("wave_period_peak", "s", "s"),  # peak wave period
}
WIND_U = "UGRD"
WIND_V = "VGRD"
WIND_CHECK = "WDIR"
UNIT_WIND_IN_FILE = "m s**-1"
UNIT_DIRECTION_IN_FILE = "Degree true"  # clockwise from TRUE north
UNIT_WIND = "m/s"
UNIT_DIRECTION = "degrees"              # where the wind comes FROM, 0-360

# The WDIR check: our direction may differ from ECCC's by at most this,
# whenever the wind is at least MIN_SPEED_FOR_CHECK. Below that, wind
# barely has a direction: tiny rounding in u and v swings it by degrees.
MAX_DIRECTION_GAP_DEG = 5.0
MIN_SPEED_FOR_CHECK = 1.0  # m/s


class ExtractError(Exception):
    """The run can't be extracted safely. Nothing from it should be stored."""


@dataclass(frozen=True)
class PointValue:
    """One forecast number: one site, one hour, one variable.

    A row of step 7's point_forecasts table.
    """

    model: str
    run_time: datetime    # UTC
    valid_time: datetime  # UTC: run_time + lead_hours
    lead_hours: int
    member: int | None    # ensemble member; None for deterministic models (Phase 3 uses it)
    site_id: str
    variable: str         # our name, e.g. "wind_speed"
    value: float
    unit: str             # e.g. "m/s"; every stored value carries its unit


@dataclass(frozen=True)
class _Points:
    """What we take from one file: values at our cells, plus header facts."""

    values: dict[int, float | None]  # grid index -> value (None = no data there)
    units: str
    uv_relative_to_grid: int         # 1 = u/v follow the grid's own axes
    grid: tuple[int, int]            # (Ni, Nj)


def extract_run(
    model: ModelConfig,
    run_time: datetime,
    cells: dict[str, SiteCell],
    lead_hours: list[int] | None = None,
) -> list[PointValue]:
    """Every value at every site for one run (or only the given lead hours).

    cells: sitegrid.locate_sites() for THIS model's grid. Each model has
    its own grid, so HRDPS cells must never be used for RDWPS.
    Raises ExtractError if anything says the values can't be trusted.
    """
    check_run_time(model, run_time)
    leads = model.lead_hours if lead_hours is None else tuple(lead_hours)
    names = {v.name for v in model.variables}
    has_wind = WIND_U in names and WIND_V in names
    scalars = [v for v in model.variables if v.name in SCALARS]
    if not has_wind and not scalars:
        raise ExtractError(f"{model.name}: none of its variables is one we extract")

    out: list[PointValue] = []
    worst_gap = 0.0  # largest WDIR gap seen, logged at the end as evidence
    for lead in leads:
        if has_wind:
            values, gap = _wind_at(model, run_time, lead, cells, check=WIND_CHECK in names)
            out += values
            worst_gap = max(worst_gap, gap)
        for var in scalars:
            out += _scalar_at(model, run_time, lead, cells, var)

    summary = f"{model.name} {run_time:%Y-%m-%d %HZ}: {len(out)} values, {len(cells)} sites, {len(leads)} hours"
    if has_wind:
        summary += f", worst WDIR gap {worst_gap:.2f} deg"
    log.info(summary)
    return out


def earth_wind(u: float, v: float, tilt_deg: float, grid_relative: bool) -> tuple[float, float]:
    """(speed, direction "from") of one u/v pair, rotated first if needed."""
    if grid_relative:
        u, v = rotate_to_earth(u, v, tilt_deg)
    return speed(u, v), direction_from(u, v)


def check_direction(ours: float, spd: float, eccc: float, where: str) -> float | None:
    """Compare our direction with ECCC's WDIR. Returns the gap in degrees,
    or None if the wind is too light for direction to mean anything.

    Raises ExtractError if the gap is over MAX_DIRECTION_GAP_DEG.
    """
    if spd < MIN_SPEED_FOR_CHECK:
        return None
    gap = angle_between(ours, eccc)  # handles 359 vs 1
    if gap > MAX_DIRECTION_GAP_DEG:
        raise ExtractError(
            f"{where}: our direction {ours:.1f} vs ECCC's WDIR {eccc:.1f} is "
            f"{gap:.1f} degrees apart (limit {MAX_DIRECTION_GAP_DEG:.0f}). Most likely a "
            f"rotation bug or a changed grid; nothing from this run should be stored."
        )
    return gap


def _wind_at(
    model: ModelConfig, run_time: datetime, lead: int, cells: dict[str, SiteCell], check: bool
) -> tuple[list[PointValue], float]:
    """Wind speed and direction at every site for one lead hour.

    Returns the values and the largest WDIR gap (0 if nothing was checked).
    """
    where = f"{model.name} {run_time:%Y-%m-%d %HZ} +{lead}h"
    wanted = [WIND_U, WIND_V] + ([WIND_CHECK] if check else [])
    files = {}
    for name in wanted:
        path = _path(model, name, run_time, lead)
        if not path.exists():
            # Without u and v there's no wind; without WDIR it can't be
            # checked, and unchecked directions aren't stored.
            log.warning("%s: %s missing, no wind for this hour", where, path.name)
            return [], 0.0
        files[name] = _read_points(path, cells)

    u_file, v_file = files[WIND_U], files[WIND_V]
    for name, f in files.items():
        expected = UNIT_DIRECTION_IN_FILE if name == WIND_CHECK else UNIT_WIND_IN_FILE
        _expect(f"{where} {name} unit", f.units, expected)
        _expect_grid(where, name, f, cells)
    # u and v must agree on what their axes are, or the pair is nonsense.
    if u_file.uv_relative_to_grid != v_file.uv_relative_to_grid:
        raise ExtractError(f"{where}: UGRD and VGRD disagree on uvRelativeToGrid")
    grid_relative = bool(u_file.uv_relative_to_grid)

    out, worst = [], 0.0
    for site_id, cell in cells.items():
        u, v = u_file.values[cell.index], v_file.values[cell.index]
        if u is None or v is None:
            log.warning("%s: no wind value at %s's cell", where, site_id)
            continue
        spd, dirn = earth_wind(u, v, cell.tilt_deg, grid_relative)
        if check:
            eccc = files[WIND_CHECK].values[cell.index]
            if eccc is None:
                log.warning("%s: no WDIR at %s's cell, wind not stored", where, site_id)
                continue
            gap = check_direction(dirn, spd, eccc, f"{where} {site_id}")
            worst = max(worst, gap or 0.0)
        out.append(_value(model, run_time, lead, site_id, "wind_speed", spd, UNIT_WIND))
        out.append(_value(model, run_time, lead, site_id, "wind_direction", dirn, UNIT_DIRECTION))
    return out, worst


def _scalar_at(
    model: ModelConfig, run_time: datetime, lead: int, cells: dict[str, SiteCell], var: Variable
) -> list[PointValue]:
    """One plain value (e.g. wave height) at every site for one lead hour."""
    where = f"{model.name} {run_time:%Y-%m-%d %HZ} +{lead}h"
    our_name, unit_in_file, unit = SCALARS[var.name]
    path = _path(model, var.name, run_time, lead)
    if not path.exists():
        log.warning("%s: %s missing, skipped", where, path.name)
        return []
    f = _read_points(path, cells)
    _expect(f"{where} {var.name} unit", f.units, unit_in_file)
    _expect_grid(where, var.name, f, cells)

    out = []
    for site_id, cell in cells.items():
        value = f.values[cell.index]
        if value is None:
            # sitegrid picked a cell with data, so this shouldn't happen;
            # if it does (e.g. a mask that changes over time), it's a gap.
            log.warning("%s: no %s value at %s's cell", where, var.name, site_id)
            continue
        out.append(_value(model, run_time, lead, site_id, our_name, value, unit))
    return out


def _value(model, run_time, lead, site_id, variable, value, unit) -> PointValue:
    return PointValue(
        model=model.name, run_time=run_time, valid_time=run_time + timedelta(hours=lead),
        lead_hours=lead, member=None, site_id=site_id, variable=variable,
        value=float(value), unit=unit,
    )


def _path(model: ModelConfig, variable_name: str, run_time: datetime, lead: int) -> Path:
    """Where sr3 saved one file of this run."""
    var = next(v for v in model.variables if v.name == variable_name)
    return model.folder / file_name(model, var, run_time, lead)


def _read_points(path: Path, cells: dict[str, SiteCell]) -> _Points:
    """Decode one file and keep only the values at our cells."""
    with open(path, "rb") as f:
        handle = eccodes.codes_grib_new_from_file(f)
        if handle is None:
            raise ExtractError(f"{path.name}: no GRIB message in the file")
        try:
            # The whole field has to be decoded (JPEG 2000 can't be read
            # in pieces); we then keep a handful of numbers from it.
            values = eccodes.codes_get_values(handle)
            # Only grids with a bitmap (RDWPS) have points without data;
            # ecCodes fills them with missingValue.
            missing = (
                eccodes.codes_get(handle, "missingValue")
                if eccodes.codes_get(handle, "bitmapPresent") else None
            )
            picked = {
                c.index: (None if values[c.index] == missing else float(values[c.index]))
                for c in cells.values()
            }
            return _Points(
                values=picked,
                units=eccodes.codes_get(handle, "units"),
                uv_relative_to_grid=eccodes.codes_get(handle, "uvRelativeToGrid"),
                grid=(eccodes.codes_get(handle, "Ni"), eccodes.codes_get(handle, "Nj")),
            )
        finally:
            eccodes.codes_release(handle)  # see gribcheck._read_single_header


def _expect(what: str, actual, expected) -> None:
    if actual != expected:
        raise ExtractError(f"{what} is {actual!r}, expected {expected!r}")


def _expect_grid(where: str, name: str, f: _Points, cells: dict[str, SiteCell]) -> None:
    """The file must be on the grid the cells were found on."""
    for cell in cells.values():
        if f.grid != cell.grid:
            raise ExtractError(
                f"{where} {name}: grid is {f.grid}, but the site cells were found "
                f"on {cell.grid}. ECCC changed the grid? Re-run the site lookup."
            )


def locate_for_run(model: ModelConfig, run_time: datetime) -> dict[str, SiteCell]:
    """Site cells for `model`, using the first of this run's files that exists."""
    for var in model.variables:
        for lead in model.lead_hours:
            path = model.folder / file_name(model, var, run_time, lead)
            if path.exists():
                return locate_sites(path, load_sites())
    raise ExtractError(f"{model.name} {run_time:%Y-%m-%d %HZ}: no files of this run in {model.folder}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print one run's forecast values at our sites.")
    parser.add_argument("model", help="e.g. HRDPS or RDWPS")
    parser.add_argument("run", help="run time in UTC, e.g. 2026-10-28T12")
    parser.add_argument("--hours", type=int, nargs="*", help="lead hours (default: all)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    model = load_models()[args.model]
    run_time = datetime.strptime(args.run, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    cells = locate_for_run(model, run_time)
    values = extract_run(model, run_time, cells, args.hours)
    _print_table(values)


def _print_table(values: list[PointValue]) -> None:
    """One table per site: a row per lead hour, a column per variable.

    Wind speed also appears in knots: display is the one place knots belong.
    """
    variables = sorted({(p.variable, p.unit) for p in values})
    for site_id in sorted({p.site_id for p in values}):
        rows: dict[int, dict[str, float]] = {}
        for p in values:
            if p.site_id == site_id:
                rows.setdefault(p.lead_hours, {"valid": p.valid_time})[p.variable] = p.value
        print(f"\n{site_id}")
        header = ["lead", "valid (UTC)     "] + [f"{name} ({unit})" for name, unit in variables]
        if any(name == "wind_speed" for name, _ in variables):
            header.append("wind_speed (kt)")
        print("  ".join(header))
        for lead in sorted(rows):
            row = rows[lead]
            cols = [f"{lead:4d}", f"{row['valid']:%Y-%m-%d %H:%M}"]
            for (name, unit), h in zip(variables, header[2:]):
                cols.append(f"{row[name]:>{len(h)}.2f}" if name in row else " " * len(h))
            if "wind_speed (kt)" in header:
                kt = row.get("wind_speed")
                cols.append(f"{ms_to_knots(kt):>15.1f}" if kt is not None else " " * 15)
            print("  ".join(cols))


# Runs main() only when started as a program, not when imported.
if __name__ == "__main__":
    main()
