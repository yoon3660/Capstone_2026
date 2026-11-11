"""Frozen expectations for #146, used by the #134 read-only audit."""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from build_chuseok_demand import DAYS, DIRECTIONS, prepare_vds, vds_profiles

from evdt.io import traffic
from evdt.io.route import GyeongbuRoute
from evdt.paths import default_db_path
from evdt.runner import load_config

ENTRIES = {
    "DOWN": [457891, 383452, 322396, 306791],
    "UP": [427813, 465320, 482877, 456804],
}
EV_COUNTS = {
    "DOWN": [18774, 15722, 13219, 12577],
    "UP": [17539, 19080, 19798, 18727],
}
PEAK = {"DOWN": ("2026-09-24", 22.98), "UP": ("2026-09-26", 20.41)}


def key_difference(a, name, left, right, keys, tables):
    for side, frame in [("left", left), ("right", right)]:
        a.check(f"{name} {side} duplicate keys", int(frame.duplicated(keys).sum()), 0)
    diff = left[keys].drop_duplicates().merge(right[keys].drop_duplicates(),
                                             on=keys, how="outer", indicator=True)
    diff = diff[diff["_merge"].ne("both")].assign(comparison=name)
    tables.append(diff)
    a.check(f"{name} left-only keys", int(diff["_merge"].eq("left_only").sum()), 0)
    a.check(f"{name} right-only keys", int(diff["_merge"].eq("right_only").sum()), 0)


def audit_coordinates(a, root, raw, processed, conn, differences):
    route_path = processed / "gyeongbu_route.json"
    route = GyeongbuRoute.load(route_path)
    meta = json.loads(route_path.read_text(encoding="utf-8"))["meta"]
    official = root / "data/raw" / meta["raw_dir"]
    ics = json.loads((official / "ic_all.json").read_text(encoding="utf-8"))
    recomputed = traffic.conzone_positions(traffic.conzones_from_raw(raw), route, ics)
    stored = pd.read_parquet(processed / "conzone_gyeongbu.parquet").drop(columns="period").drop_duplicates()
    keys = ["direction", "conzone_id"]
    cols = keys + ["offset_km_start", "offset_km_end"]
    saved = stored.loc[stored.in_corridor, cols]
    expected = recomputed.loc[recomputed.in_corridor, cols]
    key_difference(a, "conzone stored vs portal+IC", saved, expected, keys, differences)
    zones = saved.merge(expected, on=keys, suffixes=("_stored", "_source"), validate="one_to_one")
    for end in ("start", "end"):
        col = f"offset_km_{end}"
        zones[f"{end}_delta_m"] = 1000 * (zones[f"{col}_stored"] - zones[f"{col}_source"])
    zone_ok = zones[["start_delta_m", "end_delta_m"]].abs().le(1.001).all(axis=1)
    a.check("conzone endpoint differences above 1 m", int((~zone_ok).sum()), 0)

    rest = pd.DataFrame(json.loads((official / "rest_gyeongbu.json").read_text(encoding="utf-8")))
    rest = rest.rename(columns={"serviceAreaCode": "source_key", "xValue": "raw_lon", "yValue": "raw_lat"})
    rest[["raw_lon", "raw_lat"]] = rest[["raw_lon", "raw_lat"]].astype(float)
    stations = pd.read_sql_query(
        "select station_id, source_key, direction, offset_km, lat, lon from station "
        "where corridor_id in ('gyeongbu_down','gyeongbu_up')", conn)
    delta = stations.merge(rest[["source_key", "raw_lon", "raw_lat"]],
                           on="source_key", how="outer", validate="one_to_one", indicator=True)
    unmatched = delta[delta["_merge"].ne("both")][["source_key", "_merge"]]
    differences.append(unmatched.assign(comparison="station DB vs official rest inventory"))
    # The raw list includes two facilities outside the seeded charging stations.
    a.check("DB station missing official rest key", int(delta["_merge"].eq("left_only").sum()), 0)
    a.check("official rest keys outside charging DB",
            sorted(delta.loc[delta["_merge"].eq("right_only"), "source_key"]), ["A00088", "A00230"])
    pairs = delta[delta["_merge"].eq("both")].copy()
    pairs["lat_delta_deg"] = pairs.lat - pairs.raw_lat
    pairs["lon_delta_deg"] = pairs.lon - pairs.raw_lon
    pairs["source_offset_km"] = [route.offset_of(q.raw_lat, q.raw_lon, q.direction)
                                 for q in pairs.itertuples()]
    pairs["offset_delta_m"] = 1000 * (pairs.offset_km - pairs.source_offset_km)
    coord_ok = pairs[["lat_delta_deg", "lon_delta_deg"]].abs().le(1e-6).all(axis=1)
    a.check("station lat/lon differences above 1e-6 degrees", int((~coord_ok).sum()), 0)
    a.check("station offset differences above 1 m", int((~pairs.offset_delta_m.abs().le(1.001)).sum()), 0)
    return zones, pairs


