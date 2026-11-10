"""Reproduce #162 FIFO inputs from the original PSReadLine one-off code.

Transformation recovered verbatim from ConsoleHost_history.txt lines 10503-10558,
except for parameterized input paths and a read-only SQLite connection.
No UE or FIFO simulations are invoked. Requires pandas.
"""

import argparse
from pathlib import Path
import sqlite3

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data/processed/issue162_20261108"


def prepare(observed: Path, crosswalk: Path, db: Path, out: Path) -> None:
    for source in (observed, crosswalk, db):
        if not source.is_file():
            raise FileNotFoundError(source)
    # Require a new directory so existing inputs/results can never be overwritten.
    out.mkdir(parents=True, exist_ok=False)

    obs = pd.read_csv(observed,
                      dtype={"station_id": str, "charger_id": str})
    cross = pd.read_csv(crosswalk,
                        dtype={"station_id": str, "charger_id": str,
                               "model_charger_id": str})

    if "duplicate_exact" in obs:
        duplicated = obs["duplicate_exact"].astype(str).str.lower().isin(["true", "1"])
        obs = obs.loc[~duplicated].copy()

    mapping = cross[["station_id", "charger_id", "model_charger_id"]].dropna().drop_duplicates()

    if mapping.duplicated(["station_id", "charger_id"]).any():
        raise ValueError("실측 장비 ID에 여러 모델 충전기가 대응함. 매핑 확인 필요")

    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as con:
        supply = pd.read_sql_query(
            """SELECT charger_id AS model_charger_id,
                      station_id AS model_station_id,
                      n_units, is_active
               FROM charger""", con
        )

    supply["model_charger_id"] = supply["model_charger_id"].astype(str)
    supply["model_station_id"] = supply["model_station_id"].astype(str)

    matched = obs.merge(mapping, on=["station_id", "charger_id"],
                        how="left", validate="many_to_one")
    matched = matched.merge(supply, on="model_charger_id",
                            how="left", validate="many_to_one")
    matched = matched[matched["model_station_id"].notna()].copy()

    standardized = pd.DataFrame({
        "station_id": matched["model_station_id"],
        "reported_charge_min": pd.to_numeric(matched["reported_charge_min"], errors="coerce"),
        "elapsed_min": pd.to_numeric(matched["elapsed_min"], errors="coerce")
    })
    standardized = standardized.dropna(subset=["reported_charge_min", "elapsed_min"])
    standardized = standardized[
        (standardized["reported_charge_min"] >= 0) &
        (standardized["elapsed_min"] >= 0)
    ]
    standardized.to_csv(out / "observed_standardized.csv", index=False,
                        encoding="utf-8-sig")

    eligible = matched.loc[matched["is_active"].eq(1),
                           ["model_charger_id", "n_units"]].drop_duplicates()

    unit_ids = []
    for r in eligible.itertuples(index=False):
        for i in range(1, int(r.n_units) + 1):
            unit_ids.append(f"{r.model_charger_id}#{i}")

    pd.DataFrame({"charger_id": sorted(set(unit_ids))}).to_csv(
        out / "me_model_unit_ids.csv", index=False, encoding="utf-8-sig"
    )

    print(f"observed_standardized.csv: {len(standardized)} rows")
    print(f"me_model_unit_ids.csv: {len(set(unit_ids))} rows")
    print(f"Output: {out.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed", type=Path,
                        default=PROCESSED / "observed_chuseok_demand_history.csv")
    parser.add_argument("--crosswalk", type=Path,
                        default=ROOT / "runs/issue162_rebuild/passenger266_rebuild/model_charger_crosswalk.csv")
    parser.add_argument("--supply-db", type=Path,
                        default=ROOT / "docs/data_validation/issue162_20261108/passenger266/scenario_sep18.sqlite")
    parser.add_argument("--out-dir", type=Path, required=True,
                        help="New output directory (must not already exist).")
    args = parser.parse_args()
    prepare(args.observed, args.crosswalk, args.supply_db, args.out_dir)


if __name__ == "__main__":
    main()
