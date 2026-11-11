import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/processed/issue162_20261108"
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from evdt.io.charger_ingest import resolve_charger_key  # noqa: E402

# Reuse the already reviewed #64 XML reader and September mapping, not February main().
source = (ROOT / "scripts/issue162_charging_diagnostic.py").read_text(encoding="utf-8")
# Instrument the diagnostic source without depending on its formatting.
source, n_rows = re.subn(
    r"(?m)^for row in it:$",
    "for source_row, row in enumerate(it, start=2):",
    source,
)
source, n_selected = re.subn(
    r"selected\.append\(\s*dict\(",
    "selected.append(dict(source_row=source_row, ",
    source,
    count=1,
)
source, n_holiday = re.subn(
    r"(?m)^holiday\s*=",
    "d.to_csv(OUT / 'observed_september_history.csv', index=False, encoding='utf-8-sig')\nholiday =",
    source,
    count=1,
)
if (n_rows, n_selected, n_holiday) != (1, 1, 1):
    raise RuntimeError(f"전처리 코드 삽입 실패: {(n_rows, n_selected, n_holiday)}")
compile(source, str(ROOT / "scripts/issue162_charging_diagnostic.py"), "exec")
if "--resume" not in sys.argv:
    exec(compile(source, str(ROOT / "scripts/issue162_charging_diagnostic.py"), "exec"), globals())
else:
    supply = pd.read_csv(ROOT / "data/processed/sp3_64_current_charger_supply.csv")
    known = set(zip(supply.station_name, supply.direction, strict=False))
    sep = pd.read_csv(
        ROOT / "data/processed/issue145_chuseok/sep18_gyeongbu_supply_candidates.csv",
        dtype={"station_id": str, "charger_id": str},
    )
    sep["key"] = sep.rest_name.map(lambda x: resolve_charger_key(x, known))
    d = pd.read_csv(
        OUT / "observed_september_history.csv",
        dtype={"station_id": str, "charger_id": str},
        parse_dates=["start", "end"],
    )
    holiday = d[d.start.ge("2026-09-24") & d.start.lt("2026-09-28")].copy()
shutil.copyfile(
    OUT / "sp5_charging_chuseok_sessions.csv", OUT / "observed_chuseok_demand_history.csv"
)

raw = {}
for p in sorted((ROOT / "data/raw/issue145_chuseok/chargers_20260918").glob("*.json")):
    for r in json.loads(p.read_text(encoding="utf-8"))["items"]["item"]:
        raw[(r["statId"], str(r["chgerId"]).zfill(2))] = r
sep["station_name"] = sep["key"].map(lambda k: k[0])
review = pd.read_csv(ROOT / "data/processed/sp3_64_2026_equipment_review.csv", dtype=str).rename(
    columns={"stat_id": "station_id", "power_kw": "feb_review_power_kw"}
)
review["charger_id"] = review.charger_id.str.zfill(2)
sep = sep.merge(
    review[["station_id", "charger_id", "review_status", "evidence_note"]],
    on=["station_id", "charger_id"],
    how="left",
    validate="one_to_one",
)
for col in ["year", "method", "chgerType", "limitYn", "limitDetail", "note", "statUpdDt"]:
    sep[col] = [
        raw.get((r.station_id, r.charger_id.zfill(2)), {}).get(col, "") for r in sep.itertuples()
    ]
sep["inclusion_basis"] = "SEP18_REGISTERED_PROXY_NOT_VERIFIED_OPERATION"
sep["model_charger_id"] = sep.station_id + "_" + sep.charger_id.str.zfill(2)
assert len(sep) == 282 and not sep.model_charger_id.duplicated().any()
sep.drop(columns="key").to_csv(OUT / "supply_sep18_audit.csv", index=False, encoding="utf-8-sig")

obs_ids = (
    d.groupby(
        ["direction", "station_name", "station_id", "charger_id", "charger_power"], dropna=False
    )
    .agg(
        september_starts=("source_row", "size"),
        first_start=("start", "min"),
        last_start=("start", "max"),
    )
    .reset_index()
)
obs_ids = obs_ids.merge(
    sep[["station_id", "charger_id", "output_kw", "year", "method"]],
    on=["station_id", "charger_id"],
    how="left",
)
obs_ids["observed_rated_kw"] = pd.to_numeric(
    obs_ids.charger_power.str.extract(r"(\d+)\s*kW", expand=False), errors="coerce"
)
obs_ids["power_differs"] = obs_ids.output_kw.notna() & obs_ids.observed_rated_kw.ne(
    obs_ids.output_kw
)
obs_ids["inventory_status"] = obs_ids.output_kw.notna().map(
    {True: "MATCHED_SEP18", False: "OBSERVED_ID_ABSENT_SEP18_DO_NOT_ASSUME_NEW_INSTALL"}
)
obs_ids.to_csv(
    OUT / "observed_charger_inventory_reconciliation.csv", index=False, encoding="utf-8-sig"
)

# Preserve #146 supply count/power, but expose each original API identity in local model copy.
db = OUT / "scenario_sep18.sqlite"
if not db.exists():
    shutil.copyfile(ROOT / "data/processed/issue146_chuseok/scenario_master.sqlite", db)