def audit_chuseok(a, root: Path, report_dir: Path | None = None) -> None:
    processed = root / "data/processed"
    out = processed / "issue146_chuseok"
    raw = root / "data/raw/issue145_chuseok/chuseok_vds"
    vds = prepare_vds(raw, processed)
    stored_vds = pd.read_parquet(out / "traffic_gyeongbu_chuseok.parquet")
    speed = pd.read_parquet(out / "speed_gyeongbu_chuseok.parquet")
    differences = []
    keys = ["date", "direction", "conzone_id", "hour"]
    key_difference(a, "VDS stored vs recomputed", stored_vds, vds, keys, differences)
    key_difference(a, "VDS volume vs speed", stored_vds, speed, keys, differences)
    a.check("VDS stored rows match raw recomputation", stored_vds.equals(vds), True)
    length = GyeongbuRoute.load().length_km
    weather = pd.read_csv(processed / "issue145_chuseok/kma_observation_samples.csv")
    for direction in DIRECTIONS:
        date, expected_temp = PEAK[direction]
        sample = weather[weather.time_kst.str[:10].eq(date.replace("-", "."))]
        a.check(f"{direction} KMA sample size", len(sample), 16)
        a.check(f"{direction} KMA mean", round(sample.temp_c.mean(), 2), expected_temp)
        for suffix, share in [("", .05), ("_adoption", .10)]:
            cfg = load_config(root / f"config/scenario_chuseok_{direction.lower()}{suffix}.yaml")
            from evdt.demand_layers import effective_ev_share
            label = f"{direction}{suffix}"
            a.check(f"{label} period", cfg.demand.period, "chuseok2026")
            a.check(f"{label} temperature", cfg.environment.temp_c, expected_temp)
            a.check(f"{label} effective EV share", effective_ev_share(cfg.demand.layers, cfg.demand.ev_share), share)
            a.check(f"{label} OD policy", cfg.demand.od_profile, None)
            a.check(f"{label} peak volume path", Path(cfg.demand.volume_profile).as_posix(),
                    f"data/processed/issue146_chuseok/{date}/{direction}/volume.csv")
        for i, day in enumerate(DAYS):
            tag = f"{day.date()} {direction}"
            ramps, head, points = vds_profiles(vds, "chuseok2026", direction, day)
            folder = out / str(day.date()) / direction
            stored = pd.read_csv(folder / "entry_exit.csv")
            key_difference(a, f"{tag} ramps", stored.round({"offset_km": 6}),
                           ramps.round({"offset_km": 6}), ["hour", "offset_km"], differences)
            a.check(f"{tag} ramp shape", stored.shape, ramps.shape)
            a.check(f"{tag} ramp keys and values", stored.shape == ramps.shape and
                    np.allclose(stored, ramps, rtol=0, atol=1e-8), True)
            volume = pd.read_csv(folder / "volume.csv")
            a.check(f"{tag} volume hours", volume.hour.tolist(), list(range(24)))
            a.check(f"{tag} volume recomputation", np.array_equal(volume.volume_veh, head.entry_veh), True)
            ledger = pd.read_parquet(folder / "ctm_entries.parquet")
            key_difference(a, f"{tag} entry ledger", ledger, points, ["hour", "offset_km"], differences)
            a.check(f"{tag} entry keys and values", ledger.equals(points), True)
            a.check(f"{tag} entries", int(ledger.entry_veh.sum()), ENTRIES[direction][i])
            od = pd.read_parquet(folder / "hourly_od.parquet")
            a.check(f"{tag} OD duplicate keys", int(od.duplicated(["hour", "start_offset_km", "end_offset_km"]).sum()), 0)
            ev = pd.read_parquet(folder / "synthetic_ev.parquet")
            a.check(f"{tag} EV count at 4.1%", len(ev), EV_COUNTS[direction][i])
            a.check(f"{tag} EV duplicate IDs", int(ev.ev_id.duplicated().sum()), 0)
            valid = (ev.entry_time_min.between(0, 1440, inclusive="left") & ev.entry_offset_km.ge(0)
                     & ev.dest_offset_km.le(length) & ev.entry_offset_km.lt(ev.dest_offset_km)
                     & ev.day.eq(str(day.date())) & ev.direction.eq(direction))
            a.check(f"{tag} EV time and position violations", int((~valid).sum()), 0)

    with sqlite3.connect(default_db_path().as_uri() + "?mode=ro", uri=True) as conn:
        zones, stations_cmp = audit_coordinates(a, root, raw, processed, conn, differences)
        for direction, stations, chargers in [("DOWN", 19, 153), ("UP", 16, 129)]:
            cid = f"gyeongbu_{direction.lower()}"
            a.check(f"{direction} DB length", conn.execute(
                "select length_km from corridor where corridor_id=?", (cid,)).fetchone()[0], length)
            a.check(f"{direction} DB cells", conn.execute(
                "select count(*) from cell where corridor_id=?", (cid,)).fetchone()[0], 396)
            a.check(f"{direction} DB stations", conn.execute(
                "select count(*) from station where corridor_id=?", (cid,)).fetchone()[0], stations)
            a.check(f"{direction} DB active charger units", conn.execute(
                "select sum(c.n_units) from charger c join station s on s.station_id=c.station_id "
                "where s.corridor_id=? and c.is_active=1", (cid,)).fetchone()[0], chargers)

    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        pd.concat(differences, ignore_index=True).to_csv(report_dir / "key_differences.csv", index=False)
        zones.to_csv(report_dir / "conzone_coordinates.csv", index=False)
        stations_cmp.to_csv(report_dir / "station_coordinates.csv", index=False)
