"""Build four-day Chuseok demand using the Seollal VDS pipeline and audit TCS."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from _bootstrap import ROOT
from evdt.io import traffic
from evdt.io.demand_profile import through_share
from evdt.io.entry_exit import (
    corridor_entry_hourly, entry_exit_profile, entry_points,
    sample_entry_offsets, sample_exit_offsets,
)
from evdt.io.route import GyeongbuRoute
from evdt.io.synthetic_ev import generate_evs, hourly_ev_counts
from evdt.runner import DEST_STREAM, load_config
from validate_sections import hazard_destinations, passage, scoring_grid


DAYS = pd.date_range("2026-09-24", "2026-09-27")
DIRECTIONS = ("DOWN", "UP")
OD_COLUMNS = ["start_offset_km", "end_offset_km", "hour", "veh"]


def prepare_vds(raw, processed):
    positions = pd.read_parquet(processed / "conzone_gyeongbu.parquet")
    positions = positions.drop(columns="period").drop_duplicates()
    if positions.conzone_id.duplicated().any():
        raise ValueError("Conflicting existing conzone positions")
    source = traffic.read_dataset(raw, "volume")
    key = ["conzone_id", "date", "hour"]
    if source.duplicated(key).any() or not source.hour.between(0, 23).all():
        raise ValueError("Invalid VDS hour keys")
    totals = traffic.check_daily_totals(source, traffic.read_dataset(raw, "volume_daily"))
    if not totals.empty:
        raise ValueError("Complete hourly VDS days disagree with daily source")
    return traffic.tidy(source, positions, "volume", "chuseok2026", DAYS)


def read_tcs(reference_dir):
    crosswalk = reference_dir
    routes = pd.read_csv(crosswalk / "route_position_crosswalk.csv")
    routes = routes.rename(columns={"engine_entry_offset_km": "entry", "engine_dest_offset_km": "dest"})
    routes = routes.loc[routes.position_status.eq("CONDITIONAL_POSITION_MATCH"),
                        ["origin", "destination", "direction", "entry", "dest"]]
    daily = pd.read_parquet(reference_dir / "chuseok_tcs_all_daily.parquet")
    daily = daily.rename(columns={"start_office_code": "origin", "end_office_code": "destination"})
    daily[["origin", "destination"]] = daily[["origin", "destination"]].astype(int)
    daily = daily.merge(routes, on=["origin", "destination"], validate="many_to_one")
    hourly = pd.read_parquet(crosswalk / "conditional_engine_position_inputs.parquet")
    hourly = hourly.rename(columns={"entry_offset_km": "entry", "dest_offset_km": "dest",
                                    "departure_hour": "hour"})
    hourly["date"] = pd.to_datetime(hourly.day)
    return daily, hourly


def vds_profiles(frame, period, direction, day):
    args = dict(period=period, direction=direction, date=day)
    ramps = entry_exit_profile(frame, **args)
    head = corridor_entry_hourly(frame, **args)
    return ramps, head, entry_points(ramps, head)


def hazard_od(points, ramps, length):
    rows = []
    for hour, group in points.groupby("hour"):
        table = hazard_destinations(group.offset_km.unique(), int(hour), ramps, length)
        for point in group.itertuples():
            dest, probability = table[point.offset_km]
            rows.extend((point.offset_km, d, hour, point.entry_veh * p)
                        for d, p in zip(dest, probability, strict=True))
    return pd.DataFrame(rows, columns=OD_COLUMNS)


def blend(baseline, points, daily, hourly=None):
    """Use TCS only where the mapped daily count is supported by VDS entries."""
    offsets = np.sort(points.offset_km.unique())
    daily = daily.copy()
    nearest = np.abs(daily.entry.to_numpy()[:, None] - offsets).argmin(axis=1)
    daily["start_offset_km"] = offsets[nearest]
    daily = daily[(daily.entry - daily.start_offset_km).abs().le(2)
                  & daily.dest.gt(daily.start_offset_km)]
    mapping = daily[["entry", "start_offset_km"]].drop_duplicates()
    entries = points.groupby("offset_km").entry_veh.sum()
    counts = daily.groupby("start_offset_km").volume_veh.sum()
    # An unsupported TCS margin signals a population or mapping mismatch.
    covered = counts[counts.le(entries.reindex(counts.index))] / entries
    daily_shape = daily.groupby(["start_offset_km", "dest"]).volume_veh.sum()
    hourly_shapes = {}
    if hourly is not None:
        hourly = hourly.merge(mapping, on="entry", validate="many_to_one")
        hourly = hourly[hourly.dest.gt(hourly.start_offset_km)]
        hourly_shapes = {(origin, hour): g.groupby("dest").volume_veh.sum()
                         for (origin, hour), g in hourly.groupby(["start_offset_km", "hour"])}
    base = baseline.copy()
    base["veh"] *= 1 - base.start_offset_km.map(covered).fillna(0)
    rows = []
    for q in points.itertuples():
        fraction = covered.get(q.offset_km, np.nan)
        if pd.isna(fraction) or fraction <= 0 or q.entry_veh <= 0:
            continue
        weights = hourly_shapes.get((q.offset_km, q.hour))
        if weights is None or weights.sum() <= 0:
            weights = daily_shape.loc[q.offset_km]
        rows.extend((q.offset_km, dest, q.hour, q.entry_veh * fraction * w / weights.sum())
                    for dest, w in weights.items())
    result = pd.concat([base, pd.DataFrame(rows, columns=OD_COLUMNS)], ignore_index=True)
    return result[result.veh.gt(0)], float(sum(row[3] for row in rows))


def check_od(od, points, length):
    expected = points.groupby(["hour", "offset_km"]).entry_veh.sum()
    actual = od.groupby(["hour", "start_offset_km"]).veh.sum()
    np.testing.assert_allclose(actual.reindex(expected.index, fill_value=0), expected, atol=1e-8)
    assert od.start_offset_km.ge(0).all() and od.end_offset_km.le(length).all()
    assert od.end_offset_km.gt(od.start_offset_km).all() and od.veh.ge(0).all()
    assert od.hour.between(0, 23).all()


def score(od, grid):
    model = passage(od, grid[["conzone_id", "position_km"]].drop_duplicates(), 90.)
    check = grid.merge(model, on=["conzone_id", "hour"], validate="one_to_one")
    check = check[check.hour.ge(5)]
    return (check.model_veh - check.vds_veh).abs().sum() / check.vds_veh.sum()


def sample_vehicles(points, ramps, cfg, length, prefix):
    hourly = points.groupby("hour").entry_veh.sum().reindex(range(24), fill_value=0)
    ev = generate_evs(hourly.rename("volume_veh").reset_index(), cfg, length)
    rng = np.random.default_rng([cfg.vehicles.seed, DEST_STREAM])
    ev["hour"] = (ev.entry_time_min // 60).astype(int)
    for hour, group in ev.groupby("hour"):
        origins = sample_entry_offsets(len(group), int(hour), points, rng)
        destinations = sample_exit_offsets(origins, int(hour), ramps, rng, corridor_end_km=length)
        ev.loc[group.index, "entry_offset_km"] = origins
        ev.loc[group.index, "dest_offset_km"] = destinations
    ev["ev_id"] = prefix + ":" + ev.ev_id
    assert ev.ev_id.is_unique and ev.entry_time_min.between(0, 1440, inclusive="left").all()
    assert ev.dest_offset_km.gt(ev.entry_offset_km).all()
    assert ev.entry_offset_km.ge(0).all() and ev.dest_offset_km.le(length).all()
    assert ev.initial_soc.between(0, 1).all()
    return ev


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference-dir", type=Path, default=ROOT / "data/processed/issue145_chuseok/reference",
                    help="Shared TCS position reference inputs")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ev-share", type=float, default=.041)
    args = ap.parse_args()
    if not 0 < args.ev_share <= 1:
        ap.error("--ev-share must be in (0, 1]")
    processed = ROOT / "data/processed"
    length = GyeongbuRoute.load().length_km
    vds = prepare_vds(ROOT / "data/raw/issue145_chuseok/chuseok_vds", processed)
    seollal = pd.read_parquet(processed / "traffic_gyeongbu.parquet")
    daily, hourly = read_tcs(args.reference_dir)
    args.out.mkdir(parents=True, exist_ok=True)
    quality, totals = [], []
    for direction in DIRECTIONS:
        cfg = load_config(ROOT / f"config/scenario_seollal_{direction.lower()}.yaml")
        cfg = replace(cfg, demand=replace(cfg.demand, ev_share=args.ev_share))
        for day in DAYS:
            ramps, head, points = vds_profiles(vds, "chuseok2026", direction, day)
            d = daily[daily.date.eq(day) & daily.direction.eq(direction)]
            h = hourly[hourly.date.eq(day) & hourly.direction.eq(direction)]
            base = hazard_od(points, ramps, length)
            grid = scoring_grid(ramps)
            candidates = {"vds": (base, 0.), "tcs_daily": blend(base, points, d)}
            if day.day in (25, 26, 27) and (day.day, direction) != (27, "UP"):
                candidates["tcs_hourly"] = blend(base, points, d, h)
            for model, (od, mass) in candidates.items():
                check_od(od, points, length)
                quality.append(dict(day=day.date(), direction=direction, model=model,
                                    wape=score(od, grid), tcs_assigned_veh=mass))
            # The scoring VDS also supplied the entries and exits. Keep the
            # Seollal baseline until a disjoint validation confirms a change.
            folder = args.out / str(day.date()) / direction
            folder.mkdir(parents=True, exist_ok=True)
            head.rename(columns={"entry_veh": "volume_veh"})[["hour", "volume_veh"]].to_csv(
                folder / "volume.csv", index=False)
            ramps.to_csv(folder / "entry_exit.csv", index=False)
            through_share(vds, period="chuseok2026", direction=direction, date=day).to_csv(
                folder / "through.csv", index=False)
            points.to_parquet(folder / "ctm_entries.parquet", index=False)
            base.groupby(OD_COLUMNS[:-1], as_index=False).veh.sum().to_parquet(
                folder / "hourly_od.parquet", index=False)
            ev = sample_vehicles(points, ramps, cfg, length, f"{day.date()}:{direction}")
            ev.assign(day=str(day.date()), direction=direction).to_parquet(folder / "synthetic_ev.parquet", index=False)
            totals.append(dict(period="chuseok2026", day=day.date(), direction=direction,
                               entries=points.entry_veh.sum(), synthetic_ev=len(ev)))
        for day in pd.date_range("2026-02-15", "2026-02-18"):
            _, _, points = vds_profiles(seollal, "202602", direction, day)
            profile = points.groupby("hour").entry_veh.sum().rename("volume_veh").reset_index()
            totals.append(dict(period="seollal2026", day=day.date(), direction=direction,
                               entries=points.entry_veh.sum(),
                               synthetic_ev=hourly_ev_counts(profile, cfg).n_ev.sum()))
    pd.DataFrame(quality).to_csv(args.out / "tcs_comparison.csv", index=False)
    pd.DataFrame(totals).assign(ev_share=args.ev_share).to_csv(args.out / "holiday_comparison.csv", index=False)


if __name__ == "__main__":
    main()
