"""#178 재현을 다시 돌려 모델 충전 기록을 저장한다 — 운영사(ME/NONME)를 붙여서 (#180).

    python scripts/run_events_180.py --period quiet  --seeds 1-3
    python scripts/run_events_180.py --period seollal --seeds 1-3

#178 `run_quiet_replay_178.py` 와 같은 세계·설정이다(같은 `day_config`·`prepare`·`run_cell`·
`run_ue_cell`). MNL 은 #168 모드 A(main 채택값 θ 0.05·w 1.0·이탈 S0 규칙,
`run_cell(world, BASE, mnl=...)`). 평시는 실측 기온, 설은 #64 config(−5 °C). 다른 점은 채점 대신 **충전 기록 전체**를
남기는 것뿐이다. 결과는 #178 grid 의 같은 칸과 같아야 한다(검산: 휴게소 TVD 재계산).

운영사는 #64 연구실의 `model_unit_to_operator.csv`(BASE215 슬롯 → ME/NONME)로 붙인다.
매핑이 없는 충전기가 나오면 멈춘다.

출력 runs/issue180_events/<period>/events.parquet
    period, direction, date, seed, model, ev_id, station_id, key, charger_id, operator_bucket,
    power_kw, t_arrive_min, t_start_min, t_end_min, wait_min, charge_min, energy_kwh
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from calibrate_mnl_seollal import DATES as SEOLLAL_DATES  # noqa: E402
from calibrate_mnl_seollal import db_copy, legacy  # noqa: E402
from run_mnl_world import BASE, _git_sha, prepare, run_cell, run_ue_cell  # noqa: E402
from run_quiet_replay_178 import QUIET_DATES, daily_temps, day_config  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import MnlSettings  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402

COLS = ["ev_id", "station_id", "charger_id", "power_kw", "t_arrive_min", "t_start_min",
        "t_end_min", "wait_min", "charge_min", "energy_kwh"]


def operator_map(root: Path, daily) -> dict[str, str]:
    m = pd.read_csv(root / daily.SOURCE_LABS["DOWN"] / "model_unit_to_operator.csv", encoding="utf-8-sig")
    return dict(zip(m["charger_id"].astype(str), m["operator_bucket"].astype(str), strict=True))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", choices=("quiet", "seollal"), required=True)
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--models", default="S0,UE", help="S0 · UE · MNL (#168 모드 A)")
    ap.add_argument("--theta", type=float, default=0.05, help="MNL θ (#168 채택값)")
    ap.add_argument("--tag", default="", help="출력 파일 이름 꼬리 (events_<tag>.parquet)")
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.data_root)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    daily, previous = legacy(root)
    seeds = R.parse_seeds(args.seeds)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    dates = QUIET_DATES if args.period == "quiet" else SEOLLAL_DATES
    temps = daily_temps(root) if args.period == "quiet" else {}
    out = Path(args.out) if args.out else RUNS_DIR / "issue180_events" / args.period
    out.mkdir(parents=True, exist_ok=True)
    _units, overlay, _ = daily.get_units(root)
    ops = operator_map(root, daily)
    (out / (f"manifest_{args.tag}.json" if args.tag else "manifest.json")).write_text(json.dumps({
        "issue": 180, "script": "scripts/run_events_180.py", "period": args.period,
        "seeds": seeds, "models": models, "mnl": {"theta": args.theta, "wait_weight": 1.0,
                                                  "escape_mode": "s0"}, "temps": temps or "설 config(−5 °C)",
        "supply": "BASE215", "operator_map": f"{daily.SOURCE_LABS['DOWN']}/model_unit_to_operator.csv",
        "git": _git_sha(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False,
        indent=2), encoding="utf-8")

    frames = []
    quiet = lambda *_: None  # noqa: E731
    for direction in ("DOWN", "UP"):
        db = db_copy(root, direction, daily, out / "db")
        os.environ["EVDT_DB_PATH"] = str(db)
        lookup = previous.read_station_lookup(db, direction).set_index("station_id")["key"]
        for day in dates:
            cfg = day_config(root, args.period, direction, day, daily, temps.get(day))
            for seed in seeds:
                t0 = time.perf_counter()
                with daily.adapter(R, overlay):
                    world = prepare(cfg, seed, root=root, log=quiet)
                for model in models:
                    if model == "S0":
                        _row, ev = run_cell(world, "S0", p=1.0, mnl=MnlSettings(seed=seed),
                                            strength=0.0, oracle=None, s3_window=None,
                                            log=quiet, classic=True)
                    elif model == "MNL":
                        mnl = MnlSettings(theta=args.theta, wait_weight=1.0, seed=seed,
                                          escape_mode="s0")
                        _row, ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0,
                                            oracle=None, s3_window=None, log=quiet)
                    elif model == "UE":
                        _row, ev = run_ue_cell(world, quiet)
                    else:
                        raise SystemExit(f"모르는 모델: {model}")
                    f = pd.DataFrame(ev, columns=COLS) if ev else pd.DataFrame(columns=COLS)
                    f["key"] = f["station_id"].map(lookup)
                    f["operator_bucket"] = f["charger_id"].astype(str).map(ops)
                    if f["operator_bucket"].isna().any():
                        bad = f.loc[f.operator_bucket.isna(), "charger_id"].head(3).tolist()
                        raise SystemExit(f"[중단] 운영사 매핑 없는 충전기: {bad}")
                    frames.append(f.assign(period=args.period, direction=direction, date=day,
                                           seed=seed, model=model))
                print(f"  {direction} {day} s{seed}: {time.perf_counter() - t0:.0f}초", flush=True)
    ev = pd.concat(frames, ignore_index=True)
    ev.to_parquet(out / (f"events_{args.tag}.parquet" if args.tag else "events.parquet"), index=False)
    print(f"충전 기록 {len(ev):,}건 · 출력 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
