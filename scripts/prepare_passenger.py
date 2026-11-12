import json
import shutil
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data/processed/issue162_20261108"
OUT = ROOT / "runs/issue162_rebuild/passenger266_rebuild"
OUT.mkdir(parents=True, exist_ok=True)
cross = pd.read_csv(
    BASE / "model_charger_crosswalk.csv", dtype={"station_id": str, "charger_id": str}
)
old = pd.concat(
    [
        pd.read_csv(
            ROOT / f"data/processed/sp3_64_operator_lab_{d}/model_unit_to_operator.csv", dtype=str
        )
        for d in ["down_s0007", "up_s0007_retry1"]
    ]
)
oldkeys = set(zip(old.api_station_id, old.api_charger_id.str.zfill(2), strict=False))
cross["in_feb_base215"] = [
    (r.station_id, r.charger_id.zfill(2)) in oldkeys for r in cross.itertuples()
]
assert cross.in_feb_base215.sum() == 215
# #64 provenance: all 51 year2026 exclusions are temporal, the remaining 16 are BUS.
bus = (~cross.in_feb_base215) & (cross.year < 2026)
assert bus.sum() == 16
assert cross.loc[bus].groupby(["station_name", "direction"]).size().tolist() == [4, 4, 4, 4]
cross["sep_passenger_included"] = ~bus
cross["sept_decision"] = bus.map(
    {True: "EXCLUDE_UNCONFIRMED_PASSENGER_ACCESS_BUS16", False: "INCLUDE_SEP18_REGISTERED_PROXY"}
)
cross.to_csv(OUT / "supply_feb_to_sep_decisions.csv", index=False, encoding="utf-8-sig")
cross[bus].to_csv(OUT / "excluded_commercial16.csv", index=False, encoding="utf-8-sig")
selected = cross[~bus].copy()
assert len(selected) == 266
selected.to_csv(OUT / "model_charger_crosswalk.csv", index=False, encoding="utf-8-sig")
db = OUT / "scenario_sep18.sqlite"
assert not db.exists()
shutil.copyfile(ROOT / "data/processed/issue146_chuseok/scenario_master.sqlite", db)
src = sqlite3.connect(BASE / "scenario_sep18.sqlite")
dst = sqlite3.connect(db)
schema = src.execute("select sql from sqlite_master where name='charger'").fetchone()[0]
dst.execute("drop table charger")
dst.execute(schema)
dst.execute("create index idx_charger_station on charger(station_id)")
keep = set(selected.model_charger_id)
dst.executemany(
    "insert into charger values (?,?,?,?,?,?,?,?)",
    [r for r in src.execute("select * from charger") if r[0] in keep],
)
dst.commit()
dst.close()
src.close()
for name in ["observed_chuseok_demand_history.csv", "observed_concurrency.csv"]:
    shutil.copyfile(BASE / name, OUT / name)
(OUT / "supply_decision.json").write_text(
    json.dumps(
        {
            "registered_proxy": 282,
            "excluded_bus16": 16,
            "passenger_proxy": 266,
            "feb_base215": 215,
            "restored_2026_registered": 51,
            "not_claimed": "actual operating date, passenger access at all remaining units, simultaneous slot independence",
        },
        indent=2,
    ),
    encoding="utf-8",
)
print("Prepared passenger proxy266; commercial exclusions16 verified against #64 ID list")
