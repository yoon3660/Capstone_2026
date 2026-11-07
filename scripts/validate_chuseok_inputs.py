"""Validate Chuseok VDS, TCS and CTM without running charging policies."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys

import numpy as np
import pandas as pd

from _bootstrap import ROOT
from build_chuseok_demand import DAYS, DIRECTIONS, prepare_vds, read_tcs, vds_profiles
from evdt.io import traffic
from evdt.io.charger_ingest import normalize_station_name
from evdt.paths import default_db_path
from evdt.io.route import GyeongbuRoute
from evdt.runner import build_travel_field, load_config
from validate_ctm_speed import JAM_KMH, PASS_FALSE_ALARM, PASS_MAPE, PASS_RECALL, cell_to_conzone, record_end_hours
from validate_sections import scoring_grid


def validate_speed(cfg, day, speed, volume, dsrc):
    length = GyeongbuRoute.load().length_km
    result = build_travel_field(cfg, length)
    field = result.speed_field
    cells = pd.DataFrame(result.snapshot_rows)
    cells = cells[cells.entity_type.eq("cell") & cells.state.eq("speed_kmh")]
    overlap = cell_to_conzone(cfg.corridor_id, cfg.direction)
    model = cells.assign(hour=record_end_hours(cells.t_min)).merge(
        overlap, left_on="entity_id", right_on="cell_id")
    model = model.assign(weighted=model.value * model.w).groupby(
        ["conzone_id", "hour"])[["weighted", "w"]].sum()
    model = (model.weighted / model.w).rename("ctm").reset_index()
    observed = speed[speed.date.eq(day) & speed.direction.eq(cfg.direction)]
    cmp = model.merge(observed, on=["conzone_id", "hour"], validate="one_to_one")
    cmp = cmp[cmp.speed_kmh.gt(0)]
    if cmp.empty or abs(result.conservation_error_veh) > 1e-5:
        raise ValueError("CTM mass or observation coverage invalid")
    actual_jam, model_jam = cmp.speed_kmh.lt(JAM_KMH), cmp.ctm.lt(JAM_KMH)
    mape = ((cmp.ctm - cmp.speed_kmh).abs() / cmp.speed_kmh).mean() * 100
    recall = (actual_jam & model_jam).sum() / max(1, actual_jam.sum())
    false_alarm = (~actual_jam & model_jam).sum() / max(1, (~actual_jam).sum())
    # Compare travel-time proxies on the same observed segments and hour.
    span = cmp.offset_km_end - cmp.offset_km_start
    cmp = cmp.assign(vds_min=60 * span / cmp.speed_kmh, ctm_min=60 * span / cmp.ctm)
    hours = cmp.groupby("hour")[["vds_min", "ctm_min"]].sum()
    with sqlite3.connect(default_db_path().as_uri() + "?mode=ro", uri=True) as conn:
        lanes = pd.read_sql_query("select cell_id, lanes from cell where corridor_id=?",
                                  conn, params=(cfg.corridor_id,))
    lane_map = overlap.merge(lanes, on="cell_id", validate="many_to_one")
    lane_map["weighted"] = lane_map.w * lane_map.lanes
    lane_map = lane_map.groupby("conzone_id")[["weighted", "w"]].sum()
    flow = volume[volume.date.eq(day) & volume.direction.eq(cfg.direction)].merge(
        (lane_map.weighted / lane_map.w).rename("lanes"), on="conzone_id")
    peak_per_lane = (flow.volume_veh / flow.lanes).max()
    candidates = cmp.merge(dsrc[dsrc.date.eq(day)], left_on=["conzone_id", "hour"],
                           right_on=["segment_id", "hour"], validate="one_to_one")
    candidates = candidates[((candidates.offset_km_end - candidates.offset_km_start)
                             / candidates.length_km - 1).abs().le(.10)]
    dsrc_mape = ((candidates.ctm_min * 60 - candidates.travel_time_seconds_candidate).abs()
                 / candidates.travel_time_seconds_candidate).mean() * 100
    return dict(day=day.date(), direction=cfg.direction, speed_pairs=len(cmp),
                max_vds_veh_h_per_model_lane=peak_per_lane,
                provisional_dsrc_pairs=len(candidates), provisional_dsrc_mape_pct=dsrc_mape,
                vds_jam_cells=int(actual_jam.sum()), speed_mape_pct=mape,
                jam_recall=recall, false_alarm=false_alarm,
                matched_travel_time_mape_pct=((hours.ctm_min - hours.vds_min).abs() / hours.vds_min).mean() * 100,
                ctm_end_to_end_noon_min=field.minutes(0, length, 720),
                conservation_error=result.conservation_error_veh,
                pass_threshold=bool(mape <= PASS_MAPE and recall >= PASS_RECALL and false_alarm <= PASS_FALSE_ALARM))


def tcs_coverage(ramps, head, points, daily):
    grid = scoring_grid(ramps)
    flows = grid.groupby("position_km").vds_veh.sum().sort_index()
    edges = flows.index.to_numpy()
    widths = np.diff(edges)
    vds = float(np.dot(widths, flows.to_numpy()[:-1]))
    overlap = np.clip(np.minimum(daily.dest.to_numpy()[:, None], edges[1:])
                      - np.maximum(daily.entry.to_numpy()[:, None], edges[:-1]), 0, None)
    tcs = float(np.dot(daily.volume_veh.to_numpy(), overlap.sum(axis=1)))
    origins = np.sort(points.offset_km.unique())
    grouped = daily.groupby("entry").volume_veh.sum()
    distances = np.abs(grouped.index.to_numpy()[:, None] - origins)
    nearest = distances.argmin(axis=1)
    tcs_entries = pd.Series(grouped.to_numpy(), index=origins[nearest])
    tcs_entries = tcs_entries[distances.min(axis=1) <= 2].groupby(level=0).sum()
    vds_entries = points.groupby("offset_km").entry_veh.sum().reindex(tcs_entries.index)
    return dict(vds_veh_km=vds, tcs_veh_km=tcs, tcs_vkt_shortfall=1-tcs/vds,
                tcs_origin_count=len(tcs_entries), tcs_origin_exceeds_vds=int((tcs_entries > vds_entries).sum()),
                vds_head_veh=head.entry_veh.sum(), vds_total_entries=points.entry_veh.sum())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference-dir", type=Path, default=ROOT / "data/processed/issue145_chuseok/reference",
                    help="Shared TCS position and DSRC reference inputs")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    processed = ROOT / "data/processed"
    raw = ROOT / "data/raw/issue145_chuseok/chuseok_vds"
    vds = prepare_vds(raw, processed)
    positions = pd.read_parquet(processed / "conzone_gyeongbu.parquet").drop(columns="period").drop_duplicates()
    speed = traffic.tidy(traffic.read_dataset(raw, "speed"), positions, "speed", "chuseok2026", DAYS)
    vds.to_parquet(args.out / "traffic_gyeongbu_chuseok.parquet", index=False)
    speed.to_parquet(args.out / "speed_gyeongbu_chuseok.parquet", index=False)
    daily, _ = read_tcs(args.reference_dir)
    dsrc = pd.read_parquet(args.reference_dir / "travel_time_candidates_14days.parquet")
    dsrc = dsrc[dsrc.class_code.eq(1) & dsrc.travel_time_seconds_candidate.gt(0)].copy()
    dsrc["date"] = pd.to_datetime(dsrc.date)
    seollal = pd.read_parquet(processed / "traffic_gyeongbu.parquet")
    speed_rows, coverage, comparison = [], [], []
    for direction in DIRECTIONS:
        path = ROOT / f"config/scenario_chuseok_{direction.lower()}.yaml"
        cfg = load_config(path)
        load_config(path.with_stem(path.stem + "_adoption"))
        section = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/validate_sections.py"),
                                  "--config", str(path)], cwd=ROOT, check=True,
                                 capture_output=True, encoding="utf-8")
        (args.out / f"validate_sections_{direction.lower()}.txt").write_text(section.stdout, encoding="utf-8")
        for day in DAYS:
            ramps, head, points = vds_profiles(vds, "chuseok2026", direction, day)
            tcs = daily[daily.date.eq(day) & daily.direction.eq(direction)]
            coverage.append(dict(day=day.date(), direction=direction, **tcs_coverage(ramps, head, points, tcs)))
            folder = processed / "issue146_chuseok" / str(day.date()) / direction
            scoped = replace(cfg, demand=replace(cfg.demand,
                volume_profile=str(folder / "volume.csv"), entry_exit_profile=str(folder / "entry_exit.csv")))
            speed_rows.append(validate_speed(scoped, day, speed, vds, dsrc))
            pd.DataFrame(speed_rows).to_csv(args.out / "ctm_validation.csv", index=False)
            comparison.append(dict(period="chuseok2026", day=day.date(), direction=direction,
                                   head_traffic=head.entry_veh.sum(), total_entries=points.entry_veh.sum()))
        for day in pd.date_range("2026-02-15", "2026-02-18"):
            _, head, points = vds_profiles(seollal, "202602", direction, day)
            comparison.append(dict(period="seollal2026", day=day.date(), direction=direction,
                                   head_traffic=head.entry_veh.sum(), total_entries=points.entry_veh.sum()))
    snapshot = pd.read_csv(processed / "issue145_chuseok/sep18_gyeongbu_supply_candidates.csv")
    snapshot = snapshot[snapshot.coordinate_pass & snapshot.deleted.eq("N")]
    with sqlite3.connect(default_db_path().as_uri() + "?mode=ro", uri=True) as conn:
        existing = pd.read_sql_query(
            "select s.name, s.direction, sum(c.n_units) as db_units from station s "
            "join charger c on s.station_id=c.station_id where c.is_active=1 group by s.station_id", conn)
    existing["key"] = existing.name.map(normalize_station_name)
    snapshot["key"] = snapshot.rest_name.map(normalize_station_name)
    supply = snapshot.groupby(["direction", "key"]).size().rename("sep18_units").reset_index()
    supply = supply.merge(existing, on=["direction", "key"], how="outer", validate="one_to_one")
    supply["difference"] = supply.sep18_units - supply.db_units
    supply.to_csv(args.out / "supply_comparison.csv", index=False)
    pd.DataFrame(coverage).to_csv(args.out / "tcs_coverage.csv", index=False)
    pd.DataFrame(comparison).to_csv(args.out / "traffic_comparison.csv", index=False)


if __name__ == "__main__":
    main()