conn = sqlite3.connect(db)
assert conn.execute("select count(*) from run").fetchone()[0] == 0, (
    "Refuse to change supply after any run"
)
stations = pd.read_sql_query("select * from station", conn)
station_keys = {(r.name, r.direction): r.station_id for r in stations.itertuples()}
sep["model_station_id"] = [station_keys[(r.station_name, r.direction)] for r in sep.itertuples()]
old = pd.read_sql_query("select station_id,power_kw,sum(n_units) n from charger group by 1,2", conn)
new = sep.groupby(["model_station_id", "output_kw"]).size().rename("n").reset_index()
assert sorted(old.itertuples(index=False, name=None)) == sorted(
    new.itertuples(index=False, name=None)
)
schema = conn.execute("select sql from sqlite_master where name='charger'").fetchone()[0]
schema = re.sub(r",\s*UNIQUE\s*\(station_id, power_kw, connector_type\)", "", schema)
conn.execute("drop table charger")
conn.execute(schema)
conn.execute("create index idx_charger_station on charger(station_id)")
types = {
    "01": "DC_CHADEMO",
    "02": "AC_SLOW",
    "03": "DC_CHADEMO_AC3",
    "04": "DC_COMBO",
    "05": "DC_CHADEMO_DC_COMBO",
    "06": "DC_CHADEMO_AC3_DC_COMBO",
    "07": "AC3",
    "08": "DC_COMBO",
}
for r in sep.itertuples():
    conn.execute(
        "insert into charger values (?,?,?,?,?,?,?,?)",
        (
            r.model_charger_id,
            r.model_station_id,
            float(r.output_kw),
            types.get(r.chgerType, "API_" + r.chgerType),
            1,
            1,
            "sep18_proxy",
            r.station_id + ":" + r.charger_id,
        ),
    )
conn.commit()
conn.close()
sep.drop(columns="key").to_csv(
    OUT / "model_charger_crosswalk.csv", index=False, encoding="utf-8-sig"
)

# Full 96-hour grids; no observations at a station remain N/A rather than zero.
grid = pd.MultiIndex.from_product(
    [sorted(known), pd.date_range("2026-09-24", "2026-09-28", freq="h", inclusive="left")],
    names=["key", "hour_start"],
).to_frame(index=False)
grid["station_name"] = grid.key.str[0]
grid["direction"] = grid.key.str[1]
grid = grid.drop(columns="key")
clean = holiday[~holiday.duplicate_exact].copy()
clean["hour_start"] = clean.start.dt.floor("h")
agg = (
    clean.groupby(["station_name", "direction", "hour_start"])
    .agg(session_starts=("source_row", "size"), energy_kwh=("energy_kwh", "sum"))
    .reset_index()
)
grid = grid.merge(agg, on=["station_name", "direction", "hour_start"], how="left")
observed = set(zip(clean.station_name, clean.direction, strict=False))
grid["observed_station"] = [(r.station_name, r.direction) in observed for r in grid.itertuples()]
grid.loc[grid.observed_station, ["session_starts", "energy_kwh"]] = grid.loc[
    grid.observed_station, ["session_starts", "energy_kwh"]
].fillna(0)
busy = {}
evts = {}
lo = pd.Timestamp("2026-09-24")
hi = pd.Timestamp("2026-09-28")
carry = 0
for r in d[~d.duplicate_exact & d.valid_elapsed & d.start.lt(hi) & d.end.gt(lo)].itertuples():
    carry += int(r.start < lo)
    a = max(r.start, lo)
    b = min(r.end, hi)
    key = (r.station_name, r.direction)
    evts.setdefault(key, []).extend([(a, 1), (b, -1)])
    h = a.floor("h")
    while h < b:
        k = (*key, h)
        busy[k] = (
            busy.get(k, 0) + (min(b, h + pd.Timedelta(hours=1)) - max(a, h)).total_seconds() / 60
        )
        h += pd.Timedelta(hours=1)
grid["recorded_elapsed_busy_min"] = [
    busy.get((r.station_name, r.direction, r.hour_start), 0) if r.observed_station else float("nan")
    for r in grid.itertuples()
]
grid.to_csv(OUT / "observed_station_hourly_96h.csv", index=False, encoding="utf-8-sig")
peaks = []
for key, events in evts.items():
    active = peak = 0
    for _t, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    peaks.append(dict(station_name=key[0], direction=key[1], peak_recorded_sessions=peak))
pd.DataFrame(peaks).to_csv(OUT / "observed_concurrency.csv", index=False, encoding="utf-8-sig")
assert int(grid.session_starts.sum()) == 4480
assert abs(grid.energy_kwh.sum() - clean.energy_kwh.sum()) < 1e-8
meta = {
    "carry_in_sessions": carry,
    "grid_rows": len(grid),
    "observed_hour_cells": int(grid.observed_station.sum()),
    "supply_records": len(sep),
    "unmatched_observed_ids": int(obs_ids.output_kw.isna().sum()),
    "power_disagreement_rows": int(obs_ids.power_differs.sum()),
    "note": "Supply is registered proxy; absent IDs quarantined, not added using observed outcome.",
}
(OUT / "preparation_checks.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
print(json.dumps(meta), flush=True)
